"""
Module 2 — Data Pipeline & Market Data

Collects and normalizes: order book snapshots, trade history, market metadata.
In production, this would use WebSockets + Kafka + TimescaleDB + Redis.
This implementation uses REST polling with in-memory state for simplicity.
"""

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import requests

from config import CLOB_BASE, GAMMA_BASE, DATA_API_BASE

log = logging.getLogger(__name__)


@dataclass
class OrderBookSnapshot:
    token_id: str
    bids: List[List[float]]  # [[price, size], ...]
    asks: List[List[float]]
    timestamp: float = 0.0

    def __post_init__(self):
        # Polymarket documents sorted books, but sorting defensively keeps
        # top-of-book logic correct if an endpoint or test fixture differs.
        self.bids.sort(key=lambda row: row[0], reverse=True)
        self.asks.sort(key=lambda row: row[0])

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return self.best_bid or self.best_ask

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    def depth(self, side: str, levels: int = 5) -> float:
        data = self.asks if side == "BUY" else self.bids
        return sum(row[1] for row in data[:levels])

    def vwap_price(self, side: str, size: float) -> tuple:
        """
        Walk the book to compute the actual fill price for `size` shares.
        side="BUY" walks asks (you pay the ask); side="SELL" walks bids.

        Returns: (vwap_price, fillable_size)
        If book can't fill the full size, fillable_size < size.
        """
        data = self.asks if side == "BUY" else self.bids
        if not data:
            return (None, 0.0)

        filled = 0.0
        cost = 0.0
        for price, level_size in data:
            take = min(level_size, size - filled)
            cost += take * price
            filled += take
            if filled >= size:
                break

        if filled <= 0:
            return (None, 0.0)
        return (cost / filled, filled)

    def slippage(self, side: str, size: float) -> float:
        """How much worse is the VWAP vs best price for a given size."""
        best = self.best_ask if side == "BUY" else self.best_bid
        vwap, _ = self.vwap_price(side, size)
        if best is None or vwap is None:
            return 0.0
        return abs(vwap - best)

    def has_sufficient_depth(self, side: str, required_size: float, max_levels: int = 10) -> bool:
        """Check if book has enough depth to fill required_size."""
        available = self.depth(side, levels=max_levels)
        return available >= required_size * 0.8

    def total_value(self, side: str, levels: int = 10) -> float:
        """Total USDC value on one side of the book."""
        data = self.asks if side == "BUY" else self.bids
        return sum(row[0] * row[1] for row in data[:levels])

    def age_seconds(self) -> float:
        if self.timestamp <= 0:
            return 999.0
        return time.time() - self.timestamp

    def is_stale(self, max_age: float = 10.0) -> bool:
        return self.age_seconds() > max_age


@dataclass
class MarketInfo:
    condition_id: str
    question: str
    outcomes: List[str]
    token_ids: List[str]
    prices: List[float]
    neg_risk: bool
    tick_size: str
    end_date: str = ""
    volume_24h: float = 0.0
    liquidity: float = 0.0


class MarketDataFeed:
    """
    Centralized market data provider. All strategy modules read from here.
    In production: backed by Redis (hot) + TimescaleDB (cold).
    Here: in-memory with REST polling.
    """

    def __init__(self, timeout: float = 10.0):
        self._books: Dict[str, OrderBookSnapshot] = {}
        self._price_history: Dict[str, List[float]] = defaultdict(list)
        self._market_cache: Dict[str, MarketInfo] = {}
        self.timeout = timeout
        self.session = requests.Session()

    # ---- Order Book ----

    def fetch_order_book(self, token_id: str) -> OrderBookSnapshot:
        resp = self.session.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()

        bids = [[float(b["price"]), float(b["size"])] for b in data.get("bids", [])]
        asks = [[float(a["price"]), float(a["size"])] for a in data.get("asks", [])]

        snap = OrderBookSnapshot(
            token_id=token_id,
            bids=bids,
            asks=asks,
            timestamp=time.time(),
        )
        self._books[token_id] = snap

        # Track mid price history for volatility
        if snap.mid is not None:
            self._price_history[token_id].append(snap.mid)
            # Keep last 1000 prices
            if len(self._price_history[token_id]) > 1000:
                self._price_history[token_id] = self._price_history[token_id][-500:]

        return snap

    def get_book(self, token_id: str) -> Optional[OrderBookSnapshot]:
        return self._books.get(token_id)

    def get_fresh_book(self, token_id: str, max_age: float = 10.0) -> Optional[OrderBookSnapshot]:
        """Return cached book if fresh, otherwise re-fetch."""
        book = self._books.get(token_id)
        if book and not book.is_stale(max_age):
            return book
        try:
            return self.fetch_order_book(token_id)
        except Exception as e:
            log.warning("Book fetch failed for %s: %s", token_id[:16], e)
            return None

    # ---- Volatility ----

    def rolling_volatility(self, token_id: str, window: int = 60) -> float:
        """Rolling std dev of mid prices. Window = number of observations."""
        prices = self._price_history.get(token_id, [])
        if len(prices) < 2:
            return 0.01  # default low vol
        recent = prices[-window:]
        return float(np.std(recent)) if len(recent) > 1 else 0.01

    # ---- Market Metadata ----

    def fetch_market(self, condition_id: str) -> Optional[MarketInfo]:
        resp = self.session.get(f"{GAMMA_BASE}/markets", params={"conditionId": condition_id}, timeout=self.timeout)
        resp.raise_for_status()
        markets = resp.json()
        if not markets:
            return None
        return self._parse_market(markets[0])

    def search_markets(self, query: str = "", limit: int = 20) -> List[MarketInfo]:
        params = {"_limit": limit, "active": True, "closed": False}
        resp = self.session.get(f"{GAMMA_BASE}/markets", params=params, timeout=self.timeout)
        resp.raise_for_status()
        markets = resp.json()

        if query:
            q = query.lower()
            markets = [m for m in markets if q in m.get("question", "").lower()]

        return [self._parse_market(m) for m in markets[:limit]]

    def _parse_market(self, m: dict) -> MarketInfo:
        outcomes = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else (m.get("outcomes") or [])
        clob_ids = json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else (m.get("clobTokenIds") or [])
        prices_raw = m.get("outcomePrices", "[]")
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else (prices_raw or [])

        info = MarketInfo(
            condition_id=m.get("conditionId", ""),
            question=m.get("question", ""),
            outcomes=outcomes,
            token_ids=clob_ids,
            prices=[float(p) for p in prices],
            neg_risk=m.get("negRisk", False),
            tick_size=str(m.get("orderPriceMinTickSize", "0.01")),
            end_date=m.get("endDate", ""),
            volume_24h=float(m.get("volume24hr", 0)),
            liquidity=float(m.get("liquidity", 0)),
        )
        self._market_cache[info.condition_id] = info
        return info

    # ---- Adjusted Midpoint (Anti-Toxic-Flow) ----

    def adjusted_midpoint(self, token_id: str, min_incentive_size: float = 50.0) -> Optional[float]:
        """
        Filter out bait orders from other bots trying to pin a fake price.
        Only use orders >= min_incentive_size USDC when computing the mid.
        """
        book = self._books.get(token_id)
        if not book:
            return None
        filtered_bids = [b for b in book.bids if b[1] >= min_incentive_size]
        filtered_asks = [a for a in book.asks if a[1] >= min_incentive_size]
        if filtered_bids and filtered_asks:
            return (filtered_bids[0][0] + filtered_asks[0][0]) / 2
        return book.mid

    # ---- OHLCV Regime Classification ----

    @staticmethod
    def classify_regime(price: float) -> str:
        if price <= 0.15 or price >= 0.85:
            return "tail"
        elif 0.35 <= price <= 0.65:
            return "contested"
        else:
            return "trending"
