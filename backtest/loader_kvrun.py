"""kv.run prediction-market loader.

The exact kv.run payloads are discovered at runtime. This loader accepts any
endpoint URL that returns JSON, wrapped JSON rows, or JSONL, persists raw
pages, then emits canonical rows through backtest.canonicalize.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from backtest.canonicalize import normalize_records, parse_payload_text, schema_summary


def fetch_pages(
    url: str,
    params: dict[str, Any] | None = None,
    max_pages: int = 100,
    timeout: int = 30,
    sleep_seconds: float = 0.0,
) -> list[str]:
    pages: list[str] = []
    cursor = None
    for _page in range(max_pages):
        request_params = dict(params or {})
        if cursor:
            request_params["cursor"] = cursor
        final_url = url
        if request_params:
            final_url += "?" + urllib.parse.urlencode(request_params)
        request = urllib.request.Request(final_url, headers={"Accept": "application/json, application/x-ndjson"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                time.sleep(max(1.0, sleep_seconds or 1.0))
                continue
            raise
        pages.append(text)
        cursor = _next_cursor(text)
        if not cursor:
            break
        if sleep_seconds:
            time.sleep(sleep_seconds)
    return pages


def load_endpoint(
    url: str,
    out_raw_dir: Path | None = None,
    params: dict[str, Any] | None = None,
    max_pages: int = 100,
    source_granularity: str = "unknown",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pages = fetch_pages(url, params=params, max_pages=max_pages)
    rows: list[dict[str, Any]] = []
    for idx, text in enumerate(pages):
        if out_raw_dir:
            out_raw_dir.mkdir(parents=True, exist_ok=True)
            (out_raw_dir / f"page_{idx:03d}.json").write_text(text, encoding="utf-8")
        rows.extend(parse_payload_text(text))
    result = normalize_records(rows, source_granularity=source_granularity)
    return result.rows, result.audit


def load_file(path: Path, source_granularity: str = "unknown") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = parse_payload_text(path.read_text(encoding="utf-8"))
    result = normalize_records(rows, source_granularity=source_granularity)
    return result.rows, result.audit


def _next_cursor(text: str) -> str | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("next_cursor") or payload.get("nextCursor") or payload.get("next") or payload.get("cursor")
    return str(value) if value else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch or normalize kv.run prediction-market data")
    parser.add_argument("--url", default="", help="Endpoint URL to fetch")
    parser.add_argument("--input", default="", help="Local JSON/JSONL file to normalize")
    parser.add_argument("--out", required=True, help="Canonical JSONL output path")
    parser.add_argument("--audit-out", default="", help="Audit JSONL output path")
    parser.add_argument("--raw-dir", default="", help="Optional raw page directory")
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--source-granularity", default="unknown")
    args = parser.parse_args()

    if not args.url and not args.input:
        raise SystemExit("Provide --url or --input")
    if args.url:
        rows, audit = load_endpoint(
            args.url,
            out_raw_dir=Path(args.raw_dir) if args.raw_dir else None,
            max_pages=args.max_pages,
            source_granularity=args.source_granularity,
        )
    else:
        rows, audit = load_file(Path(args.input), source_granularity=args.source_granularity)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    if args.audit_out:
        audit_out = Path(args.audit_out)
        audit_out.parent.mkdir(parents=True, exist_ok=True)
        with audit_out.open("w", encoding="utf-8") as fh:
            for row in audit:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
    print(json.dumps(schema_summary(rows), sort_keys=True))


if __name__ == "__main__":
    main()
