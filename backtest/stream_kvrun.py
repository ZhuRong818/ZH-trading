"""Capture kv.run prediction-market SSE ticks into replayable JSONL files."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Iterator

from backtest.canonicalize import normalize_records
from backtest.loader_kvrun_prediction import DEFAULT_BASE_URL


def stream_prediction_events(
    base_url: str = DEFAULT_BASE_URL,
    api_key: str | None = None,
    bearer_token: str | None = None,
    condition_ids: list[str] | None = None,
    asset_ids: list[str] | None = None,
    timeout: int = 60,
) -> Iterator[dict[str, Any]]:
    params = {
        "condition_ids": ",".join(condition_ids or []),
        "asset_ids": ",".join(asset_ids or []),
    }
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value})
    url = f"{base_url.rstrip('/')}/prediction-markets/stream"
    if query:
        url = f"{url}?{query}"
    headers = {"Accept": "text/event-stream"}
    if api_key:
        headers["X-API-Key"] = api_key
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        lines = (line.decode("utf-8").rstrip("\n") for line in response)
        yield from parse_sse_blocks(lines)


def parse_sse_blocks(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    event_name = "message"
    data_lines: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                yield _make_event(event_name, data_lines)
            event_name = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())
    if data_lines:
        yield _make_event(event_name, data_lines)


def canonicalize_tick(event: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("event") != "tick":
        return None
    payload = event.get("data")
    if not isinstance(payload, dict):
        return None
    record = dict(payload)
    record.setdefault("row_type", "sse_tick")
    if "condition_id" not in record and "conditionId" in record:
        record["condition_id"] = record["conditionId"]
    if "market_id" not in record and record.get("condition_id"):
        record["market_id"] = record["condition_id"]
    result = normalize_records([record], source_granularity="sse_tick")
    return result.rows[0] if result.rows else None


def capture_stream(
    raw_out: Path,
    canonical_out: Path,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str | None = None,
    bearer_token: str | None = None,
    condition_ids: list[str] | None = None,
    asset_ids: list[str] | None = None,
    max_events: int = 0,
) -> int:
    raw_out.parent.mkdir(parents=True, exist_ok=True)
    canonical_out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with raw_out.open("a", encoding="utf-8") as raw_fh, canonical_out.open("a", encoding="utf-8") as canonical_fh:
        for event in stream_prediction_events(base_url, api_key, bearer_token, condition_ids, asset_ids):
            raw_fh.write(json.dumps(event, sort_keys=True) + "\n")
            canonical = canonicalize_tick(event)
            if canonical:
                canonical_fh.write(json.dumps(canonical, sort_keys=True) + "\n")
            count += 1
            if max_events and count >= max_events:
                break
    return count


def default_stream_paths(root: Path | None = None) -> tuple[Path, Path]:
    date = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    base = root or Path("data/kvrun")
    return base / f"raw_stream_{date}.jsonl", base / f"canonical_stream_{date}.jsonl"


def _make_event(event_name: str, data_lines: list[str]) -> dict[str, Any]:
    text = "\n".join(data_lines)
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        data = text
    return {"event": event_name, "data": data}


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture kv.run prediction-market SSE stream")
    parser.add_argument("--base-url", default=os.environ.get("KVRUN_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--api-key", default=os.environ.get("KVRUN_API_KEY"))
    parser.add_argument("--bearer-token", default=os.environ.get("KVRUN_BEARER_TOKEN"))
    parser.add_argument("--condition-ids", default="", help="Comma-separated Polymarket condition IDs")
    parser.add_argument("--asset-ids", default="", help="Comma-separated asset IDs")
    parser.add_argument("--raw-out", default="")
    parser.add_argument("--canonical-out", default="")
    parser.add_argument("--max-events", type=int, default=0)
    args = parser.parse_args()

    raw_default, canonical_default = default_stream_paths()
    count = capture_stream(
        raw_out=Path(args.raw_out) if args.raw_out else raw_default,
        canonical_out=Path(args.canonical_out) if args.canonical_out else canonical_default,
        base_url=args.base_url,
        api_key=args.api_key,
        bearer_token=args.bearer_token,
        condition_ids=[item for item in args.condition_ids.split(",") if item],
        asset_ids=[item for item in args.asset_ids.split(",") if item],
        max_events=args.max_events,
    )
    print(f"events={count}")


if __name__ == "__main__":
    main()
