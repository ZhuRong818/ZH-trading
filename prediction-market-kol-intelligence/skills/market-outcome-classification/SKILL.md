# Market Outcome Classification

Purpose: map extracted predictions to a market domain and direction.

Domains:

- Politics / election
- Crypto price
- Crypto regulation
- Company earnings/performance
- Geopolitics / war / sanctions
- Macro / Fed / CPI / rates
- Sports
- Other

Directions:

- YES
- NO
- UP
- DOWN
- OVER
- UNDER
- WIN
- LOSE
- DELAY
- NO EVENT

Rules:

- Use topic entities first, then verbs and market terms.
- Classify direction separately from domain.
- Prefer `YES`/`NO` for event occurrence markets.
- Prefer `UP`/`DOWN` for price, rate, stock, or odds movement claims.
- Prefer `WIN`/`LOSE` for elections and sports.

## Market Matching (kv.run:5000)

After classification, resolve the prediction against live Polymarket markets:

```bash
# Search for matching market by event keywords
curl -s "https://kv.run:5000/prediction-markets/markets/search?q=<classified_event>"
```

Match the extracted domain + event text against the returned `title` and `slug` fields. Prefer markets where `end_date` aligns with the prediction's deadline and `closed` is false.

