"""
Volatility convexity arbitrage for rolling 5-minute markets.

Uses short-window realized spot volatility as an IV proxy. When the market is
near the strike late in the window, digital option fair value becomes highly
convex. If Polymarket asks lag that convexity, buy the underpriced side.
"""

import logging
import math
import time
from collections import deque
from typing import Deque, List, Optional

from data_pipeline.market_provider import MarketContext
from pipeline.signal import TradingSignal
from strategies.base import BaseStrategy
from strategies.kelly import kelly_size

log = logging.getLogger(__name__)

CRYPTO_FEE_RATE = 0.07


class VolatilityConvexityArb(BaseStrategy):
    name = "vol_convexity"

    def __init__(
        self,
        asset: str = "btc",
        bankroll: float = 10_000.0,
        lookback_seconds: float = 30.0,
        min_range_bps: float = 8.0,
        max_distance_bps: float = 20.0,
        min_seconds: float = 20.0,
        max_seconds: float = 120.0,
        min_price: float = 0.20,
        max_price: float = 0.55,
        min_edge: float = 0.04,
        far_edge: float = 0.15,
        far_seconds: float = 120.0,
        near_seconds: float = 40.0,
        alt_min_edge: float = 0.08,
        depth_notional_mult: float = 3.0,
        max_spread: float = 0.08,
        max_notional_usdc: float = 150.0,
        max_bet_pct: float = 0.01,
        kelly_frac: float = 0.20,
        max_vwap_slippage: float = 0.015,
        cooldown_seconds: float = 10.0,
    ):
        self.asset = asset
        self.bankroll = bankroll
        self.lookback_seconds = lookback_seconds
        self.min_range_bps = min_range_bps
        self.max_distance_bps = max_distance_bps
        self.min_seconds = min_seconds
        self.max_seconds = max_seconds
        self.min_price = min_price
        self.max_price = max_price
        self.min_edge = min_edge
        self.far_edge = far_edge
        self.far_seconds = far_seconds
        self.near_seconds = near_seconds
        self.alt_min_edge = alt_min_edge
        self.depth_notional_mult = depth_notional_mult
        self.max_spread = max_spread
        self.max_notional_usdc = max_notional_usdc
        self.max_bet_pct = max_bet_pct
        self.kelly_frac = kelly_frac
        self.max_vwap_slippage = max_vwap_slippage
        self.cooldown_seconds = cooldown_seconds

        self._prices: Deque[tuple[float, float]] = deque(maxlen=500)
        self._has_position = False
        self._last_signal_time = 0.0
        self._last_condition_id = ""
        self._signaled_window = False

        self.total_signals = 0
        self.rejected = 0
        self.last_reject = ""
        self.last_range_bps = 0.0
        self.last_distance_bps = 0.0
        self.last_fair_up = 0.5

    def on_fill(self, fill):
        if fill.side == "BUY":
            self._has_position = True
        elif fill.side == "SELL":
            self._has_position = False

    def on_cancel(self):
        self._has_position = False

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        up_ctx, down_ctx = self._resolve_contexts(contexts)
        if not up_ctx or not down_ctx:
            return self._reject("context")

        condition_id = up_ctx.condition_id or ""
        if condition_id and condition_id != self._last_condition_id:
            self._last_condition_id = condition_id
            self._signaled_window = False

        spot = up_ctx.external_price or up_ctx.mid_price or 0.0
        strike = up_ctx.strike_price
        now = time.time()
        if spot > 0:
            self._prices.append((now, spot))
            self._trim_prices(now)

        if self._has_position:
            return []
        if self._signaled_window:
            return self._reject("window")
        if now - self._last_signal_time < self.cooldown_seconds:
            return self._reject("cooldown")

        remaining = up_ctx.seconds_remaining
        if remaining < self.min_seconds or remaining > self.max_seconds:
            return self._reject("time")
        if spot <= 0 or strike <= 0:
            return self._reject("price")

        distance_bps = abs(spot - strike) / spot * 10_000
        self.last_distance_bps = distance_bps
        if distance_bps > self.max_distance_bps:
            return self._reject("distance")

        if not self._is_atm(up_ctx, down_ctx):
            return self._reject("atm")

        range_bps, sigma = self._realized_vol(now)
        self.last_range_bps = range_bps
        if range_bps < self.min_range_bps or sigma <= 0:
            return self._reject("vol")

        fair_up = self._digital_fair_up(spot, strike, sigma, remaining)
        self.last_fair_up = fair_up
        candidates = [
            self._candidate("UP", up_ctx, fair_up),
            self._candidate("DOWN", down_ctx, 1.0 - fair_up),
        ]
        candidates = [c for c in candidates if c is not None]
        if not candidates:
            return self._reject("candidate")

        candidate = max(candidates, key=lambda c: c["net_edge"])
        required_edge = self._required_edge(remaining)
        if candidate["net_edge"] < required_edge:
            return self._reject("edge")

        signal = self._build_signal(candidate, range_bps, distance_bps, remaining, required_edge)
        if signal is None:
            return []

        self.total_signals += 1
        self._signaled_window = True
        self._last_signal_time = now
        log.info(
            "VOLCONV: %s %.1f @ %.4f fair=%.4f net_edge=%.4f range=%.1fbps dist=%.1fbps rem=%.1fs",
            signal.direction, signal.size, signal.price, signal.fair_value,
            signal.edge, range_bps, distance_bps, remaining,
        )
        return [signal]

    def snapshot(self) -> dict:
        return {
            "asset": self.asset,
            "signals": self.total_signals,
            "rejected": self.rejected,
            "last_reject": self.last_reject,
            "last_range_bps": self.last_range_bps,
            "last_distance_bps": self.last_distance_bps,
            "last_fair_up": self.last_fair_up,
            "required_edge": self._required_edge(0.0),
            "has_position": self._has_position,
        }

    def _reject(self, reason: str) -> list:
        self.rejected += 1
        self.last_reject = reason
        log.debug("VOLCONV REJECT %s", reason)
        return []

    def _resolve_contexts(self, contexts: List[MarketContext]) -> tuple[Optional[MarketContext], Optional[MarketContext]]:
        up_ctx = down_ctx = None
        for ctx in contexts:
            if not ctx or not ctx.is_valid:
                continue
            q = (ctx.question or "").upper()
            if q.endswith(" UP"):
                up_ctx = ctx
            elif q.endswith(" DOWN"):
                down_ctx = ctx
        if up_ctx and down_ctx and up_ctx.condition_id == down_ctx.condition_id:
            return up_ctx, down_ctx
        return None, None

    def _is_atm(self, up_ctx: MarketContext, down_ctx: MarketContext) -> bool:
        up_mid = up_ctx.mid_price or 0.0
        down_mid = down_ctx.mid_price or 0.0
        return 0.40 <= up_mid <= 0.60 or 0.40 <= down_mid <= 0.60

    def _trim_prices(self, now: float):
        cutoff = now - self.lookback_seconds
        while self._prices and self._prices[0][0] < cutoff:
            self._prices.popleft()

    def _realized_vol(self, now: float) -> tuple[float, float]:
        self._trim_prices(now)
        prices = list(self._prices)
        if len(prices) < 3:
            return 0.0, 0.0

        values = [p for _, p in prices if p > 0]
        if len(values) < 3:
            return 0.0, 0.0

        last = values[-1]
        range_bps = (max(values) - min(values)) / last * 10_000 if last > 0 else 0.0

        variance_sum = 0.0
        dt_sum = 0.0
        prev_t, prev_p = prices[0]
        for t, p in prices[1:]:
            dt = max(t - prev_t, 1e-6)
            if prev_p > 0 and p > 0:
                ret = math.log(p / prev_p)
                variance_sum += ret * ret
                dt_sum += dt
            prev_t, prev_p = t, p

        sigma_per_sqrt_sec = math.sqrt(variance_sum / dt_sum) if dt_sum > 0 else 0.0
        return range_bps, sigma_per_sqrt_sec

    @staticmethod
    def _digital_fair_up(spot: float, strike: float, sigma_per_sqrt_sec: float, seconds_remaining: float) -> float:
        denom = spot * sigma_per_sqrt_sec * math.sqrt(max(seconds_remaining, 1e-6))
        if denom <= 0:
            return 0.5
        z = (spot - strike) / denom
        fair = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        return max(0.001, min(0.999, fair))

    def _candidate(self, direction: str, ctx: MarketContext, fair: float) -> Optional[dict]:
        ask = ctx.best_ask or 0.0
        spread = ctx.spread or 0.0
        if ask < self.min_price or ask > self.max_price:
            return None
        if spread < 0 or spread > self.max_spread:
            return None
        fee_drag = CRYPTO_FEE_RATE * ask * (1.0 - ask)
        net_edge = fair - ask - fee_drag - self.max_vwap_slippage
        ask_depth = ctx.book.depth("BUY") if ctx.book else 0.0
        return {
            "direction": direction,
            "ctx": ctx,
            "ask": ask,
            "fair": fair,
            "fee_drag": fee_drag,
            "ask_depth": ask_depth,
            "net_edge": net_edge,
        }

    def _build_signal(
        self,
        candidate: dict,
        range_bps: float,
        distance_bps: float,
        remaining: float,
        required_edge: float,
    ) -> Optional[TradingSignal]:
        ctx = candidate["ctx"]
        ask = candidate["ask"]
        fair = candidate["fair"]

        sizing = kelly_size(
            fair_prob=fair,
            market_price=ask,
            bankroll=self.bankroll,
            kelly_fraction=self.kelly_frac,
            max_bet_pct=self.max_bet_pct,
            min_edge=required_edge,
        )
        notional = min(self.max_notional_usdc, sizing.size_usdc)
        if notional < 5.0 or ask <= 0:
            self._reject("size")
            return None

        if candidate["ask_depth"] * ask < notional * self.depth_notional_mult:
            self._reject("depth_guard")
            return None

        size = notional / ask
        vwap = ask
        if ctx.book:
            vwap_price, fillable = ctx.book.vwap_price("BUY", size)
            if vwap_price is None or fillable < 1:
                self._reject("depth")
                return None
            if fillable < size:
                size = fillable
                notional = size * ask
            if vwap_price - ask > self.max_vwap_slippage:
                self._reject("slippage")
                return None
            vwap = vwap_price

        fee_drag = CRYPTO_FEE_RATE * vwap * (1.0 - vwap)
        net_edge = fair - vwap - fee_drag - self.max_vwap_slippage
        if net_edge < required_edge:
            self._reject("edge_vwap")
            return None

        confidence = min(max(net_edge / max(required_edge * 3.0, 1e-6), 0.0), 1.0)
        return TradingSignal(
            token_id=ctx.token_id,
            side="BUY",
            price=vwap,
            size=size,
            strategy=self.name,
            tick_size=ctx.tick_size,
            neg_risk=ctx.neg_risk,
            order_type="FAK",
            edge=net_edge,
            confidence=confidence,
            fair_value=fair,
            direction=candidate["direction"],
        )

    def _required_edge(self, remaining: float) -> float:
        base_edge = self.alt_min_edge if self.asset.lower() in ("sol", "xrp") else self.min_edge
        near = max(0.0, self.near_seconds)
        far = max(self.far_seconds, near + 1e-6)
        if remaining <= near:
            time_edge = self.min_edge
        elif remaining >= far:
            time_edge = self.far_edge
        else:
            slope = (self.far_edge - self.min_edge) / (far - near)
            time_edge = self.min_edge + (remaining - near) * slope
        return max(base_edge, time_edge)
