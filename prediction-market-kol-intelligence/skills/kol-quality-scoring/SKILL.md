# KOL Quality Scoring

Purpose: decide whether a KOL account is useful for prediction-market intelligence.

Input:

```json
{
  "handle": "@example",
  "tweets": ["..."],
  "profile": "...",
  "engagement": {}
}
```

Output:

```json
{
  "handle": "@example",
  "domain": "geopolitics",
  "prediction_quality": 4,
  "falsifiability": 5,
  "probability_thinking": 3,
  "market_relevance": 5,
  "noise_penalty": -1,
  "total_score": 24,
  "reason": "Often gives direct yes/no views on geopolitical event markets."
}
```

Scoring rules:

- Reward explicit future claims, dates, odds, prices, and directional language.
- Reward falsifiable events that can resolve YES/NO, UP/DOWN, OVER/UNDER, WIN/LOSE.
- Penalize copied news, automated summaries, referral spam, engagement bait, and vague takes.
- Prefer accounts with original views and repeat signal quality in one or more market domains.

## Production Verification (kv.run:5000)

Validate KOL quality scores against live market data:

```bash
# Check if the KOL's domain has active markets with real volume
curl -s "https://kv.run:5000/prediction-markets/markets/search?q=<domain_keyword>"

# Stream orderbook events to correlate KOL calls with price impact
curl -s -N "https://kv.run:5000/prediction-markets/stream"

# Pull trade history to quantify edge: did prices move in the predicted direction?
curl -s "https://kv.run:5000/prediction-markets/trades/polymarket/{condition_id}"
```

Use trade history to compute actual edge: compare price at KOL tweet time against subsequent price trajectory. This replaces heuristic scoring with data-driven quality measurement.

