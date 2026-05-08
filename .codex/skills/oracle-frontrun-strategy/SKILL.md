---
name: oracle-frontrun-strategy
description: Maintain, debug, tune, and evaluate the ZH-trading oracle_frontrun strategy for rolling BTC/ETH 5-minute Polymarket markets. Use when Codex is asked to modify strategies/v2/oracle_frontrun.py, the modular skills-based oracle implementation, oracle dry-run/live execution behavior, rolling strategy setup, risk gates, sizing, slippage controls, post-session CSV/JSON reports, or terminal logs from commands such as python main.py --strategy rolling,oracle --rolling-asset btc --dry-run.
---

# Oracle Frontrun Strategy

## Quick Start

Start with the active execution path, not only the modular helper files:

- `main.py` wires `--strategy rolling,oracle` to `strategies/v2/oracle_frontrun.py`.
- `strategies/v2/runner.py` settles rolling positions and feeds fills back to strategies.
- `pipeline/engine.py` and `pipeline/stages.py` run risk, capital, execution, tracking, and logging.
- `ems/execution.py` and `ems/dry_run_sim.py` determine simulated fills and VWAP slippage.
- `analytics/post_session.py`, `analytics/trade_recorder.py`, and `reports/*` determine report quality.

Read `references/oracle-frontrun-system.md` when the task involves strategy behavior, risk tuning, report interpretation, or changes that could affect PnL/risk.

## Workflow

1. Reproduce the user's claim from logs or reports before editing.
2. Identify whether the task touches signal generation, sizing, execution, settlement, risk, or analytics.
3. Preserve the invariant: sizing and risk must be based on executable/VWAP price, not just displayed best ask, whenever available.
4. Treat dry-run profits skeptically when fills show high slippage, low-price tail entries, or circuit-breaker rejections.
5. Run a syntax check for modified Python files. For behavior changes, prefer a short dry-run or targeted unit-like script if local network/API access is not required.

## Strategy Rules

Keep the oracle strategy narrow:

- Detect a short-horizon external asset move.
- Estimate fair UP/DOWN probabilities from that move.
- Compare fair value against current Polymarket ask prices.
- Gate weak, stale, illiquid, tail, or late-window entries.
- Size from approved risk budget after accounting for executable price.
- Emit one `TradingSignal` and let the unified runner/pipeline handle execution and settlement.

Do not bypass the pipeline or call EMS directly from the strategy.

## Common Fixes

For "signal generated but no execution":

- Check `pipeline` stats for rejected signals.
- Check risk circuit breaker logs.
- Check whether the strategy lock is set on signal before an actual fill.
- Make sure rejected orders do not leave `_has_position` or `EntryLock` stuck until a later window.

For "good backtest/dry-run but unsafe live behavior":

- Compare `signal price` in terminal logs with `entry_price` and `slippage` in CSV.
- Flag entries where actual VWAP materially exceeds the signal price.
- Cap or reject low-price tail trades when VWAP turns a small intended bet into a large notional loss.
- Use FOK/FAK or explicit depth checks when the user wants live-safe sizing.

For "report cannot explain performance":

- Ensure `TradingSignal.edge` and `fair_value` propagate into trade records.
- Validate regime, slippage, and edge columns in CSV before drawing conclusions.
- Group PnL by `entry_regime`, price bucket, slippage, and rejected reason.

## Validation

Use these commands when relevant:

```powershell
python -m py_compile strategies\v2\oracle_frontrun.py
python -m py_compile skills\strategies.py skills\price_features.py skills\fair_value.py skills\risk_gate.py skills\sizing.py
python main.py --strategy rolling,oracle --rolling-asset btc --dry-run
```

Only run the live mode when the user explicitly requests it and credentials are configured.
