"""
Oracle Front-Run Strategy — unified interface (v2).

Exploits the lag between Binance BTC price (real-time) and Polymarket
5m market odds (delayed 1-3 seconds).

How it works:
  1. Polls BTC price from Binance every 0.5 seconds
  2. Detects sharp moves (> threshold) in the last few seconds
  3. Checks if Polymarket odds haven't adjusted yet (stale)
  4. If Binance says UP but Polymarket still prices UP cheaply → BUY UP
  5. Holds to settlement

The edge: you're not predicting direction. You're trading on something
that already happened on Binance but hasn't been priced in on Polymarket.

Key parameters:
  - move_threshold_bps: minimum BTC move to trigger (default 3 bps = 0.03%)
  - staleness_threshold: how much Polymarket must lag behind fair value
  - max_price: don't buy above this (risk/reward gate)
"""

import logging
import math
import time
from collections import deque
from typing import List, Optional

import requests

from strategies.base import BaseStrategy
from strategies.kelly import kelly_size
from data_pipeline.market_provider import MarketContext
from pipeline.signal import TradingSignal

log = logging.getLogger(__name__)

BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"


class OracleFrontrun(BaseStrategy):
    name = "oracle_frontrun"

    def __init__(
        self,
        asset: str = "btc",
        move_threshold_bps: float = 3.0,    # min BTC move to act (0.03%)
        staleness_threshold: float = 0.05,   # min gap between fair and market
        lookback_ticks: int = 5,             # compare price over last N ticks
        max_price: float = 0.60,             # don't buy above this
        min_price: float = 0.05,             # don't buy below this
        min_remaining_seconds: float = 60,   # need at least 1 min left
        kelly_frac: float = 0.25,
        max_bet_pct: float = 0.05,
        bankroll: float = 10_000,
        cooldown_seconds: float = 10.0,      # wait between trades
    ):
        self.asset = asset
        self.move_threshold_bps = move_threshold_bps
        self.staleness_threshold = staleness_threshold
        self.lookback_ticks = lookback_ticks
        self.max_price = max_price
        self.min_price = min_price
        self.min_remaining = min_remaining_seconds
        self.kelly_frac = kelly_frac
        self.max_bet_pct = max_bet_pct
        self.bankroll = bankroll
        self.cooldown_seconds = cooldown_seconds

        self._prices: deque = deque(maxlen=200)
        self._session = requests.Session()
        self._has_position = False
        self._last_trade_time = 0.0

        # Stats
        self.total_trades = 0
        self.signals_detected = 0
        self.signals_stale = 0      # market was stale (good)
        self.signals_already_priced = 0  # market already adjusted (missed)

    def on_fill(self, fill):
        if fill.side == "BUY":
            self._has_position = True
        elif fill.side == "SELL":
            self._has_position = False

    def on_cancel(self):
        self._has_position = False

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        # Poll BTC price
        self._poll_price()

        if self._has_position:
            return []

        if len(self._prices) < self.lookback_ticks + 1:
            return []

        # Cooldown
        if time.time() - self._last_trade_time < self.cooldown_seconds:
            return []

        # Detect sharp BTC move
        move = self._detect_move()
        if move is None:
            return []

        direction, move_bps, fair_prob_up = move

        # Check each context for staleness
        signals = []
        for ctx in contexts:
            if not ctx.is_valid:
                continue
            if ctx.seconds_remaining < self.min_remaining:
                continue
            s = self._check_staleness(ctx, direction, fair_prob_up, move_bps)
            if s:
                signals.append(s)
                break  # one trade per detection

        return signals

    def _poll_price(self):
        try:
            symbol = f"{self.asset.upper()}USDT"
            resp = self._session.get(BINANCE_TICKER, params={"symbol": symbol}, timeout=3)
            price = float(resp.json()["price"])
            self._prices.append((time.time(), price))
        except Exception:
            pass

    def _detect_move(self) -> Optional[tuple]:
        """
        Detect a sharp BTC move in the last few ticks.
        Returns (direction, move_bps, fair_prob_up) or None.
        """
        if len(self._prices) < self.lookback_ticks + 1:
            return None

        current_time, current_price = self._prices[-1]
        old_time, old_price = self._prices[-self.lookback_ticks - 1]

        if old_price <= 0:
            return None

        move_pct = (current_price - old_price) / old_price
        move_bps = abs(move_pct) * 10_000

        if move_bps < self.move_threshold_bps:
            return None

        self.signals_detected += 1

        # Estimate fair probability based on the move
        # Sharp move up → higher prob of finishing above strike
        # Use distance from strike to estimate
        # For simplicity: convert bps move to probability shift
        # 3 bps = ~0.55 fair, 10 bps = ~0.70 fair, 30 bps = ~0.85 fair
        prob_shift = min(move_bps / 50, 0.35)  # cap at 35% shift

        if move_pct > 0:
            direction = "UP"
            fair_prob_up = 0.50 + prob_shift
        else:
            direction = "DOWN"
            fair_prob_up = 0.50 - prob_shift

        return direction, move_bps, fair_prob_up

    def _check_staleness(
        self, ctx: MarketContext, direction: str, fair_prob_up: float, move_bps: float,
    ) -> Optional[TradingSignal]:
        """
        Check if Polymarket odds are stale (haven't adjusted to the BTC move).
        If stale, buy the underpriced side.
        """
        market_up = ctx.mid_price  # Up token mid

        if direction == "UP":
            # BTC went up → fair UP probability is high → is UP token still cheap?
            fair = fair_prob_up
            market_price = market_up
            token_id = ctx.token_id

            staleness = fair - market_price
        else:
            # BTC went down → fair DOWN probability is high → is DOWN token still cheap?
            fair = 1.0 - fair_prob_up
            market_price = 1.0 - market_up  # DOWN token price
            token_id = ctx.token_id_other

            staleness = fair - market_price

        # Is the market stale enough?
        if staleness < self.staleness_threshold:
            self.signals_already_priced += 1
            log.debug(
                "ORACLE: %s move %.1fbps but market already priced (stale=%.4f < threshold=%.4f)",
                direction, move_bps, staleness, self.staleness_threshold,
            )
            return None

        self.signals_stale += 1

        # Risk/reward gate
        if market_price > self.max_price:
            log.debug("ORACLE: skip — market_price %.3f > max %.3f", market_price, self.max_price)
            return None
        if market_price < self.min_price:
            return None

        # Positive edge required
        edge = fair - market_price
        if edge <= 0:
            return None

        # Kelly sizing
        kelly = kelly_size(
            fair_prob=fair,
            market_price=market_price,
            bankroll=self.bankroll,
            kelly_fraction=self.kelly_frac,
            max_bet_pct=self.max_bet_pct,
            min_edge=self.staleness_threshold,
        )
        if kelly.direction == "NONE" or kelly.size_usdc < 5:
            return None

        size = kelly.size_usdc / market_price if market_price > 0 else 0

        self.total_trades += 1
        self._last_trade_time = time.time()

        log.info(
            "ORACLE FRONTRUN: %s %.1f shares @ %.4f | "
            "BTC move=%.1fbps stale=%.4f fair=%.3f mkt=%.3f edge=%.4f",
            direction, size, market_price,
            move_bps, staleness, fair, market_price, edge,
        )

        return TradingSignal(
            token_id=token_id,
            side="BUY",
            price=market_price,
            size=size,
            strategy=self.name,
            edge=edge,
            fair_value=fair,
            confidence=min(staleness / 0.20, 1.0),
            tick_size=ctx.tick_size,
        )

    def snapshot(self) -> dict:
        return {
            "btc_price": self._prices[-1][1] if self._prices else 0,
            "total_trades": self.total_trades,
            "signals_detected": self.signals_detected,
            "signals_stale": self.signals_stale,
            "signals_already_priced": self.signals_already_priced,
            "stale_rate": f"{self.signals_stale / max(self.signals_detected, 1) * 100:.0f}%",
            "has_position": self._has_position,
        }
