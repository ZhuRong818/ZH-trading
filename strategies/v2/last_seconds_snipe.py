"""
Last Seconds Snipe (v2).

Targets 5m rolling markets in the final seconds when the BTC price is
clearly on one side of the strike and the market odds already reflect
near-certain resolution. This is a low-risk, low-margin strategy that
prioritizes speed and certainty over size.
"""

import logging
import time
from typing import List, Optional

from strategies.base import BaseStrategy
from strategies.v2.skills import BTCPriceFeed, PositionSizer
from data_pipeline.market_provider import MarketContext
from pipeline.signal import TradingSignal

log = logging.getLogger(__name__)


class LastSecondsSnipe(BaseStrategy):
    name = "btc5m_last_snipe"

    def __init__(
        self,
        asset: str = "btc",
        max_seconds_remaining: float = 30.0,
        min_seconds_remaining: float = 12.0,
        min_distance_usd: float = 25.0,
        min_distance_bps: float = 0.0,
        min_market_odds: float = 0.98,
        min_edge: float = 0.005,
        min_fair: float = 0.99,
        soft_max_seconds_remaining: float = 60.0,
        soft_min_distance_usd: float = 50.0,
        soft_min_distance_bps: float = 0.0,
        soft_min_market_odds: float = 0.90,
        soft_min_edge: float = 0.02,
        soft_min_fair: float = 0.95,
        kelly_frac: float = 0.10,
        max_bet_pct: float = 0.01,
        bankroll: float = 5_000.0,
        max_notional_usdc: float = 250.0,
        max_vwap_slippage: float = 0.01,
        cooldown: float = 5.0,
    ):
        self.feed = BTCPriceFeed(asset)
        self.sizer = PositionSizer(
            kelly_frac=kelly_frac,
            max_bet_pct=max_bet_pct,
            bankroll=bankroll,
            min_bet=5.0,
            min_edge=min_edge,
        )
        self.max_seconds_remaining = max_seconds_remaining
        self.min_seconds_remaining = min_seconds_remaining
        self.min_distance_usd = min_distance_usd
        self.min_distance_bps = min_distance_bps
        self.min_market_odds = min_market_odds
        self.min_edge = min_edge
        self.min_fair = min_fair
        self.soft_max_seconds_remaining = soft_max_seconds_remaining
        self.soft_min_distance_usd = soft_min_distance_usd
        self.soft_min_distance_bps = soft_min_distance_bps
        self.soft_min_market_odds = soft_min_market_odds
        self.soft_min_edge = soft_min_edge
        self.soft_min_fair = soft_min_fair
        self.max_notional_usdc = max_notional_usdc
        self.max_vwap_slippage = max_vwap_slippage
        self.cooldown = cooldown

        self._has_position = False
        self._last_signal_time = 0.0
        self._last_condition_id = ""
        self._signaled_window = False

        self.total_trades = 0
        self.total_signals = 0
        self.rejected = 0

    def on_fill(self, fill):
        if fill.side == "BUY":
            self._has_position = True
        elif fill.side == "SELL":
            self._has_position = False

    def on_cancel(self):
        self._has_position = False

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        self.feed.poll()

        if self._has_position:
            return []

        now = time.time()
        if now - self._last_signal_time < self.cooldown:
            return []

        up_ctx, down_ctx = self._resolve_contexts(contexts)
        if not up_ctx or not down_ctx:
            log.debug("SNIPE REJECT context: up=%s down=%s", bool(up_ctx), bool(down_ctx))
            return []

        condition_id = up_ctx.condition_id or ""
        if condition_id and condition_id != self._last_condition_id:
            self._last_condition_id = condition_id
            self._signaled_window = False

        remaining = up_ctx.seconds_remaining
        if remaining < self.min_seconds_remaining:
            log.debug("SNIPE REJECT time: remaining=%.1f min=%.1f", remaining, self.min_seconds_remaining)
            return []
        if remaining > self.soft_max_seconds_remaining:
            log.debug("SNIPE REJECT time: remaining=%.1f soft_max=%.1f", remaining, self.soft_max_seconds_remaining)
            return []
        if self._signaled_window:
            log.debug("SNIPE REJECT window: already_signaled cond=%s", condition_id)
            return []

        tier = "strict" if remaining <= self.max_seconds_remaining else "soft"
        min_distance = self.min_distance_usd if tier == "strict" else self.soft_min_distance_usd
        min_distance_bps = self.min_distance_bps if tier == "strict" else self.soft_min_distance_bps
        min_market_odds = self.min_market_odds if tier == "strict" else self.soft_min_market_odds
        min_edge = self.min_edge if tier == "strict" else self.soft_min_edge
        min_fair = self.min_fair if tier == "strict" else self.soft_min_fair

        current = up_ctx.external_price or self.feed.current
        strike = up_ctx.strike_price
        if current <= 0 or strike <= 0:
            log.debug("SNIPE REJECT price: current=%.2f strike=%.2f", current, strike)
            return []

        distance = abs(current - strike)
        distance_bps = distance / current * 10_000 if current > 0 else 0.0
        if distance < min_distance and distance_bps < min_distance_bps:
            log.debug("SNIPE REJECT distance: distance=%.2f distance_bps=%.2f min_usd=%.2f min_bps=%.2f", distance, distance_bps, min_distance, min_distance_bps)
            self.rejected += 1
            return []

        direction = "UP" if current >= strike else "DOWN"
        market_ctx = up_ctx if direction == "UP" else down_ctx
        opposite_ctx = down_ctx if direction == "UP" else up_ctx
        market_price = market_ctx.best_ask or 0.0
        opposite_price = opposite_ctx.best_ask or 0.0
        if market_price <= 0:
            log.debug(
                "SNIPE REJECT market_price: direction=%s market_price=%.4f up_ask=%.4f down_ask=%.4f current=%.2f price_to_beat=%.2f",
                direction, market_price, up_ctx.best_ask or 0.0, down_ctx.best_ask or 0.0, current, strike,
            )
            return []
        if opposite_price >= min_market_odds and market_price < min_market_odds:
            log.debug(
                "SNIPE REJECT direction_mismatch: predicted=%s target_ask=%.4f opposite_ask=%.4f current=%.2f price_to_beat=%.2f dist=%.2f",
                direction, market_price, opposite_price, current, strike, distance,
            )
            self.rejected += 1
            return []
        if market_price < min_market_odds:
            log.debug(
                "SNIPE REJECT market_odds: direction=%s market_price=%.4f min=%.4f up_ask=%.4f down_ask=%.4f current=%.2f price_to_beat=%.2f dist=%.2f",
                direction, market_price, min_market_odds, up_ctx.best_ask or 0.0, down_ctx.best_ask or 0.0, current, strike, distance,
            )
            self.rejected += 1
            return []

        fair = max(min_fair, min(0.999, market_price + min_edge))
        edge = fair - market_price
        if edge < min_edge:
            log.debug("SNIPE REJECT edge: edge=%.6f min=%.6f fair=%.6f mkt=%.6f", edge, min_edge, fair, market_price)
            self.rejected += 1
            return []

        size = self.sizer.size(fair, market_price)
        if size is None:
            log.debug("SNIPE REJECT sizing: size=None fair=%.6f market=%.6f", fair, market_price)
            self.rejected += 1
            return []

        notional = size * market_price
        if notional > self.max_notional_usdc and market_price > 0:
            size = self.max_notional_usdc / market_price

        if size < 1:
            log.debug("SNIPE REJECT size: size=%.3f", size)
            self.rejected += 1
            return []

        vwap = market_price
        book = market_ctx.book
        if book:
            vwap_price, fillable = book.vwap_price("BUY", size)
            if vwap_price is None or fillable < 1:
                log.debug("SNIPE REJECT depth: vwap=%s fillable=%.1f", str(vwap_price), fillable)
                self.rejected += 1
                return []
            if fillable < size:
                log.debug("SNIPE ADJUST size: from=%.3f to=%.3f", size, fillable)
                size = fillable
            if vwap_price - market_price > self.max_vwap_slippage:
                log.debug("SNIPE REJECT slippage: vwap=%.6f mkt=%.6f diff=%.6f max=%.6f", vwap_price, market_price, vwap_price - market_price, self.max_vwap_slippage)
                self.rejected += 1
                return []
            vwap = vwap_price

        edge = fair - vwap
        if edge < min_edge:
            log.debug("SNIPE REJECT edge_vwap: edge=%.6f min=%.6f fair=%.6f vwap=%.6f", edge, min_edge, fair, vwap)
            self.rejected += 1
            return []

        self.total_signals += 1
        self.total_trades += 1
        self._last_signal_time = now
        self._signaled_window = True

        log.info(
            "SNIPE[%s]: %s %.1f @ %.4f fair=%.4f edge=%.4f dist=$%.2f current=$%.2f price_to_beat=$%.2f rem=%.1fs",
            tier, direction, size, vwap, fair, edge, distance, current, strike, remaining,
        )

        return [TradingSignal(
            token_id=market_ctx.token_id,
            side="BUY",
            price=vwap,
            size=size,
            strategy=self.name,
            edge=edge,
            fair_value=fair,
            confidence=min(edge / max(min_edge, 1e-6), 1.0),
            direction=direction,
            tick_size=market_ctx.tick_size,
            order_type="FAK",
        )]

    def _resolve_contexts(self, contexts: List[MarketContext]) -> tuple[Optional[MarketContext], Optional[MarketContext]]:
        ctx_map = {c.token_id: c for c in contexts if c and c.is_valid}
        up_ctx = down_ctx = None

        for ctx in ctx_map.values():
            q = (ctx.question or "").upper()
            if q.endswith(" UP"):
                up_ctx = ctx
            elif q.endswith(" DOWN"):
                down_ctx = ctx

        if not up_ctx or not down_ctx:
            log.debug("SNIPE REJECT context: up=%s down=%s", bool(up_ctx), bool(down_ctx))
            for ctx in ctx_map.values():
                other = ctx_map.get(ctx.token_id_other)
                if other:
                    up_ctx = up_ctx or ctx
                    down_ctx = down_ctx or other
                    if up_ctx and down_ctx:
                        break

        return up_ctx, down_ctx

    def snapshot(self) -> dict:
        return {
            "btc_price": self.feed.current,
            "total_trades": self.total_trades,
            "total_signals": self.total_signals,
            "rejected": self.rejected,
            "has_position": self._has_position,
        }
