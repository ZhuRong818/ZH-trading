---
name: rl-shadow-strategy
description: Maintain, debug, train, tune, and evaluate the ZH-trading tabular RL shadow strategy for rolling 5-minute Polymarket crypto markets. Use when Codex is asked about strategies/v2/rl_shadow.py, backtest/rl_env.py, backtest/rl_train.py, RLReplayStrategy in backtest/replay.py, reports/rl_model.json, RL_SHADOW logs, tabular Q actions/features/gates, or commands such as python -m backtest.rl_train and python main.py --strategy rolling,rl_shadow --dry-run.
---

# RL Shadow Strategy

## Quick Start

Use this map first:

- `strategies/v2/rl_shadow.py` is the live V2 shadow policy logger.
- `backtest/rl_env.py` owns the action space, feature buckets, rewards, tabular Q model, and gate thresholds.
- `backtest/rl_train.py` trains `reports/rl_model.json` and writes `reports/rl_training_report.json`.
- `backtest/replay.py` contains `RLReplayStrategy` for evaluating the trained model on recorded JSONL data.
- `main.py` wires `--strategy rl_shadow`; it auto-adds `rolling` because the strategy needs paired UP/DOWN rolling contexts.
- `data_pipeline/market_provider.py` supplies rolling UP/DOWN `MarketContext` objects.

RL shadow must remain observational unless the user explicitly asks to convert it into an executing strategy. Its `step()` method logs intended actions and always returns `[]`.

## Model Shape

The tabular policy:

1. Converts a paired UP/DOWN rolling market tick into a record.
2. Builds a binned `feature_key` from asset, time regime, distance to strike, momentum, market prices, spread, implied probability, and position fields.
3. Scores actions in `ACTIONS`: `HOLD`, `BUY_UP_SMALL/MED/LARGE`, and `BUY_DOWN_SMALL/MED/LARGE`.
4. Rejects invalid actions with `valid_action_reason`.
5. Applies live shadow gates for minimum Q, minimum edge, maximum spread, and depth.
6. Logs the selected action, Q values, gate reason, and intended notional.

Important defaults:

- `model_path="reports/rl_model.json"`
- `min_price=0.20`
- `max_price=0.95`
- `min_q=5.0`
- `min_edge=0.02`
- `max_spread=0.10`
- `fee_edge_multiplier=0.25`
- `min_depth=50.0`
- `depth_buffer=1.25`

Keep action ordering stable. Existing model files validate that their saved `actions` match `ACTIONS`; changing the tuple breaks old models unless migration is intentional.

## Training Workflow

Train from collected replay JSONL data:

```bash
python -m backtest.rl_train --data-dir data_v2 --assets btc,eth,sol,xrp --model-out reports/rl_model.json --report-out reports/rl_training_report.json
python -m backtest.rl_train --file data_v2/market_data_2026-05-14.jsonl --assets btc,eth,sol,xrp --verbose
```

The training flow:

- Loads records through `backtest.replay.load_records`.
- Splits each asset by time into 70/15/15 train, validation, and test sets.
- Computes window outcomes only from the training split.
- Trains average reward per state/action through `train_model`.
- Evaluates all splits with `RLReplayStrategy`.

When judging a model, compare validation/test PnL, trade count, win rate, and max drawdown. Treat high train PnL with weak validation/test as overfit.

## Live Shadow Workflow

Dry-run shadow logging:

```bash
python main.py --strategy rolling,rl_shadow --rolling-asset btc --dry-run --no-learn --verbose
python main.py --strategy rolling,rl_shadow --rolling-asset eth --dry-run --no-learn --verbose
```

Use `--rolling-asset` to choose the asset. The strategy reads the model and logs `RL_SHADOW` lines; it should not place FAK orders or produce `TradingSignal` objects.

If model loading fails, expected behavior is repeated `action=HOLD reason=model_unavailable` logs with the load error. Fix the model path or regenerate the model before changing strategy logic.

## Common Diagnoses

For "RL shadow never trades/logs only HOLD":

- Confirm `reports/rl_model.json` exists and action space matches current `ACTIONS`.
- Check `RL_SHADOW` gate reasons: `min_q`, `min_edge`, `spread`, `depth`, or `model_unavailable`.
- Inspect whether current `feature_key` states are unseen and therefore use `default_q`.
- Check `min_price`, `max_price`, deadzone handling, and missing strike/price fields.

For "trained report looks too good":

- Verify outcomes are computed from the correct window settlement, not future leakage inside features.
- Compare train, validation, and test metrics; do not tune from train alone.
- Confirm replay fills use executable ask/VWAP assumptions consistent with live gates.
- Inspect per-asset concentration; one asset can hide weak generalization elsewhere.

For "shadow log suggests good actions but live would not fill":

- Compare intended notional against top five ask depth.
- Check spread and fee-adjusted edge gates.
- Verify the live rolling provider resolves UP and DOWN sides correctly.
- Do not remove shadow-only behavior without adding execution risk controls and explicit user approval.

## Validation

Use these commands when relevant:

```bash
python -m py_compile strategies/v2/rl_shadow.py backtest/rl_env.py backtest/rl_train.py backtest/replay.py main.py
python -m backtest.rl_train --file data_v2/market_data_2026-05-14.jsonl --assets btc,eth,sol,xrp --verbose
python -m backtest.replay --strategy rl --assets btc,eth,sol,xrp --file data_v2/market_data_2026-05-14.jsonl --rl-model reports/rl_model.json
python main.py --strategy rolling,rl_shadow --rolling-asset btc --dry-run --no-learn --verbose
```

Only run live mode without `--dry-run` when the user explicitly asks for live trading and credentials/risk limits are configured. RL shadow still should not emit orders unless the strategy has deliberately been redesigned.
