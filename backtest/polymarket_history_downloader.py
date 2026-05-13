"""
Download historical Polymarket CLOB token price history for rolling markets.

This complements backtest.recorder:
- recorder.py records live orderbook snapshots going forward
- this script fetches public historical token price history from the CLOB API

Important limitation:
Polymarket's public prices-history endpoints expose historical token prices with
fidelity expressed in minutes. They do not provide historical millisecond-level
orderbook snapshots with bid/ask/depth. For execution-realistic backtests, keep
using recorder.py for forward collection.

Examples:
    python -m backtest.polymarket_history_downloader --asset btc --start 2026-05-01T00:00:00 --end 2026-05-01T01:00:00
    python -m backtest.polymarket_history_downloader --asset eth --start 2026-05-01 --end 2026-05-02 --format jsonl
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import requests

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


def parse_time(value: str) -> datetime:
    if "T" not in value and len(value) == 10:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def interval_seconds(interval: str) -> int:
    if not interval.endswith("m"):
        raise ValueError("Only minute rolling intervals are supported, e.g. 5m")
    return int(interval[:-1]) * 60


def aligned_windows(start_ts: int, end_ts: int, step: int) -> Iterable[int]:
    current = start_ts - (start_ts % step)
    while current < end_ts:
        yield current
        current += step


class PolymarketHistoryDownloader:
    def __init__(
        self,
        asset: str = "btc",
        interval: str = "5m",
        out_dir: str = "data/polymarket_history",
        fidelity: int = 1,
        request_sleep: float = 0.2,
    ):
        self.asset = asset.lower()
        self.interval = interval
        self.out_dir = Path(out_dir)
        self.fidelity = fidelity
        self.request_sleep = request_sleep
        self.session = requests.Session()

    def download_range(
        self,
        start: datetime,
        end: datetime,
        output_format: str = "csv",
        include_empty_windows: bool = False,
        include_outside_window: bool = False,
    ) -> tuple[int, int]:
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        step = interval_seconds(self.interval)
        windows = []

        for window_ts in aligned_windows(start_ts, end_ts, step):
            window = self._load_window_metadata(window_ts, step)
            if window:
                windows.append(window)
            elif include_empty_windows:
                windows.append(
                    {
                        "asset": self.asset,
                        "slug": self._slug(window_ts),
                        "start_ts": window_ts,
                        "end_ts": window_ts + step,
                        "condition_id": "",
                        "question": "",
                        "up_token": "",
                        "down_token": "",
                    }
                )
            if self.request_sleep:
                time.sleep(self.request_sleep)

        if not windows:
            log.warning("No Polymarket windows discovered for %s", self.asset)
            return 0, 0

        price_rows = self._load_price_history(
            windows,
            start_ts,
            end_ts,
            include_outside_window=include_outside_window,
        )
        metadata_path = self._metadata_path(start, end)
        self._write_metadata(windows, metadata_path)

        prices_path = self._prices_path(start, end, output_format)
        if output_format == "jsonl":
            self._write_prices_jsonl(price_rows, prices_path)
        else:
            self._write_prices_csv(price_rows, prices_path)

        log.info("Wrote metadata: %s", metadata_path)
        log.info("Wrote prices: %s", prices_path)
        return len(windows), len(price_rows)

    def _slug(self, window_ts: int) -> str:
        return f"{self.asset}-updown-{self.interval}-{window_ts}"

    def _load_window_metadata(self, window_ts: int, step: int) -> Optional[dict]:
        slug = self._slug(window_ts)
        try:
            resp = self.session.get(f"{GAMMA_BASE}/events/slug/{slug}", timeout=10)
            if resp.status_code != 200:
                log.debug("Window not found: %s", slug)
                return None

            event = resp.json()
            markets = event.get("markets", [])
            if not markets:
                return None

            market = markets[0]
            clob_raw = market.get("clobTokenIds", "[]")
            clob = json.loads(clob_raw) if isinstance(clob_raw, str) else (clob_raw or [])
            if len(clob) < 2:
                return None

            end_ts = window_ts + step
            end_date = market.get("endDate")
            if end_date:
                try:
                    end_ts = int(datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp())
                except ValueError:
                    pass

            question = market.get("question") or event.get("title") or ""
            return {
                "asset": self.asset,
                "slug": slug,
                "start_ts": window_ts,
                "start_iso": iso_from_ts(window_ts),
                "end_ts": end_ts,
                "end_iso": iso_from_ts(end_ts),
                "condition_id": market.get("conditionId", ""),
                "question": question,
                "up_token": clob[0],
                "down_token": clob[1],
            }
        except Exception as exc:
            log.warning("Failed to load window %s: %s", slug, exc)
            return None

    def _load_price_history(
        self,
        windows: list[dict],
        start_ts: int,
        end_ts: int,
        include_outside_window: bool = False,
    ) -> list[dict]:
        token_meta = {}
        for window in windows:
            if window.get("up_token"):
                token_meta[window["up_token"]] = (window, "UP")
            if window.get("down_token"):
                token_meta[window["down_token"]] = (window, "DOWN")

        tokens = list(token_meta.keys())
        rows = []
        for i in range(0, len(tokens), 20):
            batch = tokens[i:i + 20]
            try:
                resp = self.session.post(
                    f"{CLOB_BASE}/batch-prices-history",
                    json={
                        "markets": batch,
                        "start_ts": start_ts,
                        "end_ts": end_ts,
                        "fidelity": self.fidelity,
                    },
                    timeout=20,
                )
                resp.raise_for_status()
                history = resp.json().get("history", {})
            except Exception as exc:
                log.warning("Price history batch failed: %s", exc)
                continue

            for token_id, points in history.items():
                window, side = token_meta.get(token_id, ({}, ""))
                for point in points or []:
                    ts = point.get("t")
                    price = point.get("p")
                    if ts is None or price is None:
                        continue
                    if not include_outside_window:
                        token_ts = float(ts)
                        window_start = float(window.get("start_ts", start_ts))
                        window_end = float(window.get("end_ts", end_ts))
                        if token_ts < max(start_ts, window_start) or token_ts > min(end_ts, window_end):
                            continue
                    rows.append(
                        {
                            "ts": ts,
                            "time": iso_from_ts(float(ts)),
                            "asset": self.asset,
                            "slug": window.get("slug", ""),
                            "side": side,
                            "token_id": token_id,
                            "price": price,
                            "window_start_ts": window.get("start_ts", ""),
                            "window_end_ts": window.get("end_ts", ""),
                            "fidelity_minutes": self.fidelity,
                        }
                    )

            log.info("Loaded price history batch %d-%d/%d", i + 1, i + len(batch), len(tokens))
            if self.request_sleep:
                time.sleep(self.request_sleep)

        rows.sort(key=lambda r: (float(r["ts"]), r["slug"], r["side"]))
        return rows

    def _metadata_path(self, start: datetime, end: datetime) -> Path:
        name = self._range_name(start, end)
        return self.out_dir / "metadata" / self.asset / f"{name}.windows.jsonl"

    def _prices_path(self, start: datetime, end: datetime, output_format: str) -> Path:
        name = self._range_name(start, end)
        return self.out_dir / "prices" / self.asset / f"{name}.prices.{output_format}"

    def _range_name(self, start: datetime, end: datetime) -> str:
        start_name = start.strftime("%Y%m%dT%H%M%S")
        end_name = end.strftime("%Y%m%dT%H%M%S")
        return f"{self.asset}-{self.interval}-{start_name}-{end_name}-f{self.fidelity}m"

    def _write_metadata(self, windows: list[dict], path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for window in windows:
                f.write(json.dumps(window, ensure_ascii=True) + "\n")

    def _write_prices_jsonl(self, rows: list[dict], path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

    def _write_prices_csv(self, rows: list[dict], path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "ts",
            "time",
            "asset",
            "slug",
            "side",
            "token_id",
            "price",
            "window_start_ts",
            "window_end_ts",
            "fidelity_minutes",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download historical Polymarket rolling market token prices",
    )
    parser.add_argument("--asset", default="btc", help="Rolling asset, e.g. btc or eth")
    parser.add_argument("--interval", default="5m", help="Rolling market interval, default 5m")
    parser.add_argument("--start", required=True, help="UTC date/time: YYYY-MM-DD or ISO timestamp")
    parser.add_argument("--end", required=True, help="UTC date/time: YYYY-MM-DD or ISO timestamp")
    parser.add_argument("--out-dir", default="data/polymarket_history")
    parser.add_argument(
        "--fidelity",
        type=int,
        default=1,
        help="CLOB prices-history fidelity in minutes. Public API minimum is minute-level.",
    )
    parser.add_argument("--format", choices=["csv", "jsonl"], default="csv")
    parser.add_argument("--sleep", type=float, default=0.2, help="Delay between API calls")
    parser.add_argument(
        "--include-empty-windows",
        action="store_true",
        help="Write metadata placeholders for missing Gamma windows",
    )
    parser.add_argument(
        "--include-outside-window",
        action="store_true",
        help="Keep token price points outside their own rolling window bounds",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    start = parse_time(args.start)
    end = parse_time(args.end)
    if end <= start:
        raise ValueError("--end must be after --start")

    downloader = PolymarketHistoryDownloader(
        asset=args.asset,
        interval=args.interval,
        out_dir=args.out_dir,
        fidelity=args.fidelity,
        request_sleep=args.sleep,
    )
    windows, rows = downloader.download_range(
        start=start,
        end=end,
        output_format=args.format,
        include_empty_windows=args.include_empty_windows,
        include_outside_window=args.include_outside_window,
    )
    log.info("Done. windows=%d price_rows=%d", windows, rows)
    return 0 if windows else 1


if __name__ == "__main__":
    sys.exit(main())
