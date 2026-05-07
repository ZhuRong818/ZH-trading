"""
Mean Reversion — unified interface (v2).

Input:  List[MarketContext]
Output: List[TradingSignal] (buy dips, sell rips)
"""

import logging
import time
from typing import Dict, List, Optional
from dataclasses import dataclass

import numpy as np

from strategies.base import BaseStrategy
from strategies.kelly import kelly_size
from data_pipeline.market_provider import MarketContext
from pipeline.signal import TradingSignal

log = logging.getLogger(__name__)


@dataclass
class OpenPosition:
    token_id: str
    side: str
    entry_price: float
    entry_mean: float
    size: float
    stop_price: float
    target_price: float
    entry_time: float


class MeanReversion(BaseStrategy):
    name = "mean_rev"

    def __init__(
        self,
        lookback: int = 20,
        entry_threshold: float = 0.01,
        exit_threshold: float = 0.003,
        stop_multiple: float = 2.0,
        min_price: float = 0.20,
        max_price: float = 0.80,
        kelly_frac: float = 0.25,
        max_bet_pct: float = 0.03,
        bankroll: float = 10_000,
        cooldown: float = 120.0,
        min_obs: int = 10,
    ):
        self.lookback = lookback
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.stop_multiple = stop_multiple
        self.min_price = min_price
        self.max_price = max_price
        self.kelly_frac = kelly_frac
        self.max_bet_pct = max_bet_pct
        self.bankroll = bankroll
        self.cooldown = cooldown
        self.min_obs = min_obs

        self._positions: Dict[str, OpenPosition] = {}
        self._cooldown_until: Dict[str, float] = {}
        self._price_history: Dict[str, List[float]] = {}
        self.total_signals = 0
        self.total_trades = 0

    def on_fill(self, fill):
        pos = self._positions.get(fill.token_id)
        if pos and fill.side != pos.side:
            # Exit fill — remove position
            del self._positions[fill.token_id]

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        signals = []
        for ctx in contexts:
            if not ctx.is_valid:
                continue
            s = self._process(ctx)
            if s:
                signals.append(s)
        return signals

    def _process(self, ctx: MarketContext) -> Optional[TradingSignal]:
        mid = ctx.mid_price
        tid = ctx.token_id

        # Track price history
        if tid not in self._price_history:
            self._price_history[tid] = []
        self._price_history[tid].append(mid)
        if len(self._price_history[tid]) > 200:
            self._price_history[tid] = self._price_history[tid][-100:]

        # Regime filter
        if mid < self.min_price or mid > self.max_price:
            if tid in self._positions:
                return self._exit_signal(tid, mid, "regime_exit")
            return None

        # Cooldown
        if time.time() < self._cooldown_until.get(tid, 0):
            return None

        # Need enough history
        prices = self._price_history[tid]
        if len(prices) < self.min_obs:
            return None

        window = min(self.lookback, len(prices))
        moving_avg = float(np.mean(prices[-window:]))
        deviation = mid - moving_avg

        # Manage existing position
        if tid in self._positions:
            return self._manage(tid, mid, moving_avg)

        # Check entry
        if abs(deviation) < self.entry_threshold:
            return None

        self.total_signals += 1

        if deviation < -self.entry_threshold:
            direction = "BUY"
            fair = moving_avg
            stop = mid - abs(deviation) * self.stop_multiple
        elif deviation > self.entry_threshold:
            direction = "SELL"
            fair = moving_avg
            stop = mid + abs(deviation) * self.stop_multiple
        else:
            return None

        kelly = kelly_size(
            fair_prob=fair, market_price=mid,
            bankroll=self.bankroll,
            kelly_fraction=self.kelly_frac,
            max_bet_pct=self.max_bet_pct,
        )
        if kelly.direction == "NONE" or kelly.size_usdc < 1:
            return None

        size = kelly.size_usdc / mid if mid > 0 else 0
        if size < 1:
            return None

        self._positions[tid] = OpenPosition(
            token_id=tid, side=direction, entry_price=mid,
            entry_mean=moving_avg, size=size, stop_price=stop,
            target_price=moving_avg, entry_time=time.time(),
        )
        self.total_trades += 1

        log.info("MEANREV ENTRY: %s %s %.1f @ %.4f mean=%.4f dev=%.4f",
                 direction, tid[:12], size, mid, moving_avg, deviation)

        return TradingSignal(
            token_id=tid, side=direction, price=mid, size=size,
            strategy=self.name, edge=abs(deviation),
            tick_size=ctx.tick_size if hasattr(ctx, 'tick_size') else "0.01",
        )

    def _manage(self, tid: str, mid: float, moving_avg: float) -> Optional[TradingSignal]:
        pos = self._positions[tid]

        # Stop loss
        if pos.side == "BUY" and mid <= pos.stop_price:
            self._cooldown_until[tid] = time.time() + self.cooldown
            return self._exit_signal(tid, mid, "stop_loss")
        if pos.side == "SELL" and mid >= pos.stop_price:
            self._cooldown_until[tid] = time.time() + self.cooldown
            return self._exit_signal(tid, mid, "stop_loss")

        # Target hit
        if pos.side == "BUY" and mid >= moving_avg - self.exit_threshold:
            return self._exit_signal(tid, mid, "target_hit")
        if pos.side == "SELL" and mid <= moving_avg + self.exit_threshold:
            return self._exit_signal(tid, mid, "target_hit")

        return None

    def _exit_signal(self, tid: str, price: float, reason: str) -> TradingSignal:
        pos = self._positions[tid]
        close_side = "SELL" if pos.side == "BUY" else "BUY"
        log.info("MEANREV EXIT [%s]: %s %s %.1f @ %.4f", reason, close_side, tid[:12], pos.size, price)
        return TradingSignal(
            token_id=tid, side=close_side, price=price, size=pos.size,
            strategy=f"{self.name}_exit",
        )

    def snapshot(self) -> dict:
        return {
            "positions": len(self._positions),
            "total_signals": self.total_signals,
            "total_trades": self.total_trades,
        }
