---
name: momentum-strategy
description: Maintain, debug, tune, and evaluate the ZH-trading V2 momentum strategy for rolling 5-minute Polymarket crypto markets. Use when Codex is asked about strategies/v2/momentum.py, MomentumReplayStrategy in backtest/replay.py, backtest/run.py, BTC5M or rolling momentum wiring in main.py, MOMENTUM or MOMENTUM_DIAG logs, z-score and momentum/volatility gates, VWAP slippage, confirmation counts, or commands such as python main.py --strategy btc5m --dry-run and python -m backtest.replay --strategy momentum.
---

# Momentum Strategy

## Quick Start

Use this map first:

- `strategies/v2/momentum.py` is the live V2 strategy.
- `main.py` wires it through `--strategy btc5m`, `--strategy momentum`, or `--strategy rolling,momentum`.
- `backtest/replay.py` contains `MomentumReplayStrategy` for collected JSONL market data.
- `backtest/run.py` contains the older offline runner for Binance/synthetic and optional real-price windows.
- `config/settings.py` owns `btc5m_*` defaults.
- `strategies/v2/portfolio.py` can wrap momentum with oracle, lead-lag, and snipe.
- `data_pipeline/market_provider.py` supplies rolling UP/DOWN contexts, strike, remaining time, external price, books, and tick size.

Momentum trades rolling 5-minute UP/DOWN markets from short-term external price movement, strike distance, and executable Polymarket asks. The strategy emits BUY-only FAK signals and relies on runner/pipeline/OMS for execution, fills, settlement, and reporting.

## Strategy Rules

The live strategy:

1. Resolves paired UP/DOWN contexts.
2. Polls Binance `ASSETUSDT` spot price and stores recent price/time buffers.
3. Waits for enough price samples and the configured window age.
4. Computes momentum, realized tick volatility, distance to strike, z-score, and momentum/volatility ratio.
5. Converts adjusted z-score to capped fair probability.
6. Chooses UP when fair UP is high, DOWN when fair UP is low.
7. Requires market ask inside `min_price` and `max_price`.
8. Requires edge above direction-specific required edge.
9. Sizes with Kelly and rechecks VWAP, depth, price band, and edge after executable pricing.
10. Requires consecutive same-side confirmations before emitting one signal.

Core defaults in `strategies/v2/momentum.py` and `config/settings.py`:

- `min_edge=0.16`
- `min_price=0.40`
- `max_price=0.55`
- `momentum_window=20`
- `min_mom_vol_ratio=0.8`
- `min_entry_age=60.0`
- `entry_deadline=180.0`
- `min_abs_z=0.15`
- `down_edge_boost=0.10`
- `down_min_abs_z=0.45`
- `fair_cap=0.80`
- `confirmations_required=2`
- `max_vwap_slippage=0.015`

Be careful with time semantics: `window_age = 300 - seconds_remaining`. Entries are allowed only after `min_entry_age` and while `seconds_remaining >= entry_deadline`.

## Commands

Live dry-run:

```bash
python main.py --strategy btc5m --rolling-asset btc --dry-run --no-learn --verbose
python main.py --strategy rolling,momentum --rolling-asset btc --dry-run --no-learn --verbose
python main.py --strategy rolling,momentum --rolling-asset eth --dry-run --no-learn --verbose
```

Collected JSONL replay:

```bash
python -m backtest.replay --strategy momentum --assets btc,eth,sol,xrp --file data_v2/market_data_2026-05-14.jsonl
python -m backtest.replay --strategy momentum --assets btc --file data_v2/market_data_2026-05-14.jsonl --min-edge 0.18 --min-price 0.40 --max-price 0.55
```

Older offline runner:

```bash
python -m backtest.run --strategy momentum --days 7
python -m backtest.run --strategy momentum --real-prices --hours 168
```

Use replay from collected JSONL when validating live-like market microstructure. Use `backtest.run` for quick synthetic or historical exploratory checks.

## Common Diagnoses

For "no momentum trades":

- Check `MOMENTUM_DIAG` top rejects: `warmup`, `age`, `mom_vol`, `z`, `distance_bps`, `price_band`, `edge`, `down_z`, `vwap_depth`, `vwap_slippage`, or `confirmation`.
- Confirm Binance polling succeeds and `_prices` has enough samples.
- Confirm window age is at least `min_entry_age` and remaining time is still at least `entry_deadline`.
- Check whether the ask is inside `min_price` to `max_price`.
- Check whether the same direction/token repeats enough times for `confirmations_required`.

For "momentum trades too much":

- Increase `min_edge`, `min_abs_z`, `min_mom_vol_ratio`, or `confirmations_required`.
- Narrow price band or lower `fair_cap`.
- Increase `down_min_abs_z` or `down_edge_boost` if DOWN entries dominate losses.
- Tighten `max_vwap_slippage` when depth is weak.

For "replay and live disagree":

- Compare replay price source and live Binance polling cadence.
- Compare best ask to VWAP after size.
- Check whether replay uses the same `btc5m_*` defaults and confirmation count.
- Inspect book depth, latency, and whether the paired UP/DOWN contexts are from the same condition.

For "DOWN behavior is weak":

- DOWN requires both edge boost and `down_min_abs_z`.
- Keep DOWN gates direction-specific; do not weaken them globally unless replay proves both UP and DOWN improve out of sample.
- Bucket results by direction before changing shared thresholds.

## Validation

Use these commands when relevant:

```bash
python -m py_compile strategies/v2/momentum.py backtest/replay.py backtest/run.py main.py config/settings.py
python -m backtest.replay --strategy momentum --assets btc,eth,sol,xrp --file data_v2/market_data_2026-05-14.jsonl
python -m backtest.run --strategy momentum --days 7
python main.py --strategy btc5m --rolling-asset btc --dry-run --no-learn --verbose
```

Only run live mode without `--dry-run` when the user explicitly asks for live trading and credentials/risk limits are configured.
