---
name: resolution-convergence-strategy
description: Maintain, debug, tune, and evaluate resolution fade and settlement convergence logic in ZH-trading. Use when Codex is asked about strategies/resolution_fade/resolution_fade.py, ConvergenceReplayStrategy in backtest/replay.py, fade/convergence behavior, near-expiry high-odds trades, final-window settlement PnL, or replay commands such as python -m backtest.replay --strategy convergence.
---

# Resolution Convergence Strategy

## Quick Start

Use this map:

- `backtest/replay.py` contains `ConvergenceReplayStrategy` for 5-minute recorded data.
- `strategies/resolution_fade/resolution_fade.py` is the older long-dated resolution fade strategy.
- `main.py` may not actively wire a live `fade` or `convergence` strategy in the current branch.
- `strategies/v2/last_seconds_snipe.py` is the live V2 late-window strategy most similar to replay convergence.

Convergence buys the side that appears almost certain near expiry. It can have a high win rate but small average wins and occasional large losses.

## Strategy Rules

Replay convergence:

1. Watch UP/DOWN recorded market prices.
2. Enter near expiry when one side exceeds a confidence threshold.
3. Require consecutive confirmations.
4. Buy the likely winning side.
5. Settle at $1 for win or $0 for loss.

Typical replay parameters:

- `--conv-threshold`, default around `0.88`
- `--conv-window`, default around `45` seconds remaining
- `--conv-confirm`, default around `3` consecutive ticks
- `--max-notional`, default around `$500`

Legacy resolution fade includes:

- Certainty fade days before resolution.
- Last-minute liquidity posting.
- Resolution convergence in final days.

## Commands

Replay collected data:

```bash
python -m backtest.replay \
  --strategy convergence \
  --assets btc,eth,sol,xrp \
  --file data_v2/market_data_2026-05-14.jsonl
```

Parameter sweep:

```bash
python -m backtest.replay --strategy convergence --assets btc,eth,sol,xrp --file data_v2/market_data_2026-05-14.jsonl --conv-threshold 0.90 --conv-window 30 --conv-confirm 3
python -m backtest.replay --strategy convergence --assets btc,eth,sol,xrp --file data_v2/market_data_2026-05-14.jsonl --conv-threshold 0.95 --conv-window 20 --conv-confirm 4
```

## Common Diagnoses

For "convergence has high win rate but low PnL":

- Fees and entries near 0.95-0.99 consume most edge.
- One loss can offset many small wins.
- High trade count may increase fee drag.

For "backtest looks unrealistic":

- Check whether entries assume best ask without depth.
- Check whether final seconds have enough real liquidity.
- Check whether recorded data frequency misses rapid reversals.
- Compare with live `snipe` behavior before using real capital.

For "live convergence requested":

- Prefer adapting `LastSecondsSnipe` rather than reviving old direct-EMS code.
- Require VWAP/depth gates and FAK/FOK orders.
- Use strict max notional.

## Validation

Use these commands when relevant:

```bash
python -m py_compile strategies/resolution_fade/resolution_fade.py backtest/replay.py
python -m backtest.replay --strategy convergence --assets btc,eth,sol,xrp --file data_v2/market_data_2026-05-14.jsonl
```

Only run live mode without `--dry-run` when the user explicitly asks for live trading and depth/risk controls are verified.
