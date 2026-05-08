# Oracle Frontrun System Reference

## Active Code Path

The CLI command:

```powershell
python main.py --strategy rolling,oracle --rolling-asset btc --dry-run
```

uses this path:

1. `main.py::setup_rolling`
2. `RollingProvider`
3. `UnifiedRunnerV2`
4. `strategies/v2/oracle_frontrun.py::OracleFrontrun`
5. `PipelineEngine`
6. `RiskGate -> CapitalGate -> Executor -> Tracker -> Logger`
7. `ExecutionEngine` and `DryRunSimulator`
8. `PositionManager`, `TradeLog`, and `PostSessionAnalyzer`

The modular wrapper `skills/strategies.py::OracleFrontrunStrategy` is related but is not the current CLI path unless `main.py` is changed to use it.

## Current Strategy Parameters

`strategies/v2/oracle_frontrun.py` defaults:

- `move_threshold_bps=2.0`
- `staleness_threshold=0.01`
- `lookback_ticks=5`
- `max_price=0.75`
- `min_price=0.05`
- `min_remaining_seconds=60`
- `kelly_frac=0.25`
- `max_bet_pct=0.05`
- `bankroll=10_000`
- `cooldown_seconds=10`

The active fair value formula in `strategies/v2/oracle_frontrun.py` uses:

```text
prob_shift = min(move_bps / 50, 0.35)
```

The modular `skills/fair_value.py::OracleFairValueSkill` uses a different default scale (`move_bps / 100`). Do not assume the two implementations are behaviorally identical.

## Signal Lifecycle

`OracleFrontrun.step()`:

1. Polls Binance ticker into `_prices`.
2. Returns early if `_has_position` is true.
3. Requires enough ticks and cooldown.
4. Detects an external move from the current price versus `lookback_ticks`.
5. Resolves UP/DOWN contexts.
6. Computes stale edge from fair probability minus ask price.
7. Applies price and staleness gates.
8. Uses Kelly sizing.
9. Returns a `TradingSignal` with `edge` and `fair_value`.

The runner only tracks settlement entries after `result.was_executed`. If a strategy locks itself before an actual fill and the pipeline rejects the signal, it can suppress more signals until `on_cancel()` or `on_fill(SELL)` clears state.

## Risk And Execution Issues To Check

Dry-run execution may fill at VWAP, not at the requested signal price. This matters most for low ask prices, thin books, and tail regimes.

Before approving live-risk changes, check:

- Requested price versus actual fill price.
- Size in shares versus notional at actual fill.
- Whether `max_bet_pct * bankroll` remains the true maximum loss after VWAP.
- Circuit breaker rejections from `RiskEngine.check_circuit_breaker`.
- Drawdown semantics in `RiskEngine.check_drawdown`.

The CLI default `--max-drawdown` is `20.0`, overriding the `RiskConfig` dataclass default unless the user passes another value.

## Known Report Findings From 2026-05-08

Report files:

- `reports/analysis_2026-05-08_19-50-14.json`
- `reports/trades_2026-05-08_19-50-14.csv`

Observed session:

- Duration: 209.8 minutes.
- Trades: 22.
- PnL: +3172.3281.
- Win rate: 68.2%.
- Profit factor: 1.74.
- Sharpe: 3.77.
- Max drawdown: 2214.6758.
- Average slippage: 0.022709.

Regime breakdown:

- `contested`: 19 trades, +5910.8735 PnL.
- `trending`: 2 trades, -1158.2714 PnL.
- `tail`: 1 trade, -1580.2740 PnL.

Critical example:

```text
19:47:47 signal: DOWN 6250 @ 0.0800 edge=0.4931
CSV entry: BUY @ 0.252844, size 6250, slippage 0.172844
Settlement: loss, PnL -1580.2740
```

The strategy intended roughly a 500 USDC bet at 0.08, but VWAP fill made the actual loss 1580 USDC. This is the main reason to resize from executable price or reject large VWAP divergence.

## Analytics Gaps

CSV `entry_edge` was 0.0 for all completed oracle trades even though terminal logs showed nonzero edge. The likely cause is that `TradeRecorder` opens records from `Fill` events, while `Fill` does not carry `TradingSignal.edge` or `fair_value`.

When asked to improve analysis:

- Propagate signal metadata into the fill or recorder.
- Preserve `entry_edge`, `entry_fair_value`, and intended signal price.
- Report both intended price and actual fill price.
- Summarize rejections by `rejected_by:reject_reason`.

## Safer Tuning Direction

Prioritize these before live use:

1. Use executable VWAP for sizing and risk checks.
2. Reject or downsize if `vwap - signal_price` exceeds a small threshold.
3. Avoid `tail` regime unless explicitly testing a tail strategy.
4. Tighten low-price entries such as `market_price < 0.20`.
5. Track edge/fair value in CSV before using learner/tuner outputs.
6. Consider unlocking strategy state on pipeline rejection if it locks before fill.
