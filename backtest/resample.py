"""Resampling helpers for canonical prediction-market research rows."""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from typing import Any, Iterable


def build_bars(rows: Iterable[dict[str, Any]], interval_seconds: int = 60) -> list[dict[str, Any]]:
    rows = list(rows)
    if rows and all(_is_bar_like(row) for row in rows):
        return _canonical_bars(rows)
    buckets: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        ts = _epoch(row["ts"])
        bucket = int(ts // interval_seconds * interval_seconds)
        buckets[(str(row["condition_id"]), str(row["outcome"]), bucket)].append(row)

    bars = []
    last_volume: dict[tuple[str, str], float] = {}
    for (condition_id, outcome, bucket), group in sorted(buckets.items()):
        group.sort(key=lambda item: item["ts"])
        prices = [float(item["price"]) for item in group if item.get("price") is not None]
        if not prices:
            continue
        first = group[0]
        last = group[-1]
        key = (condition_id, outcome)
        volume = 0.0
        if last.get("volume_cum") is not None:
            current = float(last["volume_cum"])
            previous = last_volume.get(key)
            if previous is None:
                volume = 0.0
            elif current >= previous:
                volume = current - previous
            else:
                volume = math.nan
            last_volume[key] = current
        elif any(item.get("size") for item in group):
            volume = sum(float(item.get("size") or 0) * float(item.get("price") or 0) for item in group)
        else:
            volume = math.nan

        best_bid = _last_present(group, "best_bid")
        best_ask = _last_present(group, "best_ask")
        mid = (best_bid + best_ask) / 2.0 if best_bid is not None and best_ask is not None else prices[-1]
        spread = (best_ask - best_bid) / max(mid, 1e-6) if best_bid is not None and best_ask is not None else None
        close_at = last.get("market_close_at")
        created_at = last.get("market_created_at")
        bars.append(
            {
                "condition_id": condition_id,
                "market_id": last.get("market_id", condition_id),
                "outcome": outcome,
                "bar_ts": _iso_from_epoch(bucket + interval_seconds),
                "open": prices[0],
                "high": max(prices),
                "low": min(prices),
                "close": prices[-1],
                "volume": volume,
                "open_interest": _last_present(group, "open_interest"),
                "best_bid": best_bid,
                "best_ask": best_ask,
                "rel_spread": spread,
                "depth_bid_1": _last_present(group, "depth_bid_1"),
                "depth_ask_1": _last_present(group, "depth_ask_1"),
                "question": last.get("question", ""),
                "category": last.get("category", "unknown"),
                "status": last.get("status", "unknown"),
                "market_created_at": created_at,
                "market_close_at": close_at,
                "age_sec": _diff_seconds(_iso_from_epoch(bucket + interval_seconds), created_at),
                "secs_to_close": _diff_seconds(close_at, _iso_from_epoch(bucket + interval_seconds)),
            }
        )
    _add_returns(bars)
    return bars


def _canonical_bars(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bars = []
    for row in rows:
        best_bid = _float_or_none(row.get("best_bid"))
        best_ask = _float_or_none(row.get("best_ask"))
        close = float(row.get("close") if row.get("close") is not None else row.get("price"))
        mid = (best_bid + best_ask) / 2.0 if best_bid is not None and best_ask is not None else close
        spread = (best_ask - best_bid) / max(mid, 1e-6) if best_bid is not None and best_ask is not None else row.get("rel_spread")
        ts = row.get("bar_ts") or row.get("ts")
        bars.append(
            {
                "condition_id": row.get("condition_id"),
                "market_id": row.get("market_id") or row.get("condition_id"),
                "outcome": row.get("outcome", "YES"),
                "bar_ts": ts,
                "open": float(row.get("open") if row.get("open") is not None else close),
                "high": float(row.get("high") if row.get("high") is not None else close),
                "low": float(row.get("low") if row.get("low") is not None else close),
                "close": close,
                "volume": _float_or_nan(row.get("volume") if row.get("volume") is not None else row.get("volume_cum")),
                "open_interest": _float_or_none(row.get("open_interest")),
                "best_bid": best_bid,
                "best_ask": best_ask,
                "rel_spread": spread,
                "depth_bid_1": _float_or_none(row.get("depth_bid_1")),
                "depth_ask_1": _float_or_none(row.get("depth_ask_1")),
                "question": row.get("question", ""),
                "category": row.get("category", "unknown"),
                "status": row.get("status", "unknown"),
                "market_created_at": row.get("market_created_at"),
                "market_close_at": row.get("market_close_at"),
                "age_sec": row.get("age_sec") if row.get("age_sec") is not None else _diff_seconds(ts, row.get("market_created_at")),
                "secs_to_close": row.get("secs_to_close") if row.get("secs_to_close") is not None else _diff_seconds(row.get("market_close_at"), ts),
            }
        )
    bars.sort(key=lambda item: (str(item["condition_id"]), str(item["outcome"]), str(item["bar_ts"])))
    _add_returns(bars)
    return bars


def _is_bar_like(row: dict[str, Any]) -> bool:
    return row.get("row_type") == "bar" or ("open" in row and "high" in row and "low" in row and ("close" in row or "price" in row))


def _add_returns(bars: list[dict[str, Any]]) -> None:
    previous: dict[tuple[str, str], float] = {}
    for bar in bars:
        key = (bar["condition_id"], bar["outcome"])
        prev = previous.get(key)
        close = float(bar["close"])
        bar["ret_1bar"] = close / prev - 1.0 if prev and prev > 0 else None
        previous[key] = close


def _last_present(rows: list[dict[str, Any]], key: str) -> float | None:
    for row in reversed(rows):
        value = row.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_nan(value: Any) -> float:
    parsed = _float_or_none(value)
    return parsed if parsed is not None else math.nan


def _epoch(iso_ts: str) -> float:
    return dt.datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00")).timestamp()


def _iso_from_epoch(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _diff_seconds(later: Any, earlier: Any) -> float | None:
    if not later or not earlier:
        return None
    try:
        return _epoch(str(later)) - _epoch(str(earlier))
    except ValueError:
        return None
