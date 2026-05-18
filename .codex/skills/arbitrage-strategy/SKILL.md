---
name: arbitrage-strategy
description: Maintain, debug, tune, and evaluate the ZH-trading combinatorial arbitrage detector. Use when Codex is asked about strategies/arbitrage/arb_detector.py, sum-to-one violations, monotonic threshold arbitrage, event slug scanning, FOK all-leg execution, arb-events commands, or Polymarket pricing inconsistencies across related markets.
---

# Arbitrage Strategy

## Quick Start

Use this map:

- `strategies/arbitrage/arb_detector.py` contains the detector and executor.
- `ArbitrageDetector.scan_sum_to_one()` scans event markets for exclusive-outcome overpricing.
- `ArbitrageDetector.scan_monotonic()` checks nested threshold mispricing.
- `execute_arb()` submits FOK-style legs through EMS.
- Depending on the current `main.py`, arbitrage may be legacy or not actively wired.

Arbitrage should be treated as an all-legs-or-none problem. Partial fills can turn theoretical arbitrage into directional exposure.

## Strategy Rules

Sum-to-one:

1. Fetch active markets for an event slug.
2. Read YES prices.
3. If prices sum materially above 1.0, normalize fair values.
4. Candidate trade is to sell overpriced outcomes or buy NO equivalents.

Monotonic threshold:

1. Sort threshold markets by threshold.
2. Higher threshold probabilities should not exceed lower threshold probabilities.
3. If violated, short overpriced high threshold and buy underpriced lower threshold.

Execution rules:

- Require estimated profit above fees/slippage.
- Prefer FOK/atomic-style execution.
- Check depth on every leg.
- Never assume quoted mid is executable.

## Commands

If wired in the current branch:

```bash
python main.py --strategy arb --arb-events event-slug --dry-run --no-learn --verbose
```

If not wired, inspect or write a targeted script around `ArbitrageDetector` instead of guessing CLI behavior.

## Common Diagnoses

For "arb found but unsafe":

- Check each leg depth and executable price.
- Include fees and failed-leg risk.
- Confirm opposite-side token mechanics for binary markets.
- Avoid stale Gamma prices; prefer CLOB books.

For "no arb opportunities":

- Most obvious sum-to-one violations are quickly competed away.
- Event slug may not map to multiple active markets.
- Monotonic scans require explicit related token IDs and thresholds.

## Validation

Use these commands when relevant:

```bash
python -m py_compile strategies/arbitrage/arb_detector.py main.py
```

Only run live arbitrage without `--dry-run` when the user explicitly asks for live trading and all-leg execution/risk handling is verified.
