"""Schema probing for kv.run/OpenAPI and local sample payloads."""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path
from typing import Any

from backtest.canonicalize import ALIASES, parse_payload_text


def probe_openapi(path_or_url: str) -> dict[str, Any]:
    text = _read_text(path_or_url)
    payload = json.loads(text)
    paths = payload.get("paths", {}) if isinstance(payload, dict) else {}
    return {
        "source": path_or_url,
        "kind": "openapi",
        "title": payload.get("info", {}).get("title") if isinstance(payload, dict) else None,
        "version": payload.get("info", {}).get("version") if isinstance(payload, dict) else None,
        "path_count": len(paths),
        "prediction_market_paths": sorted(path for path in paths if str(path).startswith("/prediction-markets")),
        "kol_paths": sorted(path for path in paths if str(path).startswith("/kols")),
    }


def probe_rows(path_or_url: str) -> dict[str, Any]:
    rows = parse_payload_text(_read_text(path_or_url))
    fields = sorted({field for row in rows for field in row})
    canonical_matches = {}
    lowered = {field.lower(): field for field in fields}
    for canonical, aliases in ALIASES.items():
        for alias in aliases:
            if alias in fields:
                canonical_matches[canonical] = alias
                break
            if alias.lower() in lowered:
                canonical_matches[canonical] = lowered[alias.lower()]
                break
    return {
        "source": path_or_url,
        "kind": "rows",
        "row_count": len(rows),
        "fields": fields,
        "canonical_matches": canonical_matches,
        "missing_core": [field for field in ("market_id", "condition_id", "ts", "price") if field not in canonical_matches],
    }


def _read_text(path_or_url: str) -> str:
    if path_or_url.startswith(("http://", "https://")):
        with urllib.request.urlopen(path_or_url, timeout=30) as response:
            return response.read().decode("utf-8")
    return Path(path_or_url).read_text(encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe OpenAPI or sample rows for strategy-generation research")
    parser.add_argument("source")
    parser.add_argument("--kind", choices=["auto", "openapi", "rows"], default="auto")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    if args.kind == "openapi":
        result = probe_openapi(args.source)
    elif args.kind == "rows":
        result = probe_rows(args.source)
    else:
        text = _read_text(args.source).lstrip()
        result = probe_openapi(args.source) if text.startswith("{") and '"openapi"' in text[:200] else probe_rows(args.source)
    output = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
