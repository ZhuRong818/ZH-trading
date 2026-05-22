---
name: x-kol-discovery
description: Find and rank X/Twitter KOLs for a requested Polymarket-style domain using Method B tweet search -> extract authors, staged recent-tweet fetch, spam filtering, signal extraction, compact candidate packets, heuristic or LLM-ready scoring, and top-N result export.
---

# X KOL Discovery

Use this skill when the user wants an end-to-end KOL finder where they specify a domain/type and count, and the system returns ranked X accounts with evidence.

## Core Design

Default discovery method is Method B:

```text
topic tweet search
-> extract authors from tweet.author
-> dedupe by author.id, fallback userName.lower()
-> coarse author filter
-> staged recent-tweets fetch
-> spam/noise filter
-> predictive signal extraction
-> KOL packet build
-> heuristic and/or LLM scoring
-> top-N CSV/JSONL export
```

Do not use browser automation as the production path. Use it only for manual research. Prefer API/search evidence that can be deduped, cached, audited, and rerun.

## User Request Schema

Minimum input:

```json
{
  "kol_type": "prediction_market",
  "domain": "Politics / election",
  "count": 50,
  "language": "en",
  "min_followers": 0,
  "recent_posts_per_candidate": 40,
  "llm_provider": "none",
  "llm_model": "heuristic"
}
```

Supported domains:

```text
prediction_market
politics/election
crypto
sports
geopolitics
macro
company/events
general-polymarket
```

## Local MVP

When tweet JSONL already exists, run:

```bash
python3 prediction-market-kol-intelligence/skills/x-kol-discovery/scripts/find_kols.py \
  --domain "Politics / election" \
  --count 50 \
  --input path/to/tweets.jsonl \
  --out reports/kol_intelligence/x_kol_discovery_politics.csv \
  --packets_out reports/kol_intelligence/x_kol_discovery_politics_packets.jsonl
```

This uses the existing spam filter and predictive signal extractor from `prediction-market-kol-intelligence/scripts/run_mvp.py`.

## Search Discovery

When discovering beyond the current pool, run Method B search:

```bash
export TWITTERAPI_IO_KEY="..."

python3 prediction-market-kol-intelligence/skills/x-kol-discovery/scripts/search_discovery.py \
  --domain "prediction_market" \
  --language en \
  --max_pages_per_query 3 \
  --out reports/kol_intelligence/x_kol_discovery_candidates.jsonl
```

Then fetch recent tweets for those candidates with the existing fetch scripts, or use the candidate handles as a new pool.

## Ranking Rules

Hard reject or penalize:

- explicit automated account marker
- referral spam
- trader/PnL flex threads
- copied news with no original view
- automated market summaries
- high retrospective recap rate
- too few original predictive tweets

Reward:

- clear event/outcome claims
- explicit direction: YES/NO, UP/DOWN, OVER/UNDER, WIN/LOSE
- deadline or natural resolution window
- probability/odds/pricing language
- original reasoning and causal explanation
- domain focus
- enough recent activity

## LLM Layer

Use LLM only after compact packets are built. Do not send full raw tweet histories.

Run LLM rerank:

```bash
export DEEPSEEK_API_KEY="..."

python3 prediction-market-kol-intelligence/skills/x-kol-discovery/scripts/find_kols.py \
  --domain "Politics / election" \
  --count 50 \
  --input path/to/tweets.jsonl \
  --out reports/kol_intelligence/x_kol_discovery_politics_llm.csv \
  --packets_out reports/kol_intelligence/x_kol_discovery_politics_llm_packets.jsonl \
  --ranker llm \
  --llm_provider deepseek \
  --llm_model deepseek-v4-flash \
  --llm_candidate_limit 50
```

For Qwen:

```bash
export DASHSCOPE_API_KEY="..."
--llm_provider qwen --llm_model qwen3.5-flash
```

Packet fields:

```json
{
  "handle": "...",
  "tweets_seen": 1000,
  "signals": 42,
  "signal_density": 0.042,
  "spam_rate": 0.05,
  "recap_rate": 0.1,
  "falsifiability_rate": 0.7,
  "deadline_rate": 0.15,
  "top_domain": "Politics / election",
  "sample_predictive_tweets": [],
  "sample_rejected_tweets": []
}
```

Expected LLM output:

```json
{
  "handle": "...",
  "verdict": "include",
  "domain": "Politics / election",
  "topic_fit": 87,
  "originality": 82,
  "reasoning_quality": 79,
  "signal_density": 76,
  "engagement_quality": 55,
  "noise_penalty": 8,
  "total_score": 84.6,
  "reason": "Focused on event-level market calls.",
  "best_evidence_tweet_url": "...",
  "risk_flags": []
}
```

## References

- Query templates: `references/query_templates.yaml`
- LLM scorer prompt: `references/llm_ranker_prompt.md`
- Production notes: `references/production_notes.md`
