# prediction-market-kol-intelligence

MVP app scaffold for turning KOL tweets into prediction-market signals.

Pipeline:

```text
fetch KOL tweets
-> filter spam/noise
-> detect predictive tweets
-> extract event + direction + deadline
-> classify market domain
-> score KOL
-> match to Polymarket-style market
-> backtest outcome
-> publish ranked signals
```

This version does not require Polymarket data. It builds the prediction intelligence layer first:

```text
tweet JSONL
-> candidate predictive tweet extraction
-> domain / direction / deadline classification
-> tweet-only KOL proxy scoring
-> latest signal feed
-> manual labeling sample
```

Market matching and backtest scoring stay as placeholders until resolved market data is available.

Local MVP run:

```bash
python3 prediction-market-kol-intelligence/scripts/run_mvp.py \
  --input path/to/tweets.jsonl \
  --signals_out reports/kol_intelligence/signals.jsonl \
  --leaderboard_out reports/kol_intelligence/leaderboard.csv \
  --signal_feed_out reports/kol_intelligence/signal_feed.csv \
  --labeling_out reports/kol_intelligence/labeling_sample.jsonl \
  --rejected_out reports/kol_intelligence/rejected_spam_samples.jsonl \
  --max_rows 0
```

Use `--max_rows 10000` for a quick sample run.

Outputs:

```text
signals.jsonl
  Structured prediction-like tweet records with event, direction, deadline, domain, score, and tweet URL.

leaderboard.csv
  Tweet-only KOL ranking with signal density, falsifiability rate, deadline rate, domain focus, engagement, and proxy score.

signal_feed.csv
  Recent high-signal rows for dashboard review.

labeling_sample.jsonl
  Pre-filled manual labeling set. Fill label_* fields to create benchmark data.

rejected_spam_samples.jsonl
  Sample rejected spam/noise tweets by reason, useful for auditing the spam-bot filter.
```

Useful next step without Polymarket data:

1. Run the MVP on your tweet JSONL exports.
2. Manually label `reports/kol_intelligence/labeling_sample.jsonl`.
3. Move confirmed labels into `datasets/labeled_predictive_tweets.jsonl`.
4. Use the leaderboard to choose 50 KOLs for deeper review.
5. Add resolved market data later for market matching and backtests.

## End-to-End KOL Finder

To let a user specify the kind of Polymarket KOL they want and how many they need, use the `x-kol-discovery` skill:

```bash
python3 prediction-market-kol-intelligence/skills/x-kol-discovery/scripts/find_kols.py \
  --domain "Politics / election" \
  --count 50 \
  --input path/to/tweets.jsonl \
  --out reports/kol_intelligence/x_kol_discovery_politics.csv \
  --packets_out reports/kol_intelligence/x_kol_discovery_politics_packets.jsonl
```

Optional LLM rerank:

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

To discover candidates beyond the current pool, use Method B search:

```bash
export TWITTERAPI_IO_KEY="..."

python3 prediction-market-kol-intelligence/skills/x-kol-discovery/scripts/search_discovery.py \
  --domain prediction_market \
  --max_pages_per_query 3 \
  --out reports/kol_intelligence/x_kol_discovery_candidates.jsonl
```

Then fetch recent tweets for the discovered handles and run `find_kols.py` on the resulting JSONL.

One-command live discovery with LLM scoring:

```bash
export TWITTERAPI_IO_KEY="..."
export DEEPSEEK_API_KEY="..."

python3 prediction-market-kol-intelligence/skills/x-kol-discovery/scripts/run_live_discovery.py \
  --domain "pandemic forecasters and public health outbreak analysts" \
  --rank_domain "Health / pandemic" \
  --count 20 \
  --min_signals 1 \
  --query_planner llm \
  --discovery_mode topic_density \
  --query_count 8 \
  --ranker llm \
  --llm_provider deepseek \
  --llm_model deepseek-v4-flash \
  --candidate_limit 50 \
  --tweets_per_candidate 50 \
  --max_pages_per_query 1 \
  --start 2026-02-03 \
  --until 2026-05-21 \
  --work_prefix reports/kol_intelligence/live_x_kol_test
```

`--query_planner llm` asks the LLM to generate X advanced-search queries for the requested domain before calling twitterapi.io.
`--discovery_mode topic_density` makes those queries target accounts with repeated domain expertise, not one-off keyword matches.
`--tweets_per_candidate 50` caps the fetch stage at the 50 most recent kept tweets per discovered account.
Use `--ranker heuristic` if you want to skip LLM scoring.

This writes:

```text
reports/kol_intelligence/live_x_kol_test_candidates.jsonl
reports/kol_intelligence/live_x_kol_test_handles.txt
reports/kol_intelligence/live_x_kol_test_tweets.jsonl
reports/kol_intelligence/live_x_kol_test_ranked.csv
reports/kol_intelligence/live_x_kol_test_packets.jsonl
```
