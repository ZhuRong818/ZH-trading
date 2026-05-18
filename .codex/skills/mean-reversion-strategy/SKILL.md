---
name: mean-reversion-strategy
description: Maintain, debug, tune, and evaluate the ZH-trading mean reversion strategy for static and rolling Polymarket markets. Use when Codex is asked about strategies/v2/meanrev.py, legacy strategies/mean_reversion/mean_reversion.py, no-trade meanrev logs, moving average observations, entry/exit thresholds, contested market filters, stop-loss/cooldown behavior, or commands such as python main.py --strategy meanrev --token TOKEN --dry-run.
---

# Mean Reversion Strategy

## Quick Start

Start with the active path:

- `strategies/v2/meanrev.py` is the V2 strategy used by `main.py`.
- `main.py` can run it on static selected tokens with `--strategy meanrev --token ...`.
- `main.py` can also register it on rolling markets through `--strategy rolling,meanrev`.
- `data_pipeline/market_provider.py` supplies `MarketContext.mid_price`, tick size, remaining time, and validity.
- `strategies/mean_reversion/mean_reversion.py` is the older direct-EMS implementation.

Mean reversion is intentionally slow to trigger. Repeated `STATUS` lines with zero trades are normal when price does not deviate far enough from the moving average.

## Strategy Rules

The V2 strategy:

1. Records each valid `mid_price` observation per token.
2. Waits for `min_obs` observations.
3. Computes a moving average over `lookback`.
4. Trades only inside the price band `min_price` to `max_price`.
5. Buys when mid is below average by `entry_threshold`.
6. Sells when mid is above average by `entry_threshold`.
7. Exits when price returns near the moving average.
8. Stops out at `stop_multiple` times the entry deviation.
9. Applies cooldown after stop-loss.

Core defaults in `strategies/v2/meanrev.py`:

- `lookback=20`
- `entry_threshold=0.01`
- `exit_threshold=0.003`
- `stop_multiple=2.0`
- `min_price=0.20`
- `max_price=0.80`
- `min_obs=10`
- `cooldown=120`

An observation is one valid refreshed mid-price for a token. With `--interval 5`, ten observations takes about 50 seconds if every book refresh is valid.

## Commands

Static token dry-run:

```bash
python main.py --strategy meanrev --token TOKEN --dry-run --no-learn --verbose --interval 5
```

Interactive market search:

```bash
python main.py --strategy meanrev --search bitcoin --dry-run --no-learn --verbose
```

Rolling 5-minute market:

```bash
python main.py --strategy rolling,meanrev --rolling-asset btc --dry-run --no-learn --verbose
```

## Common Diagnoses

For "meanrev has no trades":

- Check if enough observations were collected.
- Check whether mid is inside the allowed price band.
- Check whether deviation exceeds `entry_threshold`.
- Check whether a stop-loss cooldown is active.
- On quiet long-dated markets, no trades for tens of minutes can be normal.

For "meanrev trades too much":

- Increase `entry_threshold`.
- Increase `min_obs` or `lookback`.
- Narrow the allowed price band toward contested markets.
- Add stronger spread/depth/VWAP gates before trusting live fills.

For "SELL-only behavior":

- A positive deviation from moving average creates a SELL signal.
- If the simulator allows short-like SELL fills without inventory constraints, inspect EMS/OMS behavior before assuming this is live-safe.
- For binary markets, prefer explicit inventory and NO-token handling before using SELL live.

## Validation

Use these commands when relevant:

```bash
python -m py_compile strategies/v2/meanrev.py strategies/mean_reversion/mean_reversion.py main.py
python main.py --strategy meanrev --token TOKEN --dry-run --no-learn --verbose --interval 5
```

Only run live mode without `--dry-run` when the user explicitly asks for live trading and credentials/risk limits are configured.
