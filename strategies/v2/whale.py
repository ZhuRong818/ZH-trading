"""
Whale Copy Trading — unified interface (v2).

Input:  List[MarketContext] (ignored — whale watches leaderboard globally)
Output: List[TradingSignal] (copy trades from top traders)
"""

import logging
import time
from typing import Dict, List, Optional

import requests

from strategies.base import BaseStrategy
from data_pipeline.market_provider import MarketContext
from pipeline.signal import TradingSignal
from config import DATA_API_BASE, WhaleTrackingConfig

log = logging.getLogger(__name__)


class WhaleCopy(BaseStrategy):
    name = "whale_copy"

    def __init__(self, config: WhaleTrackingConfig):
        self.config = config
        self.session = requests.Session()
        self._whales: Dict[str, dict] = {}  # wallet -> {username, pnl, win_rate, trust}
        self._known_positions: Dict[str, Dict[str, float]] = {}  # wallet -> {token: size}
        self._last_refresh = 0.0
        self.total_signals = 0

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        now = time.time()

        # Refresh leaderboard every 10 min
        if now - self._last_refresh > 600:
            self._refresh_whales()
            self._last_refresh = now

        signals = []
        for wallet, whale in self._whales.items():
            if whale.get("trust", 0) < 0.5:
                continue
            new_signals = self._check_whale(wallet, whale)
            signals.extend(new_signals)

        return signals

    def _refresh_whales(self):
        try:
            for period in ["MONTH", "ALL"]:
                resp = self.session.get(
                    f"{DATA_API_BASE}/v1/leaderboard",
                    params={"orderBy": "PNL", "timePeriod": period,
                            "limit": self.config.top_n_traders},
                    timeout=10,
                )
                resp.raise_for_status()
                for entry in resp.json():
                    wallet = entry.get("proxyWallet", "")
                    if not wallet:
                        continue
                    self._whales[wallet] = {
                        "username": entry.get("userName", ""),
                        "pnl": float(entry.get("pnl", 0)),
                        "vol": float(entry.get("vol", 0)),
                        "win_rate": 0.8,  # approximate, enrichment is slow
                        "trust": 0.7,
                    }
            log.info("Whale registry: %d whales tracked", len(self._whales))
        except Exception as e:
            log.warning("Whale refresh failed: %s", e)

    def _check_whale(self, wallet: str, whale: dict) -> List[TradingSignal]:
        signals = []
        try:
            resp = self.session.get(
                f"{DATA_API_BASE}/positions",
                params={"user": wallet, "sizeThreshold": 1, "limit": 50},
                timeout=10,
            )
            resp.raise_for_status()
            positions = resp.json()

            old = self._known_positions.get(wallet, {})
            new = {}

            for pos in positions:
                tid = pos.get("asset", "")
                size = float(pos.get("size", 0))
                if not tid or size <= 0:
                    continue
                new[tid] = size

                delta = size - old.get(tid, 0)
                cur_price = float(pos.get("curPrice", 0.5))

                # New conviction move: delta > 0 and notional > $100
                if delta > 0 and delta * cur_price >= 100:
                    if cur_price > 0.90 or cur_price < 0.10:
                        continue  # tail zone, skip

                    if whale.get("win_rate", 0) < self.config.high_confidence_win_rate:
                        continue

                    copy_size = min(
                        delta * self.config.copy_fraction,
                        self.config.max_copy_size_usdc / cur_price if cur_price > 0 else 0,
                    )
                    if copy_size < 1:
                        continue

                    self.total_signals += 1
                    username = whale.get("username", wallet[:10])

                    log.info("WHALE SIGNAL: %s (WR=%.0f%%) BUY %.0f @ %.4f | %s",
                             username, whale.get("win_rate", 0) * 100,
                             copy_size, cur_price, pos.get("title", "")[:50])

                    signals.append(TradingSignal(
                        token_id=tid,
                        side="BUY",
                        price=cur_price,
                        size=copy_size,
                        strategy=f"whale_copy_{username}",
                        confidence=whale.get("win_rate", 0),
                    ))

            self._known_positions[wallet] = new

        except Exception as e:
            log.debug("Whale check failed for %s: %s", wallet[:10], e)

        return signals

    def snapshot(self) -> dict:
        return {
            "whales_tracked": len(self._whales),
            "total_signals": self.total_signals,
        }
