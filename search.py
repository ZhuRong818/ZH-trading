"""
Search Polymarket markets by keyword or strategy fit.

Usage:
    python search.py bitcoin                    # search by keyword
    python search.py iran --min-volume 100000   # filter by volume
    python search.py                            # top markets by volume
    python search.py --mm                       # best markets for market making
    python search.py --mm --limit 10            # top 10 MM candidates
"""

import json
import sys
from datetime import datetime, timezone

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"


def fetch_all_markets(max_pages: int = 30) -> list:
    """Fetch active markets ordered by volume."""
    all_markets = []
    for offset in range(0, max_pages * 100, 100):
        resp = requests.get(
            f"{GAMMA_BASE}/markets",
            params={
                "_limit": 100,
                "_offset": offset,
                "active": True,
                "closed": False,
                "order": "volume24hr",
                "ascending": False,
            },
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        all_markets.extend(batch)
    return all_markets


def parse_market(m: dict) -> dict:
    """Parse raw Gamma API market into a clean dict."""
    outcomes_raw = m.get("outcomePrices", "[]")
    prices = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])
    outcomes = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else (m.get("outcomes") or [])
    clob_raw = m.get("clobTokenIds", "[]")
    clob_ids = json.loads(clob_raw) if isinstance(clob_raw, str) else (clob_raw or [])

    vol = float(m.get("volume24hr", 0))
    liq = float(m.get("liquidity", 0))
    end_date = m.get("endDate", "")
    spread = float(m.get("spread", 0)) if m.get("spread") else None
    prices_float = [float(p) for p in prices] if prices else []

    # Days to resolution
    days_left = None
    if end_date:
        try:
            end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            days_left = max((end - datetime.now(timezone.utc)).total_seconds() / 86400, 0)
        except Exception:
            pass

    return {
        "question": m.get("question", ""),
        "outcomes": outcomes,
        "prices": prices_float,
        "volume_24h": vol,
        "liquidity": liq,
        "condition_id": m.get("conditionId", ""),
        "end_date": end_date,
        "days_left": days_left,
        "spread": spread,
        "clob_ids": clob_ids,
        "neg_risk": m.get("negRisk", False),
        "tick_size": m.get("orderPriceMinTickSize", "0.01"),
    }


def search(query: str = "", limit: int = 20, min_volume: float = 0) -> list:
    """Search markets by keyword."""
    all_markets = fetch_all_markets(max_pages=30 if query else 1)

    if query:
        q = query.lower()
        all_markets = [m for m in all_markets if q in m.get("question", "").lower()]

    results = []
    for m in all_markets:
        parsed = parse_market(m)
        if parsed["volume_24h"] >= min_volume:
            results.append(parsed)

    return results[:limit]


def search_mm(limit: int = 20) -> list:
    """
    Find the best markets for market making.

    Scoring criteria:
      - Price in contested zone (0.30-0.70): +40 points
      - High volume (log scale):             +25 points
      - High liquidity (log scale):          +15 points
      - Days to resolution (14-60 sweet spot): +20 points

    Filters out:
      - Price < 0.15 or > 0.85 (tail zone, one-sided flow)
      - Volume < $50k/day (not enough counterparties)
      - Resolves within 3 days (not enough time)
      - Already resolved or no end date
    """
    import math

    all_markets = fetch_all_markets(max_pages=10)
    scored = []

    for m in all_markets:
        parsed = parse_market(m)
        prices = parsed["prices"]
        vol = parsed["volume_24h"]
        liq = parsed["liquidity"]
        days = parsed["days_left"]

        # Hard filters
        if not prices or len(prices) < 2:
            continue
        yes_price = prices[0]
        if yes_price < 0.15 or yes_price > 0.85:
            continue
        if vol < 50_000:
            continue
        if days is None or days < 3:
            continue

        # Score: contested zone (best at 0.50)
        distance_from_center = abs(yes_price - 0.50)
        contested_score = max(0, 1.0 - distance_from_center / 0.35) * 40

        # Score: volume (log scale, $100k = 20, $1M = 25)
        vol_score = min(math.log10(max(vol, 1)) / 7.0, 1.0) * 25

        # Score: liquidity
        liq_score = min(math.log10(max(liq, 1)) / 7.0, 1.0) * 15

        # Score: days to resolution (sweet spot 14-60 days)
        if days < 7:
            days_score = days / 7.0 * 10
        elif days < 14:
            days_score = 15
        elif days <= 60:
            days_score = 20
        else:
            days_score = max(0, 20 - (days - 60) / 30 * 5)

        total_score = contested_score + vol_score + liq_score + days_score

        parsed["mm_score"] = round(total_score, 1)
        scored.append(parsed)

    # Deduplicate by condition_id
    seen = set()
    unique = []
    for s in scored:
        cid = s["condition_id"]
        if cid and cid not in seen:
            seen.add(cid)
            unique.append(s)

    unique.sort(key=lambda x: x["mm_score"], reverse=True)
    return unique[:limit]


def print_results(results: list, header: str, show_score: bool = False):
    if not results:
        print(f"No markets found for {header}")
        return

    print(f"\nFound {len(results)} market(s) for {header}:\n")
    for i, r in enumerate(results):
        prices_str = " / ".join(
            f"{o}={p:.3f}" for o, p in zip(r["outcomes"], r["prices"])
        )
        end = r["end_date"][:10] if r["end_date"] else "no date"
        days = f"{r['days_left']:.0f}d" if r["days_left"] is not None else "?"

        score_str = f"  score={r['mm_score']}" if show_score and "mm_score" in r else ""

        print(f"  [{i:2d}] {r['question']}")
        print(f"       {prices_str}")
        print(f"       vol=${r['volume_24h']:,.0f}  liq=${r['liquidity']:,.0f}  ends={end} ({days}){score_str}")
        if r["clob_ids"]:
            print(f"       YES token={r['clob_ids'][0][:40]}...")
        print()


def main():
    query = ""
    limit = 20
    min_volume = 0
    mm_mode = False

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        elif args[i] == "--min-volume" and i + 1 < len(args):
            min_volume = float(args[i + 1])
            i += 2
        elif args[i] == "--mm":
            mm_mode = True
            i += 1
        else:
            if not args[i].startswith("--"):
                query = args[i]
            i += 1

    if mm_mode:
        results = search_mm(limit)
        print_results(results, "market making candidates (ranked by score)", show_score=True)
    else:
        results = search(query, limit, min_volume)
        header = f"'{query}'" if query else "all (by volume)"
        print_results(results, header)


if __name__ == "__main__":
    main()
