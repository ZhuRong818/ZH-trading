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
