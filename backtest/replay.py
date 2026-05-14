"""
Replay Backtester — replays recorded JSONL data through trading strategies.

Uses the real recorded Polymarket prices (mid, buy, sell) and Binance prices
to simulate what strategies would have done.

Strategies:
  oracle      — Oracle frontrun (BTC move → stale Polymarket odds)
  leadlag     — Cross-asset lead-lag (BTC leads → trade alt assets)
  convergence — Settlement convergence (buy near-certain outcomes near expiry)

Usage:
    python -m backtest.replay --strategy oracle
    python -m backtest.replay --strategy leadlag
    python -m backtest.replay --strategy convergence
    python -m backtest.replay --strategy oracle,leadlag,convergence
    python -m backtest.replay --strategy convergence --conv-threshold 0.88 --conv-window 45
"""

import argparse
import json
import logging
import os
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)

FEE_RATE = 0.07  # Polymarket parabolic fee rate


# ─────────────────────────────────────────────────────────────────────────────
# Shared data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ReplayTrade:
    strategy: str
    window_slug: str
    asset: str
    direction: str  # "UP" or "DOWN"
    entry_price: float
    fair_value: float
    edge: float
    size_usdc: float
    shares: float
    entry_ts: float
    move_bps: float = 0
    staleness: float = 0
    settlement_ts: float = 0
    outcome: str = ""  # "WIN" or "LOSS"
    pnl: float = 0
    fee: float = 0
    seconds_remaining: float = 0
    meta: str = ""  # extra info


@dataclass
class ReplayResult:
    asset: str
    strategy: str = ""
    trades: List[ReplayTrade] = field(default_factory=list)
    total_pnl: float = 0
    total_fees: float = 0
    wins: int = 0
    losses: int = 0
    bankroll: float = 10_000
    equity_curve: List[float] = field(default_factory=list)
    max_drawdown: float = 0

    @property
    def win_rate(self):
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0

    @property
    def avg_pnl(self):
        return self.total_pnl / len(self.trades) if self.trades else 0


# ─────────────────────────────────────────────────────────────────────────────
# Shared utilities
# ─────────────────────────────────────────────────────────────────────────────

def compute_window_outcomes(records: List[dict]) -> dict:
    """Determine UP/DOWN outcome for each window from recorded data."""
    windows = defaultdict(list)
    for rec in records:
        windows[rec["slug"]].append(rec)

    outcomes = {}
    for slug, recs in windows.items():
        last_recs = [r for r in recs if r.get("seconds_remaining", 999) < 10]
        if not last_recs:
            last_recs = recs[-5:]

        final = last_recs[-1]
        up_mid = final.get("up_mid", 0.5)

        if up_mid > 0.70:
            outcomes[slug] = "UP"
        elif up_mid < 0.30:
            outcomes[slug] = "DOWN"
        else:
            strike = final.get("strike", 0)
            price = final.get("price", 0)
            outcomes[slug] = "UP" if price >= strike else "DOWN"

    return outcomes


def settle_trade(trade: ReplayTrade, outcomes: dict, result: ReplayResult):
    """Settle a trade based on window outcome."""
    outcome = outcomes.get(trade.window_slug)
    if outcome is None:
        return

    if trade.direction == outcome:
        payout = trade.shares * 1.0
        cost = trade.shares * trade.entry_price
        trade.pnl = payout - cost - trade.fee
        trade.outcome = "WIN"
        result.wins += 1
    else:
        trade.pnl = -(trade.shares * trade.entry_price) - trade.fee
        trade.outcome = "LOSS"
        result.losses += 1

    result.total_pnl += trade.pnl
    result.total_fees += trade.fee
    result.bankroll += trade.pnl
    result.equity_curve.append(result.bankroll)
    result.trades.append(trade)


def compute_max_drawdown(result: ReplayResult):
    peak = result.equity_curve[0] if result.equity_curve else result.bankroll
    for eq in result.equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0
        result.max_drawdown = max(result.max_drawdown, dd)


def kelly_size(fair: float, market_price: float, bankroll: float,
               kelly_frac: float = 0.25, max_bet_pct: float = 0.05) -> float:
    if market_price <= 0 or market_price >= 1 or bankroll <= 0:
        return 0
    edge = fair - market_price
    if edge <= 0:
        return 0
    odds = (1.0 - market_price) / market_price
    k = (fair * odds - (1 - fair)) / odds
    k = max(0, k) * kelly_frac
    k = min(k, max_bet_pct)
    return k * bankroll


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 1: Oracle Frontrun
# ─────────────────────────────────────────────────────────────────────────────

class OracleReplayStrategy:
    """Replays oracle frontrun logic on recorded data (per-asset)."""
    name = "oracle"

    def __init__(
        self,
        move_threshold_bps: float = 6.0,
        staleness_threshold: float = 0.15,
        lookback_ticks: int = 5,
        max_price: float = 0.55,
        min_price: float = 0.20,
        min_remaining_seconds: float = 60,
        max_notional_usdc: float = 500.0,
        cooldown_seconds: float = 10.0,
    ):
        self.move_threshold_bps = move_threshold_bps
        self.staleness_threshold = staleness_threshold
        self.lookback_ticks = lookback_ticks
        self.max_price = max_price
        self.min_price = min_price
        self.min_remaining = min_remaining_seconds
        self.max_notional_usdc = max_notional_usdc
        self.cooldown_seconds = cooldown_seconds

    def run(self, records: List[dict], bankroll: float = 10_000) -> ReplayResult:
        result = ReplayResult(
            asset=records[0]["asset"] if records else "?",
            strategy=self.name,
            bankroll=bankroll,
        )
        result.equity_curve.append(bankroll)

        prices = deque(maxlen=200)
        current_trade: Optional[ReplayTrade] = None
        last_trade_ts = 0.0
        current_slug = None
        window_outcomes = compute_window_outcomes(records)

        for rec in records:
            ts = rec["ts"]
            btc_price = rec["price"]
            slug = rec["slug"]
            up_sell = rec.get("up_sell", 0) or rec.get("up_mid", 0)
            down_sell = rec.get("down_sell", 0) or rec.get("down_mid", 0)
            remaining = rec.get("seconds_remaining", 0)

            if slug != current_slug and current_trade is not None:
                settle_trade(current_trade, window_outcomes, result)
                current_trade = None
            current_slug = slug

            prices.append((ts, btc_price))

            if current_trade is not None:
                continue
            if len(prices) < self.lookback_ticks + 1:
                continue
            if ts - last_trade_ts < self.cooldown_seconds:
                continue
            if remaining < self.min_remaining:
                continue

            current_price = prices[-1][1]
            old_price = prices[-self.lookback_ticks - 1][1]
            if old_price <= 0:
                continue

            move_pct = (current_price - old_price) / old_price
            move_bps = abs(move_pct) * 10_000
            if move_bps < self.move_threshold_bps:
                continue

            prob_shift = min(move_bps / 50, 0.35)

            if move_pct > 0:
                direction, fair, market_price = "UP", 0.50 + prob_shift, up_sell
            else:
                direction, fair, market_price = "DOWN", 0.50 + prob_shift, down_sell

            if market_price <= 0:
                continue

            staleness = fair - market_price
            if staleness < self.staleness_threshold:
                continue
            if market_price > self.max_price or market_price < self.min_price:
                continue

            edge = fair - market_price
            if edge <= 0:
                continue

            size_usdc = min(kelly_size(fair, market_price, result.bankroll), self.max_notional_usdc)
            if size_usdc < 5:
                continue
            shares = size_usdc / market_price
            fee = shares * FEE_RATE * market_price * (1 - market_price)

            current_trade = ReplayTrade(
                strategy=self.name, window_slug=slug, asset=rec["asset"],
                direction=direction, entry_price=market_price, fair_value=fair,
                edge=edge, move_bps=move_bps, staleness=staleness,
                size_usdc=size_usdc, shares=shares, entry_ts=ts, fee=fee,
                seconds_remaining=remaining,
            )
            last_trade_ts = ts

        if current_trade is not None:
            settle_trade(current_trade, window_outcomes, result)

        compute_max_drawdown(result)
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 2: Momentum V2
# ─────────────────────────────────────────────────────────────────────────────

class MomentumReplayStrategy:
    """
    Replays V2 momentum logic on recorded data.

    Uses z-score from distance to strike, adjusted for volatility and
    momentum drift. Mirrors strategies/v2/momentum.py logic.
    """
    name = "momentum"

    def __init__(
        self,
        min_edge: float = 0.16,
        max_price: float = 0.55,
        min_price: float = 0.40,
        down_edge_boost: float = 0.10,
        fair_cap: float = 0.80,
        min_abs_z: float = 0.15,
        down_min_abs_z: float = 0.45,
        min_mom_vol_ratio: float = 0.8,
        min_distance_bps: float = 2.0,
        min_entry_age: float = 60.0,
        entry_deadline: float = 180.0,
        confirmations_required: int = 2,
        momentum_window: int = 20,
        max_notional_usdc: float = 500.0,
    ):
        self.min_edge = min_edge
        self.max_price = max_price
        self.min_price = min_price
        self.down_edge_boost = down_edge_boost
        self.fair_cap = fair_cap
        self.min_abs_z = min_abs_z
        self.down_min_abs_z = down_min_abs_z
        self.min_mom_vol_ratio = min_mom_vol_ratio
        self.min_distance_bps = min_distance_bps
        self.min_entry_age = min_entry_age
        self.entry_deadline = entry_deadline
        self.confirmations_required = max(1, confirmations_required)
        self.momentum_window = momentum_window
        self.max_notional_usdc = max_notional_usdc

    def run(self, records: List[dict], bankroll: float = 10_000) -> ReplayResult:
        import math

        result = ReplayResult(
            asset=records[0]["asset"] if records else "?",
            strategy=self.name,
            bankroll=bankroll,
        )
        result.equity_curve.append(bankroll)

        prices = deque(maxlen=200)
        current_trade: Optional[ReplayTrade] = None
        current_slug = None
        window_outcomes = compute_window_outcomes(records)

        # Confirmation tracking
        pending_key: Optional[tuple] = None  # (direction, slug)
        pending_count = 0

        for rec in records:
            ts = rec["ts"]
            btc_price = rec["price"]
            slug = rec["slug"]
            strike = rec.get("strike", 0)
            up_sell = rec.get("up_sell", 0) or rec.get("up_mid", 0)
            down_sell = rec.get("down_sell", 0) or rec.get("down_mid", 0)
            remaining = rec.get("seconds_remaining", 0)
            window_age = max(0, 300 - remaining)

            # Window change — settle and reset
            if slug != current_slug:
                if current_trade is not None:
                    settle_trade(current_trade, window_outcomes, result)
                    current_trade = None
                current_slug = slug
                pending_key = None
                pending_count = 0

            prices.append(btc_price)

            # Already in a trade — hold to settlement
            if current_trade is not None:
                continue

            # Need enough price history
            if len(prices) < self.momentum_window + 1:
                continue

            # Entry window: after min_entry_age, before entry_deadline
            if window_age < self.min_entry_age:
                continue
            if remaining < self.entry_deadline:
                continue

            if btc_price <= 0 or strike <= 0:
                continue

            # Compute momentum and volatility
            n = min(self.momentum_window, len(prices))
            price_list = list(prices)
            momentum = (price_list[-1] - price_list[-n]) / price_list[-n]
            returns = [(price_list[i] - price_list[i-1]) / price_list[i-1]
                       for i in range(-n+1, 0)]
            vol = max((sum(r**2 for r in returns) / len(returns)) ** 0.5, 0.00002) if returns else 0.001

            # Momentum quality filter
            mom_vol_ratio = abs(momentum) / vol if vol > 0 else 0
            if mom_vol_ratio < self.min_mom_vol_ratio:
                continue

            # Z-score from distance to strike
            # Estimate sample interval (~0.3s for async recorder)
            sample_interval = 0.3
            horizon_ticks = max(remaining / sample_interval, 1.0)
            horizon_sigma = btc_price * vol * math.sqrt(horizon_ticks)
            distance = btc_price - strike
            z_score = distance / horizon_sigma if horizon_sigma > 0 else 0
            distance_bps = abs(distance) / btc_price * 10_000

            if abs(z_score) < self.min_abs_z:
                continue
            if distance_bps < self.min_distance_bps:
                continue

            # Drift adjustment
            mom_z = max(-2.0, min(2.0, momentum / vol if vol > 0 else 0))
            adjusted_z = z_score + 0.15 * mom_z

            # Convert to probability
            fair_prob_up = 0.5 * (1.0 + math.erf(adjusted_z / math.sqrt(2.0)))
            fair_prob_up = max(1.0 - self.fair_cap, min(self.fair_cap, fair_prob_up))

            # Direction
            if fair_prob_up > 0.52:
                direction = "UP"
                fair = fair_prob_up
                market_price = up_sell
            elif fair_prob_up < 0.48:
                direction = "DOWN"
                fair = 1 - fair_prob_up
                market_price = down_sell
            else:
                continue

            if market_price <= 0:
                continue

            # DOWN-specific z gate
            if direction == "DOWN" and abs(z_score) < self.down_min_abs_z:
                continue

            # Price band
            if market_price > self.max_price or market_price < self.min_price:
                continue

            # Required edge
            if direction == "DOWN":
                required_edge = max(self.min_edge + self.down_edge_boost, 0.26)
            elif direction == "UP" and market_price >= 0.50:
                required_edge = max(self.min_edge, 0.18)
            else:
                required_edge = self.min_edge

            edge = fair - market_price
            if edge <= 0 or edge < required_edge:
                continue

            # Confirmation: need N consecutive same-direction signals
            key = (direction, slug)
            if key == pending_key:
                pending_count += 1
            else:
                pending_key = key
                pending_count = 1

            if pending_count < self.confirmations_required:
                continue

            # Kelly sizing
            size_usdc = min(
                kelly_size(fair, market_price, result.bankroll),
                self.max_notional_usdc,
            )
            if size_usdc < 5:
                continue
            shares = size_usdc / market_price
            fee = shares * FEE_RATE * market_price * (1 - market_price)

            current_trade = ReplayTrade(
                strategy=self.name, window_slug=slug, asset=rec["asset"],
                direction=direction, entry_price=market_price, fair_value=fair,
                edge=edge, size_usdc=size_usdc, shares=shares, entry_ts=ts, fee=fee,
                seconds_remaining=remaining,
                meta=f"z={z_score:.2f} mom={momentum*100:.3f}% dist={distance_bps:.1f}bps",
            )
            # Reset confirmation after entry
            pending_key = None
            pending_count = 0

        # Settle final trade
        if current_trade is not None:
            settle_trade(current_trade, window_outcomes, result)

        compute_max_drawdown(result)
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 3: Cross-Asset Lead-Lag
# ─────────────────────────────────────────────────────────────────────────────

class LeadLagReplayStrategy:
    """
    Uses BTC as a leading indicator to trade ETH/SOL/XRP.

    When BTC moves sharply, alt-asset Polymarket markets lag by 1-5 seconds.
    Buy the corresponding side on alt assets before their odds adjust.
    """
    name = "leadlag"

    def __init__(
        self,
        leader: str = "btc",
        move_threshold_bps: float = 5.0,
        staleness_threshold: float = 0.10,
        lookback_ticks: int = 5,
        max_price: float = 0.55,
        min_price: float = 0.20,
        min_remaining_seconds: float = 60,
        max_notional_usdc: float = 500.0,
        cooldown_seconds: float = 15.0,
        max_lag_seconds: float = 5.0,   # max time gap between BTC signal and alt entry
    ):
        self.leader = leader
        self.move_threshold_bps = move_threshold_bps
        self.staleness_threshold = staleness_threshold
        self.lookback_ticks = lookback_ticks
        self.max_price = max_price
        self.min_price = min_price
        self.min_remaining = min_remaining_seconds
        self.max_notional_usdc = max_notional_usdc
        self.cooldown_seconds = cooldown_seconds
        self.max_lag_seconds = max_lag_seconds

    def run_multi(self, all_records: Dict[str, List[dict]], bankroll: float = 10_000) -> Dict[str, ReplayResult]:
        """Run lead-lag across all assets. Returns results per follower asset."""
        if self.leader not in all_records:
            log.warning("Leader asset %s not in data", self.leader)
            return {}

        leader_records = all_records[self.leader]
        followers = {a: recs for a, recs in all_records.items() if a != self.leader}

        if not followers:
            log.warning("No follower assets found")
            return {}

        # Build leader price history indexed by timestamp
        leader_prices = deque(maxlen=200)

        # Build follower state
        follower_state = {}
        for asset in followers:
            follower_state[asset] = {
                "result": ReplayResult(asset=asset, strategy=self.name, bankroll=bankroll),
                "current_trade": None,
                "last_trade_ts": 0.0,
                "current_slug": None,
                "rec_idx": 0,
                "outcomes": compute_window_outcomes(followers[asset]),
            }
            follower_state[asset]["result"].equity_curve.append(bankroll)

        # Process leader records chronologically
        for leader_rec in leader_records:
            leader_ts = leader_rec["ts"]
            leader_price = leader_rec["price"]
            leader_prices.append((leader_ts, leader_price))

            if len(leader_prices) < self.lookback_ticks + 1:
                continue

            # Detect BTC move
            current_price = leader_prices[-1][1]
            old_price = leader_prices[-self.lookback_ticks - 1][1]
            if old_price <= 0:
                continue

            move_pct = (current_price - old_price) / old_price
            move_bps = abs(move_pct) * 10_000

            if move_bps < self.move_threshold_bps:
                continue

            prob_shift = min(move_bps / 50, 0.35)
            if move_pct > 0:
                btc_direction = "UP"
                fair = 0.50 + prob_shift
            else:
                btc_direction = "DOWN"
                fair = 0.50 + prob_shift

            # Check each follower asset
            for asset, f_recs in followers.items():
                state = follower_state[asset]

                # Advance follower index to current time
                while state["rec_idx"] < len(f_recs) and f_recs[state["rec_idx"]]["ts"] <= leader_ts:
                    state["rec_idx"] += 1

                # Get the most recent follower record (within lag window)
                idx = state["rec_idx"] - 1
                if idx < 0:
                    continue
                f_rec = f_recs[idx]

                # Check time lag
                time_gap = abs(leader_ts - f_rec["ts"])
                if time_gap > self.max_lag_seconds:
                    continue

                slug = f_rec["slug"]
                remaining = f_rec.get("seconds_remaining", 0)

                # Settle on window change
                if slug != state["current_slug"] and state["current_trade"] is not None:
                    settle_trade(state["current_trade"], state["outcomes"], state["result"])
                    state["current_trade"] = None
                state["current_slug"] = slug

                # Skip if already in a trade
                if state["current_trade"] is not None:
                    continue
                if leader_ts - state["last_trade_ts"] < self.cooldown_seconds:
                    continue
                if remaining < self.min_remaining:
                    continue

                # Get follower Polymarket price
                if btc_direction == "UP":
                    market_price = f_rec.get("up_sell", 0) or f_rec.get("up_mid", 0)
                    direction = "UP"
                else:
                    market_price = f_rec.get("down_sell", 0) or f_rec.get("down_mid", 0)
                    direction = "DOWN"

                if market_price <= 0:
                    continue

                # Staleness: is the follower still cheap?
                staleness = fair - market_price
                if staleness < self.staleness_threshold:
                    continue

                # Price band
                if market_price > self.max_price or market_price < self.min_price:
                    continue

                edge = fair - market_price
                if edge <= 0:
                    continue

                size_usdc = min(
                    kelly_size(fair, market_price, state["result"].bankroll),
                    self.max_notional_usdc,
                )
                if size_usdc < 5:
                    continue
                shares = size_usdc / market_price
                fee = shares * FEE_RATE * market_price * (1 - market_price)

                state["current_trade"] = ReplayTrade(
                    strategy=self.name, window_slug=slug, asset=asset,
                    direction=direction, entry_price=market_price, fair_value=fair,
                    edge=edge, move_bps=move_bps, staleness=staleness,
                    size_usdc=size_usdc, shares=shares, entry_ts=leader_ts, fee=fee,
                    seconds_remaining=remaining,
                    meta=f"BTC {btc_direction} {move_bps:.1f}bps",
                )
                state["last_trade_ts"] = leader_ts

        # Settle remaining trades
        results = {}
        for asset, state in follower_state.items():
            if state["current_trade"] is not None:
                settle_trade(state["current_trade"], state["outcomes"], state["result"])
            compute_max_drawdown(state["result"])
            results[asset] = state["result"]

        return results


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 4: Settlement Convergence
# ─────────────────────────────────────────────────────────────────────────────

class ConvergenceReplayStrategy:
    """
    Near expiry, buy the side that is almost certain to win.

    With 30-45 seconds left, if UP mid > 0.88, the outcome is nearly decided.
    Buy at 0.88 → settle at $1.00 → ~12% return in 30 seconds.

    Very high win rate, small profit per trade, occasional large loss on reversal.
    """
    name = "convergence"

    def __init__(
        self,
        confidence_threshold: float = 0.88,    # min mid to consider "certain"
        max_seconds_remaining: float = 45,      # only trade in last N seconds
        min_seconds_remaining: float = 5,       # need at least this much time
        max_notional_usdc: float = 500.0,
        cooldown_windows: int = 0,             # 0 = trade every window if signal
        confirm_ticks: int = 3,                # consecutive ticks above threshold
    ):
        self.confidence_threshold = confidence_threshold
        self.max_seconds = max_seconds_remaining
        self.min_seconds = min_seconds_remaining
        self.max_notional_usdc = max_notional_usdc
        self.cooldown_windows = cooldown_windows
        self.confirm_ticks = confirm_ticks

    def run(self, records: List[dict], bankroll: float = 10_000) -> ReplayResult:
        result = ReplayResult(
            asset=records[0]["asset"] if records else "?",
            strategy=self.name,
            bankroll=bankroll,
        )
        result.equity_curve.append(bankroll)

        window_outcomes = compute_window_outcomes(records)
        current_trade: Optional[ReplayTrade] = None
        current_slug = None
        windows_since_trade = 999
        consecutive_up = 0
        consecutive_down = 0

        for rec in records:
            ts = rec["ts"]
            slug = rec["slug"]
            up_mid = rec.get("up_mid", 0)
            down_mid = rec.get("down_mid", 0)
            up_sell = rec.get("up_sell", 0) or up_mid
            down_sell = rec.get("down_sell", 0) or down_mid
            remaining = rec.get("seconds_remaining", 0)

            # Window change — settle and reset
            if slug != current_slug:
                if current_trade is not None:
                    settle_trade(current_trade, window_outcomes, result)
                    current_trade = None
                    windows_since_trade = 0
                else:
                    windows_since_trade += 1
                current_slug = slug
                consecutive_up = 0
                consecutive_down = 0

            # Track consecutive ticks above threshold
            if up_mid >= self.confidence_threshold:
                consecutive_up += 1
                consecutive_down = 0
            elif down_mid >= self.confidence_threshold:
                consecutive_down += 1
                consecutive_up = 0
            else:
                consecutive_up = 0
                consecutive_down = 0

            # Already in a trade — hold to settlement
            if current_trade is not None:
                continue

            # Cooldown
            if windows_since_trade < self.cooldown_windows:
                continue

            # Time window check
            if remaining > self.max_seconds or remaining < self.min_seconds:
                continue

            # Determine direction based on which side is dominant
            if consecutive_up >= self.confirm_ticks and up_mid >= self.confidence_threshold:
                direction = "UP"
                market_price = up_sell
                fair = up_mid  # use mid as fair estimate
            elif consecutive_down >= self.confirm_ticks and down_mid >= self.confidence_threshold:
                direction = "DOWN"
                market_price = down_sell
                fair = down_mid
            else:
                continue

            if market_price <= 0 or market_price >= 1.0:
                continue

            edge = fair - market_price
            # For convergence, edge is the gap to 1.0 (expected payout)
            expected_edge = 1.0 - market_price  # profit per share if we win

            # Size: fixed notional (don't use Kelly — edge is small but high probability)
            size_usdc = min(self.max_notional_usdc, result.bankroll * 0.10)
            if size_usdc < 5:
                continue
            shares = size_usdc / market_price
            fee = shares * FEE_RATE * market_price * (1 - market_price)

            current_trade = ReplayTrade(
                strategy=self.name, window_slug=slug, asset=rec["asset"],
                direction=direction, entry_price=market_price,
                fair_value=fair, edge=expected_edge,
                size_usdc=size_usdc, shares=shares, entry_ts=ts, fee=fee,
                seconds_remaining=remaining,
                meta=f"mid={fair:.3f} confirm={self.confirm_ticks}",
            )

        # Settle final trade
        if current_trade is not None:
            settle_trade(current_trade, window_outcomes, result)

        compute_max_drawdown(result)
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────────

def load_records(data_dir: str = "data_v2", file_path: str = None, assets: list = None) -> dict:
    """Load JSONL records grouped by asset."""
    records = defaultdict(list)

    if file_path:
        files = [file_path]
    else:
        import glob
        files = sorted(glob.glob(os.path.join(data_dir, "market_data_*.jsonl")))

    for f in files:
        log.info("Loading %s ...", f)
        with open(f) as fh:
            for line in fh:
                rec = json.loads(line)
                asset = rec.get("asset", "btc")
                if assets and asset not in assets:
                    continue
                records[asset].append(rec)

    return dict(records)


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────

def print_report(strategy_name: str, results: dict[str, ReplayResult], params: dict):
    """Print backtest results."""
    print("\n" + "=" * 80)
    print(f"{strategy_name.upper()} REPLAY BACKTEST")
    print("=" * 80)
    param_str = ", ".join(f"{k}={v}" for k, v in params.items())
    print(f"Parameters: {param_str}")
    print()

    total_pnl = 0
    total_trades = 0
    total_wins = 0
    total_losses = 0
    total_fees = 0

    for asset, result in sorted(results.items()):
        total_pnl += result.total_pnl
        total_trades += len(result.trades)
        total_wins += result.wins
        total_losses += result.losses
        total_fees += result.total_fees

        print(f"--- {asset.upper()} ---")
        print(f"  Trades: {len(result.trades)} | Wins: {result.wins} | Losses: {result.losses} | "
              f"Win Rate: {result.win_rate * 100:.1f}%")
        print(f"  PnL: ${result.total_pnl:+.2f} | Fees: ${result.total_fees:.2f} | "
              f"Avg PnL/trade: ${result.avg_pnl:+.2f}")
        print(f"  Max Drawdown: {result.max_drawdown * 100:.1f}%")

        if result.trades:
            up_trades = [t for t in result.trades if t.direction == "UP"]
            down_trades = [t for t in result.trades if t.direction == "DOWN"]
            up_wins = sum(1 for t in up_trades if t.outcome == "WIN")
            down_wins = sum(1 for t in down_trades if t.outcome == "WIN")
            print(f"  UP: {len(up_trades)} ({up_wins}W/{len(up_trades)-up_wins}L) | "
                  f"DOWN: {len(down_trades)} ({down_wins}W/{len(down_trades)-down_wins}L)")

            avg_entry = sum(t.entry_price for t in result.trades) / len(result.trades)
            avg_edge = sum(t.edge for t in result.trades) / len(result.trades)
            print(f"  Avg entry: {avg_entry:.3f} | Avg edge: {avg_edge:.3f}")

            print(f"  Trades:")
            for t in result.trades:
                extra = f" | {t.meta}" if t.meta else ""
                print(f"    {t.direction:4s} @ {t.entry_price:.3f} | fair={t.fair_value:.3f} "
                      f"edge={t.edge:.3f} | "
                      f"${t.size_usdc:.0f} → {t.outcome:4s} ${t.pnl:+.2f} | "
                      f"remain={t.seconds_remaining:.0f}s{extra}")
        print()

    print("=" * 80)
    all_wr = total_wins / max(total_wins + total_losses, 1) * 100
    print(f"TOTAL: {total_trades} trades | {total_wins}W/{total_losses}L ({all_wr:.1f}%) | "
          f"PnL: ${total_pnl:+.2f} | Fees: ${total_fees:.2f}")
    print("=" * 80)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Replay backtest on recorded data")
    parser.add_argument("--strategy", type=str, default="oracle",
                        help="Strategy: oracle, momentum, leadlag, convergence, or comma-separated")
    parser.add_argument("--assets", type=str, default="btc,eth,sol,xrp",
                        help="Assets to backtest (default: btc,eth,sol,xrp)")
    parser.add_argument("--data-dir", type=str, default="data_v2",
                        help="Data directory (default: data_v2)")
    parser.add_argument("--file", type=str, default=None,
                        help="Specific JSONL file to replay")
    parser.add_argument("--bankroll", type=float, default=10_000,
                        help="Starting bankroll (default: 10000)")

    # Oracle params
    parser.add_argument("--move-bps", type=float, default=6.0,
                        help="Oracle/leadlag move threshold bps (default: 6.0)")
    parser.add_argument("--staleness", type=float, default=0.15,
                        help="Oracle/leadlag staleness threshold (default: 0.15)")
    parser.add_argument("--max-price", type=float, default=0.55,
                        help="Max entry price (default: 0.55)")
    parser.add_argument("--min-price", type=float, default=0.20,
                        help="Min entry price (default: 0.20)")
    parser.add_argument("--max-notional", type=float, default=500,
                        help="Max notional per trade (default: 500)")

    # Lead-lag params
    parser.add_argument("--leader", type=str, default="btc",
                        help="Lead asset for leadlag strategy (default: btc)")
    parser.add_argument("--lag-bps", type=float, default=5.0,
                        help="Lead-lag move threshold bps (default: 5.0)")
    parser.add_argument("--lag-staleness", type=float, default=0.10,
                        help="Lead-lag staleness threshold (default: 0.10)")

    # Momentum params
    parser.add_argument("--min-edge", type=float, default=0.16,
                        help="Momentum min edge (default: 0.16)")
    parser.add_argument("--mom-min-price", type=float, default=0.40,
                        help="Momentum min entry price (default: 0.40)")
    parser.add_argument("--fair-cap", type=float, default=0.80,
                        help="Momentum fair probability cap (default: 0.80)")
    parser.add_argument("--confirmations", type=int, default=2,
                        help="Momentum confirmations required (default: 2)")
    parser.add_argument("--down-edge-boost", type=float, default=0.10,
                        help="Momentum extra DOWN min edge (default: 0.10)")

    # Convergence params
    parser.add_argument("--conv-threshold", type=float, default=0.88,
                        help="Convergence confidence threshold (default: 0.88)")
    parser.add_argument("--conv-window", type=float, default=45,
                        help="Convergence max seconds remaining (default: 45)")
    parser.add_argument("--conv-confirm", type=int, default=3,
                        help="Convergence consecutive confirm ticks (default: 3)")

    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    assets = [a.strip() for a in args.assets.split(",")]
    all_records = load_records(data_dir=args.data_dir, file_path=args.file, assets=assets)

    if not all_records:
        print("No data found!")
        sys.exit(1)

    strategies = [s.strip() for s in args.strategy.split(",")]

    for strat_name in strategies:
        if strat_name == "oracle":
            strategy = OracleReplayStrategy(
                move_threshold_bps=args.move_bps,
                staleness_threshold=args.staleness,
                max_price=args.max_price,
                min_price=args.min_price,
                max_notional_usdc=args.max_notional,
            )
            results = {}
            for asset in sorted(all_records):
                recs = all_records[asset]
                windows = len(set(r["slug"] for r in recs))
                print(f"[oracle] Replaying {asset.upper()}: {len(recs):,} ticks, {windows} windows...")
                results[asset] = strategy.run(recs, bankroll=args.bankroll)
                strategy = OracleReplayStrategy(
                    move_threshold_bps=args.move_bps,
                    staleness_threshold=args.staleness,
                    max_price=args.max_price,
                    min_price=args.min_price,
                    max_notional_usdc=args.max_notional,
                )
            print_report("Oracle Frontrun", results, {
                "move_bps": args.move_bps, "staleness": args.staleness,
                "max_price": args.max_price, "min_price": args.min_price,
            })

        elif strat_name == "momentum":
            results = {}
            for asset in sorted(all_records):
                recs = all_records[asset]
                windows = len(set(r["slug"] for r in recs))
                print(f"[momentum] Replaying {asset.upper()}: {len(recs):,} ticks, {windows} windows...")
                strategy = MomentumReplayStrategy(
                    min_edge=args.min_edge,
                    max_price=args.max_price,
                    min_price=args.mom_min_price,
                    fair_cap=args.fair_cap,
                    confirmations_required=args.confirmations,
                    down_edge_boost=args.down_edge_boost,
                    max_notional_usdc=args.max_notional,
                )
                results[asset] = strategy.run(recs, bankroll=args.bankroll)
            print_report("Momentum V2", results, {
                "min_edge": args.min_edge, "max_price": args.max_price,
                "min_price": args.mom_min_price, "fair_cap": args.fair_cap,
                "confirmations": args.confirmations, "down_edge_boost": args.down_edge_boost,
            })

        elif strat_name == "leadlag":
            strategy = LeadLagReplayStrategy(
                leader=args.leader,
                move_threshold_bps=args.lag_bps,
                staleness_threshold=args.lag_staleness,
                max_price=args.max_price,
                min_price=args.min_price,
                max_notional_usdc=args.max_notional,
            )
            print(f"[leadlag] Leader: {args.leader.upper()}, followers: "
                  f"{[a.upper() for a in all_records if a != args.leader]}")
            total_ticks = sum(len(r) for r in all_records.values())
            print(f"[leadlag] Total: {total_ticks:,} ticks")
            results = strategy.run_multi(all_records, bankroll=args.bankroll)
            print_report("Cross-Asset Lead-Lag", results, {
                "leader": args.leader, "move_bps": args.lag_bps,
                "staleness": args.lag_staleness, "max_price": args.max_price,
            })

        elif strat_name == "convergence":
            strategy = ConvergenceReplayStrategy(
                confidence_threshold=args.conv_threshold,
                max_seconds_remaining=args.conv_window,
                confirm_ticks=args.conv_confirm,
                max_notional_usdc=args.max_notional,
            )
            results = {}
            for asset in sorted(all_records):
                recs = all_records[asset]
                windows = len(set(r["slug"] for r in recs))
                print(f"[convergence] Replaying {asset.upper()}: {len(recs):,} ticks, {windows} windows...")
                results[asset] = strategy.run(recs, bankroll=args.bankroll)
                strategy = ConvergenceReplayStrategy(
                    confidence_threshold=args.conv_threshold,
                    max_seconds_remaining=args.conv_window,
                    confirm_ticks=args.conv_confirm,
                    max_notional_usdc=args.max_notional,
                )
            print_report("Settlement Convergence", results, {
                "threshold": args.conv_threshold, "window_sec": args.conv_window,
                "confirm_ticks": args.conv_confirm,
            })

        else:
            print(f"Unknown strategy: {strat_name}")
            sys.exit(1)


if __name__ == "__main__":
    main()
