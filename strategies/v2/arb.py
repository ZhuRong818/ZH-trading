"""
Combinatorial Arbitrage — unified interface (v2).

Input:  List[MarketContext] (ignored — arb scans event slugs)
Output: List[TradingSignal] (FOK legs for sum-to-one violations)
"""

import json
import logging
import time
from typing import List, Optional

import requests

from strategies.base import BaseStrategy
from data_pipeline.market_provider import MarketContext
from pipeline.signal import TradingSignal
from config import GAMMA_BASE

log = logging.getLogger(__name__)


class Arbitrage(BaseStrategy):
    name = "arb"

    def __init__(self, event_slugs: List[str], scan_interval: float = 30.0):
        self.event_slugs = event_slugs
        self.scan_interval = scan_interval
        self.session = requests.Session()
        self._last_scan = 0.0
        self.total_opportunities = 0

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        now = time.time()
        if now - self._last_scan < self.scan_interval:
            return []
        self._last_scan = now

        signals = []
        for slug in self.event_slugs:
            s = self._scan_sum_to_one(slug)
            signals.extend(s)
        return signals

    def _scan_sum_to_one(self, slug: str) -> List[TradingSignal]:
        try:
            resp = self.session.get(
                f"{GAMMA_BASE}/markets",
                params={"slug": slug, "active": True, "closed": False, "_limit": 50},
                timeout=10,
            )
            resp.raise_for_status()
            markets = resp.json()

            if len(markets) < 2:
                return []

            prices = []
            token_ids = []
            for m in markets:
                op = m.get("outcomePrices", "[]")
                op = json.loads(op) if isinstance(op, str) else (op or [])
                clob = m.get("clobTokenIds", "[]")
                clob = json.loads(clob) if isinstance(clob, str) else (clob or [])
                if op and clob:
                    prices.append(float(op[0]))
                    token_ids.append(clob[0])

            total = sum(prices)
            if total <= 1.02:
                return []

            excess = total - 1.0
            profit_est = excess * 10.0

            if profit_est < 0.50:
                return []

            self.total_opportunities += 1
            log.info("ARB [sum_to_one]: %d markets sum=%.4f excess=%.4f profit=$%.2f",
                     len(prices), total, excess, profit_est)

            signals = []
            for tid in token_ids:
                if tid:
                    signals.append(TradingSignal(
                        token_id=tid, side="SELL", price=0.0, size=10.0,
                        strategy=f"arb_sum_to_one",
                        order_type="FOK", edge=excess / len(token_ids),
                    ))
            return signals

        except Exception as e:
            log.warning("Arb scan failed for %s: %s", slug, e)
            return []

    def snapshot(self) -> dict:
        return {
            "event_slugs": self.event_slugs,
            "total_opportunities": self.total_opportunities,
        }
