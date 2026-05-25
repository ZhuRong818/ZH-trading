---
name: strategy-creator
description: Create, wire, validate, and document new ZH-trading strategy modules that fit the existing v2 architecture. Use when Codex is asked to add a new trading strategy, prototype a strategy, port strategy logic into strategies/v2, expose a strategy through main.py, add strategy config flags, add replay/backtest support, or ensure a strategy works with BaseStrategy, MarketContext, TradingSignal, UnifiedRunnerV2, the pipeline, OMS, risk, and analytics.
---

# Strategy Creator

## Quick Start

Create new live strategies under `strategies/v2/`. Do not add new active strategy code under legacy folders outside `v2/` unless the user explicitly asks for a reference-only port.

Use this map first:

- `strategies/base.py` defines `BaseStrategy`.
- `data_pipeline/market_provider.py` defines `MarketContext`, `StaticProvider`, and `RollingProvider`.
- `pipeline/signal.py` defines `TradingSignal`.
- `strategies/v2/runner.py` runs strategies and handles rolling-window settlement.
- `main.py` wires CLI names, config values, providers, runners, and post-session analyzers.
- `config/settings.py` owns stable defaults.
- `strategies/v2/skills.py` contains shared price-feed/sizing helpers.
- `backtest/replay.py` contains replay-only wrappers for collected rolling JSONL data.

## Workflow

1. Inspect the closest existing v2 strategy before editing. Prefer copying the local shape of `momentum.py`, `last_seconds_snipe.py`, `leadlag.py`, `portfolio.py`, `meanrev.py`, or `mm.py` over inventing a new interface.
2. Implement the live strategy as one class inheriting `BaseStrategy` in `strategies/v2/<name>.py`.
3. Emit only `TradingSignal` objects from `step()`. Never call EMS, OMS, capital allocator, risk engine, or CLOB APIs directly from a strategy.
4. Use executable prices from `MarketContext.best_ask`, `best_bid`, or `book.vwap_price()` for entries and exits. Avoid mid-price order assumptions unless the strategy is explicitly passive.
5. Keep strategy state local: cooldowns, recent observations, fill state, counters, diagnostics, and window state.
6. Implement `on_fill()` and `on_cancel()` when the strategy tracks positions, pending entries, or window-local state.
7. Implement `snapshot()` with concise diagnostics needed by post-session analysis.
8. Wire the strategy into `main.py` only after the class compiles and the CLI behavior is clear.
9. Add replay support in `backtest/replay.py` when the strategy targets rolling 5-minute markets or needs offline parameter evaluation.
10. Update README or strategy docs when the user asks for documentation or the command surface changes.

## Live Strategy Contract

Use this minimal pattern:

```python
from typing import List

from data_pipeline.market_provider import MarketContext
from pipeline.signal import TradingSignal
from strategies.base import BaseStrategy


class NewStrategy(BaseStrategy):
    name = "new_strategy"

    def __init__(self, bankroll: float = 10_000.0, min_edge: float = 0.02):
        self.bankroll = bankroll
        self.min_edge = min_edge
        self._has_position = False
        self.signals = 0
        self.rejected = 0

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        valid = [c for c in contexts if c.is_valid]
        if not valid or self._has_position:
            return []

        ctx = valid[0]
        price = ctx.best_ask or 0.0
        if price <= 0:
            self.rejected += 1
            return []

        fair = price + self.min_edge
        edge = fair - price
        if edge < self.min_edge:
            self.rejected += 1
            return []

        size = min(25.0, self.bankroll * 0.001 / price)
        self.signals += 1
        return [TradingSignal(
            token_id=ctx.token_id,
            side="BUY",
            price=price,
            size=size,
            strategy=self.name,
            tick_size=ctx.tick_size,
            neg_risk=ctx.neg_risk,
            order_type="FAK",
            edge=edge,
            confidence=0.5,
            fair_value=fair,
            direction="",
        )]

    def on_fill(self, fill):
        if fill.side == "BUY":
            self._has_position = True
        elif fill.side == "SELL":
            self._has_position = False

    def on_cancel(self):
        self._has_position = False

    def snapshot(self) -> dict:
        return {
            "signals": self.signals,
            "rejected": self.rejected,
            "has_position": self._has_position,
        }
```

Keep `strategy` names stable and unique. The runner routes fills back with `result.strategy.startswith(s.name)`, so child/wrapper strategies may use prefixes, but avoid ambiguous names.

## MarketContext Rules

For static markets, expect fixed token contexts from `StaticProvider`.

For rolling 5-minute markets, expect paired UP/DOWN contexts from `RollingProvider`:

- `token_id` and `token_id_other` identify the side pair.
- `question` contains side text such as `BTC 5m UP`.
- `condition_id` identifies the rolling window.
- `seconds_remaining` counts down to settlement.
- `external_price` is the current spot price from the configured price feed.
- `strike_price` is the window start price.
- `best_ask`, `best_bid`, `spread`, and `book` are executable order-book data.

Resolve UP/DOWN pairs by question text where possible, and fall back to paired token IDs only if needed. Reject mixed or missing windows.

## Signal Rules

Prefer these conventions:

- Aggressive rolling entries: `side="BUY"`, executable ask/VWAP price, `order_type="FAK"` or `FOK`.
- Passive market making: use explicit bid/ask quotes and `GTC` only when queue risk is intended.
- Exits: emit `SELL` only if the strategy deliberately manages exits; rolling settlement is handled by `UnifiedRunnerV2` in dry-run mode.
- `price` must be a realistic order price, not only a model fair value.
- `size` is shares, not USDC notional.
- `edge = fair_value - executable_price` for BUY entries.
- Set `direction` to `UP`, `DOWN`, or a strategy-specific label when useful for analytics.

Always check depth when size matters:

```python
vwap, fillable = ctx.book.vwap_price("BUY", size) if ctx.book else (ctx.best_ask, size)
if vwap is None or fillable < 1:
    return []
if fillable < size:
    size = fillable
```

## Wiring In main.py

For rolling strategies:

1. Import the class inside `TradingSystem.setup_rolling()`.
2. Add a branch such as `if "newname" in strategies:`.
3. Instantiate with config and `actual_bankroll` where relevant.
4. Call `runner.add(strategy)`.
5. Call `self.post_analyzer.register_strategy(strategy, "v2_newname")`.
6. Add the CLI name to the `roll_strats` filter in `setup_strategies()`.
7. If the strategy should imply rolling, add it near the existing `portfolio` and `rl_shadow` auto-rolling checks.
8. Update the parser help/epilog.

For static strategies:

1. Import the class inside `setup_v2_strategies()`.
2. Add a branch using `self.static_runner.add(strategy)`.
3. Register with `post_analyzer`.
4. Add the CLI name to static strategy selection and market-selection logic if it needs `--token` or `--search`.

Do not wire a new strategy into `all` unless it is safe enough for broad dry-run sessions.

## Config

Add durable defaults to `config/settings.py` when values should be reused by CLI, live runs, and tests. Use `SystemConfig` for rolling strategy parameters and smaller dataclasses only when the strategy has a large independent config surface.

Add CLI flags in `main.py` when the user needs fast tuning without editing code. After parsing, copy CLI values onto `config` before strategy setup.

Keep defaults conservative:

- Limit notional before risk/capital gates.
- Use cooldowns for high-frequency signals.
- Require positive edge after fees/slippage.
- Reject weak depth and wide spread.
- Avoid live GTC unless the user explicitly wants maker behavior.

## Replay Support

Add replay support when the strategy's edge depends on rolling-market timing, spot moves, or historical microstructure.

Use `backtest/replay.py` patterns:

- Create a replay strategy class with a stable `name`.
- Consume recorded JSONL records rather than live `MarketContext`.
- Recompute the same gates as live where practical.
- Add parser flags for tunable thresholds.
- Add a branch in the strategy dispatch near existing `momentum`, `leadlag`, `convergence`, `snipe`, `portfolio`, and `rl`.
- Print concise totals and write reports consistently with existing replay output.

Do not claim live/replay parity unless the same price source, fees, slippage, timing, and confirmation logic are represented.

## Validation

Run the smallest relevant validation first:

```bash
python -m py_compile strategies/v2/<new_strategy>.py main.py config/settings.py
```

If replay changed:

```bash
python -m py_compile backtest/replay.py
python -m backtest.replay --strategy <name> --assets btc,eth,sol,xrp
```

If live runner wiring changed:

```bash
python main.py --strategy rolling,<name> --rolling-asset btc --dry-run --no-learn --verbose
```

Only run live mode when the user explicitly asks for real trading and has configured credentials, live caps, and risk acknowledgement.

## Common Mistakes

- Adding a strategy outside `strategies/v2/` for active use.
- Calling execution code directly inside a strategy.
- Returning model fair prices as order prices.
- Sizing in USDC but assigning the value to `TradingSignal.size`, which expects shares.
- Forgetting `on_fill()` state updates, causing repeated entries.
- Forgetting `on_cancel()` for rolling window resets.
- Adding a CLI strategy name but not adding it to `roll_strats` or static setup.
- Ignoring fees, spread, VWAP slippage, and depth in a strategy that assumes immediate fills.
- Letting a shadow/diagnostic strategy emit orders unintentionally.
