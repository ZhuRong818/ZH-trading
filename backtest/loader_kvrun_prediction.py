"""Dedicated kv.run prediction-market client and canonical export CLI.

The generic ``backtest.loader_kvrun`` module can normalize arbitrary JSON
endpoints. This module knows the concrete kv.run prediction-market paths and
attaches venue/market context before canonicalization.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backtest.canonicalize import extract_rows, normalize_records, schema_summary


DEFAULT_BASE_URL = "https://kv.run:5000"


@dataclass
class KvRunPredictionClient:
    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    bearer_token: str | None = None
    rpm: int | None = None
    timeout: int = 30

    def __post_init__(self) -> None:
        self.base_url = (self.base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = self.api_key if self.api_key is not None else os.environ.get("KVRUN_API_KEY")
        self.bearer_token = (
            self.bearer_token if self.bearer_token is not None else os.environ.get("KVRUN_BEARER_TOKEN")
        )
        if self.rpm is None:
            self.rpm = int(os.environ.get("KVRUN_RPM") or (550 if self.api_key or self.bearer_token else 55))
        self._last_request_ts = 0.0

    @classmethod
    def from_env(cls, timeout: int = 30) -> "KvRunPredictionClient":
        return cls(
            base_url=os.environ.get("KVRUN_BASE_URL", DEFAULT_BASE_URL),
            api_key=os.environ.get("KVRUN_API_KEY"),
            bearer_token=os.environ.get("KVRUN_BEARER_TOKEN"),
            timeout=timeout,
        )

    def build_url(self, path: str, params: dict[str, Any] | None = None) -> str:
        clean_path = path if path.startswith("/") else f"/{path}"
        query = {
            key: value
            for key, value in (params or {}).items()
            if value is not None and value != ""
        }
        url = f"{self.base_url}{clean_path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return url

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self._throttle()
        request = urllib.request.Request(self.build_url(path, params), headers=self._headers())
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def search_markets(
        self,
        q: str,
        venue: str | None = None,
        status: str = "open",
        limit: int = 50,
    ) -> Any:
        return self.get_json(
            "/prediction-markets/markets/search",
            {"q": q, "venue": venue, "status": status, "limit": limit},
        )

    def get_market(self, venue: str, market_id: str) -> Any:
        return self.get_json(f"/prediction-markets/markets/{venue}/{market_id}")

    def trades(
        self,
        venue: str,
        market_id: str,
        start: str | None = None,
        end: str | None = None,
        limit: int = 5000,
    ) -> Any:
        return self.get_json(
            f"/prediction-markets/trades/{venue}/{market_id}",
            {"from": start, "to": end, "limit": limit},
        )

    def candles(self, venue: str, market_id: str, interval: int = 1, limit: int = 5000) -> Any:
        return self.get_json(
            f"/prediction-markets/candles/{venue}/{market_id}",
            {"interval": interval, "limit": limit},
        )

    def open_interest(self, venue: str, market_id: str, limit: int = 500) -> Any:
        return self.get_json(
            f"/prediction-markets/open-interest/{venue}/{market_id}",
            {"limit": limit},
        )

    def top_holders(self, venue: str, market_id: str, limit: int = 50) -> Any:
        return self.get_json(
            f"/prediction-markets/top-holders/{venue}/{market_id}",
            {"limit": limit},
        )

    def events(self, q: str | None = None, status: str = "open", limit: int = 200) -> Any:
        return self.get_json("/prediction-markets/events", {"q": q, "status": status, "limit": limit})

    def matched_pairs(self, venue: str, venue_id: str, limit: int = 20) -> Any:
        return self.get_json(f"/prediction-markets/matched-pairs/{venue}/{venue_id}", {"limit": limit})

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    def _throttle(self) -> None:
        if not self.rpm or self.rpm <= 0:
            return
        min_interval = 60.0 / float(self.rpm)
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_request_ts = time.monotonic()


def endpoint_payload(client: KvRunPredictionClient, args: argparse.Namespace) -> tuple[Any, str, dict[str, Any]]:
    command = args.command
    context: dict[str, Any] = {"venue": getattr(args, "venue", None)}
    if command == "search":
        return client.search_markets(args.q, args.venue, args.status, args.limit), "market", context
    if command == "market":
        context.update({"market_id": args.market_id, "condition_id": args.market_id})
        return client.get_market(args.venue, args.market_id), "market", context
    if command == "trades":
        context.update({"market_id": args.market_id, "condition_id": args.market_id})
        return client.trades(args.venue, args.market_id, args.start, args.end, args.limit), "trade", context
    if command == "candles":
        context.update({"market_id": args.market_id, "condition_id": args.market_id})
        return client.candles(args.venue, args.market_id, args.interval, args.limit), "bar", context
    if command == "open-interest":
        context.update({"market_id": args.market_id, "condition_id": args.market_id})
        return client.open_interest(args.venue, args.market_id, args.limit), "open_interest", context
    if command == "top-holders":
        context.update({"market_id": args.market_id, "condition_id": args.market_id})
        return client.top_holders(args.venue, args.market_id, args.limit), "holder", context
    if command == "events":
        return client.events(args.q, args.status, args.limit), "event", context
    if command == "matched-pairs":
        context.update({"market_id": args.venue_id, "condition_id": args.venue_id})
        return client.matched_pairs(args.venue, args.venue_id, args.limit), "market", context
    raise ValueError(f"Unsupported command: {command}")


def normalize_payload(payload: Any, row_type: str, context: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records = [_with_context(row, row_type, context) for row in extract_rows(payload)]
    result = normalize_records(records, source_granularity=row_type)
    return result.rows, result.audit


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def fetch_research_bundle(
    client: KvRunPredictionClient,
    q: str,
    venue: str = "polymarket",
    status: str = "open",
    market_limit: int = 10,
    candle_interval: int = 1,
    candle_limit: int = 5000,
    oi_limit: int = 500,
    include_trades: bool = False,
    trade_start: str | None = None,
    trade_end: str | None = None,
    trade_limit: int = 5000,
) -> dict[str, list[dict[str, Any]]]:
    market_payload = client.search_markets(q, venue=venue, status=status, limit=market_limit)
    markets, _market_audit = normalize_payload(market_payload, "market", {"venue": venue})
    market_ids = [_market_identifier(row) for row in markets]
    market_ids = [item for item in dict.fromkeys(market_ids) if item][:market_limit]

    candles: list[dict[str, Any]] = []
    open_interest: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    for market_id in market_ids:
        candle_payload = client.candles(venue, market_id, interval=candle_interval, limit=candle_limit)
        candle_rows, _ = normalize_payload(
            candle_payload,
            "bar",
            {"venue": venue, "market_id": market_id, "condition_id": market_id},
        )
        candles.extend(candle_rows)

        oi_payload = client.open_interest(venue, market_id, limit=oi_limit)
        oi_rows, _ = normalize_payload(
            oi_payload,
            "open_interest",
            {"venue": venue, "market_id": market_id, "condition_id": market_id},
        )
        open_interest.extend(oi_rows)

        if include_trades:
            trade_payload = client.trades(venue, market_id, start=trade_start, end=trade_end, limit=trade_limit)
            trade_rows, _ = normalize_payload(
                trade_payload,
                "trade",
                {"venue": venue, "market_id": market_id, "condition_id": market_id},
            )
            trades.extend(trade_rows)

    return {"markets": markets, "candles": candles, "open_interest": open_interest, "trades": trades}


def _with_context(row: dict[str, Any], row_type: str, context: dict[str, Any]) -> dict[str, Any]:
    merged = dict(row)
    merged.setdefault("row_type", row_type)
    for key, value in context.items():
        if value is not None:
            merged.setdefault(key, value)
    return merged


def _market_identifier(row: dict[str, Any]) -> str:
    return str(row.get("market_id") or row.get("condition_id") or row.get("ticker") or row.get("slug") or "")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch concrete kv.run prediction-market endpoints")
    parser.add_argument("--base-url", default=os.environ.get("KVRUN_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--api-key", default=os.environ.get("KVRUN_API_KEY"))
    parser.add_argument("--bearer-token", default=os.environ.get("KVRUN_BEARER_TOKEN"))
    parser.add_argument("--rpm", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--raw-out", default="", help="Optional raw JSON payload path")
    parser.add_argument("--out", required=True, help="Canonical JSONL output path")
    parser.add_argument("--audit-out", default="", help="Optional audit JSONL output path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search")
    search.add_argument("--q", required=True)
    search.add_argument("--venue", default="polymarket")
    search.add_argument("--status", default="open")
    search.add_argument("--limit", type=int, default=50)

    market = subparsers.add_parser("market")
    market.add_argument("--venue", required=True)
    market.add_argument("--market-id", required=True)

    trades = subparsers.add_parser("trades")
    trades.add_argument("--venue", required=True)
    trades.add_argument("--market-id", required=True)
    trades.add_argument("--start", default="")
    trades.add_argument("--end", default="")
    trades.add_argument("--limit", type=int, default=5000)

    candles = subparsers.add_parser("candles")
    candles.add_argument("--venue", required=True)
    candles.add_argument("--market-id", required=True)
    candles.add_argument("--interval", type=int, default=1)
    candles.add_argument("--limit", type=int, default=5000)

    oi = subparsers.add_parser("open-interest")
    oi.add_argument("--venue", required=True)
    oi.add_argument("--market-id", required=True)
    oi.add_argument("--limit", type=int, default=500)

    holders = subparsers.add_parser("top-holders")
    holders.add_argument("--venue", required=True)
    holders.add_argument("--market-id", required=True)
    holders.add_argument("--limit", type=int, default=50)

    events = subparsers.add_parser("events")
    events.add_argument("--q", default="")
    events.add_argument("--status", default="open")
    events.add_argument("--limit", type=int, default=200)

    pairs = subparsers.add_parser("matched-pairs")
    pairs.add_argument("--venue", required=True)
    pairs.add_argument("--venue-id", required=True)
    pairs.add_argument("--limit", type=int, default=20)

    bundle = subparsers.add_parser("bundle")
    bundle.add_argument("--q", required=True)
    bundle.add_argument("--venue", default="polymarket")
    bundle.add_argument("--status", default="open")
    bundle.add_argument("--market-limit", type=int, default=10)
    bundle.add_argument("--out-dir", required=True)
    bundle.add_argument("--candle-interval", type=int, default=1)
    bundle.add_argument("--candle-limit", type=int, default=5000)
    bundle.add_argument("--oi-limit", type=int, default=500)
    bundle.add_argument("--include-trades", action="store_true")
    bundle.add_argument("--trade-start", default="")
    bundle.add_argument("--trade-end", default="")
    bundle.add_argument("--trade-limit", type=int, default=5000)

    args = parser.parse_args()
    client = KvRunPredictionClient(
        base_url=args.base_url,
        api_key=args.api_key,
        bearer_token=args.bearer_token,
        rpm=args.rpm,
        timeout=args.timeout,
    )
    if args.command == "bundle":
        bundle_rows = fetch_research_bundle(
            client,
            q=args.q,
            venue=args.venue,
            status=args.status,
            market_limit=args.market_limit,
            candle_interval=args.candle_interval,
            candle_limit=args.candle_limit,
            oi_limit=args.oi_limit,
            include_trades=args.include_trades,
            trade_start=args.trade_start,
            trade_end=args.trade_end,
            trade_limit=args.trade_limit,
        )
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, rows in bundle_rows.items():
            write_jsonl(out_dir / f"{name}.jsonl", rows)
        combined = [row for rows in bundle_rows.values() for row in rows]
        write_jsonl(Path(args.out), combined)
        if args.audit_out:
            write_jsonl(Path(args.audit_out), [])
        print(json.dumps(schema_summary(combined), sort_keys=True))
        return
    payload, row_type, context = endpoint_payload(client, args)
    if args.raw_out:
        raw_out = Path(args.raw_out)
        raw_out.parent.mkdir(parents=True, exist_ok=True)
        raw_out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    rows, audit = normalize_payload(payload, row_type, context)
    write_jsonl(Path(args.out), rows)
    if args.audit_out:
        write_jsonl(Path(args.audit_out), audit)
    print(json.dumps(schema_summary(rows), sort_keys=True))


if __name__ == "__main__":
    main()
