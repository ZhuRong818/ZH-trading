---
name: btc5m-momentum-strategy
description: Maintain, debug, tune, and evaluate the ZH-trading BTC 5-minute Momentum strategy for rolling Polymarket markets. Use when Codex is asked to modify strategies/v2/momentum.py, BTC5m rolling setup in main.py, config/settings.py btc5m parameters, learner adjustments, backtest/run.py, backtest/simulator.py, backtest/polymarket_loader.py, dry-run/live terminal logs from python main.py --strategy btc5m --rolling-asset btc --dry-run, or BTC5m post-session reports and trade CSV/JSON analysis.
---

# BTC5m Momentum Strategy

## Quick Start

Start with the active live path:

- `main.py` wires `--strategy btc5m` to `rolling` and instantiates `strategies/v2/momentum.py`.
- `strategies/v2/momentum.py` polls Binance ticker, computes fair value, compares against executable Polymarket ask/VWAP, and emits `TradingSignal`.
- `data_pipeline/market_provider.py` discovers rolling 5m markets, supplies strike, remaining time, UP/DOWN contexts, and books.
- `data_pipeline/market_data.py` defines `OrderBookSnapshot.vwap_price()`, which must drive executable price checks.
- `strategies/v2/runner.py`, `pipeline/*`, `ems/*`, and `oms/*` handle execution, fill callbacks, settlement, and PnL.
- `backtest/run.py`, `backtest/simulator.py`, and `backtest/polymarket_loader.py` are only approximations unless replaying recorded live snapshots with real books.

## Workflow

1. Reproduce the user's claim from logs, reports, or a focused backtest before changing parameters.
2. Decide whether the issue is signal quality, market data, orderbook/VWAP, execution, settlement, analytics, or backtest realism.
3. Preserve the live invariant: all edge, Kelly sizing, and final signal price must use executable price or VWAP when a book is available.
4. Treat high backtest win rate skeptically unless the backtest uses historical data available at or before entry time and clearly marks its price source.
5. For code changes, run `python -m py_compile` on modified Python files. For strategy behavior, prefer a short dry-run or a constrained backtest.

## Live Strategy Rules

Keep Momentum narrow:

- Poll Binance BTC price frequently.
- Track recent BTC prices and compute momentum, realized volatility, distance-to-strike z-score, and remaining-time horizon.
- Estimate UP/DOWN fair probability with a bounded `fair_cap`; avoid 90%+ certainty from 5m noise.
- Select direction only when fair probability is away from 50/50 and momentum quality filters pass.
- Compare fair value against Polymarket best ask and then VWAP for intended size.
- Reject entries outside price band, insufficient edge, excessive VWAP slippage, weak DOWN z-score, early-window noise, and late-window entries.
- Emit at most one directional BUY signal per window and let runner/pipeline handle fill, settlement, and position state.

Do not bypass the pipeline or place orders directly from `Momentum`.

## Current Parameter Posture

Default conservative BTC5m parameters live in `config/settings.py` and are wired through `main.py`:

- `btc5m_min_edge`: baseline edge gate; learner may raise it after poor sessions.
- `btc5m_max_price`: avoid expensive binary entries where losses exceed wins.
- `btc5m_min_price`: avoid tail/lottery entries.
- `btc5m_down_edge_boost`: additional DOWN edge requirement due observed weaker DOWN performance.

When tuning, prefer fewer, higher-quality trades. Do not loosen after small samples. Require at least 30-50 settled trades before concluding a parameter change helped.

## Backtest Reality Checks

Before trusting backtest output, verify:

- `Price source` shows `real_history` when running `--real-prices`; otherwise results are synthetic.
- Historical Polymarket prices are selected with `t <= entry_ts`, never nearest future points.
- Binance 1m candle closes are treated as known at close time, not minute open.
- Strike comes from true market metadata or at least window open, not first-minute close.
- Final fill price respects `max_price`, not only pre-slippage signal price.
- Reports include trade rate, price bucket, edge bucket, direction bucket, entry age bucket, and required win rate.

Remember: `prices-history` is not orderbook depth. It cannot exactly reproduce live `book.vwap_price("BUY", size)`. A real execution backtest needs recorded live snapshots containing Binance tick, UP/DOWN books, best ask, and VWAP.

## Common Diagnoses

For "too many trades":

- Check `min_edge`, `confirmations_required`, `max_price`, `min_price`, and `down_edge_boost`.
- Bucket losses by entry price and entry age.
- Consider stricter rules for `0.50-0.55` entries or early-window entries.

For "no trades for a long time":

- Confirm windows still roll and STATUS logs continue.
- Check whether books are valid and asks are inside the price band.
- Compare fair, market price, VWAP, z-score, distance, momentum, age, and `conf` in the signal log.
- A 5-15% window trade rate can be normal for the conservative setup.

For "backtest looks too good":

- Look for synthetic quote usage.
- Check for future price selection in Polymarket or Binance data.
- Verify late-window entries are not using information unavailable at decision time.
- Compare backtest signal cadence with live dry-run cadence.

For "dry-run PnL differs from intuition":

- Settlement is binary: winning payout is $1 per share, losing payout is $0.
- Inspect actual `avg_entry`, `entry_price`, VWAP resizing, and fees.
- Losses near 0.50 can offset several small wins if the win rate is not high enough.

## Validation

Use these commands when relevant:

```powershell
python -m py_compile strategies\v2\momentum.py main.py config\settings.py analytics\learner.py
python -m py_compile backtest\run.py backtest\simulator.py backtest\polymarket_loader.py backtest\data_loader.py
python -m backtest.run --strategy momentum --days 7 --min-edge 0.14 --max-price 0.55 --min-price 0.40
python -m backtest.run --strategy momentum --real-prices --hours 24 --min-edge 0.14 --max-price 0.55 --min-price 0.40
python main.py --strategy btc5m --rolling-asset btc --dry-run
```

Only run live mode when the user explicitly requests it and credentials are configured.
