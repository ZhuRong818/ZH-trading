"""
BTC Momentum — unified interface (v2).

Input:  List[MarketContext] (uses external_price + strike_price)
Output: List[TradingSignal] (directional bet on 5m outcome)

Replaces the old btc_5m standalone strategy with a clean signal generator.
The rolling window management is handled by the provider, not the strategy.
"""

import logging
import math
import time
from typing import List, Optional
from collections import deque

import requests

from strategies.base import BaseStrategy
from strategies.kelly import kelly_size
from data_pipeline.market_provider import MarketContext
from pipeline.signal import TradingSignal

log = logging.getLogger(__name__)

BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"


class Momentum(BaseStrategy):
    name = "btc5m_momentum"

    def __init__(
        self,
        asset: str = "btc",
        min_edge: float = 0.14,
        kelly_frac: float = 0.20,
        max_bet_pct: float = 0.025,
        bankroll: float = 5_000,
        max_price: float = 0.55,
        min_price: float = 0.40,
        momentum_window: int = 20,
        min_mom_vol_ratio: float = 0.8,
        min_entry_age: float = 20.0,
        entry_deadline: float = 60.0,
        min_abs_z: float = 0.15,
        min_distance_bps: float = 2.0,
        max_vwap_slippage: float = 0.02,
        down_edge_boost: float = 0.08,
        down_min_abs_z: float = 0.35,
        fair_cap: float = 0.80,
        confirmations_required: int = 2,
    ):
        self.asset = asset
        self.min_edge = min_edge
        self.kelly_frac = kelly_frac
        self.max_bet_pct = max_bet_pct
        self.bankroll = bankroll
        self.max_price = max_price
        self.min_price = min_price
        self.momentum_window = momentum_window
        self.min_mom_vol_ratio = min_mom_vol_ratio
        self.min_entry_age = min_entry_age
        self.entry_deadline = entry_deadline
        self.min_abs_z = min_abs_z
        self.min_distance_bps = min_distance_bps
        self.max_vwap_slippage = max_vwap_slippage
        self.down_edge_boost = down_edge_boost
        self.down_min_abs_z = down_min_abs_z
        self.fair_cap = fair_cap
        self.confirmations_required = max(1, confirmations_required)

        self._prices: deque = deque(maxlen=200)
        self._price_times: deque = deque(maxlen=200)
        self._session = requests.Session()
        self._has_position = False
        self._last_condition_id = ""
        self._pending_signal_key: tuple[str, str] | None = None
        self._pending_signal_count = 0
        self._candidate_details: dict = {}
        self.total_trades = 0
        self.wins = 0
        self.losses = 0

    def on_fill(self, fill):
        if fill.side == "BUY":
            self._has_position = True
        elif fill.side == "SELL":
            self._has_position = False
            self._pending_signal_key = None
            self._pending_signal_count = 0

    def on_cancel(self):
        self._pending_signal_key = None
        self._pending_signal_count = 0

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        up_ctx, down_ctx = self._resolve_up_down_contexts(contexts)

        # Poll price
        self._poll_price()

        if self._has_position:
            return []  # already in a trade, wait for settlement

        if len(self._prices) < 5:
            return []

        condition_id = up_ctx.condition_id if up_ctx else ""
        if condition_id and condition_id != self._last_condition_id:
            self._last_condition_id = condition_id
            self._pending_signal_key = None
            self._pending_signal_count = 0

        # One signal per window — only process once using resolved UP/DOWN contexts.
        # We use executable book prices (best ask) instead of mid to reduce
        # paper-trade vs fill price divergence.
        if up_ctx and down_ctx:
            if up_ctx.seconds_remaining >= self.entry_deadline:
                s = self._compute(up_ctx, down_ctx)
                if s:
                    key = (s.direction, s.token_id)
                    if key == self._pending_signal_key:
                        self._pending_signal_count += 1
                    else:
                        self._pending_signal_key = key
                        self._pending_signal_count = 1

                    if self._pending_signal_count < self.confirmations_required:
                        return []

                    self.total_trades += 1
                    d = self._candidate_details
                    log.info(
                        "MOMENTUM: %s %.1f @ %.4f vwap=%.4f edge=%.4f z=%.2f "
                        "dist=$%.2f mom=%.4f%% btc=$%.0f age=%.0fs conf=%d",
                        s.direction, s.size, d.get("market_price", s.price),
                        s.price, s.edge, d.get("z_score", 0.0),
                        d.get("distance", 0.0), d.get("momentum", 0.0) * 100,
                        d.get("current", 0.0), d.get("window_age", 0.0),
                        self._pending_signal_count,
                    )
                    return [s]
                self._pending_signal_key = None
                self._pending_signal_count = 0
        return []

    def _poll_price(self):
        try:
            symbol = f"{self.asset.upper()}USDT"
            resp = self._session.get(BINANCE_TICKER, params={"symbol": symbol}, timeout=5)
            self._prices.append(float(resp.json()["price"]))
            self._price_times.append(time.time())
        except Exception:
            pass

    def _momentum(self, window: int = 20) -> float:
        if len(self._prices) < 2:
            return 0.0
        n = min(window, len(self._prices))
        return (self._prices[-1] - self._prices[-n]) / self._prices[-n]

    def _volatility(self, window: int = 20) -> float:
        if len(self._prices) < 3:
            return 0.001
        import numpy as np
        n = min(window, len(self._prices))
        recent = list(self._prices)[-n:]
        returns = [(recent[i] - recent[i-1]) / recent[i-1] for i in range(1, len(recent))]
        return float(np.std(returns)) if returns else 0.001

    def _sample_interval(self) -> float:
        if len(self._price_times) < 2:
            return 5.0
        intervals = [
            self._price_times[i] - self._price_times[i - 1]
            for i in range(1, len(self._price_times))
            if self._price_times[i] > self._price_times[i - 1]
        ]
        if not intervals:
            return 5.0
        intervals.sort()
        return max(1.0, min(10.0, intervals[len(intervals) // 2]))

    def _compute(self, up_ctx: MarketContext, down_ctx: MarketContext) -> Optional[TradingSignal]:
        momentum = self._momentum(self.momentum_window)
        vol = max(self._volatility(self.momentum_window), 0.00002)

        current = up_ctx.external_price or (self._prices[-1] if self._prices else 0)
        strike = up_ctx.strike_price
        remaining = up_ctx.seconds_remaining
        window_age = max(0.0, 300.0 - remaining)

        if current <= 0 or strike <= 0:
            return None

        if window_age < self.min_entry_age:
            return None

        # Z-score from distance to strike
        horizon_ticks = max(remaining / self._sample_interval(), 1.0)
        horizon_sigma = current * vol * math.sqrt(horizon_ticks)
        distance = current - strike
        z_score = distance / horizon_sigma if horizon_sigma > 0 else 0
        distance_bps = abs(distance) / current * 10_000

        # Momentum quality filter
        mom_vol_ratio = abs(momentum) / vol if vol > 0 else 0
        if (
            mom_vol_ratio < self.min_mom_vol_ratio
            or abs(z_score) < self.min_abs_z
            or distance_bps < self.min_distance_bps
        ):
            return None

        # Drift adjustment
        mom_z = max(-2.0, min(2.0, momentum / vol if vol > 0 else 0))
        adjusted_z = z_score + 0.15 * mom_z

        # Convert to probability
        fair_prob_up = 0.5 * (1.0 + math.erf(adjusted_z / math.sqrt(2.0)))
        fair_prob_up = max(1.0 - self.fair_cap, min(self.fair_cap, fair_prob_up))

        # Direction
        if fair_prob_up > 0.52:
            direction = "UP"
            fair = fair_prob_up
            market_price = up_ctx.best_ask or 0.0
            ctx = up_ctx
        elif fair_prob_up < 0.48:
            direction = "DOWN"
            fair = 1 - fair_prob_up
            market_price = down_ctx.best_ask or 0.0
            ctx = down_ctx
        else:
            return None

        if market_price <= 0:
            return None

        if direction == "DOWN" and abs(z_score) < self.down_min_abs_z:
            return None

        # Risk/reward gate
        if market_price > self.max_price or market_price < self.min_price:
            return None

        # Edge check
        edge = fair - market_price
        required_edge = self.min_edge + (self.down_edge_boost if direction == "DOWN" else 0.0)
        if edge <= 0 or edge < required_edge:
            return None

        # Kelly sizing
        kelly = kelly_size(
            fair_prob=fair, market_price=market_price,
            bankroll=self.bankroll,
            kelly_fraction=self.kelly_frac,
            max_bet_pct=self.max_bet_pct,
            min_edge=self.min_edge,
        )
        if kelly.direction == "NONE" or kelly.size_usdc < 5:
            return None

        size = kelly.size_usdc / market_price if market_price > 0 else 0

        book = ctx.book
        vwap = market_price
        if book:
            vwap_price, fillable = book.vwap_price("BUY", size)
            if vwap_price is None or fillable < 1:
                return None
            if fillable < size:
                size = fillable
            if vwap_price - market_price > self.max_vwap_slippage:
                return None
            vwap = vwap_price

        if vwap > self.max_price or vwap < self.min_price:
            return None

        edge = fair - vwap
        if edge < required_edge:
            return None

        kelly = kelly_size(
            fair_prob=fair, market_price=vwap,
            bankroll=self.bankroll,
            kelly_fraction=self.kelly_frac,
            max_bet_pct=self.max_bet_pct,
            min_edge=required_edge,
        )
        if kelly.direction == "NONE" or kelly.size_usdc < 5:
            return None

        size = kelly.size_usdc / vwap if vwap > 0 else 0
        if book:
            vwap_price, fillable = book.vwap_price("BUY", size)
            if vwap_price is None or fillable < 1:
                return None
            if fillable < size:
                size = fillable
            vwap = vwap_price
            edge = fair - vwap
            if (
                vwap - market_price > self.max_vwap_slippage
                or edge < required_edge
                or vwap > self.max_price
                or vwap < self.min_price
            ):
                return None

        # Use the correct token
        if direction == "UP":
            token_id = up_ctx.token_id
        else:
            token_id = down_ctx.token_id

        self._candidate_details = {
            "market_price": market_price,
            "z_score": z_score,
            "distance": distance,
            "momentum": momentum,
            "current": current,
            "window_age": window_age,
        }

        return TradingSignal(
            token_id=token_id, side="BUY", price=vwap, size=size,
            strategy=self.name, edge=edge,
            fair_value=fair, confidence=min(abs(adjusted_z) / 2, 1.0),
            direction=direction,
            tick_size=up_ctx.tick_size,
            order_type="FAK",
        )

    def _resolve_up_down_contexts(
        self, contexts: List[MarketContext]
    ) -> tuple[Optional[MarketContext], Optional[MarketContext]]:
        ctx_map = {c.token_id: c for c in contexts if c and c.is_valid}

        up_ctx = None
        down_ctx = None
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
                    if up_ctx and down_ctx:
                        break

        return up_ctx, down_ctx

    def snapshot(self) -> dict:
        return {
            "btc_price": self._prices[-1] if self._prices else 0,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "prices_buffered": len(self._prices),
        }
