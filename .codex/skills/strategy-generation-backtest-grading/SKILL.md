---
name: strategy-generation-backtest-grading
description: Generate report-only Polymarket-style research strategies, probe kv.run schemas, map raw data to canonical fields, run deterministic forecast/execution backtests, and grade candidates without editing live strategy code or placing orders.
---

# Strategy Generation, Backtest, And Grading

Use this skill when the user wants a research factory for prediction-market
strategy ideas rather than a live `strategies/v2/` implementation.

## Scope

This skill is report-only. It may create research artifacts, metrics, and
recommendations, but it must not change live strategy config, EMS/OMS behavior,
or execution code unless the user explicitly switches to `strategy-creator`.

The deterministic path is:

```text
schema probe
-> canonicalize raw rows
-> resample bars/features/labels
-> generate constrained strategy candidates
-> run forecast and optional execution-track studies
-> grade A/B/C/Reject
-> explain results
```

## Repo Map

- `research/schema_probe.py` inspects OpenAPI or sample rows.
- `backtest/loader_kvrun.py` fetches/normalizes endpoint data.
- `backtest/canonicalize.py` owns canonical field aliases and validation.
- `backtest/resample.py` builds OHLCV-style bars.
- `backtest/execution_costs.py` owns fee, VWAP, and sizing helpers.
- `research/strategy_templates.py` lists allowed research families.
- `research/generic_engine.py` runs deterministic family studies.
- `research/grader.py` applies A/B/C/Reject grading.
- `research/orchestrator.py` remains the report-only batch runner for existing replay/signal_eval experiments.

## kv.run Rules

Do not guess fields. First probe:

```bash
python -m research.schema_probe prediction-market-kol-intelligence/datasets/api-1.jsonl --kind openapi
```

For live endpoints, prefer `/openapi.json` or concrete prediction-market
paths from the reference. `https://kv.run:5000/reference` is documentation,
not the data endpoint. Real prediction-market data is under
`/prediction-markets/...`.

The kv.run prediction-market API supports anonymous MVP access with a
conservative throttle. Use `KVRUN_API_KEY` or `KVRUN_BEARER_TOKEN` only when
higher limits are needed.

Concrete paths:

- `GET /prediction-markets/markets/search?q=bitcoin&venue=polymarket&status=open&limit=50`
- `GET /prediction-markets/markets/{venue}/{market_id}`
- `GET /prediction-markets/trades/{venue}/{market_id}?from=...&to=...&limit=5000`
- `GET /prediction-markets/candles/{venue}/{market_id}?interval=1&limit=5000`
- `GET /prediction-markets/open-interest/{venue}/{market_id}?limit=500`
- `GET /prediction-markets/top-holders/{venue}/{market_id}?limit=50`
- `GET /prediction-markets/events?q=bitcoin&status=open&limit=200`
- `GET /prediction-markets/matched-pairs/{venue}/{venue_id}?limit=20`
- `GET /prediction-markets/stream?asset_ids=...&condition_ids=...`

When data is fetched from an endpoint, persist raw pages first, then canonical
JSONL:

```bash
python -m backtest.loader_kvrun_prediction \
  --raw-out data/bronze/kvrun_search.json \
  --out data/silver/kvrun_search_canonical.jsonl \
  --audit-out data/silver/kvrun_search_audit.jsonl \
  search --q bitcoin --venue polymarket --status open --limit 50
```

For baseline research, prefer kv.run candles over local resampling because
they are already continuous aggregates. Keep `backtest.resample` for custom
windows, raw JSONL replay, locally captured stream data, and validation.

For the immediate research path, fetch a bundle:

```bash
python -m backtest.loader_kvrun_prediction \
  --out data/silver/kvrun_bundle.jsonl \
  bundle \
  --q bitcoin \
  --venue polymarket \
  --market-limit 10 \
  --out-dir data/silver/kvrun_bundle
```

Capture live SSE ticks only as replay data, never inside deterministic
backtests:

```bash
python -m backtest.stream_kvrun \
  --condition-ids "<condition_id_1>,<condition_id_2>" \
  --raw-out data/kvrun/raw_stream_YYYYMMDD.jsonl \
  --canonical-out data/kvrun/canonical_stream_YYYYMMDD.jsonl
```

Build deterministic peers from market metadata and matched-pair payloads before
using embeddings:

```bash
python -m research.kvrun_peer_graph \
  --markets data/silver/kvrun_bundle/markets.jsonl \
  --out reports/kvrun_peer_edges.jsonl
```

## Allowed Strategy Families

The default research families are:

- `momentum`
- `mean_reversion`
- `volume_shock`
- `early_attention`
- `open_interest_growth`
- `volatility_regime`
- `closing_time_behavior`

If required fields are missing, disable that family explicitly. Do not
substitute unrelated fields, for example volume for open interest.

## Grading

Use forecast-track metrics for information value and execution-track metrics
for tradability:

- Forecast: signals, hit rate, Brier, log loss, calibration ECE.
- Execution: trades, fees, PnL, profit factor, drawdown, high-vs-low edge win rate.

Only recommend promotion when deterministic metrics support it. Compile,
schema parsing, and a tiny smoke run are not live-readiness evidence.
