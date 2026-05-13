"""
Live Data Recorder — records real-time BTC/ETH/SOL/XRP price + Polymarket data
for future backtesting with real data.

Saves every 1 second:
- Asset price from Binance
- Polymarket UP/DOWN token prices from CLOB /midpoint and /price endpoints
- Book depth from CLOB /book (top 5 levels)
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

            # Fetch real Polymarket prices for UP and DOWN tokens
            up_data = self._fetch_token_data(window["up_token"])
            down_data = self._fetch_token_data(window["down_token"])

            record = {
                "ts": round(now, 3),
                "time": ts_iso,
                "asset": asset,
                "slug": window["slug"],
                "strike": window["strike"],
                "price": price,
                "up_token": window["up_token"][:20],
                "up_mid": up_data.get("mid", 0),
                "up_buy": up_data.get("buy", 0),
                "up_sell": up_data.get("sell", 0),
                "up_spread": up_data.get("spread", 0),
                "up_bid_depth": up_data.get("bid_depth", 0),
                "up_ask_depth": up_data.get("ask_depth", 0),
                "down_token": window["down_token"][:20],
                "down_mid": down_data.get("mid", 0),
                "down_buy": down_data.get("buy", 0),
                "down_sell": down_data.get("sell", 0),
                "down_spread": down_data.get("spread", 0),
                "down_bid_depth": down_data.get("bid_depth", 0),
                "down_ask_depth": down_data.get("ask_depth", 0),
                "window_end_ts": window["end_ts"],
                "seconds_remaining": max(0, window["end_ts"] - now),
            }

            self._write_record(record)
            self._tick_count += 1

            if self._tick_count % 60 == 0:
                remaining = max(0, window["end_ts"] - now)
                log.info(
                    "[%s] tick=%d price=$%.2f up_mid=%.3f(%.3f/%.3f) down_mid=%.3f(%.3f/%.3f) remain=%.0fs",
                    asset, self._tick_count, price,
                    up_data.get("mid", 0), up_data.get("buy", 0), up_data.get("sell", 0),
                    down_data.get("mid", 0), down_data.get("buy", 0), down_data.get("sell", 0),
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

    def _fetch_token_data(self, token_id: str) -> dict:
        """Fetch real prices from CLOB /midpoint + /price + /book endpoints."""
        result = {}

        # 1. Midpoint — the real mid price
        try:
            resp = self.session.get(
                f"{CLOB_BASE}/midpoint",
                params={"token_id": token_id},
                timeout=3,
            )
            if resp.status_code == 200:
                result["mid"] = float(resp.json().get("mid", 0))
        except Exception:
            pass

        # 2. Buy/sell prices — what you'd actually pay/receive
        try:
            resp = self.session.get(
                f"{CLOB_BASE}/price",
                params={"token_id": token_id, "side": "buy"},
                timeout=3,
            )
            if resp.status_code == 200:
                result["buy"] = float(resp.json().get("price", 0))
        except Exception:
            pass

        try:
            resp = self.session.get(
                f"{CLOB_BASE}/price",
                params={"token_id": token_id, "side": "sell"},
                timeout=3,
            )
            if resp.status_code == 200:
                result["sell"] = float(resp.json().get("price", 0))
        except Exception:
            pass

        # Compute spread: sell (best ask) - buy (best bid)
        buy = result.get("buy", 0)
        sell = result.get("sell", 0)
        if buy and sell:
            result["spread"] = sell - buy

        # 3. Book depth (top 5 levels)
        try:
            resp = self.session.get(
                f"{CLOB_BASE}/book",
                params={"token_id": token_id},
                timeout=3,
            )
            if resp.status_code == 200:
                data = resp.json()
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                result["bid_depth"] = sum(float(b["size"]) for b in bids[:5])
                result["ask_depth"] = sum(float(a["size"]) for a in asks[:5])
        except Exception:
            pass

        return result

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
