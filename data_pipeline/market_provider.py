"""
Market Provider — abstracts market discovery and token rotation.

Two implementations:
  StaticProvider  — for long-dated markets (token doesn't change)
  RollingProvider — for 5-minute markets (new token every 5 min)

Strategies receive a MarketContext each cycle. They don't know or care
whether the underlying market lasts 5 minutes or 5 months.
"""

import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

from config import CLOB_BASE, GAMMA_BASE
from data_pipeline.market_data import MarketDataFeed, OrderBookSnapshot

log = logging.getLogger(__name__)


@dataclass
class MarketContext:
    """Everything a strategy needs to know about a market on this cycle."""
    token_id: str = ""
    token_id_other: str = ""       # the opposite side (YES if you have NO, etc.)
    mid_price: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    book: Optional[OrderBookSnapshot] = None
    seconds_remaining: float = 999999.0
    tick_size: str = "0.01"
    neg_risk: bool = False
    regime: str = "contested"
    volatility: float = 0.01
    condition_id: str = ""
    question: str = ""
    is_valid: bool = False         # True if data is fresh and usable

    # For external price feed (BTC 5m uses Binance price, not book mid)
    external_price: float = 0.0    # e.g., BTC price from Binance
    strike_price: float = 0.0      # e.g., starting BTC price for 5m window


class MarketProvider:
    """Base class — override refresh() for different market types."""

    def __init__(self, data_feed: MarketDataFeed):
        self.data = data_feed
        self.contexts: Dict[str, MarketContext] = {}

    def refresh(self) -> Dict[str, MarketContext]:
        """Refresh all market contexts. Called each loop iteration."""
        raise NotImplementedError

    def get_context(self, token_id: str) -> Optional[MarketContext]:
        return self.contexts.get(token_id)

    def all_contexts(self) -> List[MarketContext]:
        return [c for c in self.contexts.values() if c.is_valid]


class StaticProvider(MarketProvider):
    """
    For long-dated markets — token doesn't change.
    Refreshes book data and computes mid/spread/volatility each cycle.
    """

    def __init__(self, data_feed: MarketDataFeed, markets: List[dict]):
        """
        markets: list of dicts with keys:
            token_id, tick_size, neg_risk, end_date, question, gamma_price,
            condition_id, all_token_ids
        """
        super().__init__(data_feed)
        self._markets = markets

        # Initialize contexts
        for mkt in markets:
            token_id = mkt["token_id"]
            all_tokens = mkt.get("all_token_ids", [])
            other_token = ""
            if len(all_tokens) == 2:
                other_token = all_tokens[1] if all_tokens[0] == token_id else all_tokens[0]

            self.contexts[token_id] = MarketContext(
                token_id=token_id,
                token_id_other=other_token,
                tick_size=mkt.get("tick_size", "0.01"),
                neg_risk=mkt.get("neg_risk", False),
                condition_id=mkt.get("condition_id", ""),
                question=mkt.get("question", ""),
            )

    def refresh(self) -> Dict[str, MarketContext]:
        for mkt in self._markets:
            token_id = mkt["token_id"]
            ctx = self.contexts[token_id]

            try:
                book = self.data.get_fresh_book(token_id, max_age=10.0)
                if book and book.mid is not None:
                    ctx.book = book
                    ctx.mid_price = book.mid
                    ctx.best_bid = book.best_bid or 0
                    ctx.best_ask = book.best_ask or 0
                    ctx.spread = book.spread or 0
                    ctx.is_valid = True
                elif mkt.get("gamma_price"):
                    # Fallback to gamma price if book is empty
                    ctx.mid_price = mkt["gamma_price"]
                    ctx.is_valid = True
                else:
                    ctx.is_valid = False

                ctx.volatility = self.data.rolling_volatility(token_id)
                ctx.regime = MarketDataFeed.classify_regime(ctx.mid_price)

                # Time to resolution
                end_date = mkt.get("end_date", "")
                if end_date:
                    try:
                        end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        ctx.seconds_remaining = max((end - datetime.now(timezone.utc)).total_seconds(), 0)
                    except (ValueError, TypeError):
                        ctx.seconds_remaining = 999999.0

            except Exception as e:
                log.warning("StaticProvider refresh failed for %s: %s", token_id[:16], e)
                ctx.is_valid = False

        return self.contexts


class RollingProvider(MarketProvider):
    """
    For 5-minute rolling markets (BTC, ETH).
    Auto-discovers the current window and rotates tokens every 5 minutes.
    """

    def __init__(
        self,
        data_feed: MarketDataFeed,
        asset: str = "btc",
        interval: str = "5m",
        price_feed=None,  # callable that returns current price (e.g., Binance)
    ):
        super().__init__(data_feed)
        self.asset = asset
        self.interval = interval
        self.interval_seconds = int(interval.replace("m", "")) * 60
        self.price_feed = price_feed
        self.session = requests.Session()

        self._current_slug: str = ""
        self._current_window_ts: int = 0
        self._strike_price: float = 0.0
        self._up_token: str = ""
        self._down_token: str = ""
        self._end_time: Optional[datetime] = None

    def refresh(self) -> Dict[str, MarketContext]:
        now = int(time.time())
        window_ts = now - (now % self.interval_seconds)

        # Check if we need to discover a new window
        if window_ts != self._current_window_ts:
            self._discover_window(window_ts)

        if not self._up_token:
            return self.contexts

        # Get external price
        external_price = 0.0
        if self.price_feed:
            try:
                external_price = self.price_feed()
            except Exception:
                pass

        # Time remaining
        remaining = 0.0
        if self._end_time:
            remaining = max((self._end_time - datetime.now(timezone.utc)).total_seconds(), 0)

        # Build contexts for both Up and Down tokens
        for token_id, side in [(self._up_token, "up"), (self._down_token, "down")]:
            try:
                book = self.data.get_fresh_book(token_id, max_age=5.0)
            except Exception:
                book = None

            mid = book.mid if book and book.mid is not None else 0.5

            ctx = MarketContext(
                token_id=token_id,
                token_id_other=self._down_token if side == "up" else self._up_token,
                mid_price=mid,
                best_bid=book.best_bid if book else 0,
                best_ask=book.best_ask if book else 0,
                spread=book.spread if book and book.spread else 0,
                book=book,
                seconds_remaining=remaining,
                tick_size="0.01",
                neg_risk=False,
                regime="5m_rolling",
                volatility=self.data.rolling_volatility(token_id) if book else 0.01,
                condition_id=self._current_slug,
                question=f"{self.asset.upper()} {self.interval} {side.upper()}",
                is_valid=book is not None and remaining > 0,
                external_price=external_price,
                strike_price=self._strike_price,
            )
            self.contexts[token_id] = ctx

        return self.contexts

    def _discover_window(self, window_ts: int):
        """Find the 5m market for a given timestamp."""
        slug = f"{self.asset}-updown-{self.interval}-{window_ts}"
        try:
            resp = self.session.get(
                f"{GAMMA_BASE}/events/slug/{slug}",
                timeout=5,
            )
            if resp.status_code != 200:
                # Try next window
                slug = f"{self.asset}-updown-{self.interval}-{window_ts + self.interval_seconds}"
                resp = self.session.get(f"{GAMMA_BASE}/events/slug/{slug}", timeout=5)
                if resp.status_code != 200:
                    return

            event = resp.json()
            markets = event.get("markets", [])
            if not markets:
                return

            m = markets[0]
            clob_raw = m.get("clobTokenIds", "[]")
            clob = json.loads(clob_raw) if isinstance(clob_raw, str) else (clob_raw or [])
            if len(clob) < 2:
                return

            end_str = m.get("endDate", "")
            try:
                self._end_time = datetime.fromisoformat(end_str.replace("Z", "+00:00")) if end_str else None
            except (ValueError, TypeError):
                self._end_time = datetime.fromtimestamp(window_ts + self.interval_seconds, tz=timezone.utc)

            self._current_slug = slug
            self._current_window_ts = window_ts
            self._up_token = clob[0]
            self._down_token = clob[1]

            # Set strike price from external feed
            if self.price_feed:
                try:
                    self._strike_price = self.price_feed()
                except Exception:
                    self._strike_price = 0.0

            # Clear old contexts
            self.contexts.clear()

            log.info("Rolling window: %s | strike=$%.2f | ends=%s",
                     slug, self._strike_price,
                     self._end_time.strftime("%H:%M:%S") if self._end_time else "?")

        except Exception as e:
            log.warning("Rolling discovery failed for %s: %s", slug, e)

    @property
    def up_token(self) -> str:
        return self._up_token

    @property
    def down_token(self) -> str:
        return self._down_token

    @property
    def strike(self) -> float:
        return self._strike_price

    @property
    def seconds_remaining(self) -> float:
        if self._end_time:
            return max((self._end_time - datetime.now(timezone.utc)).total_seconds(), 0)
        return 0
