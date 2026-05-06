"""
Search Polymarket markets by keyword.

Usage:
    python search.py bitcoin
    python search.py "world cup"
    python search.py iran --limit 20
    python search.py trump --min-volume 100000
    python search.py              # no keyword = top markets by volume
"""

import json
import sys
import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"


def search(query: str = "", limit: int = 20, min_volume: float = 0):
    # Fetch top markets by volume (most likely to contain what you want)
    all_markets = []
    for offset in range(0, 3000, 100):
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

        # Early stop: if we already have enough matches, stop paginating
        if query:
            q = query.lower()
            matches = [m for m in all_markets if q in m.get("question", "").lower()]
            if len(matches) >= limit:
                break
        else:
            if len(all_markets) >= limit:
                break

    # Filter by keyword
    if query:
        q = query.lower()
        all_markets = [m for m in all_markets if q in m.get("question", "").lower()]

    results = []
    for m in all_markets:
        outcomes_raw = m.get("outcomePrices", "[]")
        prices = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])
        outcomes = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else (m.get("outcomes") or [])
        vol = float(m.get("volume24hr", 0))

        if vol < min_volume:
            continue

        clob_raw = m.get("clobTokenIds", "[]")
        clob_ids = json.loads(clob_raw) if isinstance(clob_raw, str) else (clob_raw or [])

        results.append({
            "question": m.get("question", ""),
            "outcomes": outcomes,
            "prices": [float(p) for p in prices],
            "volume_24h": vol,
            "liquidity": float(m.get("liquidity", 0)),
            "condition_id": m.get("conditionId", ""),
            "end_date": m.get("endDate", ""),
            "clob_ids": clob_ids,
        })

    # Already sorted by volume from API
    return results[:limit]


def main():
    query = ""
    limit = 20
    min_volume = 0

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        elif args[i] == "--min-volume" and i + 1 < len(args):
            min_volume = float(args[i + 1])
            i += 2
        elif not args[i].startswith("--"):
            query = args[i]
            i += 1
        else:
            i += 1

    results = search(query, limit, min_volume)

    if not results:
        print(f"No markets found" + (f" for '{query}'" if query else ""))
        return

    header = f"'{query}'" if query else "all (by volume)"
    print(f"\nFound {len(results)} market(s) for {header}:\n")
    for i, r in enumerate(results):
        prices_str = " / ".join(
            f"{o}={p:.3f}" for o, p in zip(r["outcomes"], r["prices"])
        )
        end = r["end_date"][:10] if r["end_date"] else "no date"
        print(f"  [{i:2d}] {r['question']}")
        print(f"       {prices_str}")
        print(f"       vol=${r['volume_24h']:,.0f}  liq=${r['liquidity']:,.0f}  ends={end}")
        if r["clob_ids"]:
            print(f"       YES token={r['clob_ids'][0][:40]}...")
        print()


if __name__ == "__main__":
    main()
