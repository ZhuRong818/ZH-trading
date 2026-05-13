"""
Download historical Binance BTC trade data from Binance public data archives.

This is meant to complement backtest.recorder:
- recorder.py captures live Binance ticker + Polymarket books going forward
- this script fetches historical Binance trades/aggTrades/1s klines from
  https://data.binance.vision for backtests that need sub-second BTC prices

Examples:
    python -m backtest.binance_history_downloader --start 2026-05-01 --end 2026-05-02
    python -m backtest.binance_history_downloader --data-type aggTrades --start 2026-05-01 --end 2026-05-07
    python -m backtest.binance_history_downloader --data-type klines --interval 1s --start 2026-05-01 --end 2026-05-01
    python -m backtest.binance_history_downloader --period monthly --start 2026-01 --end 2026-03 --raw-only
"""

import argparse
import csv
import logging
import os
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://data.binance.vision/data"

TRADE_COLUMNS = [
    "trade_id",
    "price",
    "qty",
    "quote_qty",
    "time",
    "is_buyer_maker",
    "is_best_match",
]

AGG_TRADE_COLUMNS = [
    "agg_trade_id",
    "price",
    "qty",
    "first_trade_id",
    "last_trade_id",
    "time",
    "is_buyer_maker",
    "is_best_match",
]

KLINE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "number_of_trades",
    "taker_buy_base_asset_volume",
    "taker_buy_quote_asset_volume",
    "ignore",
]


def parse_daily_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_month(value: str) -> date:
    return datetime.strptime(value, "%Y-%m").date()


def iter_days(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def iter_months(start: date, end: date) -> Iterable[date]:
    current = date(start.year, start.month, 1)
    final = date(end.year, end.month, 1)
    while current <= final:
        yield current
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)


def archive_name(symbol: str, data_type: str, period_key: str, interval: Optional[str]) -> str:
    if data_type == "klines":
        if not interval:
            raise ValueError("--interval is required when --data-type klines")
        return f"{symbol}-{interval}-{period_key}.zip"
    return f"{symbol}-{data_type}-{period_key}.zip"


def archive_url(
    market: str,
    period: str,
    data_type: str,
    symbol: str,
    period_key: str,
    interval: Optional[str],
) -> str:
    if data_type == "klines":
        return (
            f"{BASE_URL}/{market}/{period}/klines/{symbol}/{interval}/"
            f"{archive_name(symbol, data_type, period_key, interval)}"
        )
    return (
        f"{BASE_URL}/{market}/{period}/{data_type}/{symbol}/"
        f"{archive_name(symbol, data_type, period_key, interval)}"
    )


def download_file(session: requests.Session, url: str, path: Path, retries: int) -> bool:
    if path.exists() and path.stat().st_size > 0:
        log.info("Exists: %s", path)
        return True

    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, retries + 1):
        try:
            with session.get(url, stream=True, timeout=30) as resp:
                if resp.status_code == 404:
                    log.warning("Not found: %s", url)
                    return False
                resp.raise_for_status()
                tmp_path = path.with_suffix(path.suffix + ".tmp")
                with tmp_path.open("wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                tmp_path.replace(path)
                log.info("Downloaded: %s", path)
                return True
        except Exception as exc:
            log.warning("Download failed (%d/%d): %s", attempt, retries, exc)
            time.sleep(min(2 * attempt, 10))
    return False


def has_header(row: list[str], data_type: str) -> bool:
    if not row:
        return False
    first = row[0].strip().lower().replace(" ", "_")
    return first in {"trade_id", "aggregate_tradeid", "open_time"}


def timestamp_unit(value: int) -> str:
    # Binance spot public data uses milliseconds historically; from 2025-01-01
    # spot archives may use microseconds. Keep both normalized columns.
    if value >= 10_000_000_000_000_000:
        return "ns"
    if value >= 10_000_000_000_000:
        return "us"
    return "ms"


def normalize_timestamp(value: str) -> tuple[int, int, str]:
    raw = int(value)
    unit = timestamp_unit(raw)
    if unit == "ns":
        return raw // 1_000_000, raw // 1_000, unit
    if unit == "us":
        return raw // 1_000, raw, unit
    return raw, raw * 1_000, unit


def iso_from_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def csv_columns(data_type: str) -> list[str]:
    if data_type == "trades":
        return TRADE_COLUMNS
    if data_type == "aggTrades":
        return AGG_TRADE_COLUMNS
    if data_type == "klines":
        return KLINE_COLUMNS
    raise ValueError(f"Unsupported data type: {data_type}")


def convert_archive(zip_path: Path, out_path: Path, data_type: str) -> int:
    columns = csv_columns(data_type)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0

    with zipfile.ZipFile(zip_path) as zf, out_path.open("w", newline="", encoding="utf-8") as out:
        names = [name for name in zf.namelist() if name.lower().endswith(".csv")]
        if not names:
            raise ValueError(f"No CSV file found in {zip_path}")

        writer = csv.writer(out)
        if data_type == "klines":
            writer.writerow(
                [
                    "open_time_ms",
                    "open_time_us",
                    "open_time_iso",
                    "close_time_ms",
                    "close_time_us",
                    "close_time_iso",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "quote_asset_volume",
                    "number_of_trades",
                    "taker_buy_base_asset_volume",
                    "taker_buy_quote_asset_volume",
                    "source_time_unit",
                ]
            )
        else:
            writer.writerow(
                [
                    "time_ms",
                    "time_us",
                    "time_iso",
                    "price",
                    "qty",
                    "quote_qty",
                    "is_buyer_maker",
                    "is_best_match",
                    "trade_id",
                    "agg_trade_id",
                    "first_trade_id",
                    "last_trade_id",
                    "source_time_unit",
                ]
            )

        with zf.open(names[0]) as raw:
            text = (line.decode("utf-8").strip() for line in raw)
            reader = csv.reader(text)
            for row in reader:
                if not row or has_header(row, data_type):
                    continue
                if len(row) < len(columns):
                    log.debug("Skipping short row in %s: %s", zip_path, row)
                    continue

                data = dict(zip(columns, row))
                if data_type == "klines":
                    open_ms, open_us, open_unit = normalize_timestamp(data["open_time"])
                    close_ms, close_us, _ = normalize_timestamp(data["close_time"])
                    writer.writerow(
                        [
                            open_ms,
                            open_us,
                            iso_from_ms(open_ms),
                            close_ms,
                            close_us,
                            iso_from_ms(close_ms),
                            data["open"],
                            data["high"],
                            data["low"],
                            data["close"],
                            data["volume"],
                            data["quote_asset_volume"],
                            data["number_of_trades"],
                            data["taker_buy_base_asset_volume"],
                            data["taker_buy_quote_asset_volume"],
                            open_unit,
                        ]
                    )
                else:
                    time_ms, time_us, unit = normalize_timestamp(data["time"])
                    writer.writerow(
                        [
                            time_ms,
                            time_us,
                            iso_from_ms(time_ms),
                            data["price"],
                            data["qty"],
                            data.get("quote_qty", ""),
                            data["is_buyer_maker"],
                            data["is_best_match"],
                            data.get("trade_id", ""),
                            data.get("agg_trade_id", ""),
                            data.get("first_trade_id", ""),
                            data.get("last_trade_id", ""),
                            unit,
                        ]
                    )
                rows_written += 1

    log.info("Converted %s rows: %s", rows_written, out_path)
    return rows_written


def period_items(period: str, start: str, end: str) -> Iterable[tuple[str, date]]:
    if period == "daily":
        start_date = parse_daily_date(start)
        end_date = parse_daily_date(end)
        for item in iter_days(start_date, end_date):
            yield item.isoformat(), item
    else:
        start_month = parse_month(start)
        end_month = parse_month(end)
        for item in iter_months(start_month, end_month):
            yield item.strftime("%Y-%m"), item


def default_end(period: str) -> str:
    today = datetime.now(timezone.utc).date()
    if period == "daily":
        # Daily files are normally available the next day, so yesterday is a
        # safer default than today.
        return (today - timedelta(days=1)).isoformat()
    return today.strftime("%Y-%m")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download Binance historical BTC trades/aggTrades/klines archives",
    )
    parser.add_argument("--symbol", default="BTCUSDT", help="Trading pair, default BTCUSDT")
    parser.add_argument(
        "--market",
        default="spot",
        choices=["spot", "futures/um", "futures/cm"],
        help="Binance public data market namespace",
    )
    parser.add_argument(
        "--data-type",
        default="trades",
        choices=["trades", "aggTrades", "klines"],
        help="trades gives individual executions; aggTrades is smaller; klines can use --interval 1s",
    )
    parser.add_argument(
        "--interval",
        default=None,
        help="Kline interval when --data-type klines, e.g. 1s, 1m, 5m",
    )
    parser.add_argument("--period", default="daily", choices=["daily", "monthly"])
    parser.add_argument("--start", required=True, help="YYYY-MM-DD for daily, YYYY-MM for monthly")
    parser.add_argument(
        "--end",
        default=None,
        help="Inclusive end date/month. Defaults to yesterday for daily or current month for monthly.",
    )
    parser.add_argument(
        "--out-dir",
        default="data/binance_history",
        help="Output directory for raw zip files and normalized CSVs",
    )
    parser.add_argument("--raw-only", action="store_true", help="Only download zip files")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--sleep", type=float, default=0.2, help="Delay between archive downloads")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    symbol = args.symbol.upper()
    end = args.end or default_end(args.period)
    out_dir = Path(args.out_dir)
    session = requests.Session()
    downloaded = 0
    converted = 0

    for period_key, _ in period_items(args.period, args.start, end):
        name = archive_name(symbol, args.data_type, period_key, args.interval)
        url = archive_url(args.market, args.period, args.data_type, symbol, period_key, args.interval)
        raw_path = out_dir / "raw" / args.market.replace("/", "_") / args.data_type / symbol / name
        ok = download_file(session, url, raw_path, args.retries)
        if not ok:
            continue

        downloaded += 1
        if not args.raw_only:
            stem = name[:-4]
            normalized_path = (
                out_dir
                / "normalized"
                / args.market.replace("/", "_")
                / args.data_type
                / symbol
                / f"{stem}.normalized.csv"
            )
            converted += convert_archive(raw_path, normalized_path, args.data_type)

        if args.sleep > 0:
            time.sleep(args.sleep)

    log.info("Done. archives=%d converted_rows=%d", downloaded, converted)
    return 0 if downloaded else 1


if __name__ == "__main__":
    sys.exit(main())
