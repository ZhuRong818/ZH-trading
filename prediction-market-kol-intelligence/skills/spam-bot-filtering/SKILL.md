# Spam/Bot Filtering

Purpose: reject noisy accounts and tweets that do not contain useful original prediction signal.

Hard-reject tweets that are mostly:

- referral spam
- generic "edge" posts without reasoning
- copied news without an original view
- automated market summaries
- engagement bait
- no clear event outcome
- pure volume/price alerts without a thesis
- trader-flex threads whose main claim is another trader's PnL or win rate
- "guaranteed" or "free money" market-promo posts
- posts funneling users to referral links, wallet links, profile links, or bio links
- automated daily betting summaries with volume, top-holder, edge, or ROI fields

Typical spam patterns:

```text
I found a Polymarket trader who made 3,100 trades with a 99% win rate...
His positions: https://predictparity.com/...?...code=...
```

Reject reason: `trader_flex_thread` or `predictparity_referral_link`.

```text
Top 2 Polymarket trader can hit $10,000,000 profit next month
Wallet link http://polymarket.com/@rn1?r=deposited
```

Reject reason: `polymarket_referral_link` or `trader_flex_thread`.

```text
go to polymarket - open the market ... buy NO.
that's a guaranteed winning position ... free money
```

Reject reason: `low_value_market_promo`.

```text
@signal daily_ 自动发推
Who wins this Cs2 BO3...
volume surged 50% in 24h
Edge: 29.0% ROI
Trade in bio
```

Reject reason: `automated_market_summary` or `referral_or_engagement_spam`.

Implementation rules:

```text
retweet -> reject
very short tweet -> reject
referral / code / trade-in-bio pattern -> reject
2+ trader-flex patterns -> reject
2+ automated-market-summary patterns -> reject
2+ low-value-market-promo patterns -> reject
Polymarket profile URL with ?r= referral -> reject
PredictParity URL with ?code= referral -> reject
3+ links with no prediction language -> reject
```

Signals of usefulness:

- first-person or analyst view
- explicit probability, odds, or directional claim
- market-resolvable event
- deadline or natural resolution window
- nontrivial reasoning
- original thesis, not just "copy this trade"
- reasoning that can be separated from promotional links

Output:

```json
{
  "is_spam": true,
  "reason": "automated_market_summary"
}
```

Allowed reject reasons:

```text
retweet
too_short
referral_or_engagement_spam
trader_flex_thread
automated_market_summary
low_value_market_promo
polymarket_referral_link
predictparity_referral_link
link_farm
retrospective_noise
```
