# Predictive Tweet Extraction

Purpose: convert messy KOL tweets into structured prediction records.

Input fields:

- `kol_username`
- `tweet.id`
- `tweet.text`
- `tweet.createdAt`
- engagement fields from `tweet`

Output:

```json
{
  "event": "Iran closes its airspace by May 8",
  "direction": "NO",
  "deadline": "2026-05-08",
  "confidence": null,
  "domain": "geopolitics",
  "is_falsifiable": true
}
```

Extraction rules:

- Keep only future-looking or market-like claims.
- Extract event as a compact normalized proposition.
- Extract direction from explicit negation, bullish/bearish language, win/lose language, odds, or market side.
- Extract deadline from dates like `by May 8`, `before Friday`, `in 2026`, or market title deadlines.
- If deadline is absent but the event is clearly market-resolvable, leave `deadline` null and keep the candidate.

