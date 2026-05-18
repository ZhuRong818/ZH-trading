# Research Signal Evaluation

This package is intentionally separate from the live trading pipeline, EMS/OMS,
and `backtest.replay`. It reads recorded JSONL snapshots from `data_v2`, emits
standardized probability signals, and scores them after each 5-minute window is
labeled.

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
