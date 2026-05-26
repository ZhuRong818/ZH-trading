"""Canonical schema helpers for prediction-market research data.

This module is intentionally provider-neutral. It maps kv.run, Gamma/CLOB,
or locally recorded JSONL rows into a small canonical shape used by research
tools. It does not place orders or touch live strategy code.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any, Iterable


ALIASES: dict[str, list[str]] = {
    "row_type": ["row_type", "type", "kind"],
    "venue": ["venue", "exchange"],
    "market_id": ["market_id", "id", "marketId", "market", "slug"],
    "condition_id": ["condition_id", "conditionId", "market_id", "marketId", "market", "id", "ticker", "slug"],
    "token_id": ["token_id", "asset_id", "outcomeTokenId"],
    "question": ["question", "title", "name", "question_text"],
    "slug": ["slug"],
    "category": ["category", "topic", "market_type", "asset"],
    "outcome": ["outcome", "side", "token_side", "outcome_name"],
    "ts": ["ts", "timestamp", "time", "bucket_ts", "bar_ts", "updated_at", "created_at", "createdAt"],
    "market_created_at": ["market_created_at", "createdAt", "created_at", "startDate", "start_date"],
    "market_close_at": ["market_close_at", "endDate", "end_date", "close_time", "resolution_time", "window_end_ts"],
    "market_resolved_at": ["market_resolved_at", "resolvedAt", "resolved_at"],
    "status": ["status", "state", "market_status"],
    "price": ["price", "last_price", "close", "mid"],
    "open": ["open"],
    "high": ["high"],
    "low": ["low"],
    "close": ["close"],
    "best_bid": ["best_bid", "bid", "top_bid"],
    "best_ask": ["best_ask", "ask", "top_ask"],
    "size": ["size", "shares", "qty", "quantity"],
    "notional_usdc": ["notional_usdc", "notional", "amount_usd"],
    "volume_cum": ["volume_cum", "cum_volume", "volume"],
    "trade_count": ["trades", "trade_count", "num_trades"],
    "open_interest": ["open_interest", "oi", "openInterest"],
    "depth_bid_1": ["depth_bid_1", "bid_size", "top_bid_size"],
    "depth_ask_1": ["depth_ask_1", "ask_size", "top_ask_size"],
    "trade_id": ["trade_id", "fill_id", "tx_id"],
    "wallet": ["wallet", "taker", "maker", "address"],
    "liquidity": ["liquidity"],
    "resolution_label": ["resolution_label", "winner", "resolved_outcome"],
}

REQUIRED = ("market_id", "condition_id", "outcome", "ts", "price")
REQUIRED_BY_ROW_TYPE = {
    "market": ("market_id", "condition_id"),
    "event": ("market_id",),
    "holder": ("market_id",),
    "open_interest": ("market_id", "condition_id", "ts", "open_interest"),
    "trade": ("market_id", "condition_id", "ts", "price", "size"),
    "bar": ("market_id", "condition_id", "outcome", "ts", "price"),
    "sse_tick": ("market_id", "condition_id", "ts", "price"),
    "unknown": REQUIRED,
}


@dataclass
class CanonicalResult:
    rows: list[dict[str, Any]]
    audit: list[dict[str, Any]] = field(default_factory=list)


def parse_payload_text(text: str) -> list[dict[str, Any]]:
    """Parse JSON, wrapped JSON arrays, or JSONL/NDJSON into row dicts."""
    stripped = text.strip()
    if not stripped:
        return []
    try:
        payload = json.loads(stripped)
        return extract_rows(payload)
    except json.JSONDecodeError:
        rows = []
        for line_no, line in enumerate(stripped.splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"JSONL row {line_no} is not an object")
            rows.append(row)
        return rows


def extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "rows", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return [payload]
    raise TypeError(f"Unsupported payload type: {type(payload)!r}")


def normalize_records(records: Iterable[dict[str, Any]], source_granularity: str = "unknown") -> CanonicalResult:
    rows: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    for idx, record in enumerate(records):
        expanded = _expand_wide_record(record)
        for side_row in expanded:
            row, row_audit = _normalize_one(side_row, source_granularity)
            audit.extend({"row": idx, **item} for item in row_audit)
            if row is None:
                continue
            key = (
                row.get("condition_id"),
                row.get("token_id"),
                row.get("outcome"),
                row.get("ts"),
                row.get("trade_id"),
            )
            if key in seen:
                audit.append({"row": idx, "rule": "deduplicate", "detail": "duplicate canonical key"})
                continue
            seen.add(key)
            rows.append(row)

    return CanonicalResult(rows=sorted(rows, key=_sort_key), audit=audit)


def schema_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    fields = sorted({key for row in rows for key in row})
    return {
        "row_count": len(rows),
        "fields": fields,
        "canonical_fields": sorted({field for field in ALIASES if any(field in row for row in rows)}),
        "missing_required": [field for field in REQUIRED if not any(field in row for row in rows)],
    }


def _sort_key(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(item.get("condition_id") or item.get("market_id") or ""),
        str(item.get("outcome") or ""),
        str(item.get("ts") or item.get("market_created_at") or ""),
        str(item.get("row_type") or ""),
    )


def _expand_wide_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert local rolling UP/DOWN wide rows to canonical side rows."""
    if any(key in record for key in ("up_mid", "down_mid", "up_buy", "down_buy")):
        out = []
        for side, prefix in (("YES", "up"), ("NO", "down")):
            row = dict(record)
            row["outcome"] = side
            row["price"] = _first_present(record, [f"{prefix}_mid", f"{prefix}_buy", f"{prefix}_sell"])
            row["best_bid"] = record.get(f"{prefix}_sell")
            row["best_ask"] = record.get(f"{prefix}_buy")
            row["depth_bid_1"] = record.get(f"{prefix}_bid_depth")
            row["depth_ask_1"] = record.get(f"{prefix}_ask_depth")
            row["market_id"] = record.get("slug") or record.get("market_id")
            row["condition_id"] = record.get("slug") or record.get("condition_id") or record.get("market_id")
            row["category"] = record.get("asset") or record.get("category") or "unknown"
            out.append(row)
        return out
    return [record]


def _normalize_one(record: dict[str, Any], source_granularity: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    audit: list[dict[str, Any]] = []
    row: dict[str, Any] = {}
    for canonical, aliases in ALIASES.items():
        value = _first_present(record, aliases)
        if value is not None:
            row[canonical] = value
    row_type = _infer_row_type(record, row, source_granularity)
    row["row_type"] = row_type

    if "market_id" not in row and "condition_id" in row:
        row["market_id"] = row["condition_id"]
        audit.append({"rule": "market_id_fallback", "detail": "used condition_id"})
    if "condition_id" not in row and "market_id" in row:
        row["condition_id"] = row["market_id"]
        audit.append({"rule": "condition_id_fallback", "detail": "used market_id"})
    if "category" not in row:
        row["category"] = "unknown"
    if "question" not in row:
        row["question"] = ""
    if "status" not in row:
        row["status"] = "unknown"
    if "outcome" not in row:
        row["outcome"] = "YES"
    if "price" not in row and row.get("close") is not None:
        row["price"] = row["close"]

    for key in ("ts", "market_created_at", "market_close_at", "market_resolved_at"):
        if key in row:
            parsed, assumed = _parse_utc(row[key])
            row[key] = parsed
            if assumed:
                audit.append({"rule": "tz_assumed_utc", "detail": key})

    if "market_created_at" not in row and "ts" in row:
        row["market_created_at"] = row["ts"]
        audit.append({"rule": "market_created_at_fallback", "detail": "used ts"})
    if "market_close_at" not in row and record.get("seconds_remaining") is not None and "ts" in row:
        try:
            row["market_close_at"] = _iso_from_epoch(_epoch(row["ts"]) + float(record["seconds_remaining"]))
            audit.append({"rule": "market_close_at_fallback", "detail": "used seconds_remaining"})
        except (TypeError, ValueError):
            pass

    row["outcome"] = str(row.get("outcome") or "YES").upper()
    row["status"] = str(row.get("status") or "unknown").lower()
    row["source_granularity"] = source_granularity

    for key in (
        "price",
        "open",
        "high",
        "low",
        "close",
        "best_bid",
        "best_ask",
        "size",
        "notional_usdc",
        "volume_cum",
        "trade_count",
        "open_interest",
        "depth_bid_1",
        "depth_ask_1",
        "liquidity",
    ):
        if key in row:
            row[key] = _to_float(row[key])

    required = REQUIRED_BY_ROW_TYPE.get(row_type, REQUIRED)
    missing = [field for field in required if row.get(field) in (None, "")]
    if missing:
        audit.append({"rule": "missing_required", "detail": ",".join(missing)})
        return None, audit
    price = row.get("price")
    if price is not None and not (0.0 <= float(price) <= 1.0):
        audit.append({"rule": "price_in_[0,1]", "detail": str(price)})
        return None, audit

    return row, audit


def _infer_row_type(record: dict[str, Any], row: dict[str, Any], source_granularity: str) -> str:
    explicit = str(row.get("row_type") or source_granularity or "").lower()
    if explicit in {"market", "trade", "bar", "candle", "open_interest", "holder", "event", "sse_tick"}:
        return "bar" if explicit == "candle" else explicit
    keys = {key.lower() for key in record}
    if {"open", "high", "low", "close"} & keys and "volume" in keys:
        return "bar"
    if {"trade_id", "fill_id", "tx_id"} & keys or ("size" in keys and "price" in keys):
        return "trade"
    if {"openinterest", "open_interest", "oi"} & keys:
        return "open_interest"
    if {"question", "title", "conditionid", "condition_id"} & keys and "price" not in keys and "close" not in keys:
        return "market"
    if "event" in explicit:
        return "event"
    return "unknown"


def _first_present(record: dict[str, Any], aliases: list[str]) -> Any:
    for alias in aliases:
        if alias in record and record[alias] not in (None, ""):
            return record[alias]
        lowered = alias.lower()
        for key, value in record.items():
            if key.lower() == lowered and value not in (None, ""):
                return value
    return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_utc(value: Any) -> tuple[str, bool]:
    assumed = False
    if isinstance(value, (int, float)):
        return _iso_from_epoch(float(value)), False
    text = str(value)
    if text.replace(".", "", 1).isdigit():
        return _iso_from_epoch(float(text)), False
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        # Twitter/API date strings and other formats handled by pandas are not
        # available here; keep a deterministic failure surface.
        raise ValueError(f"Could not parse timestamp: {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
        assumed = True
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"), assumed


def _iso_from_epoch(epoch: float) -> str:
    if epoch > 10_000_000_000:
        epoch = epoch / 1000.0
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch(iso_ts: str) -> float:
    return dt.datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).timestamp()
