"""
Resolution Fade — unified interface (v2).

Input:  List[MarketContext]
Output: List[TradingSignal] (time decay trades near resolution)
"""

import logging
import time
from typing import Dict, List, Optional
from dataclasses import dataclass

from strategies.base import BaseStrategy
from strategies.kelly import kelly_size
from data_pipeline.market_provider import MarketContext
from pipeline.signal import TradingSignal

log = logging.getLogger(__name__)


@dataclass
class FadePos:
    token_id: str
    sub: str  # certainty / convergence
    side: str
    entry_price: float
    size: float
    entry_time: float


class ResolutionFade(BaseStrategy):
    name = "fade"

    def __init__(
        self,
        certainty_min: float = 0.82,
        certainty_max: float = 0.97,
        certainty_min_days: float = 3.0,
        certainty_premium: float = 0.03,
        convergence_max_days: float = 3.0,
        convergence_min_price: float = 0.90,
        convergence_boost: float = 0.05,
        last_min_hours: float = 24.0,
        last_min_spread: float = 0.04,
        last_min_size: float = 10.0,
        kelly_frac: float = 0.15,
        max_bet_pct: float = 0.02,
        bankroll: float = 10_000,
        max_positions: int = 5,
    ):
        self.certainty_min = certainty_min
        self.certainty_max = certainty_max
        self.certainty_min_days = certainty_min_days
        self.certainty_premium = certainty_premium
        self.convergence_max_days = convergence_max_days
        self.convergence_min_price = convergence_min_price
        self.convergence_boost = convergence_boost
        self.last_min_hours = last_min_hours
        self.last_min_spread = last_min_spread
        self.last_min_size = last_min_size
        self.kelly_frac = kelly_frac
        self.max_bet_pct = max_bet_pct
        self.bankroll = bankroll
        self.max_positions = max_positions

        self._positions: Dict[str, FadePos] = {}
        self.total_trades = 0

    def on_fill(self, fill):
        if fill.token_id in self._positions and fill.side != self._positions[fill.token_id].side:
            del self._positions[fill.token_id]

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        signals = []
        for ctx in contexts:
            if not ctx.is_valid:
                continue
            s = self._process(ctx)
            signals.extend(s)
        return signals

    def _process(self, ctx: MarketContext) -> List[TradingSignal]:
        mid = ctx.mid_price
        tid = ctx.token_id
        days_left = ctx.seconds_remaining / 86400

        # Manage existing
        if tid in self._positions:
            return self._manage(tid, mid)

        if len(self._positions) >= self.max_positions:
            return []

        signals = []

        # Certainty fade
        s = self._certainty(ctx, mid, days_left)
        if s:
            signals.append(s)

        # Convergence
        if not signals:
            s = self._convergence(ctx, mid, days_left)
            if s:
                signals.append(s)

        # Last-minute liquidity
        hours_left = days_left * 24
        if not signals and 1 < hours_left < self.last_min_hours and 0.10 < mid < 0.90:
            half = self.last_min_spread / 2
            signals.append(TradingSignal(
                token_id=tid, side="BUY", price=mid - half,
                size=self.last_min_size, strategy=f"{self.name}_lastmin",
                tick_size=ctx.tick_size,
            ))
            signals.append(TradingSignal(
                token_id=tid, side="SELL", price=mid + half,
                size=self.last_min_size, strategy=f"{self.name}_lastmin",
                tick_size=ctx.tick_size,
            ))

        return signals

    def _certainty(self, ctx, mid, days_left) -> Optional[TradingSignal]:
        if days_left < self.certainty_min_days:
            return None

        if self.certainty_min <= mid <= self.certainty_max:
            fair = min(mid + self.certainty_premium, 0.99)
            return self._enter(ctx, mid, fair, "BUY", "certainty")
        elif (1 - self.certainty_max) <= mid <= (1 - self.certainty_min):
            fair = max(mid - self.certainty_premium, 0.01)
            return self._enter(ctx, mid, fair, "SELL", "certainty")
        return None

    def _convergence(self, ctx, mid, days_left) -> Optional[TradingSignal]:
        if days_left > self.convergence_max_days:
            return None

        if mid >= self.convergence_min_price:
            fair = min(mid + self.convergence_boost, 0.99)
            return self._enter(ctx, mid, fair, "BUY", "convergence")
        elif mid <= (1 - self.convergence_min_price):
            fair = max(mid - self.convergence_boost, 0.01)
            return self._enter(ctx, mid, fair, "SELL", "convergence")
        return None

    def _enter(self, ctx, mid, fair, side, sub) -> Optional[TradingSignal]:
        tid = ctx.token_id
        if tid in self._positions:
            return None

        kelly = kelly_size(
            fair_prob=fair if side == "BUY" else 1 - fair,
            market_price=mid if side == "BUY" else 1 - mid,
            bankroll=self.bankroll,
            kelly_fraction=self.kelly_frac,
            max_bet_pct=self.max_bet_pct,
        )
        if kelly.direction == "NONE" or kelly.size_usdc < 1:
            return None

        size = kelly.size_usdc / mid if mid > 0 else 0
        if size < 1:
            return None

        self._positions[tid] = FadePos(
            token_id=tid, sub=sub, side=side,
            entry_price=mid, size=size, entry_time=time.time(),
        )
        self.total_trades += 1

        log.info("FADE ENTRY [%s]: %s %s %.1f @ %.4f fair=%.4f",
                 sub, side, tid[:12], size, mid, fair)

        return TradingSignal(
            token_id=tid, side=side, price=mid, size=size,
            strategy=f"{self.name}_{sub}",
            edge=abs(fair - mid), tick_size=ctx.tick_size,
        )

    def _manage(self, tid, mid) -> List[TradingSignal]:
        pos = self._positions[tid]

        if pos.sub == "certainty":
            if pos.side == "BUY" and mid < pos.entry_price - 0.05:
                return [self._exit(tid, mid, "stop_loss")]
            if pos.side == "BUY" and mid > pos.entry_price + self.certainty_premium:
                return [self._exit(tid, mid, "take_profit")]
            if pos.side == "SELL" and mid > pos.entry_price + 0.05:
                return [self._exit(tid, mid, "stop_loss")]
            if pos.side == "SELL" and mid < pos.entry_price - self.certainty_premium:
                return [self._exit(tid, mid, "take_profit")]

        return []

    def _exit(self, tid, price, reason) -> TradingSignal:
        pos = self._positions[tid]
        close = "SELL" if pos.side == "BUY" else "BUY"
        log.info("FADE EXIT [%s/%s]: %s %.1f @ %.4f", pos.sub, reason, close, pos.size, price)
        return TradingSignal(
            token_id=tid, side=close, price=price, size=pos.size,
            strategy=f"{self.name}_{pos.sub}_exit",
        )

    def snapshot(self) -> dict:
        return {"positions": len(self._positions), "total_trades": self.total_trades}
