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

