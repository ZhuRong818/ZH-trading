"""
Historical Data Loader — fetches BTC kline data from Binance for backtesting.

Loads 1-second or 1-minute candles, converts to tick-level price series
that the skills-based strategies can consume.

Usage:
    loader = BinanceDataLoader()
    prices = loader.load_klines("BTCUSDT", "1m", days=7)
    # Returns list of floats (close prices at 1-minute intervals)
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import requests

log = logging.getLogger(__name__)

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


class BinanceDataLoader:

    def __init__(self):
        self.session = requests.Session()

    def load_klines(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "1m",
        days: int = 7,
        end_time: Optional[datetime] = None,
    ) -> List[dict]:
        """
        Load kline data from Binance.

        Returns list of dicts:
            {timestamp, open, high, low, close, volume}
        """
        if end_time is None:
            end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=days)

        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)

        all_klines = []
        current_start = start_ms

        while current_start < end_ms:
            try:
                resp = self.session.get(BINANCE_KLINES, params={
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": current_start,
                    "endTime": end_ms,
                    "limit": 1000,
                }, timeout=10)
                resp.raise_for_status()
                data = resp.json()

                if not data:
                    break

                for k in data:
                    all_klines.append({
                        "timestamp": k[0] / 1000,  # ms to seconds
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                    })

                current_start = data[-1][0] + 1  # next ms after last candle
                time.sleep(0.1)  # rate limit

            except Exception as e:
                log.warning("Kline fetch failed: %s", e)
                break

        log.info("Loaded %d klines for %s (%s, %d days)", len(all_klines), symbol, interval, days)
        return all_klines

    def klines_to_prices(self, klines: List[dict]) -> List[float]:
        """Extract close prices for strategy consumption."""
        return [k["close"] for k in klines]

    def klines_to_windows(self, klines: List[dict], window_minutes: int = 5) -> List[dict]:
        """
        Group klines into 5-minute windows for BTC 5m backtesting.

        Returns list of windows:
            {start_ts, end_ts, strike (open of window), end_price (close of window),
             outcome ("UP" or "DOWN"), prices (list of close prices within window)}
        """
        if not klines:
            return []

        windows = []
        window_seconds = window_minutes * 60
        current_window_start = None
        current_prices = []
        current_strike = 0

        for k in klines:
            ts = k["timestamp"]
            # Align to window boundaries
            window_start = ts - (ts % window_seconds)

            if current_window_start is None:
                current_window_start = window_start
                current_strike = k["open"]
                current_prices = [k["close"]]
            elif window_start == current_window_start:
                current_prices.append(k["close"])
            else:
                # Close previous window
                end_price = current_prices[-1]
                windows.append({
                    "start_ts": current_window_start,
                    "end_ts": current_window_start + window_seconds,
                    "strike": current_strike,
                    "end_price": end_price,
                    "outcome": "UP" if end_price >= current_strike else "DOWN",
                    "prices": current_prices,
                })
                # Start new window
                current_window_start = window_start
                current_strike = k["open"]
                current_prices = [k["close"]]

        # Close last window
        if current_prices and current_window_start is not None:
            end_price = current_prices[-1]
            windows.append({
                "start_ts": current_window_start,
                "end_ts": current_window_start + window_seconds,
                "strike": current_strike,
                "end_price": end_price,
                "outcome": "UP" if end_price >= current_strike else "DOWN",
                "prices": current_prices,
            })

        log.info("Created %d windows (%d-min) from %d klines", len(windows), window_minutes, len(klines))
        return windows
