"""Deterministic peer graph helpers for kv.run prediction-market research."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any

from backtest.canonicalize import parse_payload_text


STOPWORDS = {
    "the",
    "and",
    "or",
    "will",
    "this",
    "that",
    "with",
    "from",
    "market",
    "above",
    "below",
    "before",
    "after",
}


def build_peer_edges(markets: list[dict[str, Any]], matched_pairs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_end_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for market in markets:
        event_id = _first(market, "event_id", "eventId", "event", "slug")
        if event_id:
            by_event[str(event_id)].append(market)
        end_bucket = _end_bucket(_first(market, "end_date", "endDate", "market_close_at", "close_time"))
        if end_bucket:
            by_end_bucket[end_bucket].append(market)

    for event_id, group in by_event.items():
        edges.extend(_pair_edges(group, "same_event", 1.0, {"event_id": event_id}))
    for end_bucket, group in by_end_bucket.items():
        edges.extend(_pair_edges(group, "same_end_bucket", 0.25, {"end_bucket": end_bucket}))

    token_cache = {market_id(market): _tokens(title(market)) for market in markets}
    for left, right in combinations(markets, 2):
        left_id = market_id(left)
        right_id = market_id(right)
        if not left_id or not right_id or left_id == right_id:
            continue
        score = _jaccard(token_cache[left_id], token_cache[right_id])
        if score >= 0.25:
            edges.append({"source": left_id, "target": right_id, "relation": "title_token_overlap", "weight": score})

    for pair in matched_pairs or []:
        source = str(_first(pair, "source_id", "venue_id", "market_id", "condition_id") or "")
        target = str(_first(pair, "target_id", "matched_id", "other_market_id", "kalshi_ticker", "polymarket_condition_id") or "")
        if source and target and source != target:
            edges.append({"source": source, "target": target, "relation": "matched_pair", "weight": 1.0})

    return _dedupe_edges(edges)


def market_id(market: dict[str, Any]) -> str:
    return str(_first(market, "condition_id", "conditionId", "ticker", "market_id", "id", "slug") or "")


def title(market: dict[str, Any]) -> str:
    return str(_first(market, "question", "title", "name", "slug") or "")


def _pair_edges(group: list[dict[str, Any]], relation: str, weight: float, extra: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for left, right in combinations(group[:200], 2):
        source = market_id(left)
        target = market_id(right)
        if source and target and source != target:
            out.append({"source": source, "target": target, "relation": relation, "weight": weight, **extra})
    return out


def _dedupe_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in edges:
        left, right = sorted((edge["source"], edge["target"]))
        key = (left, right, edge["relation"])
        normalized = {**edge, "source": left, "target": right}
        if key not in best or float(normalized.get("weight", 0)) > float(best[key].get("weight", 0)):
            best[key] = normalized
    return sorted(best.values(), key=lambda item: (item["source"], item["target"], item["relation"]))


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", text.lower())
        if token not in STOPWORDS
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _end_bucket(value: Any) -> str:
    if not value:
        return ""
    return str(value)[:10]


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
        lowered = key.lower()
        for actual, value in row.items():
            if actual.lower() == lowered and value not in (None, ""):
                return value
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Build deterministic kv.run market peer graph")
    parser.add_argument("--markets", required=True, help="Market JSON/JSONL")
    parser.add_argument("--matched-pairs", default="", help="Optional matched-pairs JSON/JSONL")
    parser.add_argument("--out", required=True, help="Peer edge JSONL")
    args = parser.parse_args()

    markets = parse_payload_text(Path(args.markets).read_text(encoding="utf-8"))
    pairs = parse_payload_text(Path(args.matched_pairs).read_text(encoding="utf-8")) if args.matched_pairs else []
    edges = build_peer_edges(markets, pairs)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for edge in edges:
            fh.write(json.dumps(edge, sort_keys=True) + "\n")
    print(json.dumps({"edges": len(edges)}, sort_keys=True))


if __name__ == "__main__":
    main()
