"""
Polymarket Historical Data Loader — fetches real 5m market price data.

Loads actual Polymarket token prices for resolved BTC 5-minute windows.
Combined with Binance BTC prices, this gives the backtester real data
for both the signal source (BTC) and the trading venue (Polymarket).

Usage:
    loader = PolymarketHistoricalLoader()
    windows = loader.load_windows(hours=24)
    # Each window has real BTC prices + real Polymarket token prices
"""

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import requests

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


class PolymarketHistoricalLoader:

    def __init__(self):
        self.session = requests.Session()

    def load_windows(
        self,
        hours: int = 24,
        asset: str = "btc",
        interval: str = "5m",
        end_time: Optional[datetime] = None,
    ) -> List[dict]:
        """
        Load historical 5m windows with BOTH real BTC prices and real Polymarket prices.

        Returns list of windows:
        {
            slug, start_ts, end_ts, strike, end_price, outcome,
            btc_prices: [float],           # real BTC prices (1-min from Binance)
            up_prices: [{t, p}, ...],      # real Polymarket UP token prices
            down_prices: [{t, p}, ...],    # real Polymarket DOWN token prices
        }
        """
        if end_time is None:
            end_time = datetime.now(timezone.utc)

        interval_seconds = int(interval.replace("m", "")) * 60
        total_windows = (hours * 3600) // interval_seconds
        start_time = end_time - timedelta(hours=hours)

        # Step 1: Load BTC klines for the full period
        log.info("Loading %d hours of BTC data from Binance...", hours)
        btc_klines = self._load_btc_klines(start_time, end_time)
        btc_klines.sort(key=lambda k: k["open_time"])
        log.info("Loaded %d BTC klines", len(btc_klines))

        # Step 2: Discover and load each 5m window
        windows = []
        start_ts = int(start_time.timestamp())
        start_ts = start_ts - (start_ts % interval_seconds)  # align

        end_ts_epoch = int(end_time.timestamp())
        current_ts = start_ts
        loaded = 0
        failed = 0

        while current_ts < end_ts_epoch:
            slug = f"{asset}-updown-{interval}-{current_ts}"
            window = self._load_single_window(
                slug, current_ts, interval_seconds, btc_klines,
            )
            if window:
                windows.append(window)
                loaded += 1
            else:
                failed += 1

            current_ts += interval_seconds

            # Rate limit: don't hammer the APIs
            if (loaded + failed) % 20 == 0:
                time.sleep(0.5)

        log.info(
            "Loaded %d windows (%d failed) over %d hours",
            loaded, failed, hours,
        )
        return windows

    def _load_single_window(
        self, slug: str, window_ts: int, interval_seconds: int,
        btc_klines: List[dict],
    ) -> Optional[dict]:
        """Load one 5m window: discover tokens, fetch prices."""
        try:
            # Discover market
            resp = self.session.get(
                f"{GAMMA_BASE}/events/slug/{slug}", timeout=10,
            )
            if resp.status_code != 200:
                return None

            event = resp.json()
            markets = event.get("markets", [])
            if not markets:
                return None

            m = markets[0]
            clob_raw = m.get("clobTokenIds", "[]")
            clob = json.loads(clob_raw) if isinstance(clob_raw, str) else (clob_raw or [])
            if len(clob) < 2:
                return None

            up_token = clob[0]
            down_token = clob[1]

            # Fetch real Polymarket price history
            resp2 = self.session.post(
                f"{CLOB_BASE}/batch-prices-history",
                json={
                    "markets": [up_token, down_token],
                    "start_ts": window_ts,
                    "end_ts": window_ts + interval_seconds,
                    "fidelity": 1,
                },
                timeout=10,
            )
            if resp2.status_code != 200:
                return None

            history = resp2.json().get("history", {})
            up_prices = history.get(up_token, [])
            down_prices = history.get(down_token, [])

            if not up_prices:
                return None

            price_points = self._window_price_points(btc_klines, window_ts, window_ts + interval_seconds)
            if len(price_points) < 2:
                return None

            strike = price_points[0]["p"]
            end_btc = price_points[-1]["p"]
            outcome = "UP" if end_btc >= strike else "DOWN"

            return {
                "slug": slug,
                "start_ts": window_ts,
                "end_ts": window_ts + interval_seconds,
                "strike": strike,
                "end_price": end_btc,
                "outcome": outcome,
                "prices": [point["p"] for point in price_points],  # BTC prices known by each point time
                "price_points": price_points,
                "up_token": up_token,
                "down_token": down_token,
                "up_prices": up_prices,    # real Polymarket prices
                "down_prices": down_prices,
            }

        except Exception as e:
            log.debug("Failed to load window %s: %s", slug, e)
            return None

    @staticmethod
    def _window_price_points(btc_klines: List[dict], start_ts: int, end_ts: int) -> List[dict]:
        """Build BTC points using only prices known by each timestamp."""
        start_kline = next((k for k in btc_klines if int(k["open_time"]) == start_ts), None)
        if start_kline:
            strike = start_kline["open"]
        else:
            known_before_start = [k for k in btc_klines if int(k["close_time"]) <= start_ts]
            if not known_before_start:
                return []
            strike = known_before_start[-1]["close"]

        points = [{"t": start_ts, "p": strike}]
        for k in btc_klines:
            close_ts = int(k["close_time"])
            if start_ts < close_ts <= end_ts:
                points.append({"t": close_ts, "p": k["close"]})

        return points

    def _load_btc_klines(self, start: datetime, end: datetime) -> List[dict]:
        """Load BTC 1-minute klines from Binance."""
        start_ms = int((start - timedelta(minutes=1)).timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        all_klines = []
        current = start_ms

        while current < end_ms:
            try:
                resp = self.session.get(BINANCE_KLINES, params={
                    "symbol": "BTCUSDT",
                    "interval": "1m",
                    "startTime": current,
                    "endTime": end_ms,
                    "limit": 1000,
                }, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                if not data:
                    break

                for k in data:
                    all_klines.append({
                        "open_time": k[0] / 1000,
                        "close_time": (k[6] + 1) / 1000,
                        "open": float(k[1]),
                        "close": float(k[4]),
                    })

                current = data[-1][0] + 1
                time.sleep(0.1)
            except Exception as e:
                log.warning("BTC kline fetch failed: %s", e)
                break

        return all_klines
