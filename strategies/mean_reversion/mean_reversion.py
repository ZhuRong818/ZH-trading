"""
Mean Reversion Strategy

Core idea: In prediction markets, prices in the "contested" zone (0.35-0.65)
tend to mean-revert because there's no strong consensus. When price deviates
from its recent average, trade against the deviation.

This works because:
1. Prediction market prices represent probabilities. True probabilities rarely
   jump 10% in minutes without news. Most short-term moves are noise/liquidity.
2. In contested markets, buyers and sellers are roughly balanced, creating
   natural oscillation around the mean.
3. We avoid tail zones (<0.15 or >0.85) where moves ARE directional —
   a market going from 0.90 to 0.95 is usually real information, not noise.

Entry signals:
    BUY  when price drops below (moving_avg - threshold)
    SELL when price rises above (moving_avg + threshold)

Exit:
    Close when price returns to moving average (mean reversion target).

Position sizing: Kelly criterion based on estimated edge.

Risk controls:
    - Only trade in contested regime (0.30-0.70)
    - Max 1 open position per market
    - Stop-loss at 2x the entry deviation (the mean reversion thesis is wrong)
    - Cooldown after stop-loss (don't re-enter immediately)
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

from config import RiskConfig
from data_pipeline.market_data import MarketDataFeed
from ems.execution import ExecutionEngine
from oms.position_manager import PositionManager
from strategies.kelly import kelly_size

log = logging.getLogger(__name__)


@dataclass
class MeanReversionConfig:
    # Signal parameters
    lookback_window: int = 30        # number of price observations for moving avg
    entry_threshold: float = 0.03    # deviation from mean to trigger entry
    exit_threshold: float = 0.005    # close-to-mean threshold to exit
    stop_loss_multiple: float = 2.0  # stop-loss at 2x entry deviation

    # Regime filter
    min_price: float = 0.30          # only trade above this price
    max_price: float = 0.70          # only trade below this price

    # Sizing
    kelly_fraction: float = 0.25     # quarter-Kelly
    max_bet_pct: float = 0.03        # max 3% of bankroll per trade
    min_edge: float = 0.02           # minimum edge to trade
    bankroll: float = 10_000.0       # total capital

    # Timing
    cooldown_seconds: float = 120.0  # wait after stop-loss before re-entry
    min_observations: int = 10       # need this many prices before trading


@dataclass
class MeanRevPosition:
    """Tracks an active mean reversion trade."""
    token_id: str
    side: str           # BUY or SELL
    entry_price: float
    entry_mean: float   # the moving average at time of entry
    size: float
    stop_price: float
    target_price: float  # the mean (reversion target)
    entry_time: float


class MeanReversionStrategy:
    """
    Trades mean reversion in contested prediction markets.
    """

    def __init__(
        self,
        config: MeanReversionConfig,
        data_feed: MarketDataFeed,
        ems: ExecutionEngine,
        oms: PositionManager,
        token_ids: list[str],
        tick_sizes: dict[str, str] = None,
        neg_risks: dict[str, bool] = None,
    ):
        self.config = config
        self.data = data_feed
        self.ems = ems
        self.oms = oms
        self.token_ids = token_ids
        self.tick_sizes = tick_sizes or {}
        self.neg_risks = neg_risks or {}

        # Active positions per token
        self._positions: dict[str, MeanRevPosition] = {}
        self._cooldown_until: dict[str, float] = {}

        # Track signals for analysis
        self.total_signals = 0
        self.total_trades = 0

    def step(self):
        """One iteration: check all tokens for mean reversion signals."""
        for token_id in self.token_ids:
            self._step_token(token_id)

    def _step_token(self, token_id: str):
        """Process one token."""
        # Fetch fresh book
        book = self.data.fetch_order_book(token_id)
        mid = book.mid
        if mid is None:
            return

        # Regime filter — only trade contested zone
        if mid < self.config.min_price or mid > self.config.max_price:
            # If we have a position, check if we should exit (price left regime)
            if token_id in self._positions:
                self._close_position(token_id, mid, reason="regime_exit")
            return

        # Check cooldown
        if time.time() < self._cooldown_until.get(token_id, 0):
            return

        # Need enough history
        prices = self.data._price_history.get(token_id, [])
        if len(prices) < self.config.min_observations:
            return

        # Compute moving average
        window = min(self.config.lookback_window, len(prices))
        moving_avg = float(np.mean(prices[-window:]))
        deviation = mid - moving_avg

        # If we have an open position, manage it
        if token_id in self._positions:
            self._manage_position(token_id, mid, moving_avg)
            return

        # Check for new entry signal
        self._check_entry(token_id, mid, moving_avg, deviation)

    def _check_entry(self, token_id: str, mid: float, moving_avg: float, deviation: float):
        """Check if we should enter a new mean reversion trade."""
        cfg = self.config
        threshold = cfg.entry_threshold

        if abs(deviation) < threshold:
            return  # not enough deviation

        self.total_signals += 1

        if deviation < -threshold:
            # Price dropped below mean — BUY (expect reversion up)
            direction = "BUY"
            fair_prob = moving_avg  # we think true value is the mean
            entry_price = mid
            stop_price = mid - abs(deviation) * cfg.stop_loss_multiple
            target_price = moving_avg
        elif deviation > threshold:
            # Price rose above mean — SELL (expect reversion down)
            direction = "SELL"
            fair_prob = moving_avg
            entry_price = mid
            stop_price = mid + abs(deviation) * cfg.stop_loss_multiple
            target_price = moving_avg
        else:
            return

        # Kelly sizing
        kelly = kelly_size(
            fair_prob=fair_prob,
            market_price=mid,
            bankroll=cfg.bankroll,
            kelly_fraction=cfg.kelly_fraction,
            max_bet_pct=cfg.max_bet_pct,
            min_edge=cfg.min_edge,
        )

        if kelly.direction == "NONE" or kelly.size_usdc < 1:
            return

        # Convert USDC to shares
        size_shares = kelly.size_usdc / mid if mid > 0 else 0
        if size_shares < 1:
            return

        # Place order
        tick = self.tick_sizes.get(token_id, "0.01")
        neg = self.neg_risks.get(token_id, False)

        order_id = self.ems.place_order(
            token_id=token_id,
            side=direction,
            price=entry_price,
            size=size_shares,
            tick_size=tick,
            neg_risk=neg,
            source="mean_rev",
        )

        if order_id:
            self._positions[token_id] = MeanRevPosition(
                token_id=token_id,
                side=direction,
                entry_price=entry_price,
                entry_mean=moving_avg,
                size=size_shares,
                stop_price=stop_price,
                target_price=target_price,
                entry_time=time.time(),
            )
            self.total_trades += 1
            log.info(
                "MEAN_REV ENTRY: %s %s %.1f @ %.4f | mean=%.4f dev=%.4f "
                "stop=%.4f target=%.4f kelly=$%.0f",
                direction, token_id[:16], size_shares, entry_price,
                moving_avg, deviation, stop_price, target_price, kelly.size_usdc,
            )

    def _manage_position(self, token_id: str, mid: float, moving_avg: float):
        """Manage an existing mean reversion position — check exit conditions."""
        pos = self._positions[token_id]
        cfg = self.config

        # Check stop-loss
        if pos.side == "BUY" and mid <= pos.stop_price:
            self._close_position(token_id, mid, reason="stop_loss")
            self._cooldown_until[token_id] = time.time() + cfg.cooldown_seconds
            return
        if pos.side == "SELL" and mid >= pos.stop_price:
            self._close_position(token_id, mid, reason="stop_loss")
            self._cooldown_until[token_id] = time.time() + cfg.cooldown_seconds
            return

        # Check mean reversion target (take profit)
        if pos.side == "BUY" and mid >= moving_avg - cfg.exit_threshold:
            self._close_position(token_id, mid, reason="target_hit")
            return
        if pos.side == "SELL" and mid <= moving_avg + cfg.exit_threshold:
            self._close_position(token_id, mid, reason="target_hit")
            return

    def _close_position(self, token_id: str, price: float, reason: str):
        """Close a mean reversion position."""
        pos = self._positions.pop(token_id, None)
        if not pos:
            return

        close_side = "SELL" if pos.side == "BUY" else "BUY"
        tick = self.tick_sizes.get(token_id, "0.01")
        neg = self.neg_risks.get(token_id, False)

        pnl = (price - pos.entry_price) * pos.size if pos.side == "BUY" else (pos.entry_price - price) * pos.size

        self.ems.place_order(
            token_id=token_id,
            side=close_side,
            price=price,
            size=pos.size,
            tick_size=tick,
            neg_risk=neg,
            source="mean_rev_exit",
        )

        log.info(
            "MEAN_REV EXIT [%s]: %s %.1f @ %.4f | entry=%.4f pnl=$%.2f held=%.0fs",
            reason, close_side, pos.size, price,
            pos.entry_price, pnl, time.time() - pos.entry_time,
        )

    def status(self) -> dict:
        return {
            "active_positions": len(self._positions),
            "total_signals": self.total_signals,
            "total_trades": self.total_trades,
            "positions": {
                tid: {
                    "side": p.side,
                    "entry": p.entry_price,
                    "stop": p.stop_price,
                    "target": p.target_price,
                }
                for tid, p in self._positions.items()
            },
        }
