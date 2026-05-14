"""
Reusable Skills — shared components extracted from momentum and oracle.

Each skill is a single responsibility. Strategies compose them.

Usage:
    feed = BTCPriceFeed("btc")
    gate = RiskRewardGate(max_price=0.60)
    sizer = PositionSizer(kelly_frac=0.20, bankroll=10000)
    lock = EntryLock(cooldown=10)

    # In strategy.step():
    feed.poll()
    move = feed.detect_move(lookback=5, threshold_bps=4.0)
    fair = my_fair_value_model(move)  # strategy-specific
    edge = EdgeCalculator.compute(fair, market_price)
    if gate.passes(edge, market_price) and lock.can_trade():
        size = sizer.size(fair, market_price)
        lock.on_signal()
        return [TradingSignal(...)]
"""

import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import numpy as np
import requests

from strategies.kelly import kelly_size

log = logging.getLogger(__name__)

BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"


# ---------------------------------------------------------------------------
# Skill 1: BTC Price Feed
# ---------------------------------------------------------------------------

class BTCPriceFeed:
    """Polls BTC/ETH price from Binance. Computes momentum, volatility, trend."""

    def __init__(self, asset: str = "btc", max_history: int = 200):
        self.asset = asset
        self.prices: deque = deque(maxlen=max_history)
        self._session = requests.Session()

    def poll(self) -> Optional[float]:
        try:
            symbol = f"{self.asset.upper()}USDT"
            resp = self._session.get(BINANCE_TICKER, params={"symbol": symbol}, timeout=3)
            price = float(resp.json()["price"])
            self.prices.append(price)
            return price
        except Exception:
            return None

    @property
    def current(self) -> float:
        return self.prices[-1] if self.prices else 0.0

    def momentum(self, window: int = 20) -> float:
        if len(self.prices) < 2:
            return 0.0
        n = min(window, len(self.prices))
        return (self.prices[-1] - self.prices[-n]) / self.prices[-n]

    def volatility(self, window: int = 20) -> float:
        if len(self.prices) < 3:
            return 0.001
        n = min(window, len(self.prices))
        recent = list(self.prices)[-n:]
        returns = [(recent[i] - recent[i - 1]) / recent[i - 1] for i in range(1, len(recent))]
        return float(np.std(returns)) if returns else 0.001

    @dataclass
    class Move:
        direction: str  # "UP" or "DOWN"
        bps: float
        pct: float

    def detect_move(self, lookback: int = 5, threshold_bps: float = 4.0) -> Optional[Move]:
        """Detect a sharp price move over the last N ticks."""
        if len(self.prices) < lookback + 1:
            return None
        old = self.prices[-lookback - 1]
        new = self.prices[-1]
        if old <= 0:
            return None
        pct = (new - old) / old
        bps = abs(pct) * 10_000
        if bps < threshold_bps:
            return None
        direction = "UP" if pct > 0 else "DOWN"
        return self.Move(direction=direction, bps=bps, pct=pct)


# ---------------------------------------------------------------------------
# Skill 2: Edge Calculator
# ---------------------------------------------------------------------------

class EdgeCalculator:
    """Computes edge = fair - market."""

    @staticmethod
    def compute(fair: float, market_price: float) -> float:
        return fair - market_price

    @staticmethod
    def has_edge(fair: float, market_price: float, min_edge: float = 0.03) -> bool:
        return (fair - market_price) >= min_edge


# ---------------------------------------------------------------------------
# Skill 3: Risk/Reward Gate
# ---------------------------------------------------------------------------

class RiskRewardGate:
    """Filters out trades with bad risk/reward or in wrong regime."""

    def __init__(self, max_price: float = 0.60, min_price: float = 0.05):
        self.max_price = max_price
        self.min_price = min_price

    def passes(self, edge: float, market_price: float) -> bool:
        if market_price > self.max_price:
            return False
        if market_price < self.min_price:
            return False
        if edge <= 0:
            return False
        return True


# ---------------------------------------------------------------------------
# Skill 4: Position Sizer
# ---------------------------------------------------------------------------

class PositionSizer:
    """Kelly criterion sizing with fee awareness."""

    def __init__(self, kelly_frac: float = 0.20, max_bet_pct: float = 0.05,
                 bankroll: float = 10_000, min_bet: float = 5.0, min_edge: float = 0.02, fee_bps: float = 0.0):
        self.kelly_frac = kelly_frac
        self.max_bet_pct = max_bet_pct
        self.bankroll = bankroll
        self.min_bet = min_bet
        self.min_edge = min_edge
        self.fee_bps = fee_bps

    def size(self, fair: float, market_price: float) -> Optional[float]:
        """Returns size in shares, or None if too small."""
        result = kelly_size(
            fair_prob=fair,
            market_price=market_price,
            bankroll=self.bankroll,
            kelly_fraction=self.kelly_frac,
            max_bet_pct=self.max_bet_pct,
            min_edge=self.min_edge,
            fee_bps=self.fee_bps,
        )
        if result.direction == "NONE" or result.size_usdc < self.min_bet:
            return None
        if market_price <= 0:
            return None
        return result.size_usdc / market_price


# ---------------------------------------------------------------------------
# Skill 5: Entry Lock
# ---------------------------------------------------------------------------

class EntryLock:
    """Prevents multiple entries. One position at a time with cooldown."""

    def __init__(self, cooldown: float = 10.0):
        self.cooldown = cooldown
        self.has_position = False
        self._last_trade_time = 0.0

    def can_trade(self) -> bool:
        if self.has_position:
            return False
        if time.time() - self._last_trade_time < self.cooldown:
            return False
        return True

    def on_signal(self):
        """Call when emitting a signal — locks immediately."""
        self.has_position = True
        self._last_trade_time = time.time()

    def on_fill_buy(self):
        self.has_position = True

    def on_fill_sell(self):
        self.has_position = False

    def on_cancel(self):
        self.has_position = False
