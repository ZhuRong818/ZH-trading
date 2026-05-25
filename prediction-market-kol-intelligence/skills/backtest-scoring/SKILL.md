# Backtest Scoring

Purpose: measure whether KOL predictions were useful after matching tweets to resolved markets.

Metrics:

- hit rate
- Brier score
- calibration
- average odds move after tweet
- ROI if traded at tweet time
- lead time before resolution
- domain-specific accuracy
- noise-adjusted score

Output:

```json
{
  "handle": "@example",
  "markets_tested": 42,
  "hit_rate": 0.61,
  "avg_price_edge": 0.08,
  "brier_score": 0.21,
  "best_domain": "elections",
  "verdict": "useful"
}
```

## Production API (kv.run:5000)

Resolved market matching and trade history are now available live:

```bash
# 1. Find the market by keyword
curl -s "https://kv.run:5000/prediction-markets/markets/search?q=<keyword>"

# 2. Pull trade history for the matched condition_id
curl -s "https://kv.run:5000/prediction-markets/trades/polymarket/{condition_id}"

# 3. Check orderbook depth at time of KOL tweet
curl -s "https://kv.run:5000/prediction-markets/orderbook/polymarket/{asset_id}"
```

## Workflow

1. Extract a KOL prediction event + direction + deadline.
2. Search for matching Polymarket markets via `GET /prediction-markets/markets/search?q=<event keywords>`.
3. Select the market with the closest title/slug match and matching end_date window.
4. Pull trades via `GET /prediction-markets/trades/polymarket/{condition_id}` to reconstruct price history.
5. Compare KOL tweet timestamp against trade prices: entry price at tweet time vs final resolution price.
6. Compute hit rate, Brier score, lead time, and domain-specific accuracy from the matched pairs.

