"""
Snapshot Collector — periodically captures market and strategy state.

Records MarketSnapshots and StrategySnapshots for post-session
regime analysis. Runs on a configurable interval (default 30s).
"""

import logging
import time
from typing import Dict, List, Optional

from analytics.models import MarketSnapshot, StrategySnapshot
from data_pipeline.market_data import MarketDataFeed

log = logging.getLogger(__name__)


class SnapshotCollector:

    def __init__(self, data_feed: MarketDataFeed, interval: float = 30.0):
        self.data = data_feed
        self.interval = interval
        self._last_collect = 0.0
        self.market_snapshots: List[MarketSnapshot] = []
        self.strategy_snapshots: List[StrategySnapshot] = []
        self._token_ids: List[str] = []
        self._strategies: List = []  # objects with snapshot() method

    def register_token(self, token_id: str):
        if token_id not in self._token_ids:
            self._token_ids.append(token_id)

    def register_strategy(self, strategy_obj, name: str):
        self._strategies.append((name, strategy_obj))

    def collect_if_due(self):
        """Collect snapshots if interval has elapsed."""
        now = time.time()
        if now - self._last_collect < self.interval:
            return
        self._last_collect = now
        self._collect_market_snapshots()
        self._collect_strategy_snapshots()

    def _collect_market_snapshots(self):
        for token_id in self._token_ids:
            book = self.data.get_book(token_id)
            if not book or book.mid is None:
                continue
            snap = MarketSnapshot(
                timestamp=time.time(),
                token_id=token_id,
                mid=book.mid,
                spread=book.spread or 0,
                best_bid=book.best_bid or 0,
                best_ask=book.best_ask or 0,
                bid_depth_5=book.depth("BUY", 5),
                ask_depth_5=book.depth("SELL", 5),
                volatility=self.data.rolling_volatility(token_id),
                regime=MarketDataFeed.classify_regime(book.mid),
            )
            self.market_snapshots.append(snap)

    def _collect_strategy_snapshots(self):
        for name, obj in self._strategies:
            if hasattr(obj, "snapshot"):
                try:
                    state = obj.snapshot()
                except Exception:
                    state = {}
            elif hasattr(obj, "status"):
                try:
                    state = obj.status()
                except Exception:
                    state = {}
            else:
                state = {}

            self.strategy_snapshots.append(StrategySnapshot(
                timestamp=time.time(),
                strategy=name,
                state=state,
            ))

    def get_regime_distribution(self) -> dict:
        """What % of time was each regime observed."""
        if not self.market_snapshots:
            return {}
        counts = {}
        for s in self.market_snapshots:
            counts[s.regime] = counts.get(s.regime, 0) + 1
        total = len(self.market_snapshots)
        return {k: round(v / total * 100, 1) for k, v in counts.items()}
