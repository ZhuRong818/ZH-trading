"""
Live Data Recorder — records real-time BTC/ETH/SOL/XRP price + Polymarket data
for future backtesting with real data.

All API calls are async (aiohttp) so each tick fetches all data in parallel,
achieving sub-second tick rates even with 9 API calls per asset.

Saves every tick:
- Asset price from Binance
- Polymarket UP/DOWN token prices from CLOB /midpoint and /price endpoints
- Book depth from CLOB /book (top 5 levels)
- Window metadata (slug, strike, tokens)

Output: JSONL files in data/ directory, one per day.

Usage:
    python -m backtest.recorder                          # BTC only
    python -m backtest.recorder --assets btc,eth         # BTC + ETH
    python -m backtest.recorder --assets btc,eth,sol,xrp --interval 0.1
    caffeinate -i python -m backtest.recorder             # keep Mac awake
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import aiohttp

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
        self.running = False

        # Current window state per asset
        self._windows: Dict[str, dict] = {}
        self._file = None
        self._current_date = None
        self._tick_count = 0

    async def run(self):
        self.running = True
        log.info("Recording started (async): assets=%s interval=%.2fs", self.assets, self.interval)

        async with aiohttp.ClientSession() as session:
            self._session = session
            while self.running:
                try:
                    t0 = time.time()
                    await self._tick()
                    elapsed = time.time() - t0
                    sleep_time = max(0, self.interval - elapsed)
                    if sleep_time > 0:
                        await asyncio.sleep(sleep_time)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log.warning("Tick error: %s", e)
                    await asyncio.sleep(1)

        self._close_file()
        log.info("Recording stopped. %d ticks saved.", self._tick_count)

    async def _tick(self):
        """Record one data point for all assets in parallel."""
        now = time.time()
        ts_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()

        # Ensure windows for all assets (sequential, only on window change)
        for asset in self.assets:
            await self._ensure_window(asset)

        # Build list of assets with valid windows
        active = [(a, self._windows[a]) for a in self.assets if a in self._windows]
        if not active:
            return

        # Fetch ALL data in parallel: price + up_data + down_data for each asset
        tasks = []
        for asset, window in active:
            tasks.append(self._fetch_price(asset))
            tasks.append(self._fetch_token_data(window["up_token"]))
            tasks.append(self._fetch_token_data(window["down_token"]))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results (3 per asset: price, up_data, down_data)
        for i, (asset, window) in enumerate(active):
            price = results[i * 3]
            up_data = results[i * 3 + 1]
            down_data = results[i * 3 + 2]

            if isinstance(price, Exception) or price is None:
                continue
            if isinstance(up_data, Exception):
                up_data = {}
            if isinstance(down_data, Exception):
                down_data = {}

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

    async def _ensure_window(self, asset: str):
        """Discover or refresh the current 5m window."""
        now = int(time.time())
        window_ts = now - (now % 300)
        current = self._windows.get(asset)

        if current and current.get("window_ts") == window_ts:
            return

        slug = f"{asset}-updown-5m-{window_ts}"
        try:
            async with self._session.get(
                f"{GAMMA_BASE}/events/slug/{slug}", timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    slug = f"{asset}-updown-5m-{window_ts + 300}"
                    async with self._session.get(
                        f"{GAMMA_BASE}/events/slug/{slug}", timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp2:
                        if resp2.status != 200:
                            return
                        event = await resp2.json()
                else:
                    event = await resp.json()

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

            price = await self._fetch_price(asset)
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

    async def _fetch_price(self, asset: str) -> Optional[float]:
        try:
            async with self._session.get(
                BINANCE_TICKER,
                params={"symbol": f"{asset.upper()}USDT"},
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                data = await resp.json()
                return float(data["price"])
        except Exception:
            return None

    async def _fetch_token_data(self, token_id: str) -> dict:
        """Fetch midpoint + buy/sell prices + book depth in parallel."""
        timeout = aiohttp.ClientTimeout(total=3)

        async def get_midpoint():
            try:
                async with self._session.get(
                    f"{CLOB_BASE}/midpoint",
                    params={"token_id": token_id},
                    timeout=timeout,
                ) as resp:
                    if resp.status == 200:
                        return float((await resp.json()).get("mid", 0))
            except Exception:
                pass
            return 0

        async def get_price(side):
            try:
                async with self._session.get(
                    f"{CLOB_BASE}/price",
                    params={"token_id": token_id, "side": side},
                    timeout=timeout,
                ) as resp:
                    if resp.status == 200:
                        return float((await resp.json()).get("price", 0))
            except Exception:
                pass
            return 0

        async def get_book_depth():
            try:
                async with self._session.get(
                    f"{CLOB_BASE}/book",
                    params={"token_id": token_id},
                    timeout=timeout,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        bids = data.get("bids", [])
                        asks = data.get("asks", [])
                        return (
                            sum(float(b["size"]) for b in bids[:5]),
                            sum(float(a["size"]) for a in asks[:5]),
                        )
            except Exception:
                pass
            return (0, 0)

        # All 4 calls in parallel
        mid, buy, sell, (bid_depth, ask_depth) = await asyncio.gather(
            get_midpoint(), get_price("buy"), get_price("sell"), get_book_depth()
        )

        result = {
            "mid": mid,
            "buy": buy,
            "sell": sell,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
        }
        if buy and sell:
            result["spread"] = sell - buy

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
                        help="Assets to record: btc, eth, or btc,eth,sol,xrp")
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

    loop = asyncio.new_event_loop()
    loop.add_signal_handler(signal.SIGINT, lambda: setattr(recorder, 'running', False))
    loop.add_signal_handler(signal.SIGTERM, lambda: setattr(recorder, 'running', False))
    try:
        loop.run_until_complete(recorder.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
