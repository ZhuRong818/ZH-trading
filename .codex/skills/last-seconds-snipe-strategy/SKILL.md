---
name: last-seconds-snipe-strategy
description: Maintain, debug, tune, and evaluate the ZH-trading last-seconds snipe strategy for rolling 5-minute Polymarket crypto markets. Use when Codex is asked about strategies/v2/last_seconds_snipe.py, SnipeReplayStrategy in backtest/replay.py, late-window high-odds UP/DOWN entries, BTC/ETH/SOL/XRP snipe parameters, VWAP/slippage gates, or commands such as python main.py --strategy rolling,snipe --rolling-asset btc --dry-run.
---

# Last-Seconds Snipe Strategy

## Quick Start

Use this map first:

- `strategies/v2/last_seconds_snipe.py` is the live V2 strategy.
- `main.py` registers it through `--strategy rolling,snipe`.
- `data_pipeline/market_provider.py` provides rolling UP/DOWN contexts, strike, external price, remaining time, and order books.
- `backtest/replay.py` contains `SnipeReplayStrategy` for recorded JSONL data.
- `config/settings.py` contains asset-specific snipe thresholds for BTC/ETH/SOL/XRP.
- `strategies/v2/runner.py`, `pipeline/*`, `ems/*`, and `oms/*` handle fills, settlement, reports, and PnL.

Snipe is a late-window strategy. It should trade rarely and only when the current asset price is clearly on one side of the strike and the Polymarket winning side is already priced near certainty.

## Strategy Rules

The live strategy:

1. Polls external crypto price through `BTCPriceFeed(asset)`.
2. Resolves UP and DOWN contexts for the current rolling window.
3. Trades only inside the configured final-time window.
4. Chooses `UP` if current price is at or above strike, otherwise `DOWN`.
5. Requires distance from strike by USD or bps threshold.
6. Requires target side ask above high-odds threshold.
7. Computes a conservative fair value and edge.
8. Sizes through `PositionSizer`.
9. Checks order-book VWAP and rejects excessive slippage.
10. Emits one FAK BUY signal per window.

Core live gates:

- `min_seconds_remaining` and `max_seconds_remaining`
- soft tier between `max_seconds_remaining` and `soft_max_seconds_remaining`
- `min_distance_usd` or `min_distance_bps`
- `min_market_odds`
- `min_edge`
- `min_fair`
- `max_notional_usdc`
- `max_vwap_slippage`
- one signal per window

Do not bypass the unified runner or place orders directly from this strategy.

## Commands

Live dry-run by asset:

```bash
python main.py --strategy rolling,snipe --rolling-asset btc --dry-run --no-learn --verbose
python main.py --strategy rolling,snipe --rolling-asset eth --dry-run --no-learn --verbose
python main.py --strategy rolling,snipe --rolling-asset sol --dry-run --no-learn --verbose
python main.py --strategy rolling,snipe --rolling-asset xrp --dry-run --no-learn --verbose
```

Replay collected data:

```bash
python -m backtest.replay \
  --strategy snipe \
  --assets btc,eth,sol,xrp \
  --file data_v2/market_data_2026-05-14.jsonl
```

## Common Diagnoses

For "no snipe trades":

- Check whether the window is inside the final `soft_max_seconds_remaining`.
- Check whether the asset is far enough from strike.
- Check whether target odds meet `min_market_odds`.
- Check whether direction is mismatched, for example DOWN ask is high while current price is above strike.
- Check VWAP rejection and depth.
- Remember ETH/SOL/XRP may trigger less often if their asset-specific distance/odds thresholds are too strict.

For "backtest looks too good":

- Inspect whether entries occur too close to settlement with unrealistic fill assumptions.
- Check that replay does not use future settlement information.
- Check trade PnL after 5-minute crypto fees.
- Compare best ask with live VWAP/depth.
- High win rate can still produce weak PnL if entries are near `0.98-0.99` and occasional losses are large.

For "live result differs from replay":

- Compare replay entry price to live `fill_price`.
- Check whether FAK orders would fully fill at displayed ask.
- Bucket losses by `seconds_remaining`, distance-to-strike, and entry odds.

## Validation

Use these commands when relevant:

```bash
python -m py_compile strategies/v2/last_seconds_snipe.py backtest/replay.py main.py config/settings.py
python -m backtest.replay --strategy snipe --assets btc,eth,sol,xrp --file data_v2/market_data_2026-05-14.jsonl
python main.py --strategy rolling,snipe --rolling-asset btc --dry-run --no-learn --verbose
```

Only run live mode without `--dry-run` when the user explicitly asks for live trading and credentials/risk limits are configured.
