"""
BTC Momentum (v2 distilled) — uses shared skills.

Only contains the strategy-specific fair value model.
Everything else (price feed, edge calc, sizing, locking) is reusable.

Fair value model: z-score from distance-to-strike + momentum drift.
"""

import logging
import math
from typing import List, Optional

from strategies.base import BaseStrategy
from strategies.v2.skills import BTCPriceFeed, EdgeCalculator, RiskRewardGate, PositionSizer, EntryLock
from data_pipeline.market_provider import MarketContext
from pipeline.signal import TradingSignal

log = logging.getLogger(__name__)


class MomentumV2(BaseStrategy):
    name = "btc5m_momentum"

    def __init__(
        self,
        asset: str = "btc",
        min_edge: float = 0.03,
        move_window: int = 20,
        kelly_frac: float = 0.20,
        max_bet_pct: float = 0.05,
        bankroll: float = 5_000,
        max_price: float = 0.65,
        cooldown: float = 5.0,
    ):
        # Compose shared skills
        self.feed = BTCPriceFeed(asset)
        self.gate = RiskRewardGate(max_price=max_price)
        self.sizer = PositionSizer(kelly_frac=kelly_frac, max_bet_pct=max_bet_pct, bankroll=bankroll)
        self.lock = EntryLock(cooldown=cooldown)
        self.min_edge = min_edge
        self.move_window = move_window
        self.total_trades = 0

    def on_fill(self, fill):
        if fill.side == "BUY":
            self.lock.on_fill_buy()
        elif fill.side == "SELL":
            self.lock.on_fill_sell()

    def on_cancel(self):
        self.lock.on_cancel()

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        self.feed.poll()

        if not self.lock.can_trade():
            return []
        if len(self.feed.prices) < 5:
            return []

        up_ctx, down_ctx = self._resolve_contexts(contexts)
        if not up_ctx or not down_ctx:
            return []
        if up_ctx.seconds_remaining < 30:
            return []

        signal = self._compute(up_ctx, down_ctx)
        if signal:
            self.lock.on_signal()
            self.total_trades += 1
            return [signal]
        return []

    # ---- Strategy-specific: fair value from z-score ----

    def _compute(self, up_ctx: MarketContext, down_ctx: MarketContext) -> Optional[TradingSignal]:
        current = self.feed.current
        strike = up_ctx.strike_price
        remaining = up_ctx.seconds_remaining

        if current <= 0 or strike <= 0:
            return None

        vol = max(self.feed.volatility(self.move_window), 0.00002)
        momentum = self.feed.momentum(self.move_window)

        # Z-score: how far is BTC from strike in volatility units
        horizon_ticks = max(remaining / 0.5, 1.0)
        horizon_sigma = current * vol * math.sqrt(horizon_ticks)
        distance = current - strike
        z_score = distance / horizon_sigma if horizon_sigma > 0 else 0

        # Adjust for momentum drift
        mom_z = max(-2.0, min(2.0, momentum / vol if vol > 0 else 0))
        adjusted_z = z_score + 0.2 * mom_z

        # Convert to probability
        fair_prob_up = 0.5 * (1.0 + math.erf(adjusted_z / math.sqrt(2.0)))
        fair_prob_up = max(0.05, min(0.95, fair_prob_up))

        # Direction
        if fair_prob_up > 0.52:
            direction = "UP"
            fair = fair_prob_up
            market_price = up_ctx.best_ask or 0.0
            token_id = up_ctx.token_id
        elif fair_prob_up < 0.48:
            direction = "DOWN"
            fair = 1 - fair_prob_up
            market_price = down_ctx.best_ask or 0.0
            token_id = down_ctx.token_id
        else:
            return None

        if market_price <= 0:
            return None

        # Shared skills: gate → edge → size
        edge = EdgeCalculator.compute(fair, market_price)
        if not self.gate.passes(edge, market_price):
            return None
        if edge < self.min_edge:
            return None

        size = self.sizer.size(fair, market_price)
        if size is None:
            return None

        log.info("MOMENTUM: %s %.1f @ %.4f edge=%.4f z=%.2f mom=%.4f%% btc=$%.0f",
                 direction, size, market_price, edge, z_score, momentum * 100, current)

        return TradingSignal(
            token_id=token_id, side="BUY", price=market_price, size=size,
            strategy=self.name, edge=edge, fair_value=fair,
            confidence=min(abs(adjusted_z) / 2, 1.0),
            tick_size=up_ctx.tick_size,
        )

    def _resolve_contexts(self, contexts):
        ctx_map = {c.token_id: c for c in contexts if c and c.is_valid}
        up_ctx = down_ctx = None
        for ctx in ctx_map.values():
            q = (ctx.question or "").upper()
            if q.endswith(" UP"):
                up_ctx = ctx
            elif q.endswith(" DOWN"):
                down_ctx = ctx
        if not up_ctx or not down_ctx:
            for ctx in ctx_map.values():
                other = ctx_map.get(ctx.token_id_other)
                if other:
                    up_ctx = up_ctx or ctx
                    down_ctx = down_ctx or other
                    break
        return up_ctx, down_ctx

    def snapshot(self) -> dict:
        return {
            "btc_price": self.feed.current,
            "total_trades": self.total_trades,
            "has_position": self.lock.has_position,
        }
