"""
Stoikov Market Making — unified interface (v2).

Input:  List[MarketContext]
Output: List[TradingSignal] (bid + ask quotes at multiple levels)
"""

import math
import logging
from typing import List

from strategies.base import BaseStrategy
from data_pipeline.market_provider import MarketContext
from data_pipeline.market_data import MarketDataFeed
from pipeline.signal import TradingSignal
from config import MarketMakingConfig

log = logging.getLogger(__name__)


class StoikovMM(BaseStrategy):
    name = "stoikov_mm"

    def __init__(self, config: MarketMakingConfig, data_feed: MarketDataFeed):
        self.config = config
        self.data = data_feed
        self.inventory: dict = {}  # token_id -> size

    def on_fill(self, fill):
        tid = fill.token_id
        current = self.inventory.get(tid, 0.0)
        if fill.side == "BUY":
            self.inventory[tid] = current + fill.size
        else:
            self.inventory[tid] = current - fill.size

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        signals = []
        for ctx in contexts:
            if not ctx.is_valid:
                continue
            signals.extend(self._quote(ctx))
        return signals

    def _quote(self, ctx: MarketContext) -> List[TradingSignal]:
        mid = ctx.mid_price
        if mid <= 0 or mid >= 1:
            return []

        # Use adjusted midpoint if book spread is reasonable
        if ctx.spread and ctx.spread < 0.20 and ctx.book:
            filtered = [b for b in ctx.book.bids if b[1] >= 50]
            filtered_a = [a for a in ctx.book.asks if a[1] >= 50]
            if filtered and filtered_a:
                mid = (filtered[0][0] + filtered_a[0][0]) / 2

        cfg = self.config
        inv = self.inventory.get(ctx.token_id, 0.0)
        sigma = ctx.volatility
        T = max(ctx.seconds_remaining / 3600, 1.0)
        gamma = cfg.gamma

        # Dynamic risk aversion
        if abs(inv * mid) > 5000:
            gamma *= 1.5

        # Reservation price
        r = mid - (inv * gamma * sigma ** 2 * T)

        # Optimal spread
        raw_spread = gamma * sigma ** 2 * T + (2 / gamma) * math.log(1 + gamma / cfg.spread_k)

        # Regime adjustment
        regime = MarketDataFeed.classify_regime(mid)
        if regime == "tail":
            raw_spread *= 2.0
        elif regime == "contested":
            raw_spread *= 0.8

        half_spread = max(raw_spread / 2, float(ctx.tick_size))
        half_spread = min(half_spread, 0.05)  # cap at 5%

        bid = r - half_spread
        ask = r + half_spread

        signals = []
        for level in range(cfg.num_levels):
            offset = level * float(ctx.tick_size) * 2
            size = cfg.order_size * (1.0 - 0.2 * level)

            if inv + size <= 500:  # position limit
                signals.append(TradingSignal(
                    token_id=ctx.token_id,
                    side="BUY",
                    price=bid - offset,
                    size=size,
                    strategy=self.name,
                    tick_size=ctx.tick_size,
                    neg_risk=ctx.neg_risk,
                    edge=half_spread,
                ))

            if inv - size >= -500:
                signals.append(TradingSignal(
                    token_id=ctx.token_id,
                    side="SELL",
                    price=ask + offset,
                    size=size,
                    strategy=self.name,
                    tick_size=ctx.tick_size,
                    neg_risk=ctx.neg_risk,
                    edge=half_spread,
                ))

        if signals:
            log.info(
                "MM [%s]: mid=%.4f r=%.4f bid=%.4f ask=%.4f spread=%.4f inv=%.0f regime=%s",
                ctx.token_id[:12], mid, r, bid, ask, ask - bid, inv, regime,
            )

        return signals

    def snapshot(self) -> dict:
        return {"inventory": dict(self.inventory)}
