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

MVP status: scoring placeholders are included in the local runner; resolved market matching is a separate dataset dependency.

