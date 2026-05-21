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
import copy
import json
import logging
import math
import os
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.rl_env import (
    ACTIONS,
    TabularQModel,
    action_direction,
    action_fraction,
    ask_depth_for_direction,
    price_for_action,
    rl_gate_thresholds,
    regime as rl_regime,
    spread_for_direction,
)

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
# Strategy 5: Last Seconds Snipe
# ─────────────────────────────────────────────────────────────────────────────

class SnipeReplayStrategy:
    """
    Replays last_seconds_snipe logic on recorded data.

    Two tiers:
      - Strict (last 30s): needs 0.82 <= price <= 0.94, distance > $30
      - Soft (30-45s): needs 0.85 <= price <= 0.93, distance > $45

    Direction is determined by BTC price vs strike, then confirmed
    by Polymarket odds alignment.

    Stop-loss: if the token mid drops below stop_loss_price, sell immediately
    instead of holding to settlement. This caps catastrophic losses.
    """
    name = "snipe"

    def __init__(
        self,
        max_seconds_remaining: float = 30.0,
        min_seconds_remaining: float = 15.0,
        min_distance_usd: float = 30.0,
        min_market_odds: float = 0.82,
        max_market_odds: float = 0.94,
        min_edge: float = 0.04,
        min_fair: float = 0.99,
        soft_max_seconds_remaining: float = 45.0,
        soft_min_distance_usd: float = 45.0,
        soft_min_market_odds: float = 0.85,
        soft_max_market_odds: float = 0.93,
        soft_min_edge: float = 0.05,
        soft_min_fair: float = 0.98,
        max_notional_usdc: float = 250.0,
        stop_loss_price: float = 0.0,  # 0 = disabled, e.g. 0.70 = sell if mid drops below 0.70
    ):
        self.max_seconds = max_seconds_remaining
        self.min_seconds = min_seconds_remaining
        self.min_distance_usd = min_distance_usd
        self.min_market_odds = min_market_odds
        self.max_market_odds = max_market_odds
        self.min_edge = min_edge
        self.min_fair = min_fair
        self.soft_max_seconds = soft_max_seconds_remaining
        self.soft_min_distance_usd = soft_min_distance_usd
        self.soft_min_market_odds = soft_min_market_odds
        self.soft_max_market_odds = soft_max_market_odds
        self.soft_min_edge = soft_min_edge
        self.soft_min_fair = soft_min_fair
        self.max_notional_usdc = max_notional_usdc
        self.stop_loss_price = stop_loss_price

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
        signaled_window = False

        for rec in records:
            ts = rec["ts"]
            slug = rec["slug"]
            btc_price = rec["price"]
            strike = rec.get("strike", 0)
            up_mid = rec.get("up_mid", 0)
            down_mid = rec.get("down_mid", 0)
            up_sell = rec.get("up_sell", 0) or up_mid
            down_sell = rec.get("down_sell", 0) or down_mid
            up_buy = rec.get("up_buy", 0) or up_mid
            down_buy = rec.get("down_buy", 0) or down_mid
            remaining = rec.get("seconds_remaining", 0)

            # Window change — settle and reset
            if slug != current_slug:
                if current_trade is not None:
                    settle_trade(current_trade, window_outcomes, result)
                    current_trade = None
                current_slug = slug
                signaled_window = False

            # Already in a trade — check stop-loss, then hold
            if current_trade is not None:
                if self.stop_loss_price > 0:
                    # Check if our token's mid has dropped below stop-loss
                    if current_trade.direction == "UP":
                        current_mid = up_mid
                        sell_price = up_buy  # sell at bid
                    else:
                        current_mid = down_mid
                        sell_price = down_buy

                    if current_mid > 0 and current_mid < self.stop_loss_price:
                        # Stop-loss triggered — sell at current bid
                        if sell_price <= 0:
                            sell_price = current_mid
                        exit_fee = current_trade.shares * FEE_RATE * sell_price * (1 - sell_price)
                        current_trade.pnl = (
                            current_trade.shares * sell_price
                            - current_trade.shares * current_trade.entry_price
                            - current_trade.fee - exit_fee
                        )
                        current_trade.outcome = "STOP"
                        current_trade.meta += f" → STOP@{sell_price:.3f}"
                        result.total_pnl += current_trade.pnl
                        result.total_fees += current_trade.fee + exit_fee
                        result.bankroll += current_trade.pnl
                        result.losses += 1
                        result.equity_curve.append(result.bankroll)
                        result.trades.append(current_trade)
                        current_trade = None
                        continue
                continue

            # One signal per window
            if signaled_window:
                continue

            # Time window
            if remaining < self.min_seconds or remaining > self.soft_max_seconds:
                continue

            if btc_price <= 0 or strike <= 0:
                continue

            # Determine tier
            tier = "strict" if remaining <= self.max_seconds else "soft"
            min_distance = self.min_distance_usd if tier == "strict" else self.soft_min_distance_usd
            min_odds = self.min_market_odds if tier == "strict" else self.soft_min_market_odds
            max_odds = self.max_market_odds if tier == "strict" else self.soft_max_market_odds
            min_edge = self.min_edge if tier == "strict" else self.soft_min_edge
            min_fair = self.min_fair if tier == "strict" else self.soft_min_fair

            # Distance from strike
            distance = abs(btc_price - strike)
            if distance < min_distance:
                continue

            # Direction from BTC price vs strike
            direction = "UP" if btc_price >= strike else "DOWN"

            if direction == "UP":
                market_price = up_sell
                opposite_price = down_sell
            else:
                market_price = down_sell
                opposite_price = up_sell

            if market_price <= 0:
                continue

            # Market odds check
            if market_price < min_odds:
                continue
            if market_price > max_odds:
                continue

            # Direction mismatch: opposite side shouldn't be more confident
            if opposite_price >= min_odds and market_price < min_odds:
                continue

            # Fair value
            fair = max(min_fair, min(0.999, market_price + min_edge))
            edge = fair - market_price
            if edge < min_edge:
                continue

            # Sizing: fixed notional
            size_usdc = min(self.max_notional_usdc, result.bankroll * 0.05)
            if size_usdc < 5:
                continue
            shares = size_usdc / market_price
            fee = shares * FEE_RATE * market_price * (1 - market_price)

            current_trade = ReplayTrade(
                strategy=self.name, window_slug=slug, asset=rec["asset"],
                direction=direction, entry_price=market_price,
                fair_value=fair, edge=edge,
                size_usdc=size_usdc, shares=shares, entry_ts=ts, fee=fee,
                seconds_remaining=remaining,
                meta=f"tier={tier} dist=${distance:.0f} odds={market_price:.3f}",
            )
            signaled_window = True

        # Settle final trade
        if current_trade is not None:
            settle_trade(current_trade, window_outcomes, result)

        compute_max_drawdown(result)
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────────

class PortfolioReplayStrategy:
    """
    Portfolio layer over replay wrappers.

    The underlying wrappers remain responsible for strategy entry logic. This
    class only filters, prioritizes, and resizes candidate trades.
    """
    name = "portfolio"

    WEIGHTS = {
        "warmup": {"oracle": 0.50, "leadlag": 0.50},
        "early_contested": {"momentum": 0.40, "oracle": 0.35, "leadlag": 0.25},
        "mid_shock": {"oracle": 0.40, "leadlag": 0.40, "momentum": 0.20},
        "endgame": {"snipe": 0.50, "oracle": 0.25, "leadlag": 0.25},
        "deadzone": {},
    }
    PRIORITY = {"snipe": 4, "oracle": 3, "leadlag": 2, "momentum": 1}

    def __init__(
        self,
        move_bps: float = 6.0,
        staleness: float = 0.15,
        max_price: float = 0.55,
        min_price: float = 0.20,
        max_notional: float = 500,
        leader: str = "btc",
        lag_bps: float = 5.0,
        lag_staleness: float = 0.10,
        min_edge: float = 0.16,
        mom_min_price: float = 0.40,
        fair_cap: float = 0.80,
        confirmations: int = 2,
        down_edge_boost: float = 0.10,
        stop_loss: float = 0.0,
    ):
        self.move_bps = move_bps
        self.staleness = staleness
        self.max_price = max_price
        self.min_price = min_price
        self.max_notional = max_notional
        self.leader = leader
        self.lag_bps = lag_bps
        self.lag_staleness = lag_staleness
        self.min_edge = min_edge
        self.mom_min_price = mom_min_price
        self.fair_cap = fair_cap
        self.confirmations = confirmations
        self.down_edge_boost = down_edge_boost
        self.stop_loss = stop_loss

    def run_multi(self, all_records: Dict[str, List[dict]], bankroll: float = 10_000) -> Dict[str, ReplayResult]:
        candidates: list[ReplayTrade] = []

        for asset, recs in sorted(all_records.items()):
            oracle = OracleReplayStrategy(
                move_threshold_bps=self.move_bps,
                staleness_threshold=self.staleness,
                max_price=self.max_price,
                min_price=self.min_price,
                max_notional_usdc=self.max_notional,
            )
            momentum = MomentumReplayStrategy(
                min_edge=self.min_edge,
                max_price=self.max_price,
                min_price=self.mom_min_price,
                fair_cap=self.fair_cap,
                confirmations_required=self.confirmations,
                down_edge_boost=self.down_edge_boost,
                max_notional_usdc=self.max_notional,
            )
            snipe = SnipeReplayStrategy(max_notional_usdc=self.max_notional, stop_loss_price=self.stop_loss)

            for child_name, result in [
                ("oracle", oracle.run(recs, bankroll=bankroll)),
                ("momentum", momentum.run(recs, bankroll=bankroll)),
                ("snipe", snipe.run(recs, bankroll=bankroll)),
            ]:
                candidates.extend(self._tag_trade(t, child_name) for t in result.trades)

        if self.leader in all_records:
            leadlag = LeadLagReplayStrategy(
                leader=self.leader,
                move_threshold_bps=self.lag_bps,
                staleness_threshold=self.lag_staleness,
                max_price=self.max_price,
                min_price=self.min_price,
                max_notional_usdc=self.max_notional,
            )
            for result in leadlag.run_multi(all_records, bankroll=bankroll).values():
                candidates.extend(self._tag_trade(t, "leadlag") for t in result.trades)

        selected = self._select_candidates(candidates, bankroll)
        return self._build_results(selected, all_records, bankroll)

    def _select_candidates(self, candidates: List[ReplayTrade], bankroll: float) -> List[ReplayTrade]:
        by_window = defaultdict(list)
        for trade in candidates:
            child = self._meta_value(trade.meta, "child")
            regime = self._regime(trade.seconds_remaining)
            if self.WEIGHTS.get(regime, {}).get(child, 0.0) <= 0:
                continue
            by_window[(trade.asset, trade.window_slug)].append(trade)

        chosen: List[ReplayTrade] = []
        for trades in by_window.values():
            snipe_trades = [t for t in trades if self._meta_value(t.meta, "child") == "snipe"]
            pool = snipe_trades or trades
            pool.sort(
                key=lambda t: (
                    self.PRIORITY.get(self._meta_value(t.meta, "child"), 0),
                    t.edge,
                ),
                reverse=True,
            )
            chosen.append(pool[0])

        chosen.sort(key=lambda t: t.entry_ts)
        open_positions: list[tuple[float, float]] = []
        window_used = defaultdict(float)
        output: List[ReplayTrade] = []

        for trade in chosen:
            regime = self._regime(trade.seconds_remaining)
            child = self._meta_value(trade.meta, "child")
            weights = self.WEIGHTS.get(regime, {})
            settle_ts = trade.entry_ts + max(trade.seconds_remaining, 0)
            open_positions = [(ts, n) for ts, n in open_positions if ts > trade.entry_ts]
            open_notional = sum(n for _ts, n in open_positions)

            sleeve_cap = bankroll * weights.get(child, 0.0)
            window_cap = bankroll * 0.25
            total_cap_left = bankroll * 0.60 - open_notional
            window_key = (trade.asset, trade.window_slug)
            allowed = min(trade.size_usdc, sleeve_cap, window_cap - window_used[window_key], total_cap_left)
            if allowed < 5 or trade.size_usdc <= 0:
                continue

            resized = copy.copy(trade)
            ratio = allowed / trade.size_usdc
            resized.size_usdc = allowed
            resized.shares *= ratio
            resized.fee *= ratio
            resized.pnl *= ratio
            resized.strategy = self.name
            resized.meta = f"regime={regime} {resized.meta}".strip()

            window_used[window_key] += allowed
            open_positions.append((settle_ts, allowed))
            output.append(resized)

        return output

    def _build_results(self, trades: List[ReplayTrade], all_records: Dict[str, List[dict]], bankroll: float) -> Dict[str, ReplayResult]:
        results = {
            asset: ReplayResult(asset=asset, strategy=self.name, bankroll=bankroll, equity_curve=[bankroll])
            for asset in all_records
        }
        for trade in trades:
            result = results.setdefault(
                trade.asset,
                ReplayResult(asset=trade.asset, strategy=self.name, bankroll=bankroll, equity_curve=[bankroll]),
            )
            result.trades.append(trade)
            result.total_pnl += trade.pnl
            result.total_fees += trade.fee
            result.bankroll += trade.pnl
            if trade.outcome == "WIN":
                result.wins += 1
            else:
                result.losses += 1
            result.equity_curve.append(result.bankroll)

        for result in results.values():
            compute_max_drawdown(result)
        return results

    @staticmethod
    def _tag_trade(trade: ReplayTrade, child: str) -> ReplayTrade:
        tagged = copy.copy(trade)
        tagged.meta = f"child={child} {tagged.meta}".strip()
        return tagged

    @staticmethod
    def _regime(remaining: float) -> str:
        if remaining < 12:
            return "deadzone"
        if remaining <= 60:
            return "endgame"
        if remaining <= 180:
            return "mid_shock"
        if remaining <= 240:
            return "early_contested"
        return "warmup"

    @staticmethod
    def _meta_value(meta: str, key: str) -> str:
        prefix = f"{key}="
        for part in (meta or "").split():
            if part.startswith(prefix):
                return part[len(prefix):]
        return ""


class RLReplayStrategy:
    """Replay the lightweight tabular RL policy on recorded market ticks."""

    name = "rl"

    def __init__(
        self,
        model_path: str = "reports/rl_model.json",
        model: Optional[TabularQModel] = None,
        min_price: float = 0.20,
        max_price: float = 0.95,
        min_q: float = 5.0,
        min_edge: float = 0.02,
        max_spread: float = 0.10,
        cooldown_seconds: float = 600.0,
        q_scale: float = 0.0,
        fee_edge_multiplier: float = 0.25,
        min_depth: float = 50.0,
        depth_buffer: float = 1.25,
    ):
        self.model_path = model_path
        self.model = model
        self.min_price = min_price
        self.max_price = max_price
        self.min_q = min_q
        self.min_edge = min_edge
        self.max_spread = max_spread
        self.cooldown_seconds = cooldown_seconds
        self.q_scale = q_scale
        self.fee_edge_multiplier = fee_edge_multiplier
        self.min_depth = min_depth
        self.depth_buffer = depth_buffer

    def run_multi(self, all_records: Dict[str, List[dict]], bankroll: float = 10_000) -> Dict[str, ReplayResult]:
        if self.model is None:
            self.model = TabularQModel.load(self.model_path)

        results: Dict[str, ReplayResult] = {}
        for asset, records in sorted(all_records.items()):
            results[asset] = self.run(records, bankroll=bankroll)
        return results

    def run(self, records: List[dict], bankroll: float = 10_000) -> ReplayResult:
        if self.model is None:
            self.model = TabularQModel.load(self.model_path)

        result = ReplayResult(
            asset=records[0]["asset"] if records else "?",
            strategy=self.name,
            bankroll=bankroll,
            equity_curve=[bankroll],
        )
        if not records:
            return result

        outcomes = compute_window_outcomes(records)
        signaled_windows: set[str] = set()
        prev_price = 0.0
        last_trade_ts = -1e18

        for rec in sorted(records, key=lambda r: float(r.get("ts", 0) or 0)):
            ts = float(rec.get("ts", 0) or 0)
            slug = rec.get("slug", "")
            if slug in signaled_windows:
                prev_price = float(rec.get("price", 0) or prev_price)
                continue
            if ts - last_trade_ts < self.cooldown_seconds:
                prev_price = float(rec.get("price", 0) or prev_price)
                continue

            action, q_values, reason = self.model.choose(
                rec,
                prev_price=prev_price,
                min_price=self.min_price,
                max_price=self.max_price,
            )
            prev_price = float(rec.get("price", 0) or prev_price)

            if action == "HOLD" or reason:
                continue

            outcome = outcomes.get(slug)
            direction = action_direction(action)
            entry_price = price_for_action(rec, action)
            if not outcome or not direction or entry_price <= 0:
                continue

            action_idx = ACTIONS.index(action)
            action_q = q_values[action_idx] if action_idx < len(q_values) else 0.0
            base_size_usdc = result.bankroll * action_fraction(action)
            edge = action_q / max(base_size_usdc, 1.0)
            spread = spread_for_direction(rec, direction)
            thresholds = rl_gate_thresholds(
                rec,
                price=entry_price,
                base_min_q=self.min_q,
                base_min_edge=self.min_edge,
                base_max_spread=self.max_spread,
                fee_edge_multiplier=self.fee_edge_multiplier,
            )
            if action_q < thresholds["min_q"]:
                continue
            if edge < thresholds["min_edge"]:
                continue
            if thresholds["max_spread"] > 0 and spread > thresholds["max_spread"]:
                continue

            size_multiplier = 1.0 if self.q_scale <= 0 else min(1.0, max(0.25, action_q / self.q_scale))
            size_usdc = base_size_usdc * size_multiplier
            if size_usdc < 5:
                continue
            shares = size_usdc / entry_price
            depth = ask_depth_for_direction(rec, direction)
            required_depth = max(self.min_depth, shares * self.depth_buffer)
            if depth < required_depth:
                continue
            fee = shares * FEE_RATE * entry_price * (1 - entry_price)
            q_str = ",".join(f"{ACTIONS[i]}={q_values[i]:.2f}" for i in range(len(ACTIONS)))

            trade = ReplayTrade(
                strategy=self.name,
                window_slug=slug,
                asset=rec.get("asset", result.asset),
                direction=direction,
                entry_price=entry_price,
                fair_value=min(0.999, max(0.0, entry_price + max(edge, 0.0))),
                edge=edge,
                size_usdc=size_usdc,
                shares=shares,
                entry_ts=ts,
                fee=fee,
                seconds_remaining=float(rec.get("seconds_remaining", 0) or 0),
                meta=(
                    f"action={action} regime={rl_regime(float(rec.get('seconds_remaining', 0) or 0))} "
                    f"q={action_q:.2f} edge={edge:.4f} min_edge={thresholds['min_edge']:.4f} "
                    f"fee_edge={thresholds['fee_edge']:.4f} spread={spread:.4f} max_spread={thresholds['max_spread']:.4f} "
                    f"depth={depth:.1f} req_depth={required_depth:.1f} size_mult={size_multiplier:.2f} {q_str}"
                ),
            )
            settle_trade(trade, outcomes, result)
            signaled_windows.add(slug)
            last_trade_ts = ts

        compute_max_drawdown(result)
        return result


class VolConvexityReplayStrategy:
    """Replay volatility convexity arbitrage on recorded rolling market data."""

    name = "volconv"

    def __init__(
        self,
        lookback_seconds: float = 30.0,
        min_range_bps: float = 8.0,
        max_distance_bps: float = 20.0,
        min_seconds: float = 20.0,
        max_seconds: float = 120.0,
        min_price: float = 0.20,
        max_price: float = 0.55,
        min_edge: float = 0.04,
        max_spread: float = 0.08,
        max_notional_usdc: float = 150.0,
        max_bet_pct: float = 0.01,
        kelly_frac: float = 0.20,
        slippage_buffer: float = 0.015,
        depth_buffer: float = 1.0,
    ):
        self.lookback_seconds = lookback_seconds
        self.min_range_bps = min_range_bps
        self.max_distance_bps = max_distance_bps
        self.min_seconds = min_seconds
        self.max_seconds = max_seconds
        self.min_price = min_price
        self.max_price = max_price
        self.min_edge = min_edge
        self.max_spread = max_spread
        self.max_notional_usdc = max_notional_usdc
        self.max_bet_pct = max_bet_pct
        self.kelly_frac = kelly_frac
        self.slippage_buffer = slippage_buffer
        self.depth_buffer = depth_buffer

    def run(self, records: List[dict], bankroll: float = 10_000) -> ReplayResult:
        result = ReplayResult(
            asset=records[0]["asset"] if records else "?",
            strategy=self.name,
            bankroll=bankroll,
        )
        result.equity_curve.append(bankroll)

        outcomes = compute_window_outcomes(records)
        prices = deque(maxlen=500)
        current_slug = None
        current_trade: Optional[ReplayTrade] = None
        signaled_window = False

        for rec in records:
            ts = float(rec.get("ts", 0) or 0)
            slug = rec.get("slug", "")
            spot = float(rec.get("price", 0) or 0)
            strike = float(rec.get("strike", 0) or 0)
            remaining = float(rec.get("seconds_remaining", 0) or 0)

            if slug != current_slug:
                if current_trade is not None:
                    settle_trade(current_trade, outcomes, result)
                    current_trade = None
                current_slug = slug
                signaled_window = False

            if spot > 0 and ts > 0:
                prices.append((ts, spot))
                self._trim_prices(prices, ts)

            if current_trade is not None or signaled_window:
                continue
            if remaining < self.min_seconds or remaining > self.max_seconds:
                continue
            if spot <= 0 or strike <= 0:
                continue

            distance_bps = abs(spot - strike) / spot * 10_000
            if distance_bps > self.max_distance_bps:
                continue

            up_mid = float(rec.get("up_mid", 0) or 0)
            down_mid = float(rec.get("down_mid", 0) or 0)
            if not (0.40 <= up_mid <= 0.60 or 0.40 <= down_mid <= 0.60):
                continue

            range_bps, sigma = self._realized_vol(prices, ts)
            if range_bps < self.min_range_bps or sigma <= 0:
                continue

            fair_up = self._digital_fair_up(spot, strike, sigma, remaining)
            candidates = [
                self._candidate(rec, "UP", fair_up),
                self._candidate(rec, "DOWN", 1.0 - fair_up),
            ]
            candidates = [c for c in candidates if c is not None]
            if not candidates:
                continue

            cand = max(candidates, key=lambda c: c["net_edge"])
            if cand["net_edge"] < self.min_edge:
                continue

            size_usdc = kelly_size(
                fair=cand["fair"],
                market_price=cand["entry_price"],
                bankroll=result.bankroll,
                kelly_frac=self.kelly_frac,
                max_bet_pct=self.max_bet_pct,
            )
            size_usdc = min(size_usdc, self.max_notional_usdc)
            if size_usdc < 5:
                continue

            shares = size_usdc / cand["entry_price"]
            if cand["ask_depth"] < shares * self.depth_buffer:
                continue

            fee = shares * FEE_RATE * cand["entry_price"] * (1.0 - cand["entry_price"])
            current_trade = ReplayTrade(
                strategy=self.name,
                window_slug=slug,
                asset=rec["asset"],
                direction=cand["direction"],
                entry_price=cand["entry_price"],
                fair_value=cand["fair"],
                edge=cand["net_edge"],
                size_usdc=size_usdc,
                shares=shares,
                entry_ts=ts,
                move_bps=range_bps,
                staleness=distance_bps,
                fee=fee,
                seconds_remaining=remaining,
                meta=(
                    f"range_bps={range_bps:.1f} distance_bps={distance_bps:.1f} "
                    f"remaining={remaining:.1f} sigma={sigma:.8f} "
                    f"fee_edge={cand['fee_drag']:.4f} spread={cand['spread']:.4f} "
                    f"depth={cand['ask_depth']:.1f}"
                ),
            )
            signaled_window = True

        if current_trade is not None:
            settle_trade(current_trade, outcomes, result)

        compute_max_drawdown(result)
        return result

    def _candidate(self, rec: dict, direction: str, fair: float) -> Optional[dict]:
        prefix = "up" if direction == "UP" else "down"
        entry_price = float(rec.get(f"{prefix}_sell", 0) or rec.get(f"{prefix}_mid", 0) or 0)
        spread = float(rec.get(f"{prefix}_spread", 0) or 0)
        ask_depth = float(rec.get(f"{prefix}_ask_depth", 0) or 0)
        if entry_price < self.min_price or entry_price > self.max_price:
            return None
        if spread < 0 or spread > self.max_spread:
            return None
        if ask_depth <= 0:
            return None
        fee_drag = FEE_RATE * entry_price * (1.0 - entry_price)
        net_edge = fair - entry_price - fee_drag - self.slippage_buffer
        return {
            "direction": direction,
            "entry_price": entry_price,
            "fair": fair,
            "spread": spread,
            "ask_depth": ask_depth,
            "fee_drag": fee_drag,
            "net_edge": net_edge,
        }

    def _trim_prices(self, prices: deque, ts: float):
        cutoff = ts - self.lookback_seconds
        while prices and prices[0][0] < cutoff:
            prices.popleft()

    def _realized_vol(self, prices: deque, ts: float) -> tuple[float, float]:
        self._trim_prices(prices, ts)
        items = list(prices)
        if len(items) < 3:
            return 0.0, 0.0
        values = [p for _, p in items if p > 0]
        if len(values) < 3:
            return 0.0, 0.0
        last = values[-1]
        range_bps = (max(values) - min(values)) / last * 10_000 if last > 0 else 0.0

        variance_sum = 0.0
        dt_sum = 0.0
        prev_t, prev_p = items[0]
        for t, p in items[1:]:
            dt = max(t - prev_t, 1e-6)
            if prev_p > 0 and p > 0:
                ret = math.log(p / prev_p)
                variance_sum += ret * ret
                dt_sum += dt
            prev_t, prev_p = t, p
        sigma = math.sqrt(variance_sum / dt_sum) if dt_sum > 0 else 0.0
        return range_bps, sigma

    @staticmethod
    def _digital_fair_up(spot: float, strike: float, sigma: float, seconds_remaining: float) -> float:
        denom = spot * sigma * math.sqrt(max(seconds_remaining, 1e-6))
        if denom <= 0:
            return 0.5
        z = (spot - strike) / denom
        fair = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        return max(0.001, min(0.999, fair))

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

def _meta_value(meta: str, key: str) -> str:
    prefix = f"{key}="
    for part in (meta or "").split():
        if part.startswith(prefix):
            return part[len(prefix):]
    return ""


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

            if any("child=" in (t.meta or "") for t in result.trades):
                child_counts = defaultdict(lambda: [0, 0, 0.0])
                regime_counts = defaultdict(lambda: [0, 0, 0.0])
                for t in result.trades:
                    child = _meta_value(t.meta, "child") or "?"
                    regime = _meta_value(t.meta, "regime") or "?"
                    child_counts[child][0] += 1
                    child_counts[child][1] += 1 if t.outcome == "WIN" else 0
                    child_counts[child][2] += t.pnl
                    regime_counts[regime][0] += 1
                    regime_counts[regime][1] += 1 if t.outcome == "WIN" else 0
                    regime_counts[regime][2] += t.pnl
                child_str = ", ".join(
                    f"{k}:{v[0]}({v[1]}W ${v[2]:+.0f})" for k, v in sorted(child_counts.items())
                )
                regime_str = ", ".join(
                    f"{k}:{v[0]}({v[1]}W ${v[2]:+.0f})" for k, v in sorted(regime_counts.items())
                )
                print(f"  Child: {child_str}")
                print(f"  Regime: {regime_str}")

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
                        help="Strategy: oracle, momentum, leadlag, convergence, snipe, volconv, portfolio, rl, or comma-separated")
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

    # Snipe params
    parser.add_argument("--stop-loss", type=float, default=0.0,
                        help="Snipe stop-loss price — sell if token mid drops below this (default: 0 = disabled)")

    parser.add_argument("--rl-model", type=str, default="reports/rl_model.json",
                        help="Path to tabular RL model JSON (default: reports/rl_model.json)")
    parser.add_argument("--rl-min-price", type=float, default=0.20,
                        help="RL hard min entry price (default: 0.20)")
    parser.add_argument("--rl-max-price", type=float, default=0.95,
                        help="RL hard max entry price (default: 0.95)")
    parser.add_argument("--rl-min-q", type=float, default=5.0,
                        help="RL minimum selected action Q/PnL in dollars (default: 5.0)")
    parser.add_argument("--rl-min-edge", type=float, default=0.02,
                        help="RL minimum expected edge as Q/notional (default: 0.02)")
    parser.add_argument("--rl-max-spread", type=float, default=0.10,
                        help="RL max target-side spread; 0 disables (default: 0.10)")
    parser.add_argument("--rl-cooldown", type=float, default=600.0,
                        help="RL cooldown seconds per asset after an entry (default: 600)")
    parser.add_argument("--rl-q-scale", type=float, default=0.0,
                        help="RL Q dollars needed for full size; 0 disables size scaling (default: 0)")
    parser.add_argument("--rl-fee-edge-mult", type=float, default=0.25,
                        help="RL extra edge requirement as multiplier of fee/notional (default: 0.25)")
    parser.add_argument("--rl-min-depth", type=float, default=50.0,
                        help="RL minimum target-side ask depth in shares (default: 50)")
    parser.add_argument("--rl-depth-buffer", type=float, default=1.25,
                        help="RL required ask depth as shares * buffer (default: 1.25)")

    # Convergence params
    parser.add_argument("--conv-threshold", type=float, default=0.88,
                        help="Convergence confidence threshold (default: 0.88)")
    parser.add_argument("--conv-window", type=float, default=45,
                        help="Convergence max seconds remaining (default: 45)")
    parser.add_argument("--conv-confirm", type=int, default=3,
                        help="Convergence consecutive confirm ticks (default: 3)")

    # Volatility convexity params
    parser.add_argument("--vc-min-range-bps", type=float, default=8.0,
                        help="Vol convexity min short-window realized range bps")
    parser.add_argument("--vc-max-distance-bps", type=float, default=20.0,
                        help="Vol convexity max distance from strike in bps")
    parser.add_argument("--vc-min-edge", type=float, default=0.04,
                        help="Vol convexity min net edge after fee/slippage")
    parser.add_argument("--vc-max-notional", type=float, default=150.0,
                        help="Vol convexity max notional per trade in USDC")
    parser.add_argument("--vc-min-seconds", type=float, default=20.0,
                        help="Vol convexity minimum seconds remaining")
    parser.add_argument("--vc-max-seconds", type=float, default=120.0,
                        help="Vol convexity maximum seconds remaining")

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

        elif strat_name == "snipe":
            results = {}
            for asset in sorted(all_records):
                recs = all_records[asset]
                windows = len(set(r["slug"] for r in recs))
                print(f"[snipe] Replaying {asset.upper()}: {len(recs):,} ticks, {windows} windows...")
                strategy = SnipeReplayStrategy(
                    max_notional_usdc=args.max_notional,
                    stop_loss_price=args.stop_loss,
                )
                results[asset] = strategy.run(recs, bankroll=args.bankroll)
            sl_label = f"stop_loss={args.stop_loss}" if args.stop_loss > 0 else "stop_loss=OFF"
            print_report("Last Seconds Snipe", results, {
                "strict": "15-30s/0.82-0.94/$30",
                "soft": "30-45s/0.85-0.93/$45",
                "stop_loss": sl_label,
            })

        elif strat_name == "volconv":
            results = {}
            for asset in sorted(all_records):
                recs = all_records[asset]
                windows = len(set(r["slug"] for r in recs))
                print(f"[volconv] Replaying {asset.upper()}: {len(recs):,} ticks, {windows} windows...")
                strategy = VolConvexityReplayStrategy(
                    min_range_bps=args.vc_min_range_bps,
                    max_distance_bps=args.vc_max_distance_bps,
                    min_edge=args.vc_min_edge,
                    max_notional_usdc=args.vc_max_notional,
                    min_seconds=args.vc_min_seconds,
                    max_seconds=args.vc_max_seconds,
                    min_price=args.min_price,
                    max_price=args.max_price,
                )
                results[asset] = strategy.run(recs, bankroll=args.bankroll)
            print_report("Volatility Convexity", results, {
                "range_bps": args.vc_min_range_bps,
                "distance_bps": args.vc_max_distance_bps,
                "edge": args.vc_min_edge,
                "max_notional": args.vc_max_notional,
                "window": f"{args.vc_min_seconds}-{args.vc_max_seconds}s",
            })

        elif strat_name == "portfolio":
            strategy = PortfolioReplayStrategy(
                move_bps=args.move_bps,
                staleness=args.staleness,
                max_price=args.max_price,
                min_price=args.min_price,
                max_notional=args.max_notional,
                leader=args.leader,
                lag_bps=args.lag_bps,
                lag_staleness=args.lag_staleness,
                min_edge=args.min_edge,
                mom_min_price=args.mom_min_price,
                fair_cap=args.fair_cap,
                confirmations=args.confirmations,
                down_edge_boost=args.down_edge_boost,
                stop_loss=args.stop_loss,
            )
            total_ticks = sum(len(r) for r in all_records.values())
            print(f"[portfolio] Replaying {len(all_records)} assets, {total_ticks:,} ticks...")
            results = strategy.run_multi(all_records, bankroll=args.bankroll)
            print_report("Regime Portfolio", results, {
                "risk": "aggressive",
                "window_cap": "25%",
                "total_cap": "60%",
                "leader": args.leader,
            })

        elif strat_name == "rl":
            strategy = RLReplayStrategy(
                model_path=args.rl_model,
                min_price=args.rl_min_price,
                max_price=args.rl_max_price,
                min_q=args.rl_min_q,
                min_edge=args.rl_min_edge,
                max_spread=args.rl_max_spread,
                cooldown_seconds=args.rl_cooldown,
                q_scale=args.rl_q_scale,
                fee_edge_multiplier=args.rl_fee_edge_mult,
                min_depth=args.rl_min_depth,
                depth_buffer=args.rl_depth_buffer,
            )
            total_ticks = sum(len(r) for r in all_records.values())
            print(f"[rl] Replaying {len(all_records)} assets, {total_ticks:,} ticks with {args.rl_model}...")
            results = strategy.run_multi(all_records, bankroll=args.bankroll)
            print_report("RL Shadow Policy", results, {
                "model": args.rl_model,
                "min_price": args.rl_min_price,
                "max_price": args.rl_max_price,
                "min_q": args.rl_min_q,
                "min_edge": args.rl_min_edge,
                "max_spread": args.rl_max_spread,
                "cooldown": args.rl_cooldown,
                "fee_edge_mult": args.rl_fee_edge_mult,
                "min_depth": args.rl_min_depth,
                "depth_buffer": args.rl_depth_buffer,
                "mode": "replay",
            })

        else:
            print(f"Unknown strategy: {strat_name}")
            sys.exit(1)


if __name__ == "__main__":
    main()
