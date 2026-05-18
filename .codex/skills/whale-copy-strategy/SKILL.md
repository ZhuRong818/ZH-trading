---
name: whale-copy-strategy
description: Maintain, debug, tune, and evaluate the ZH-trading whale copy strategy. Use when Codex is asked about strategies/v2/whale.py, strategies/whale_tracking/whale_tracker.py, Polymarket leaderboard polling, whale position snapshots, copy_fraction, no first-poll signals, whale dry-run logs, rate limits, or commands such as python main.py --strategy whale --dry-run.
---

# Whale Copy Strategy

## Quick Start

Use this map:

- `strategies/v2/whale.py` is the active V2 strategy.
- `strategies/whale_tracking/whale_tracker.py` is the older richer tracker.
- `main.py` registers whale through `--strategy whale`.
- `config/settings.py` contains `WhaleTrackingConfig`.
- The strategy uses Polymarket Data API endpoints for leaderboard and wallet positions.

Whale copy does not need a selected token. It watches wallets globally and emits copy BUY signals when tracked wallets increase positions.

## Strategy Rules

The V2 strategy:

1. Refreshes top traders from the leaderboard about every 10 minutes.
2. First poll snapshots positions only and emits no signals.
3. On later polls, compares current positions to the known baseline.
4. Copies positive position deltas above a minimum notional threshold.
5. Skips extreme prices near 0 or 1.
6. Caps signals per whale and per cycle.
7. Emits BUY signals through the unified pipeline.

Important behavior:

- No signal on first poll is correct.
- Signal latency can be large because wallet positions are polled, not streamed.
- Strategy quality depends heavily on whether leaderboard/wallet data is timely.

## Commands

Dry-run:

```bash
python main.py --strategy whale --dry-run --no-learn --verbose
```

Combined with static strategies:

```bash
python main.py --strategy whale,meanrev --token TOKEN --dry-run --no-learn --verbose
```

## Common Diagnoses

For "no whale trades":

- First poll snapshots only.
- Leaderboard refresh may fail or return no wallets.
- Tracked whales may have no new qualifying position deltas.
- `copy_fraction`, `max_copy_size_usdc`, or trust/win-rate gates may suppress signals.
- Data API latency can make short 5-minute markets unsuitable.

For "too many whale signals":

- Check `MAX_SIGNALS_PER_WHALE` and `MAX_SIGNALS_PER_CYCLE`.
- Raise delta notional threshold.
- Lower `top_n_traders`.
- Restrict by market, if adding market-specific filtering.

For "bad copy performance":

- Whale buys may already have moved the market.
- Data API position updates are delayed.
- Copied positions may not reflect entry price.
- Use reports to group by source wallet, price bucket, market age, and hold time.

## Validation

Use these commands when relevant:

```bash
python -m py_compile strategies/v2/whale.py strategies/whale_tracking/whale_tracker.py main.py config/settings.py
python main.py --strategy whale --dry-run --no-learn --verbose
```

Only run live mode without `--dry-run` when the user explicitly asks for live trading and credentials/risk limits are configured.
