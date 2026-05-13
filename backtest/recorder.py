"""
Live Data Recorder — records real-time BTC/ETH price + Polymarket book data
for future backtesting with real data.

Saves every 1 second:
- BTC/ETH price from Binance
- Polymarket UP/DOWN token best_bid, best_ask, mid from CLOB API
- Window metadata (slug, strike, tokens)

Output: JSONL files in data/ directory, one per day.

Usage:
    python -m backtest.recorder                          # BTC only
    python -m backtest.recorder --assets btc,eth         # BTC + ETH
    python -m backtest.recorder --interval 0.5           # 500ms polling
    caffeinate -i python -m backtest.recorder             # keep Mac awake
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)

BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


class LiveRecorder:

    def __init__(self, assets: list = None, interval: float = 1.0, data_dir: str = "data"):
        self.assets = assets or ["btc"]
        self.interval = interval
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.session = requests.Session()
        self.running = False

        # Current window state per asset
        self._windows: Dict[str, dict] = {}  # asset -> {slug, up_token, down_token, strike, end_ts}
        self._file = None
        self._current_date = None
        self._tick_count = 0

    def run(self):
        self.running = True
        log.info("Recording started: assets=%s interval=%.1fs", self.assets, self.interval)

        while self.running:
            try:
                self._tick()
                time.sleep(self.interval)
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.warning("Tick error: %s", e)
                time.sleep(1)

        self._close_file()
        log.info("Recording stopped. %d ticks saved.", self._tick_count)

    def _tick(self):
        """Record one data point for all assets."""
        now = time.time()
        ts_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()

        for asset in self.assets:
            # Check/discover current window
            self._ensure_window(asset)
            window = self._windows.get(asset)
            if not window:
                continue

            # Fetch BTC/ETH price
            price = self._fetch_price(asset)
            if price is None:
                continue

            # Fetch Polymarket book for UP and DOWN tokens
            up_book = self._fetch_book(window["up_token"])
            down_book = self._fetch_book(window["down_token"])

            record = {
                "ts": round(now, 3),
                "time": ts_iso,
                "asset": asset,
                "slug": window["slug"],
                "strike": window["strike"],
                "btc_price": price,
                "up_token": window["up_token"][:20],
                "up_best_bid": up_book.get("best_bid", 0),
                "up_best_ask": up_book.get("best_ask", 0),
                "up_mid": up_book.get("mid", 0),
                "up_spread": up_book.get("spread", 0),
                "up_bid_depth": up_book.get("bid_depth", 0),
                "up_ask_depth": up_book.get("ask_depth", 0),
                "down_token": window["down_token"][:20],
                "down_best_bid": down_book.get("best_bid", 0),
                "down_best_ask": down_book.get("best_ask", 0),
                "down_mid": down_book.get("mid", 0),
                "down_spread": down_book.get("spread", 0),
                "down_bid_depth": down_book.get("bid_depth", 0),
                "down_ask_depth": down_book.get("ask_depth", 0),
                "window_end_ts": window["end_ts"],
                "seconds_remaining": max(0, window["end_ts"] - now),
            }

            self._write_record(record)
            self._tick_count += 1

            if self._tick_count % 60 == 0:
                remaining = max(0, window["end_ts"] - now)
                log.info(
                    "[%s] tick=%d price=$%.2f up=%.3f/%.3f down=%.3f/%.3f remain=%.0fs",
                    asset, self._tick_count, price,
                    up_book.get("best_bid", 0), up_book.get("best_ask", 0),
                    down_book.get("best_bid", 0), down_book.get("best_ask", 0),
                    remaining,
                )

    def _ensure_window(self, asset: str):
        """Discover or refresh the current 5m window."""
        now = int(time.time())
        window_ts = now - (now % 300)
        current = self._windows.get(asset)

        if current and current.get("window_ts") == window_ts:
            return  # same window

        slug = f"{asset}-updown-5m-{window_ts}"
        try:
            resp = self.session.get(f"{GAMMA_BASE}/events/slug/{slug}", timeout=5)
            if resp.status_code != 200:
                # Try next window
                slug = f"{asset}-updown-5m-{window_ts + 300}"
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
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                end_ts = end_dt.timestamp()
            except (ValueError, TypeError):
                end_ts = window_ts + 300

            # Get strike from current price
            price = self._fetch_price(asset)
            strike = price or 0

            self._windows[asset] = {
                "slug": slug,
                "window_ts": window_ts,
                "up_token": clob[0],
                "down_token": clob[1],
                "strike": strike,
                "end_ts": end_ts,
            }
            log.info("Window: %s strike=$%.2f", slug, strike)

        except Exception as e:
            log.warning("Window discovery failed for %s: %s", asset, e)

    def _fetch_price(self, asset: str) -> Optional[float]:
        try:
            symbol = f"{asset.upper()}USDT"
            resp = self.session.get(BINANCE_TICKER, params={"symbol": symbol}, timeout=3)
            return float(resp.json()["price"])
        except Exception:
            return None

    def _fetch_book(self, token_id: str) -> dict:
        try:
            resp = self.session.get(
                f"{CLOB_BASE}/book",
                params={"token_id": token_id},
                timeout=3,
            )
            if resp.status_code != 200:
                return {}

            data = resp.json()
            bids = data.get("bids", [])
            asks = data.get("asks", [])

            best_bid = float(bids[0]["price"]) if bids else 0
            best_ask = float(asks[0]["price"]) if asks else 0
            mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0
            spread = best_ask - best_bid if best_bid and best_ask else 0

            bid_depth = sum(float(b["size"]) for b in bids[:5])
            ask_depth = sum(float(a["size"]) for a in asks[:5])

            return {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "mid": mid,
                "spread": spread,
                "bid_depth": bid_depth,
                "ask_depth": ask_depth,
            }
        except Exception:
            return {}

    def _write_record(self, record: dict):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._current_date:
            self._close_file()
            self._current_date = today
            path = os.path.join(self.data_dir, f"market_data_{today}.jsonl")
            self._file = open(path, "a")
            log.info("Writing to: %s", path)

        self._file.write(json.dumps(record) + "\n")
        self._file.flush()

    def _close_file(self):
        if self._file:
            self._file.close()
            self._file = None


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Record live Polymarket + Binance data")
    parser.add_argument("--assets", type=str, default="btc",
                        help="Assets to record: btc, eth, or btc,eth")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="Seconds between ticks (default: 1.0)")
    parser.add_argument("--data-dir", type=str, default="data",
                        help="Output directory (default: data)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    assets = [a.strip() for a in args.assets.split(",")]
    recorder = LiveRecorder(assets=assets, interval=args.interval, data_dir=args.data_dir)
    signal.signal(signal.SIGINT, lambda *_: setattr(recorder, 'running', False))
    recorder.run()


if __name__ == "__main__":
    main()
