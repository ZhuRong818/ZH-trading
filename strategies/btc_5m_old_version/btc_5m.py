"""
BTC Up or Down 5-Minute Strategy

Trades Polymarket's rolling 5-minute BTC binary options.
Every 5 minutes a new market opens: "Will BTC be above the starting price
at the end of this 5-minute window?"

How it works:
    1. Auto-discovers the current 5m market via timestamp-based slug
    2. Fetches real-time BTC price from findata
    3. Computes momentum signal from recent BTC price action
    4. Compares signal vs Polymarket odds to find edge
    5. Trades via Kelly sizing if edge > threshold
    6. Rolls to the next market when the current one resolves

Resolution source: Chainlink BTC/USD data stream
We use findata spot quotes as the live proxy.

Integrated into the main trading system. Run via:
    python main.py --strategy btc5m --dry-run
    python main.py --strategy all --dry-run

The strategy runs in a background daemon thread managed by TradingSystem.
"""

import json
import logging
import math
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests
import numpy as np

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import SystemConfig, CLOB_BASE, GAMMA_BASE
from data_pipeline.market_data import MarketDataFeed, OrderBookSnapshot
from data_pipeline.price_feeds import get_asset_price_cached
from ems.execution import ExecutionEngine, ClobAuth
from oms.position_manager import PositionManager, Fill
from strategies.kelly import kelly_size

log = logging.getLogger(__name__)

@dataclass
class BTC5mConfig:
    # Signal
    momentum_window: int = 20        # BTC price ticks to compute momentum
    momentum_threshold: float = 0.00005  # min BTC % move to generate signal (0.005%)
    price_poll_interval: float = 0.5  # seconds between BTC price polls (HF mode)
    min_entry_age_seconds: float = 20.0  # skip unstable opening seconds
    max_adverse_distance_bps: float = 2.0 # do not buy against strike by more than this

    # Edge
    min_edge: float = 0.03           # min difference vs market odds to trade
    entry_deadline_seconds: float = 60  # don't enter with < 1 min left
    book_cache_ttl: float = 1.0      # seconds to reuse CLOB books inside the HF loop
    gamma_retry_seconds: float = 5.0 # backoff after a missing Gamma 5m market
    both_leg_min_edge: float = 0.005 # required buy-both discount after executable prices
    skip_log_interval: float = 10.0  # seconds between repeated skip/wait debug logs
    min_tick_volatility: float = 0.00002 # floor for per-tick BTC return volatility
    drift_z_weight: float = 0.20     # small momentum drift adjustment to strike-distance model
    trend_z_weight: float = 0.10

    # Sizing
    kelly_fraction: float = 0.20     # 20% Kelly
    max_bet_pct: float = 0.05        # max 5% of bankroll per 5m window
    bankroll: float = 5_000.0        # total capital for this strategy
    min_bet_usdc: float = 5.0        # Polymarket minimum order

    # Risk
    max_consecutive_losses: int = 5  # stop after N losses in a row
    daily_loss_limit: float = 500.0  # stop if daily loss exceeds this


@dataclass
class MarketWindow:
    """Represents one 5-minute market window."""
    slug: str
    event_id: str
    market_id: str
    condition_id: str
    up_token: str
    down_token: str
    start_time: datetime
    end_time: datetime
    tick_size: str = "0.01"
    neg_risk: bool = False
    starting_btc_price: float = 0.0


class BTCPriceFeed:
    """Real-time BTC price from findata."""

    def __init__(self):
        self.prices: deque = deque(maxlen=200)
        self._last_fetch = 0.0

    def fetch(self) -> float:
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                price = get_asset_price_cached("btc")
                self.prices.append(price)
                self._last_fetch = time.time()
                return price
            except requests.RequestException as e:
                log.debug("BTCPriceFeed.fetch network error %d/%d: %s", attempt, max_attempts, e)
                if attempt < max_attempts:
                    time.sleep(0.5 * (2 ** (attempt - 1)))
            except Exception as e:
                log.warning("BTCPriceFeed.fetch unexpected error: %s", e)
                break

        cached = self.current
        if cached is not None:
            log.warning("BTCPriceFeed.fetch using cached price $%.2f after failures", cached)
            return cached

        raise RuntimeError("BTCPriceFeed.fetch failed and no cached price available")

    @property
    def current(self) -> Optional[float]:
        return self.prices[-1] if self.prices else None

    def momentum(self, window: int = 20) -> float:
        """
        Returns the % price change over the last N ticks.
        Positive = trending up, negative = trending down.
        """
        if len(self.prices) < 2:
            return 0.0
        lookback = min(window, len(self.prices))
        old = self.prices[-lookback]
        new = self.prices[-1]
        if old == 0:
            return 0.0
        return (new - old) / old

    def volatility(self, window: int = 20) -> float:
        """Rolling std dev of returns."""
        if len(self.prices) < 3:
            return 0.001
        lookback = min(window, len(self.prices))
        recent = list(self.prices)[-lookback:]
        returns = [(recent[i] - recent[i-1]) / recent[i-1] for i in range(1, len(recent))]
        return float(np.std(returns)) if returns else 0.001

    def trend_strength(self, window: int = 20) -> float:
        """
        Trend strength from -1 (strong down) to +1 (strong up).
        Uses linear regression slope normalized by volatility.
        """
        if len(self.prices) < 5:
            return 0.0
        lookback = min(window, len(self.prices))
        recent = list(self.prices)[-lookback:]
        x = np.arange(len(recent))
        y = np.array(recent)
        # Linear regression
        slope = np.polyfit(x, y, 1)[0]
        # Normalize by price level and volatility
        avg_price = np.mean(y)
        vol = self.volatility(window)
        if avg_price == 0 or vol == 0:
            return 0.0
        normalized = (slope / avg_price) / vol
        return float(np.clip(normalized, -1.0, 1.0))


class BTC5mStrategy:
    """
    Trades rolling 5-minute BTC Up/Down markets on Polymarket.
    """

    def __init__(
        self,
        config: BTC5mConfig,
        ems: ExecutionEngine,
        oms: PositionManager,
        data_feed: Optional[MarketDataFeed] = None,
    ):
        self.config = config
        self.ems = ems
        self.oms = oms
        self.data_feed = data_feed or MarketDataFeed()
        self.btc = BTCPriceFeed()
        self.session = requests.Session()

        # State
        self.current_window: Optional[MarketWindow] = None
        self.current_position: Optional[str] = None  # "UP" or "DOWN" or None
        self.current_order_id: Optional[str] = None
        self._both_entry: Optional[dict] = None
        self._market_cache: dict[int, MarketWindow] = {}
        self._market_retry_after: dict[int, float] = {}
        self._last_skip_log = 0.0
        self._last_wait_log = 0.0
        self._last_holding_bucket: Optional[int] = None

        # Stats
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.consecutive_losses = 0
        self.daily_pnl = 0.0
        self.running = False

    # ---- Market Discovery ----

    def _discover_market(self, window_ts: int) -> Optional[MarketWindow]:
        """Find the 5m market for a given timestamp window."""
        cached = self._market_cache.get(window_ts)
        if cached:
            return cached

        retry_after = self._market_retry_after.get(window_ts, 0.0)
        if time.time() < retry_after:
            return None

        slug = f"btc-updown-5m-{window_ts}"
        try:
            resp = self.session.get(f"{GAMMA_BASE}/events/slug/{slug}")
            if resp.status_code != 200:
                self._market_retry_after[window_ts] = time.time() + self.config.gamma_retry_seconds
                return None
            event = resp.json()
            markets = event.get("markets", [])
            if not markets:
                self._market_retry_after[window_ts] = time.time() + self.config.gamma_retry_seconds
                return None

            m = markets[0]
            clob_raw = m.get("clobTokenIds", "[]")
            clob = json.loads(clob_raw) if isinstance(clob_raw, str) else (clob_raw or [])
            if len(clob) < 2:
                self._market_retry_after[window_ts] = time.time() + self.config.gamma_retry_seconds
                return None

            start_str = event.get("startDate", m.get("startDate", ""))
            end_str = m.get("endDate", "")

            try:
                start_time = datetime.fromisoformat(start_str.replace("Z", "+00:00")) if start_str else datetime.now(timezone.utc)
                end_time = datetime.fromisoformat(end_str.replace("Z", "+00:00")) if end_str else start_time
            except (ValueError, TypeError):
                # Fallback: compute from window_ts
                start_time = datetime.fromtimestamp(window_ts, tz=timezone.utc)
                end_time = datetime.fromtimestamp(window_ts + 300, tz=timezone.utc)

            window = MarketWindow(
                slug=slug,
                event_id=str(event.get("id", "")),
                market_id=str(m.get("id", "")),
                condition_id=m.get("conditionId", ""),
                up_token=clob[0],
                down_token=clob[1],
                start_time=start_time,
                end_time=end_time,
                tick_size=str(m.get("orderPriceMinTickSize", "0.01")),
                neg_risk=m.get("negRisk", False),
            )
            self._market_cache[window_ts] = window
            self._market_retry_after.pop(window_ts, None)
            return window
        except Exception as e:
            log.warning("Failed to discover market %s: %s", slug, e)
            self._market_retry_after[window_ts] = time.time() + self.config.gamma_retry_seconds
            return None

    def _get_current_window_ts(self) -> int:
        now = int(time.time())
        return now - (now % 300)

    def _get_next_window_ts(self) -> int:
        return self._get_current_window_ts() + 300

    def _seconds_remaining(self) -> float:
        if not self.current_window:
            return 0
        now = datetime.now(timezone.utc)
        return max((self.current_window.end_time - now).total_seconds(), 0)

    def _seconds_elapsed(self) -> float:
        if not self.current_window:
            return 0
        now = datetime.now(timezone.utc)
        return max((now - self.current_window.start_time).total_seconds(), 0)

    # ---- Market Data ----

    def _get_book(self, token_id: str) -> Optional[OrderBookSnapshot]:
        """Get a fresh enough CLOB book and keep the shared data feed warm."""
        return self.data_feed.get_fresh_book(token_id, max_age=self.config.book_cache_ttl)

    def _get_market_odds(self) -> Optional[dict]:
        """Get executable Up/Down buy prices from the CLOB book."""
        if not self.current_window:
            return None
        try:
            up_book = self._get_book(self.current_window.up_token)
            down_book = self._get_book(self.current_window.down_token)
            if not up_book or not down_book:
                return None
            if up_book.best_ask is None or down_book.best_ask is None:
                return None

            return {
                "up": up_book.best_ask,
                "down": down_book.best_ask,
                "up_bid": up_book.best_bid,
                "down_bid": down_book.best_bid,
                "up_mid": up_book.mid,
                "down_mid": down_book.mid,
                "up_spread": up_book.spread,
                "down_spread": down_book.spread,
            }
        except Exception as e:
            log.warning("Failed to get odds: %s", e)
            return None

    def _try_buy_both(self, odds: dict) -> bool:
        """If executable up+down < 1.0, buy equal shares of both legs.

        Returns True if orders placed (or attempted), False otherwise.
        """
        cfg = self.config
        if not self.current_window or self.current_position is not None:
            return False

        up = odds.get("up", 0.0)
        down = odds.get("down", 0.0)
        total = up + down

        # Only act when the executable combined ask is meaningfully less than $1.00.
        if total >= 1.0 - cfg.both_leg_min_edge:
            return False

        # Respect entry deadline
        remaining = self._seconds_remaining()
        if remaining < cfg.entry_deadline_seconds:
            return False

        # Determine total USD to allocate for the pair (split evenly into pair shares)
        total_usdc = cfg.bankroll * cfg.max_bet_pct
        if total_usdc < cfg.min_bet_usdc * 2:
            # not enough capital to meet minimum per-leg
            return False

        # pair_shares * (up + down) = total_usdc  => pair_shares = total_usdc / total
        pair_shares = total_usdc / total if total > 0 else 0
        if pair_shares <= 0:
            return False

        # Ensure per-leg cost >= min_bet_usdc
        up_cost = pair_shares * up
        down_cost = pair_shares * down
        if up_cost < cfg.min_bet_usdc or down_cost < cfg.min_bet_usdc:
            return False

        up_book = self._get_book(self.current_window.up_token)
        down_book = self._get_book(self.current_window.down_token)
        if not up_book or not down_book:
            return False

        up_vwap, up_fillable = up_book.vwap_price("BUY", pair_shares)
        down_vwap, down_fillable = down_book.vwap_price("BUY", pair_shares)
        if up_vwap is None or down_vwap is None:
            return False
        if up_fillable < pair_shares or down_fillable < pair_shares:
            log.debug(
                "BUY BOTH skipped: insufficient depth pair=%.2f up_fill=%.2f down_fill=%.2f",
                pair_shares, up_fillable, down_fillable,
            )
            return False

        executable_sum = up_vwap + down_vwap
        executable_cost = pair_shares * executable_sum
        if executable_sum >= 1.0 - cfg.both_leg_min_edge:
            log.debug(
                "BUY BOTH skipped: quote_sum=%.4f executable_sum=%.4f pair=%.2f",
                total, executable_sum, pair_shares,
            )
            return False
        if executable_cost > total_usdc:
            pair_shares = total_usdc / executable_sum if executable_sum > 0 else 0.0
            if pair_shares <= 0:
                return False
            up_vwap, up_fillable = up_book.vwap_price("BUY", pair_shares)
            down_vwap, down_fillable = down_book.vwap_price("BUY", pair_shares)
            if (
                up_vwap is None or down_vwap is None
                or up_fillable < pair_shares
                or down_fillable < pair_shares
            ):
                return False
            executable_sum = up_vwap + down_vwap
            if executable_sum >= 1.0 - cfg.both_leg_min_edge:
                log.debug(
                    "BUY BOTH skipped after resize: quote_sum=%.4f executable_sum=%.4f pair=%.2f",
                    total, executable_sum, pair_shares,
                )
                return False

        up_cost = pair_shares * up_vwap
        down_cost = pair_shares * down_vwap
        if up_cost < cfg.min_bet_usdc or down_cost < cfg.min_bet_usdc:
            return False

        up_oid = None
        down_oid = None

        # Place FOK buy orders for both legs at executable ask prices.
        try:
            up_oid = self.ems.place_order(
                token_id=self.current_window.up_token,
                side="BUY",
                price=up_vwap,
                size=pair_shares,
                tick_size=self.current_window.tick_size,
                neg_risk=self.current_window.neg_risk,
                order_type="FOK",
                source="btc5m_both",
            )

            down_oid = self.ems.place_order(
                token_id=self.current_window.down_token,
                side="BUY",
                price=down_vwap,
                size=pair_shares,
                tick_size=self.current_window.tick_size,
                neg_risk=self.current_window.neg_risk,
                order_type="FOK",
                source="btc5m_both",
            )

            if not up_oid or not down_oid:
                if up_oid:
                    bid = odds.get("up_bid")
                    if bid:
                        self.ems.place_order(
                            token_id=self.current_window.up_token,
                            side="SELL",
                            price=bid,
                            size=pair_shares,
                            tick_size=self.current_window.tick_size,
                            neg_risk=self.current_window.neg_risk,
                            order_type="FAK",
                            source="btc5m_both_unwind",
                        )
                if down_oid:
                    bid = odds.get("down_bid")
                    if bid:
                        self.ems.place_order(
                            token_id=self.current_window.down_token,
                            side="SELL",
                            price=bid,
                            size=pair_shares,
                            tick_size=self.current_window.tick_size,
                            neg_risk=self.current_window.neg_risk,
                            order_type="FAK",
                            source="btc5m_both_unwind",
                        )
                log.info(
                    "BUY BOTH skipped: one leg failed up_oid=%s down_oid=%s",
                    bool(up_oid), bool(down_oid),
                )
                return False

            # Mark as both position (do not re-enter in same window)
            self.current_position = "BOTH"
            self._both_entry = {
                "pair_shares": pair_shares,
                "up_oid": up_oid,
                "down_oid": down_oid,
                "entry_sum": executable_sum,
                "quoted_sum": total,
                "total_usdc": total_usdc,
            }
            # Count both legs as trades
            try:
                self.total_trades += 2
            except Exception:
                pass
            log.info(
                "BUY BOTH: up_vwap=%.4f down_vwap=%.4f executable_sum=%.4f "
                "quoted_sum=%.4f pair_shares=%.4f total_usdc=$%.2f",
                up_vwap, down_vwap, executable_sum,
                total, pair_shares, total_usdc,
            )
            return True
        except Exception as e:
            log.warning("Failed to place both-leg orders: %s", e)
            return False

    # ---- Signal ----

    def _compute_signal_legacy(self) -> dict:
        """
        Compute trading signal from BTC price action.

        Returns:
            {
                "direction": "UP" or "DOWN" or "NONE",
                "fair_prob_up": float (0-1),
                "confidence": float (0-1),
                "momentum": float,
                "trend": float,
            }
        """
        cfg = self.config
        momentum = self.btc.momentum(cfg.momentum_window)
        trend = self.btc.trend_strength(cfg.momentum_window)
        vol = self.btc.volatility(cfg.momentum_window)

        # Momentum factor: normalize so 0.1% BTC move = factor of 1
        # In 5-minute crypto, even 0.05% moves are meaningful
        momentum_factor = momentum / 0.001
        momentum_factor = max(-3.0, min(3.0, momentum_factor))

        # Trend adds conviction
        trend_factor = trend  # -1 to +1

        # Combined signal: momentum dominates, trend confirms
        signal_strength = momentum_factor * 0.6 + trend_factor * 0.4
        signal_strength = max(-3.0, min(3.0, signal_strength))

        # Convert to probability: sigmoid-like mapping
        # signal_strength of ±1 -> ~65/35, ±2 -> ~80/20, ±3 -> ~90/10
        import math
        fair_prob_up = 1.0 / (1.0 + math.exp(-signal_strength * 0.8))
        fair_prob_up = max(0.10, min(0.90, fair_prob_up))

        # Confidence based on how clear the signal is
        confidence = min(abs(signal_strength) / 2.0, 1.0)

        # Direction
        if abs(momentum) < cfg.momentum_threshold:
            direction = "NONE"
        elif momentum > 0:
            direction = "UP"
        else:
            direction = "DOWN"

        return {
            "direction": direction,
            "fair_prob_up": fair_prob_up,
            "confidence": confidence,
            "momentum": momentum,
            "trend": trend,
            "volatility": vol,
        }

    def compute_signal(self) -> dict:
        """Estimate finish-above-strike probability from distance, time, and volatility."""
        cfg = self.config
        momentum = self.btc.momentum(cfg.momentum_window)
        trend = self.btc.trend_strength(cfg.momentum_window)
        tick_vol = max(self.btc.volatility(cfg.momentum_window), cfg.min_tick_volatility)

        current = self.btc.current or 0.0
        strike = self.current_window.starting_btc_price if self.current_window else 0.0
        remaining = max(self._seconds_remaining(), cfg.price_poll_interval)

        if current <= 0 or strike <= 0:
            fair_prob_up = 0.50
            z_score = 0.0
            distance = 0.0
        else:
            horizon_ticks = max(remaining / max(cfg.price_poll_interval, 0.001), 1.0)
            horizon_sigma_price = current * tick_vol * math.sqrt(horizon_ticks)
            distance = current - strike
            z_score = distance / horizon_sigma_price if horizon_sigma_price > 0 else 0.0

            momentum_z = max(-2.0, min(2.0, momentum / tick_vol if tick_vol > 0 else 0.0))
            adjusted_z = z_score + cfg.drift_z_weight * momentum_z + cfg.trend_z_weight * trend
            fair_prob_up = 0.5 * (1.0 + math.erf(adjusted_z / math.sqrt(2.0)))
            fair_prob_up = max(0.05, min(0.95, fair_prob_up))

        confidence = min(abs(fair_prob_up - 0.5) / 0.25, 1.0)
        if fair_prob_up > 0.52:
            direction = "UP"
        elif fair_prob_up < 0.48:
            direction = "DOWN"
        else:
            direction = "NONE"

        return {
            "direction": direction,
            "fair_prob_up": fair_prob_up,
            "confidence": confidence,
            "momentum": momentum,
            "trend": trend,
            "volatility": tick_vol,
            "z_score": z_score,
            "distance": distance,
            "seconds_remaining": remaining,
        }

    # ---- Trading ----

    def _direction_allowed(self, direction: str, signal: dict) -> bool:
        current = self.btc.current or 0.0
        strike = self.current_window.starting_btc_price if self.current_window else 0.0
        if current <= 0 or strike <= 0:
            return False

        distance_bps = (current - strike) / strike * 10_000
        limit = self.config.max_adverse_distance_bps
        if direction == "UP" and distance_bps < -limit:
            log.debug(
                "Skip UP: adverse distance %.2fbps < -%.2fbps (dist=$%.2f fair=%.3f)",
                distance_bps, limit, signal.get("distance", 0.0), signal["fair_prob_up"],
            )
            return False
        if direction == "DOWN" and distance_bps > limit:
            log.debug(
                "Skip DOWN: adverse distance %.2fbps > %.2fbps (dist=$%.2f fair=%.3f)",
                distance_bps, limit, signal.get("distance", 0.0), 1 - signal["fair_prob_up"],
            )
            return False
        return True

    def _place_trade(self, direction: str, fair_prob_up: float, market_odds: dict):
        """Place a trade on the current 5m market."""
        cfg = self.config
        window = self.current_window

        if direction == "UP":
            fair = fair_prob_up
            market_price = market_odds["up"]
            token_id = window.up_token
        else:
            fair = 1.0 - fair_prob_up
            market_price = market_odds["down"]
            token_id = window.down_token

        # Kelly sizing
        kelly = kelly_size(
            fair_prob=fair,
            market_price=market_price,
            bankroll=cfg.bankroll,
            kelly_fraction=cfg.kelly_fraction,
            max_bet_pct=cfg.max_bet_pct,
            min_edge=cfg.min_edge,
        )

        if kelly.direction == "NONE" or kelly.size_usdc < cfg.min_bet_usdc:
            return

        # Convert to shares
        if market_price <= 0:
            return
        size_shares = kelly.size_usdc / market_price

        book = self._get_book(token_id)
        if not book:
            return
        vwap_price, fillable = book.vwap_price("BUY", size_shares)
        if vwap_price is None or fillable < size_shares:
            log.debug(
                "Skip %s: insufficient VWAP depth size=%.1f fillable=%.1f",
                direction, size_shares, fillable,
            )
            return

        kelly = kelly_size(
            fair_prob=fair,
            market_price=vwap_price,
            bankroll=cfg.bankroll,
            kelly_fraction=cfg.kelly_fraction,
            max_bet_pct=cfg.max_bet_pct,
            min_edge=cfg.min_edge,
        )
        if kelly.direction == "NONE" or kelly.size_usdc < cfg.min_bet_usdc:
            log.debug(
                "Skip %s after VWAP check: fair=%.3f vwap=%.4f edge=%.4f",
                direction, fair, vwap_price, fair - vwap_price,
            )
            return
        size_shares = kelly.size_usdc / vwap_price

        order_id = self.ems.place_order(
            token_id=token_id,
            side="BUY",
            price=vwap_price,
            size=size_shares,
            tick_size=window.tick_size,
            neg_risk=window.neg_risk,
            order_type="FAK",
            source=f"btc5m_{direction.lower()}",
        )

        if order_id:
            self.current_position = direction
            self.current_order_id = order_id
            self.total_trades += 1
            log.info(
                "BTC5M TRADE: %s %.1f shares @ vwap %.4f | kelly=$%.0f edge=%.4f "
                "btc=$%.0f momentum=%.4f%%",
                direction, size_shares, vwap_price,
                kelly.size_usdc, kelly.edge,
                self.btc.current or 0,
                self.btc.momentum() * 100,
            )

    # ---- Main Loop ----

    def run(self):
        """Main loop: poll BTC price, discover markets, trade, roll."""
        self.running = True
        cfg = self.config

        log.info("=" * 60)
        log.info("BTC 5-Minute Strategy started")
        log.info("  Bankroll: $%.0f", cfg.bankroll)
        log.info("  Kelly fraction: %.0f%%", cfg.kelly_fraction * 100)
        log.info("  Min edge: %.0f%%", cfg.min_edge * 100)
        log.info("  Entry deadline: %.0fs before expiry", cfg.entry_deadline_seconds)
        log.info("  Dry run: %s", self.ems.dry_run)
        log.info("=" * 60)

        # Build initial BTC price history
        log.info("Building BTC price history...")
        for _ in range(10):
            self.btc.fetch()
            time.sleep(0.5)
        log.info("BTC price: $%.2f", self.btc.current)

        try:
            while self.running:
                # Risk checks
                if self.consecutive_losses >= cfg.max_consecutive_losses:
                    log.warning("Max consecutive losses (%d) reached, stopping",
                                cfg.max_consecutive_losses)
                    break
                if self.daily_pnl < -cfg.daily_loss_limit:
                    log.warning("Daily loss limit ($%.0f) reached, stopping",
                                cfg.daily_loss_limit)
                    break

                # Poll BTC price
                btc_price = self.btc.fetch()

                # Discover or roll to current market window
                window_ts = self._get_current_window_ts()
                valid_slugs = {
                    f"btc-updown-5m-{window_ts}",
                    f"btc-updown-5m-{window_ts + 300}",
                }
                if self.current_window is None or self.current_window.slug not in valid_slugs:
                    self._roll_to_new_window(window_ts)

                if not self.current_window:
                    now = time.time()
                    if now - self._last_wait_log >= cfg.skip_log_interval:
                        log.debug("No active market window, waiting...")
                        self._last_wait_log = now
                    time.sleep(cfg.price_poll_interval)
                    continue

                now_dt = datetime.now(timezone.utc)
                if now_dt < self.current_window.start_time:
                    starts_in = (self.current_window.start_time - now_dt).total_seconds()
                    now = time.time()
                    if now - self._last_wait_log >= cfg.skip_log_interval:
                        log.debug(
                            "Next BTC5M window %s starts in %.1fs",
                            self.current_window.slug,
                            starts_in,
                        )
                        self._last_wait_log = now
                    time.sleep(min(max(starts_in, cfg.price_poll_interval), 5.0))
                    continue

                remaining = self._seconds_remaining()
                elapsed = self._seconds_elapsed()

                # Market expired — record result and wait for next
                if remaining <= 0:
                    self._record_result()
                    self.current_window = None
                    self.current_position = None
                    time.sleep(2)
                    continue

                # Already have a position — just wait
                if self.current_position is not None:
                    holding_bucket = int(remaining // 30)
                    if holding_bucket != self._last_holding_bucket:
                        log.info(
                            "BTC5M HOLDING %s | btc=$%.2f remaining=%.0fs",
                            self.current_position, btc_price, remaining,
                        )
                        self._last_holding_bucket = holding_bucket
                    time.sleep(cfg.price_poll_interval)
                    continue

                if elapsed < cfg.min_entry_age_seconds:
                    now = time.time()
                    if now - self._last_wait_log >= cfg.skip_log_interval:
                        log.debug(
                            "Window age %.1fs < min entry age %.1fs, waiting",
                            elapsed, cfg.min_entry_age_seconds,
                        )
                        self._last_wait_log = now
                    time.sleep(min(max(cfg.min_entry_age_seconds - elapsed, cfg.price_poll_interval), 2.0))
                    continue

                # Too late to enter
                if remaining < cfg.entry_deadline_seconds:
                    now = time.time()
                    if now - self._last_skip_log >= cfg.skip_log_interval:
                        log.debug(
                            "Only %.0fs left (< deadline %.0fs), skipping this window",
                            remaining,
                            cfg.entry_deadline_seconds,
                        )
                        self._last_skip_log = now
                    time.sleep(min(max(remaining, cfg.price_poll_interval), 5.0))
                    continue

                # Compute signal
                signal = self.compute_signal()

                # Get market odds
                odds = self._get_market_odds()
                if not odds:
                    time.sleep(cfg.price_poll_interval)
                    continue

                # High-frequency opportunity: try buying both legs when sum < 1.00
                try:
                    bought = self._try_buy_both(odds)
                except Exception as e:
                    log.debug("Error in _try_buy_both: %s", e)
                    bought = False
                if bought:
                    # successfully placed (or attempted) both-leg orders; skip single-leg flow
                    time.sleep(cfg.price_poll_interval)
                    continue

                # Check edge
                up_edge = signal["fair_prob_up"] - odds["up"]
                down_edge = (1 - signal["fair_prob_up"]) - odds["down"]
                if up_edge >= down_edge:
                    direction = "UP"
                    fair = signal["fair_prob_up"]
                    market_price = odds["up"]
                    edge = up_edge
                else:
                    direction = "DOWN"
                    fair = 1 - signal["fair_prob_up"]
                    market_price = odds["down"]
                    edge = down_edge

                if not self._direction_allowed(direction, signal):
                    time.sleep(cfg.price_poll_interval)
                    continue

                if edge < cfg.min_edge:
                    log.debug(
                        "Signal %s but edge %.4f < min %.4f (fair=%.3f mkt=%.3f z=%.2f dist=$%.2f)",
                        direction, edge, cfg.min_edge,
                        fair, market_price,
                        signal.get("z_score", 0.0), signal.get("distance", 0.0),
                    )
                    time.sleep(cfg.price_poll_interval)
                    continue

                # Place trade
                self._place_trade(direction, signal["fair_prob_up"], odds)

                time.sleep(cfg.price_poll_interval)

        except KeyboardInterrupt:
            log.info("Interrupted by user")
        finally:
            self._print_summary()

    def _roll_to_new_window(self, window_ts: int):
        """Transition to a new 5-minute window."""
        # Record result of previous window
        if self.current_window and self.current_position:
            self._record_result()

        self.current_position = None
        self.current_order_id = None
        self._last_holding_bucket = None

        window = self._discover_market(window_ts)
        if window:
            window.starting_btc_price = self.btc.current or 0
            self.current_window = window
            log.info(
                "NEW WINDOW: %s | btc=$%.2f | ends=%s",
                window.slug,
                window.starting_btc_price,
                window.end_time.strftime("%H:%M:%S"),
            )
        else:
            # Try next window (market might not be created yet)
            next_ts = window_ts + 300
            window = self._discover_market(next_ts)
            if window:
                window.starting_btc_price = self.btc.current or 0
                self.current_window = window
                log.info("NEW WINDOW (next): %s", window.slug)
            else:
                self.current_window = None
                log.debug("No market found for ts=%d or ts=%d", window_ts, next_ts)

    def _record_result(self):
        """Record win/loss after a window closes."""
        if not self.current_window or not self.current_position:
            return

        # Special handling for both-leg arbitrage entries
        if self.current_position == "BOTH" and self._both_entry:
            quoted_pair_shares = float(self._both_entry.get("pair_shares", 0))
            quoted_entry_sum = float(self._both_entry.get("entry_sum", 0))
            up_pos = self.oms.get_position(self.current_window.up_token)
            down_pos = self.oms.get_position(self.current_window.down_token)

            if not up_pos or not down_pos or up_pos.size <= 0 or down_pos.size <= 0:
                log.warning(
                    "BTC5M ARB RESULT: missing fill state | quoted_sum=%.4f quoted_pair_shares=%.4f",
                    quoted_entry_sum,
                    quoted_pair_shares,
                )
                self._both_entry = None
                return

            pair_shares = min(quoted_pair_shares, up_pos.size, down_pos.size)
            entry_sum = up_pos.avg_price + down_pos.avg_price

            # Profit per pair = 1.0 - actual filled (up_price + down_price).
            profit_per_pair = 1.0 - entry_sum
            profit_usdc = pair_shares * profit_per_pair

            # Update stats
            self.daily_pnl += profit_usdc
            if profit_usdc >= 0:
                self.wins += 1
                self.consecutive_losses = 0
                result = "WIN"
            else:
                self.losses += 1
                self.consecutive_losses += 1
                result = "LOSS"

            log.info(
                "BTC5M ARB RESULT: %s | entry_sum=%.4f quoted_sum=%.4f "
                "pair_shares=%.4f profit=$%.2f",
                result, entry_sum, quoted_entry_sum, pair_shares, profit_usdc,
            )

            # clear both entry
            self._both_entry = None
            return

        # Default single-leg result handling (prediction-based)
        start_price = self.current_window.starting_btc_price
        end_price = self.btc.current or start_price

        btc_went_up = end_price >= start_price
        predicted_up = self.current_position == "UP"
        won = btc_went_up == predicted_up

        profit_usdc = 0.0
        pos = self.oms.get_position(
            self.current_window.up_token if self.current_position == "UP" else self.current_window.down_token
        )
        if pos and pos.size > 0:
            if self.current_position == "UP":
                payout = 1.0 if btc_went_up else 0.0
            else:
                payout = 1.0 if not btc_went_up else 0.0
            profit_usdc = pos.size * (payout - pos.avg_price)
            self.daily_pnl += profit_usdc
            # Emit a synthetic settlement fill so OMS/performance close the position.
            self.ems._fire_fill(Fill(
                token_id=pos.token_id,
                side="SELL",
                size=pos.size,
                price=payout,
                timestamp=time.time(),
                order_id=f"settlement_{int(time.time())}",
                source="settlement",
            ))

        if won:
            self.wins += 1
            self.consecutive_losses = 0
            result = "WIN"
        else:
            self.losses += 1
            self.consecutive_losses += 1
            result = "LOSS"

        if pos and pos.size > 0:
            log.info(
                "BTC5M RESULT: %s | predicted=%s actual=%s | "
                "btc_start=$%.2f btc_end=$%.2f delta=$%.2f | "
                "profit=$%.2f | record=%d-%d (%.0f%%)",
                result, self.current_position,
                "UP" if btc_went_up else "DOWN",
                start_price, end_price, end_price - start_price,
                profit_usdc,
                self.wins, self.losses,
                self.wins / max(self.total_trades, 1) * 100,
            )
        else:
            log.info(
                "BTC5M RESULT: %s | predicted=%s actual=%s | "
                "btc_start=$%.2f btc_end=$%.2f delta=$%.2f | "
                "record=%d-%d (%.0f%%)",
                result, self.current_position,
                "UP" if btc_went_up else "DOWN",
                start_price, end_price, end_price - start_price,
                self.wins, self.losses,
                self.wins / max(self.total_trades, 1) * 100,
            )

    def _print_summary(self):
        log.info("=" * 60)
        log.info("BTC 5-Minute Strategy Summary")
        log.info("  Total trades: %d", self.total_trades)
        log.info("  Wins: %d  Losses: %d", self.wins, self.losses)
        log.info("  Win rate: %.1f%%", self.wins / max(self.total_trades, 1) * 100)
        log.info("  Daily PnL: $%.2f", self.daily_pnl)
        log.info("=" * 60)


    # ---- Post-Facto Support ----

    def snapshot(self) -> dict:
        """Return strategy state snapshot for post-session analysis."""
        return {
            "running": self.running,
            "current_window": self.current_window.slug if self.current_window else "none",
            "current_position": self.current_position or "none",
            "btc_price": self.btc.current or 0,
            "btc_volatility": self.btc.volatility(),
            "btc_momentum": self.btc.momentum(),
            "btc_trend": self.btc.trend_strength(),
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "consecutive_losses": self.consecutive_losses,
            "daily_pnl": self.daily_pnl,
        }

    def status(self) -> dict:
        """Alias for snapshot — used by SnapshotCollector fallback."""
        return self.snapshot()
