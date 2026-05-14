"""
Settlement Oracle — provides ground-truth settlement prices.

For Polymarket BTC/ETH 5m rolling markets, the actual resolution oracle is
Chainlink on Polygon.  We use Binance 1-minute klines (close price of the
candle aligned to the window end timestamp) as a high-accuracy proxy.

This module is **system-wide**: every strategy and the runner use it for
settlement decisions instead of ad-hoc streaming price comparisons.

Usage:
    oracle = SettlementOracle()
    outcome = oracle.determine_outcome("btc", strike=80000, end_ts=1778174400)
    # → "UP" or "DOWN"
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

# Known Chainlink price-feed proxy pairs on Binance
ASSET_SYMBOLS = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
}


class SettlementOracle:
    """
    Provides the ground-truth settlement price for a given asset and time.

    Queries Binance 1-minute klines to find the candle whose close time
    covers the window end timestamp, then uses its CLOSE price.  This is
    far more accurate than a streaming price that may lag the window end.
    """

    def __init__(self, max_retries: int = 4, retry_delay: float = 2.0):
        self._session = requests.Session()
        self._cache: dict[tuple, float] = {}
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_settlement_price(
        self,
        asset: str = "btc",
        end_timestamp: int = 0,
    ) -> Optional[float]:
        """
        Return the oracle settlement price at *end_timestamp* (unix s).

        Uses the CLOSE of the 1‑minute Binance kline whose close_time ≥
        end_timestamp.  Caches results per (asset, minute).
        """
        if end_timestamp <= 0:
            end_timestamp = int(time.time())

        cache_key = (asset, end_timestamp // 60)
        if cache_key in self._cache:
            return self._cache[cache_key]

        symbol = ASSET_SYMBOLS.get(asset, f"{asset.upper()}USDT")

        # Query klines from 2 min before until 1 min after the target ts
        start_ms = (end_timestamp - 120) * 1000
        end_ms   = (end_timestamp + 60) * 1000

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._session.get(
                    BINANCE_KLINES,
                    params={
                        "symbol": symbol,
                        "interval": "1m",
                        "startTime": start_ms,
                        "endTime": end_ms,
                        "limit": 5,
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                candles = resp.json()

                if not candles:
                    log.warning(
                        "Oracle: no kline data for %s at %s (attempt %d/%d)",
                        symbol,
                        datetime.fromtimestamp(end_timestamp, tz=timezone.utc),
                        attempt,
                        self.max_retries,
                    )
                    if attempt < self.max_retries:
                        time.sleep(self.retry_delay)
                    continue

                # Find the candle whose close_time >= end_timestamp * 1000
                # Kline: [open_time, open, high, low, close, volume, close_time, ...]
                for candle in candles:
                    close_time_ms = candle[6]
                    if close_time_ms >= end_timestamp * 1000:
                        close_price = float(candle[4])
                        self._cache[cache_key] = close_price
                        log.info(
                            "Oracle: %s settlement @ %s = $%.2f",
                            symbol,
                            datetime.fromtimestamp(end_timestamp, tz=timezone.utc),
                            close_price,
                        )
                        return close_price

                # Fallback: use the last candle's close
                close_price = float(candles[-1][4])
                self._cache[cache_key] = close_price
                log.info(
                    "Oracle: %s fallback settlement = $%.2f (no exact candle)",
                    symbol,
                    close_price,
                )
                return close_price

            except Exception as e:
                log.warning(
                    "Oracle: fetch error %d/%d: %s", attempt, self.max_retries, e,
                )
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay)

        return None

    def get_window_start_price(
        self,
        asset: str = "btc",
        start_timestamp: int = 0,
    ) -> Optional[float]:
        """
        Return the price-to-beat proxy for a rolling window.

        Polymarket resolves crypto up/down windows against the Chainlink data
        stream at the beginning and end of the range. We use the Binance 1m
        candle open aligned to the window start as the local proxy, rather than
        the bot's discovery-time spot price.
        """
        if start_timestamp <= 0:
            return None

        cache_key = (asset, start_timestamp // 60, "open")
        if cache_key in self._cache:
            return self._cache[cache_key]

        symbol = ASSET_SYMBOLS.get(asset, f"{asset.upper()}USDT")
        start_ms = start_timestamp * 1000
        end_ms = (start_timestamp + 60) * 1000

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._session.get(
                    BINANCE_KLINES,
                    params={
                        "symbol": symbol,
                        "interval": "1m",
                        "startTime": start_ms,
                        "endTime": end_ms,
                        "limit": 1,
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                candles = resp.json()
                if candles:
                    open_price = float(candles[0][1])
                    self._cache[cache_key] = open_price
                    log.info(
                        "Oracle: %s window start @ %s = $%.2f",
                        symbol,
                        datetime.fromtimestamp(start_timestamp, tz=timezone.utc),
                        open_price,
                    )
                    return open_price

                log.warning(
                    "Oracle: no start kline for %s at %s (attempt %d/%d)",
                    symbol,
                    datetime.fromtimestamp(start_timestamp, tz=timezone.utc),
                    attempt,
                    self.max_retries,
                )
            except Exception as e:
                log.warning("Oracle: start-price fetch error %d/%d: %s", attempt, self.max_retries, e)

            if attempt < self.max_retries:
                time.sleep(self.retry_delay)

        return None

    def determine_outcome(
        self,
        asset: str = "btc",
        strike: float = 0.0,
        end_timestamp: int = 0,
    ) -> Optional[str]:
        """
        Determine the binary outcome: 'UP' or 'DOWN'.

        Returns None if the settlement price cannot be determined.
        """
        if strike <= 0:
            log.warning("Oracle: cannot determine outcome — strike=%.2f", strike)
            return None

        end_price = self.get_settlement_price(asset, end_timestamp)
        if end_price is None or end_price <= 0:
            log.warning(
                "Oracle: cannot determine outcome — no price for %s @ ts=%d",
                asset, end_timestamp,
            )
            return None

        outcome = "UP" if end_price >= strike else "DOWN"
        log.info(
            "Oracle: outcome=%s | end=$%.2f  strike=$%.2f  delta=$%.2f",
            outcome, end_price, strike, end_price - strike,
        )
        return outcome

    def clear_cache(self) -> None:
        """Clear the per-minute price cache."""
        self._cache.clear()
