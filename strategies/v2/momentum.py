"""
BTC Momentum — unified interface (v2).

Input:  List[MarketContext] (uses external_price + strike_price)
Output: List[TradingSignal] (directional bet on 5m outcome)

Replaces the old btc_5m standalone strategy with a clean signal generator.
The rolling window management is handled by the provider, not the strategy.
"""

import logging
import math
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
        min_edge: float = 0.03,
        kelly_frac: float = 0.20,
        max_bet_pct: float = 0.05,
        bankroll: float = 5_000,
        max_price: float = 0.65,
        min_price: float = 0.05,
        momentum_window: int = 20,
        min_mom_vol_ratio: float = 0.5,
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

        self._prices: deque = deque(maxlen=200)
        self._session = requests.Session()
        self._has_position = False
        self.total_trades = 0
        self.wins = 0
        self.losses = 0

    def on_fill(self, fill):
        if fill.side == "BUY":
            self._has_position = True
        elif fill.side == "SELL":
            self._has_position = False

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        # Poll price
        self._poll_price()

        if self._has_position:
            return []  # already in a trade, wait for settlement

        if len(self._prices) < 5:
            return []

        # One signal per window — only process the first valid context.
        # RollingProvider inserts the UP-token context first, and _compute()
        # is designed to work from the UP-token perspective.
        for ctx in contexts:
            if not ctx.is_valid:
                continue
            if ctx.seconds_remaining < 30:
                continue  # too late
            s = self._compute(ctx)
            if s:
                self._has_position = True  # lock immediately
                return [s]
            return []

        return []

    def _poll_price(self):
        try:
            symbol = f"{self.asset.upper()}USDT"
            resp = self._session.get(BINANCE_TICKER, params={"symbol": symbol}, timeout=5)
            self._prices.append(float(resp.json()["price"]))
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

    def _compute(self, ctx: MarketContext) -> Optional[TradingSignal]:
        momentum = self._momentum(self.momentum_window)
        vol = max(self._volatility(self.momentum_window), 0.00002)

        current = self._prices[-1] if self._prices else 0
        strike = ctx.strike_price
        remaining = ctx.seconds_remaining

        if current <= 0 or strike <= 0:
            return None

        # Z-score from distance to strike
        horizon_ticks = max(remaining / 0.5, 1.0)
        horizon_sigma = current * vol * math.sqrt(horizon_ticks)
        distance = current - strike
        z_score = distance / horizon_sigma if horizon_sigma > 0 else 0

        # Momentum quality filter
        mom_vol_ratio = abs(momentum) / vol if vol > 0 else 0
        if mom_vol_ratio < self.min_mom_vol_ratio and abs(z_score) < 0.5:
            return None

        # Drift adjustment
        mom_z = max(-2.0, min(2.0, momentum / vol if vol > 0 else 0))
        adjusted_z = z_score + 0.2 * mom_z

        # Convert to probability
        fair_prob_up = 0.5 * (1.0 + math.erf(adjusted_z / math.sqrt(2.0)))
        fair_prob_up = max(0.05, min(0.95, fair_prob_up))

        # Direction
        if fair_prob_up > 0.52:
            direction = "UP"
            fair = fair_prob_up
            market_price = ctx.mid_price  # Up token mid
        elif fair_prob_up < 0.48:
            direction = "DOWN"
            fair = 1 - fair_prob_up
            # Need the other token's price
            market_price = 1 - ctx.mid_price
        else:
            return None

        # Risk/reward gate
        if market_price > self.max_price or market_price < self.min_price:
            return None

        # Edge check
        edge = fair - market_price
        if edge <= 0 or edge < self.min_edge:
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

        # Use the correct token
        if direction == "UP":
            token_id = ctx.token_id
        else:
            token_id = ctx.token_id_other

        self.total_trades += 1
        log.info("MOMENTUM: %s %.1f @ %.4f edge=%.4f z=%.2f mom=%.4f%% btc=$%.0f",
                 direction, size, market_price, edge, z_score, momentum * 100, current)

        return TradingSignal(
            token_id=token_id, side="BUY", price=market_price, size=size,
            strategy=self.name, edge=edge,
            fair_value=fair, confidence=min(abs(adjusted_z) / 2, 1.0),
            tick_size=ctx.tick_size,
        )

    def snapshot(self) -> dict:
        return {
            "btc_price": self._prices[-1] if self._prices else 0,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "prices_buffered": len(self._prices),
        }
