"""
Polymarket Order Book Fetcher

Fetches order book data from Polymarket's CLOB API.
No authentication required for read-only access.

Usage:
    python polymarket_orderbook.py                     # List active markets
    python polymarket_orderbook.py --search "bitcoin"  # Search markets
    python polymarket_orderbook.py --token TOKEN_ID    # Get order book for a token
    python polymarket_orderbook.py --market CONDITION_ID  # Get order books for both sides of a market
"""

import argparse
import json
import requests

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"


def search_markets(query: str, limit: int = 10) -> list[dict]:
    """Search for markets on Polymarket via the Gamma API."""
    resp = requests.get(
        f"{GAMMA_BASE}/markets",
        params={"_limit": limit, "active": True, "closed": False, "slug": query},
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        # Fallback: try listing all and filtering client-side
        resp = requests.get(
            f"{GAMMA_BASE}/markets",
            params={"_limit": 100, "active": True, "closed": False},
        )
        resp.raise_for_status()
        query_lower = query.lower()
        results = [m for m in resp.json() if query_lower in m.get("question", "").lower()][:limit]
    return results


def list_markets(limit: int = 10) -> list[dict]:
    """List active markets."""
    resp = requests.get(
        f"{GAMMA_BASE}/markets",
        params={"_limit": limit, "active": True, "closed": False, "order": "volume24hr", "ascending": False},
    )
    resp.raise_for_status()
    return resp.json()


def get_order_book(token_id: str) -> dict:
    """Get the order book for a single token."""
    resp = requests.get(f"{CLOB_BASE}/book", params={"token_id": token_id})
    resp.raise_for_status()
    return resp.json()


def get_midpoint(token_id: str) -> dict:
    """Get the midpoint price for a token."""
    resp = requests.get(f"{CLOB_BASE}/midpoint", params={"token_id": token_id})
    resp.raise_for_status()
    return resp.json()


def get_spread(token_id: str) -> dict:
    """Get the spread for a token."""
    resp = requests.post(f"{CLOB_BASE}/spreads", json=[{"token_id": token_id}])
    resp.raise_for_status()
    return resp.json()


def parse_tokens(market: dict) -> list[dict]:
    """Extract token info from a market response."""
    outcomes = json.loads(market.get("outcomes", "[]")) if isinstance(market.get("outcomes"), str) else (market.get("outcomes") or [])
    clob_ids = json.loads(market.get("clobTokenIds", "[]")) if isinstance(market.get("clobTokenIds"), str) else (market.get("clobTokenIds") or [])
    prices_raw = market.get("outcomePrices", "[]")
    prices = json.loads(prices_raw) if isinstance(prices_raw, str) else (prices_raw or [])

    tokens = []
    for i in range(len(outcomes)):
        tokens.append({
            "outcome": outcomes[i] if i < len(outcomes) else "?",
            "token_id": clob_ids[i] if i < len(clob_ids) else None,
            "price": prices[i] if i < len(prices) else "N/A",
        })
    return tokens


def print_market_summary(market: dict):
    """Print a summary of a market."""
    print(f"\n{'='*60}")
    print(f"Question: {market.get('question', 'N/A')}")
    print(f"Condition ID: {market.get('conditionId', 'N/A')}")

    tokens = parse_tokens(market)
    for token in tokens:
        print(f"  {token['outcome']}: price={token['price']}  token_id={token['token_id']}")

    volume = market.get("volume24hr", "N/A")
    liquidity = market.get("liquidity", "N/A")
    print(f"  24h Volume: {volume}  |  Liquidity: {liquidity}")


def print_order_book(book: dict, label: str = ""):
    """Pretty-print an order book."""
    if label:
        print(f"\n--- Order Book: {label} ---")
    else:
        print(f"\n--- Order Book ---")

    bids = book.get("bids", [])
    asks = book.get("asks", [])

    print(f"\n  {'BIDS':<30} {'ASKS'}")
    print(f"  {'Price':<12} {'Size':<16} {'Price':<12} {'Size'}")
    print(f"  {'-'*12} {'-'*16} {'-'*12} {'-'*16}")

    max_rows = max(len(bids), len(asks))
    for i in range(min(max_rows, 20)):
        bid_price = bids[i]["price"] if i < len(bids) else ""
        bid_size = bids[i]["size"] if i < len(bids) else ""
        ask_price = asks[i]["price"] if i < len(asks) else ""
        ask_size = asks[i]["size"] if i < len(asks) else ""
        print(f"  {bid_price:<12} {bid_size:<16} {ask_price:<12} {ask_size}")

    if max_rows > 20:
        print(f"  ... ({max_rows - 20} more levels)")

    print(f"\n  Total bid levels: {len(bids)}  |  Total ask levels: {len(asks)}")


def main():
    parser = argparse.ArgumentParser(description="Polymarket Order Book Fetcher")
    parser.add_argument("--search", type=str, help="Search for markets by keyword")
    parser.add_argument("--token", type=str, help="Get order book for a specific token ID")
    parser.add_argument("--market", type=str, help="Get order books for a market (condition ID)")
    parser.add_argument("--limit", type=int, default=10, help="Number of markets to list (default: 10)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    if args.token:
        book = get_order_book(args.token)
        if args.json:
            print(json.dumps(book, indent=2))
        else:
            print_order_book(book)

    elif args.market:
        # Find the market to get token IDs
        resp = requests.get(f"{GAMMA_BASE}/markets", params={"conditionId": args.market})
        resp.raise_for_status()
        markets = resp.json()
        if not markets:
            print(f"No market found with conditionId: {args.market}")
            return

        market = markets[0]
        print_market_summary(market)

        for token in parse_tokens(market):
            if not token["token_id"]:
                continue
            book = get_order_book(token["token_id"])
            if args.json:
                print(json.dumps(book, indent=2))
            else:
                print_order_book(book, label=token["outcome"])

    elif args.search:
        markets = search_markets(args.search, limit=args.limit)
        if not markets:
            print("No markets found.")
            return
        for m in markets:
            print_market_summary(m)

    else:
        # List top markets by volume
        print("Top active markets by 24h volume:\n")
        markets = list_markets(limit=args.limit)
        if not markets:
            print("No markets found.")
            return
        for m in markets:
            print_market_summary(m)

        print(f"\n{'='*60}")
        print("\nTo get an order book, run:")
        print("  python polymarket_orderbook.py --token <TOKEN_ID>")
        print("  python polymarket_orderbook.py --market <CONDITION_ID>")


if __name__ == "__main__":
    main()
