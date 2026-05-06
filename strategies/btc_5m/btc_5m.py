"""
BTC Up or Down 5-Minute Strategy

Trades Polymarket's rolling 5-minute BTC binary options.
Every 5 minutes a new market opens: "Will BTC be above the starting price
at the end of this 5-minute window?"

How it works:
    1. Auto-discovers the current 5m market via timestamp-based slug
    2. Fetches real-time BTC price from Binance (proxy for Chainlink oracle)
    3. Computes momentum signal from recent BTC price action
    4. Compares signal vs Polymarket odds to find edge
    5. Trades via Kelly sizing if edge > threshold
    6. Rolls to the next market when the current one resolves

Resolution source: Chainlink BTC/USD data stream
We use Binance as a proxy (Chainlink tracks major exchange prices).

Usage:
    python btc5m.py --dry-run
    python btc5m.py --dry-run --verbose
"""

import json
import logging
import math
import os
import signal
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
from ems.execution import ExecutionEngine, ClobAuth
from oms.position_manager import PositionManager, Fill
from strategies.kelly import kelly_size

log = logging.getLogger(__name__)

BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


@dataclass
class BTC5mConfig:
    # Signal
    momentum_window: int = 20        # BTC price ticks to compute momentum
    momentum_threshold: float = 0.0002  # min BTC % move to generate signal
    price_poll_interval: float = 2.0  # seconds between BTC price polls

    # Edge
    min_edge: float = 0.03           # min difference vs market odds to trade
    entry_deadline_seconds: float = 180  # don't enter with < 2 min left

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
    """Real-time BTC price from Binance."""

    def __init__(self):
        self.session = requests.Session()
        self.prices: deque = deque(maxlen=200)
        self._last_fetch = 0.0

    def fetch(self) -> float:
        resp = self.session.get(BINANCE_TICKER, params={"symbol": "BTCUSDT"})
        resp.raise_for_status()
        price = float(resp.json()["price"])
        self.prices.append(price)
        self._last_fetch = time.time()
        return price

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

    def __init__(self, config: BTC5mConfig, ems: ExecutionEngine, oms: PositionManager):
        self.config = config
        self.ems = ems
        self.oms = oms
        self.btc = BTCPriceFeed()
        self.session = requests.Session()

        # State
        self.current_window: Optional[MarketWindow] = None
        self.current_position: Optional[str] = None  # "UP" or "DOWN" or None
        self.current_order_id: Optional[str] = None

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
        slug = f"btc-updown-5m-{window_ts}"
        try:
            resp = self.session.get(f"{GAMMA_BASE}/events/slug/{slug}")
            if resp.status_code != 200:
                return None
            event = resp.json()
            markets = event.get("markets", [])
            if not markets:
                return None

            m = markets[0]
            clob_raw = m.get("clobTokenIds", "[]")
            clob = json.loads(clob_raw) if isinstance(clob_raw, str) else (clob_raw or [])
            if len(clob) < 2:
                return None

            start_str = event.get("startDate", m.get("startDate", ""))
            end_str = m.get("endDate", "")

            start_time = datetime.fromisoformat(start_str.replace("Z", "+00:00")) if start_str else datetime.now(timezone.utc)
            end_time = datetime.fromisoformat(end_str.replace("Z", "+00:00")) if end_str else start_time

            return MarketWindow(
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
        except Exception as e:
            log.warning("Failed to discover market %s: %s", slug, e)
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

    # ---- Market Data ----

    def _get_market_odds(self) -> Optional[dict]:
        """Get current Up/Down odds from the CLOB."""
        if not self.current_window:
            return None
        try:
            # Get midpoint for Up token
            resp = self.session.get(
                f"{CLOB_BASE}/midpoint",
                params={"token_id": self.current_window.up_token},
            )
            resp.raise_for_status()
            up_mid = float(resp.json().get("mid", 0.5))
            return {"up": up_mid, "down": 1.0 - up_mid}
        except Exception as e:
            log.warning("Failed to get odds: %s", e)
            return None

    # ---- Signal ----

    def compute_signal(self) -> dict:
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

        # Base probability: 50% + momentum adjustment
        # Strong momentum in one direction -> higher probability
        # Scale: 1% BTC move in 5 minutes is a very strong signal
        momentum_factor = momentum / 0.005  # normalize so 0.5% move = factor of 1
        momentum_factor = max(-1.0, min(1.0, momentum_factor))

        # Trend adds conviction
        trend_factor = trend * 0.3  # trend contributes up to 30% of signal

        # Combined signal
        signal_strength = momentum_factor * 0.7 + trend_factor * 0.3
        fair_prob_up = 0.5 + signal_strength * 0.15  # max shift: 50% ± 15%
        fair_prob_up = max(0.20, min(0.80, fair_prob_up))  # clamp

        # Confidence based on how clear the signal is
        confidence = min(abs(signal_strength), 1.0)

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

    # ---- Trading ----

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

        order_id = self.ems.place_order(
            token_id=token_id,
            side="BUY",
            price=market_price,
            size=size_shares,
            tick_size=window.tick_size,
            neg_risk=window.neg_risk,
            source=f"btc5m_{direction.lower()}",
        )

        if order_id:
            self.current_position = direction
            self.current_order_id = order_id
            self.total_trades += 1
            log.info(
                "BTC5M TRADE: %s %.1f shares @ %.4f | kelly=$%.0f edge=%.4f "
                "btc=$%.0f momentum=%.4f%%",
                direction, size_shares, market_price,
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
                if (self.current_window is None
                        or self.current_window.slug != f"btc-updown-5m-{window_ts}"):
                    self._roll_to_new_window(window_ts)

                if not self.current_window:
                    log.debug("No active market window, waiting...")
                    time.sleep(cfg.price_poll_interval)
                    continue

                remaining = self._seconds_remaining()

                # Market expired — record result and wait for next
                if remaining <= 0:
                    self._record_result()
                    self.current_window = None
                    self.current_position = None
                    time.sleep(2)
                    continue

                # Already have a position — just wait
                if self.current_position is not None:
                    if int(remaining) % 30 == 0:
                        log.info(
                            "BTC5M HOLDING %s | btc=$%.2f remaining=%.0fs",
                            self.current_position, btc_price, remaining,
                        )
                    time.sleep(cfg.price_poll_interval)
                    continue

                # Too late to enter
                if remaining < cfg.entry_deadline_seconds:
                    log.debug("Only %.0fs left, skipping this window", remaining)
                    time.sleep(cfg.price_poll_interval)
                    continue

                # Compute signal
                signal = self.compute_signal()

                if signal["direction"] == "NONE":
                    time.sleep(cfg.price_poll_interval)
                    continue

                # Get market odds
                odds = self._get_market_odds()
                if not odds:
                    time.sleep(cfg.price_poll_interval)
                    continue

                # Check edge
                if signal["direction"] == "UP":
                    edge = signal["fair_prob_up"] - odds["up"]
                else:
                    edge = (1 - signal["fair_prob_up"]) - odds["down"]

                if edge < cfg.min_edge:
                    log.debug(
                        "Signal %s but edge %.4f < min %.4f (fair=%.3f mkt=%.3f)",
                        signal["direction"], edge, cfg.min_edge,
                        signal["fair_prob_up"], odds["up"],
                    )
                    time.sleep(cfg.price_poll_interval)
                    continue

                # Place trade
                self._place_trade(signal["direction"], signal["fair_prob_up"], odds)

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

        start_price = self.current_window.starting_btc_price
        end_price = self.btc.current or start_price

        btc_went_up = end_price >= start_price
        predicted_up = self.current_position == "UP"
        won = btc_went_up == predicted_up

        if won:
            self.wins += 1
            self.consecutive_losses = 0
            result = "WIN"
        else:
            self.losses += 1
            self.consecutive_losses += 1
            result = "LOSS"

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


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="BTC 5-Minute Trading Strategy")
    parser.add_argument("--dry-run", action="store_true", help="Paper trading mode")
    parser.add_argument("--bankroll", type=float, default=5000, help="Bankroll in USDC")
    parser.add_argument("--kelly", type=float, default=0.20, help="Kelly fraction (default: 0.20)")
    parser.add_argument("--min-edge", type=float, default=0.03, help="Min edge to trade (default: 0.03)")
    parser.add_argument("--deadline", type=float, default=180, help="Entry deadline seconds (default: 180)")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = BTC5mConfig(
        bankroll=args.bankroll,
        kelly_fraction=args.kelly,
        min_edge=args.min_edge,
        entry_deadline_seconds=args.deadline,
    )

    oms = PositionManager()
    ems = ExecutionEngine(dry_run=args.dry_run)
    ems.on_fill(lambda f: oms.record_fill(f))

    if not args.dry_run:
        private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
        if not private_key:
            print("Set POLYMARKET_PRIVATE_KEY env var for live trading")
            sys.exit(1)
        auth = ClobAuth(
            private_key=private_key,
            chain_id=137,
            sig_type=int(os.environ.get("POLYMARKET_SIG_TYPE", "1")),
            funder=os.environ.get("POLYMARKET_FUNDER", ""),
        )
        auth.derive_api_creds()
        ems = ExecutionEngine(auth=auth, dry_run=False)
        ems.on_fill(lambda f: oms.record_fill(f))

    strategy = BTC5mStrategy(config=config, ems=ems, oms=oms)
    signal.signal(signal.SIGINT, lambda *_: setattr(strategy, 'running', False))
    strategy.run()


if __name__ == "__main__":
    main()
