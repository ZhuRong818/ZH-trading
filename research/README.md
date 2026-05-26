# Research Evaluation And Orchestration

This package is intentionally separate from the live trading pipeline, EMS/OMS,
and strategy deployment. It contains two report-only research tools:

- `signal_eval.py` reads recorded JSONL snapshots, emits standardized probability signals, and scores them after each 5-minute window is labeled.
- `orchestrator.py` runs batches of replay and/or signal-evaluation experiments from a spec, normalizes metrics, applies gates, ranks variants, and writes summaries.

## Signal Schema

Each emitted signal contains:

- `strategy_name`
- `market_id`
- `timestamp`
- `timestamp_iso`
- `asset`
- `predicted_prob_up`
- `confidence`
- `confidence_score`
- `reason`
- `seconds_remaining`
- `baseline_prob_up`
- `features`

## Metrics

The evaluator reports:

- accuracy
- Brier score
- log loss
- calibration ECE
- high-confidence precision
- high-confidence sample count
- average predicted UP probability
- actual UP rate

Brier score and log loss are the main metrics for probability forecasts.

## Commands

Quick smoke test:

```bash
python -m research.signal_eval \
  --file data_v2/market_data_2026-05-15.jsonl \
  --max-records 20000 \
  --sample-seconds 60
```

Evaluate all collected data and write outputs:

```bash
python -m research.signal_eval \
  --data-dir data_v2 \
  --sample-seconds 30 \
  --out-signals reports/research_signals_all.jsonl \
  --out-metrics reports/research_metrics_all.json
```

Evaluate selected strategies:

```bash
python -m research.signal_eval \
  --file data_v2/market_data_2026-05-15.jsonl \
  --strategies baseline_market_mid,momentum,oracle,leadlag
```

## Orchestrator

Run the default report-only experiment loop:

```bash
python -m research.orchestrator --spec research/experiments.yaml
```

Run a quick mechanics check with a record cap:

```bash
python -m research.orchestrator --spec research/experiments.yaml --max-records 5000
```

The experiment spec supports:

- `run_id`
- `data_dir` or `file`
- `assets`
- `bankroll`
- `gates`
- `experiments` with `name`, `mode`, `strategy`, `params`, and `tags`

Supported experiment modes:

- `replay`: calls `python -m backtest.replay --out-json ...`
- `signal_eval`: calls `python -m research.signal_eval --out-metrics ...`

Outputs are written to:

```text
reports/research_runs/<run_id>/summary.json
reports/research_runs/<run_id>/summary.md
reports/research_runs/<run_id>/<mode>_<experiment>.json
```

The orchestrator is report-only. It may recommend `promote_candidate`, `keep_testing`, or `reject`, but it does not modify `config/settings.py` or strategy code.

## Strategy Generation, Backtest, And Grading

The generic prediction-market research path is also report-only. It is meant
for generated strategy ideas that must be schema-checked, backtested, and
graded before any live strategy work begins.

Probe the kv.run OpenAPI/reference snapshot:

```bash
python -m research.schema_probe \
  prediction-market-kol-intelligence/datasets/api-1.jsonl \
  --kind openapi \
  --out reports/research_schema_probe.json
```

Normalize a local or fetched payload into canonical JSONL:

```bash
python -m backtest.loader_kvrun \
  --input path/to/raw_prediction_market_rows.jsonl \
  --out data/silver/prediction_markets_canonical.jsonl \
  --audit-out data/silver/prediction_markets_audit.jsonl
```

Fetch concrete kv.run prediction-market endpoints. `/reference` is the docs
page; data lives under `/prediction-markets/...`. Anonymous access works for
MVP usage with throttling, while `KVRUN_API_KEY` enables the higher authenticated
limit. Bearer auth is also supported through `KVRUN_BEARER_TOKEN`.

```bash
python -m backtest.loader_kvrun_prediction \
  --raw-out data/bronze/kvrun_btc_search.json \
  --out data/silver/kvrun_btc_search.jsonl \
  search --q bitcoin --venue polymarket --status open --limit 50

python -m backtest.loader_kvrun_prediction \
  --raw-out data/bronze/kvrun_btc_candles.json \
  --out data/silver/kvrun_btc_candles.jsonl \
  candles --venue polymarket --market-id "<condition_id>" --interval 1 --limit 5000

python -m backtest.loader_kvrun_prediction \
  --raw-out data/bronze/kvrun_btc_oi.json \
  --out data/silver/kvrun_btc_oi.jsonl \
  open-interest --venue polymarket --market-id "<condition_id>" --limit 500
```

Or fetch the immediate research bundle from search results:

```bash
# Markets + candles + open interest (no trades):
python -m backtest.loader_kvrun_prediction \
  --out data/silver/kvrun_btc_bundle.jsonl \
  bundle \
  --q bitcoin \
  --venue polymarket \
  --market-limit 10 \
  --out-dir data/silver/kvrun_btc_bundle

# Include trades for backtest scoring:
python -m backtest.loader_kvrun_prediction \
  --out data/silver/kvrun_btc_bundle.jsonl \
  bundle \
  --q bitcoin \
  --venue polymarket \
  --market-limit 10 \
  --include-trades \
  --trade-limit 500 \
  --out-dir data/silver/kvrun_btc_bundle
```

Writes per-type files to `--out-dir`: `markets.jsonl`, `candles.jsonl`,
`open_interest.jsonl`, and (with `--include-trades`) `trades.jsonl`.

For kv.run baseline studies, prefer `/prediction-markets/candles/...` over
local resampling. Use `backtest.resample` only for custom windows, locally
captured stream ticks, raw JSONL replay, or validation against kv.run candles.

Capture live SSE ticks as replayable data:

```bash
# Quick capture: 100 events then exit
python -m backtest.stream_kvrun \
  --max-events 100 \
  --raw-out data/kvrun/raw_stream_$(date +%Y%m%d).jsonl \
  --canonical-out data/kvrun/canonical_stream_$(date +%Y%m%d).jsonl

# Filtered by condition IDs:
python -m backtest.stream_kvrun \
  --condition-ids "<condition_id_1>,<condition_id_2>" \
  --raw-out data/kvrun/raw_stream_20260526.jsonl \
  --canonical-out data/kvrun/canonical_stream_20260526.jsonl
```

The SSE `market` field is aliased to both `market_id` and `condition_id`
in the canonical schema, so tick rows join correctly with market and trade rows.

Build a deterministic peer graph before adding embeddings:

```bash
python -m research.kvrun_peer_graph \
  --markets data/silver/kvrun_btc_bundle/markets.jsonl \
  --out reports/kvrun_btc_peer_edges.jsonl
```

Generate constrained strategy candidates from available fields:

```bash
python -m research.strategy_templates \
  --fields condition_id,outcome,bar_ts,close,volume,open_interest,secs_to_close
```

Or generate through the planner wrapper. The deterministic planner needs no
API key; `openai_compat` can be pointed at DeepSeek, Qwen-compatible gateways,
or another OpenAI-style provider:

```bash
python -m research.strategy_generator \
  --schema reports/research_schema_probe.json \
  --provider deterministic
```

Run a deterministic family study:

```bash
python -m research.generic_engine \
  --input data/silver/prediction_markets_canonical.jsonl \
  --family momentum \
  --out reports/research_runs/generic_momentum.json
```

Supported generic families:

- `momentum`
- `mean_reversion`
- `volume_shock`
- `early_attention`
- `open_interest_growth`
- `volatility_regime`
- `closing_time_behavior`

### Family Performance Notes

Empirical results from live kv.run candle data (crypto, election, fed markets):

| Family | Accuracy | Notes |
|--------|----------|-------|
| `mean_reversion` | **69-71%** | Best performer. Bets on price bouncing back from extremes (near 0 or 1). Dominant dynamic in digital options. |
| `momentum` | 16-22% | Systematically inverted — bets on continuation but markets revert. Inverting direction yields 78-84% accuracy. Consider `mean_reversion` instead. |
| `volume_shock` | 15-47% | Unreliable. Direction prediction is noisy. |
| `volatility_regime` | 10-28% | Weak signal. Volatility alone is not predictive of direction. |
| `early_attention` | 100% (5 sigs) | Too few signals to evaluate. Worth testing on more data. |
| `open_interest_growth` | 0 sigs | Requires `open_interest` field in canonical rows. Candle-only data lacks this. Fetch `open_interest` data or use `--include-trades` bundle mode. |
| `closing_time_behavior` | 0 sigs | Requires `seconds_remaining` or `secs_to_close` field. Candle-only data may lack time-to-close metadata. |

Prefer `mean_reversion` as the default baseline for new prediction-market
research. Use `open_interest_growth` and `closing_time_behavior` only with
bundles that include the required fields.

Use `--execution-track` only for research stress tests. The generic engine does
not place orders and does not wire into EMS/OMS.
