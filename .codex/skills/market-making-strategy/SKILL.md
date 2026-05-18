---
name: market-making-strategy
description: Maintain, debug, tune, and evaluate the ZH-trading Stoikov market making strategy for Polymarket. Use when Codex is asked about strategies/v2/mm.py, legacy strategies/market_making/stoikov_model.py, bid/ask quote generation, gamma/spread-k/levels/size tuning, dry-run fills, abnormal PnL, inventory skew, wide spread behavior, or commands such as python main.py --strategy mm --token TOKEN --dry-run.
---

# Market Making Strategy

## Quick Start

Use this map:

- `strategies/v2/mm.py` is the V2 pipeline strategy.
- `strategies/market_making/stoikov_model.py` is the older direct-EMS implementation.
- `main.py` wires `--strategy mm` for static selected tokens and `--strategy rolling,mm` for rolling contexts.
- `config/settings.py` and CLI flags configure `gamma`, `spread_k`, `order_size`, and `num_levels`.
- `data_pipeline/market_data.py` supplies books, adjusted midpoint, volatility, and regime classification.
- `ems/*`, `oms/*`, `pipeline/*`, and `analytics/*` determine simulated fills, inventory, and reported PnL.

Market making posts bid and ask quotes around a reservation price. It is not supposed to instantly fill both sides profitably in dry-run unless the simulator deliberately models such fills.

## Strategy Rules

Stoikov logic:

1. Read mid-price from the order book, with safeguards for unusable wide books.
2. Estimate volatility.
3. Read inventory.
4. Compute reservation price: long inventory lowers the reservation price, short inventory raises it.
5. Compute optimal spread from `gamma`, volatility, time, and `spread_k`.
6. Widen or tighten by market regime.
7. Cap maximum quoted spread.
8. Emit layered BUY and SELL signals subject to inventory limits.

Important invariants:

- Avoid quoting negative prices or prices above 1.0.
- Avoid using meaningless book mid when spread is extremely wide.
- Do not count unfilled maker quotes as realized PnL.
- Treat dry-run fills skeptically if the simulator fills passive quotes immediately.

## Commands

Static token dry-run:

```bash
python main.py --strategy mm --token TOKEN --dry-run --no-learn --verbose
```

Interactive market selection:

```bash
python main.py --strategy mm --search bitcoin --dry-run --no-learn --verbose
```

Lower-risk quote test:

```bash
python main.py --strategy mm --token TOKEN --dry-run --no-learn --size 5 --levels 1 --gamma 0.8 --spread-k 2.0 --verbose
```

## Common Diagnoses

For "dry-run PnL is absurdly good":

- Check whether dry-run fills both bid and ask immediately.
- Check whether passive maker orders are being simulated as taker fills.
- Check whether quotes were clamped from invalid prices like below 0 or above 1.
- Do not treat this as live profitability.

For "market making loses money":

- Inspect spread captured after slippage/fees.
- Widen spreads by increasing `gamma` or `spread_k`.
- Reduce size and levels.
- Avoid tail markets and thin books.
- Check inventory skew and open inventory at shutdown.

For "only sell or only buy":

- Inventory limits may suppress one side.
- Reservation price may be skewed by inventory.
- Price clamps or risk gates may reject one side.
- OMS position state may not match expected inventory.

## Validation

Use these commands when relevant:

```bash
python -m py_compile strategies/v2/mm.py strategies/market_making/stoikov_model.py main.py config/settings.py
python main.py --strategy mm --token TOKEN --dry-run --no-learn --verbose --size 5 --levels 1
```

Only run live mode without `--dry-run` when the user explicitly asks for live trading and credentials/risk limits are configured.
