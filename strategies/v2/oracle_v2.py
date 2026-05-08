"""
Oracle Front-Run (v2 distilled) — uses shared skills.

Only contains the strategy-specific fair value model.
Everything else (price feed, edge calc, sizing, locking) is reusable.

Fair value model: BTC moved sharply → compute how far Polymarket should
have adjusted → if it hasn't, the difference is our edge.
"""

import logging
from typing import List, Optional

from strategies.base import BaseStrategy
from strategies.v2.skills import BTCPriceFeed, EdgeCalculator, RiskRewardGate, PositionSizer, EntryLock
from data_pipeline.market_provider import MarketContext
from pipeline.signal import TradingSignal

log = logging.getLogger(__name__)


class OracleV2(BaseStrategy):
    name = "oracle_frontrun"

    def __init__(
        self,
        asset: str = "btc",
        move_threshold_bps: float = 4.0,
        staleness_threshold: float = 0.15,
        lookback_ticks: int = 5,
        kelly_frac: float = 0.25,
        max_bet_pct: float = 0.05,
        bankroll: float = 10_000,
        max_price: float = 0.60,
        cooldown: float = 10.0,
        min_remaining: float = 60.0,
    ):
        # Compose shared skills
        self.feed = BTCPriceFeed(asset)
        self.gate = RiskRewardGate(max_price=max_price)
        self.sizer = PositionSizer(kelly_frac=kelly_frac, max_bet_pct=max_bet_pct, bankroll=bankroll)
        self.lock = EntryLock(cooldown=cooldown)
        self.move_threshold_bps = move_threshold_bps
        self.staleness_threshold = staleness_threshold
        self.lookback_ticks = lookback_ticks
        self.min_remaining = min_remaining

        # Stats
        self.total_trades = 0
        self.signals_detected = 0
        self.signals_stale = 0
        self.signals_already_priced = 0

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
        if len(self.feed.prices) < self.lookback_ticks + 1:
            return []

        # Detect sharp BTC move
        move = self.feed.detect_move(
            lookback=self.lookback_ticks,
            threshold_bps=self.move_threshold_bps,
        )
        if move is None:
            return []

        self.signals_detected += 1

        # Resolve UP/DOWN contexts
        up_ctx, down_ctx = self._resolve_contexts(contexts)
        if not up_ctx or not down_ctx:
            return []
        if up_ctx.seconds_remaining < self.min_remaining:
            return []

        # Check staleness
        signal = self._check_staleness(up_ctx, down_ctx, move)
        if signal:
            self.lock.on_signal()
            self.total_trades += 1
            return [signal]
        return []

    # ---- Strategy-specific: fair value from BTC move + staleness ----

    def _check_staleness(self, up_ctx, down_ctx, move) -> Optional[TradingSignal]:
        """
        Core oracle logic: BTC moved → compute fair value → check if
        Polymarket is stale → if so, buy the underpriced side.
        """
        # Fair value from move magnitude
        prob_shift = min(move.bps / 50, 0.35)

        if move.direction == "UP":
            fair = 0.50 + prob_shift
            market_price = up_ctx.best_ask or 0.0
            token_id = up_ctx.token_id
        else:
            fair = 0.50 + prob_shift  # fair prob of DOWN
            market_price = down_ctx.best_ask or 0.0
            token_id = down_ctx.token_id

        if market_price <= 0:
            return None

        # Staleness = how far market is behind fair value
        staleness = fair - market_price

        if staleness < self.staleness_threshold:
            self.signals_already_priced += 1
            log.debug("ORACLE: %s move %.1fbps but stale=%.4f < threshold=%.4f",
                       move.direction, move.bps, staleness, self.staleness_threshold)
            return None

        self.signals_stale += 1

        # Shared skills: gate → edge → size
        edge = EdgeCalculator.compute(fair, market_price)
        if not self.gate.passes(edge, market_price):
            return None

        size = self.sizer.size(fair, market_price)
        if size is None:
            return None

        log.info("ORACLE FRONTRUN: %s %.1f @ %.4f | move=%.1fbps stale=%.4f fair=%.3f edge=%.4f",
                 move.direction, size, market_price, move.bps, staleness, fair, edge)

        return TradingSignal(
            token_id=token_id, side="BUY", price=market_price, size=size,
            strategy=self.name, edge=edge, fair_value=fair,
            confidence=min(staleness / 0.20, 1.0),
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
            "signals_detected": self.signals_detected,
            "signals_stale": self.signals_stale,
            "signals_already_priced": self.signals_already_priced,
            "stale_rate": f"{self.signals_stale / max(self.signals_detected, 1) * 100:.0f}%",
            "has_position": self.lock.has_position,
        }
