---
name: lead-lag-strategy
description: Maintain, debug, tune, wire, and evaluate the ZH-trading cross-asset leadlag strategy for rolling 5-minute Polymarket crypto markets. Use when Codex is asked about BTC-leading-ETH/SOL/XRP lag signals, strategies/v2/leadlag.py, LeadLagReplayStrategy in backtest/replay.py, live rolling leadlag setup in main.py, Polymarket stale follower odds, leader/follower parameters, replay results from collected market_data JSONL files, or terminal commands such as python -m backtest.replay --strategy leadlag.
---

# Lead-Lag Strategy

## Quick Start

Start with the exact path the user is discussing:

- `strategies/v2/leadlag.py` is the live V2 strategy implementation.
- `backtest/replay.py` contains `LeadLagReplayStrategy` for collected JSONL market data.
- `data_pipeline/market_provider.py` supplies rolling UP/DOWN `MarketContext` objects for one follower asset at a time.
- `data_pipeline/price_feeds.py` and Binance ticker data provide the leader price stream.
- `strategies/v2/runner.py`, `pipeline/*`, `ems/*`, and `oms/*` handle execution, fills, position state, settlement, and reports.
- `main.py` must explicitly register `LeadLag` in `setup_rolling()` before `python main.py --strategy rolling,leadlag ...` works live.

Do not confuse lead-lag with oracle:

- `oracle` trades one asset when its own external price moves before its Polymarket odds adjust.
- `leadlag` watches a leader asset, usually BTC, and trades follower assets, usually ETH/SOL/XRP, when their Polymarket odds have not yet repriced.

## Strategy Model

Lead-lag tries to capture propagation delay:

1. Poll leader price, default `BTCUSDT`.
2. Keep a short leader price history.
3. Detect a leader move over `lookback_ticks`.
4. Convert move size to follower fair probability.
5. Compare follower UP/DOWN ask against fair value.
6. If follower odds are stale by at least `staleness_threshold`, emit one BUY signal.
7. Let the runner and pipeline handle execution and settlement.

Core defaults in `strategies/v2/leadlag.py`:

- `leader="btc"`
- `follower="eth"`
- `move_threshold_bps=5.0`
- `staleness_threshold=0.10`
- `lookback_ticks=5`
- `min_price=0.20`
- `max_price=0.55`
- `min_remaining_seconds=60`
- `cooldown_seconds=15`
- `max_notional_usdc=500`
- `correlation_discount=0.90`

Keep the strategy narrow. It should not place orders directly, bypass the pipeline, or settle positions itself.

## Live Wiring

If the user asks why live lead-lag does not run, check `main.py` first. The repo may have `LeadLag` implemented but not registered in `setup_rolling()`.

A correct live wiring should:

- Import `LeadLag` inside `setup_rolling()`.
- Add `"leadlag"` to accepted CLI/help text if missing.
- Register one lead-lag strategy for the chosen rolling follower asset.
- Pass `leader="btc"` and `follower=asset`.
- Avoid registering a BTC follower when the leader is BTC, unless the user explicitly wants BTC self-lead behavior.
- Register it with `post_analyzer`, for example as `v2_leadlag`.

Useful dry-run command after wiring:

```bash
python main.py --strategy rolling,leadlag --rolling-asset eth --dry-run --no-learn --verbose
python main.py --strategy rolling,leadlag --rolling-asset sol --dry-run --no-learn --verbose
python main.py --strategy rolling,leadlag --rolling-asset xrp --dry-run --no-learn --verbose
```

For multiple followers, run separate terminals, one per `--rolling-asset`, unless the code has been changed to support a multi-asset rolling provider.

## Replay Workflow

Use replay for collected Polymarket/Binance JSONL data:

```bash
python -m backtest.replay \
  --strategy leadlag \
  --assets btc,eth,sol,xrp \
  --file data_v2/market_data_2026-05-14.jsonl
```

Parameter sweep examples:

```bash
python -m backtest.replay --strategy leadlag --assets btc,eth,sol,xrp --file data_v2/market_data_2026-05-14.jsonl --lag-bps 5 --lag-staleness 0.10
python -m backtest.replay --strategy leadlag --assets btc,eth,sol,xrp --file data_v2/market_data_2026-05-14.jsonl --lag-bps 6 --lag-staleness 0.15
python -m backtest.replay --strategy leadlag --assets btc,eth,sol,xrp --file data_v2/market_data_2026-05-14.jsonl --max-price 0.50 --max-notional 250
```

When comparing results, prioritize:

- PnL after fees.
- Win rate by follower asset.
- Number of trades by follower asset.
- Max drawdown.
- Entry price bucket.
- Whether one follower carries all returns.

Do not tune from one short session. Require enough windows and settled trades before changing defaults.

## Common Diagnoses

For "no leadlag trades":

- Confirm `btc` records exist in replay data; BTC is the default leader.
- Confirm follower assets exist in the same file.
- Check whether leader moves exceed `--lag-bps`.
- Check whether follower records are within replay `max_lag_seconds`.
- Check `staleness_threshold`, `min_price`, `max_price`, and `min_remaining_seconds`.
- In live mode, confirm `main.py` actually registers `LeadLag`.

For "signal generated but no execution":

- Check pipeline rejected counts and top rejection reasons.
- Compare signal price with executable/VWAP fill price.
- Check capital allocator caps and risk circuit breaker state.
- Make sure `_has_position` is not stuck after a rejection or window roll.

For "backtest looks too good":

- Check that replay uses only follower records with timestamps at or before the leader timestamp.
- Check whether time gaps between leader and follower records are realistic.
- Check whether fills assume best ask when live depth would force worse VWAP.
- Compare replay trade cadence with live dry-run signal cadence.
- Treat 5-minute crypto fees and late-window slippage as first-order costs.

For "BTC follower behavior is confusing":

- Lead-lag is cross-asset by design. BTC is usually the leader, not the follower.
- To trade BTC 5m directly, use `momentum`, `oracle`, `snipe`, or `btc5m`.
- To trade lead-lag, run ETH/SOL/XRP followers with BTC as leader.

## Validation

Use these commands when relevant:

```bash
python -m py_compile strategies/v2/leadlag.py backtest/replay.py main.py
python -m backtest.replay --strategy leadlag --assets btc,eth,sol,xrp --file data_v2/market_data_2026-05-14.jsonl
python main.py --strategy rolling,leadlag --rolling-asset eth --dry-run --no-learn --verbose
```

Only run live mode without `--dry-run` when the user explicitly asks for live trading and credentials/risk limits are configured.
