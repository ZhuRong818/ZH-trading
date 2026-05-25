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

1. Inspect the closest existing v2 strategy before editing. Prefer copying the local shape of an existing strategy over inventing a new interface. Available reference strategies:
   - **Directional**: `momentum.py` (multi-gate rolling), `momentum_v2.py`
   - **Speed/latency**: `leadlag.py` (cross-asset propagation), `oracle_frontrun.py` (same-asset oracle), `last_seconds_snipe.py`
   - **Volatility**: `vol_convexity.py` (time-dependent edge, convexity arbitrage)
   - **Portfolio/RL**: `portfolio.py` (multi-asset), `rl_shadow.py` (shadow learning)
   - **Market making**: `mm.py` (Stoikov-style)
   - **Mean reversion**: `meanrev.py`
   - **Whale tracking**: `whale.py`
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

Every v2 strategy must apply gates in this order before emitting a signal. Skipping a layer is the most common cause of negative PnL.

```python
from collections import Counter
from typing import List, Optional

from data_pipeline.market_provider import MarketContext
from pipeline.signal import TradingSignal
from strategies.base import BaseStrategy


class NewStrategy(BaseStrategy):
    name = "new_strategy"

    def __init__(
        self,
        bankroll: float = 5_000.0,
        min_edge: float = 0.04,
        min_price: float = 0.20,
        max_price: float = 0.55,
        max_spread: float = 0.08,
        max_notional_usdc: float = 150.0,
        cooldown_seconds: float = 10.0,
        confirmations_required: int = 2,
    ):
        self.bankroll = bankroll
        self.min_edge = min_edge
        self.min_price = min_price
        self.max_price = max_price
        self.max_spread = max_spread
        self.max_notional_usdc = max_notional_usdc
        self.cooldown_seconds = cooldown_seconds
        self.confirmations_required = confirmations_required

        # Position & cooldown state
        self._has_position = False
        self._last_signal_ts = 0.0
        self._last_signal_key = ""
        self._confirmation_count = 0

        # Counters for diagnostics
        self.signals = 0
        self.rejected: Counter = Counter()

    # ── gate helpers ──────────────────────────────────────────

    def _gate_warmup(self, ctx: MarketContext) -> Optional[str]:
        """Reject if strike or spot is not yet available."""
        if ctx.strike_price is None or ctx.strike_price <= 0:
            return "warmup_no_strike"
        if ctx.external_price is None or ctx.external_price <= 0:
            return "warmup_no_spot"
        return None

    def _gate_age(self, ctx: MarketContext, min_age: float = 60.0) -> Optional[str]:
        """Reject if window is too young — not enough price history."""
        age = ctx.seconds_since_start or 0.0
        if age < min_age:
            return "window_too_young"
        return None

    def _gate_time_remaining(self, ctx: MarketContext, min_sec: float = 60.0) -> Optional[str]:
        """Reject if too little time remains for the trade to play out."""
        if ctx.seconds_remaining is None or ctx.seconds_remaining < min_sec:
            return "insufficient_time"
        return None

    def _gate_price_band(self, price: float) -> Optional[str]:
        """Reject if market price is outside the acceptable range."""
        if price < self.min_price:
            return "price_too_low"
        if price > self.max_price:
            return "price_too_high"
        return None

    def _gate_spread(self, ctx: MarketContext) -> Optional[str]:
        """Reject if the market spread is too wide."""
        spread = ctx.spread or 1.0
        if spread > self.max_spread:
            return "spread_too_wide"
        return None

    def _gate_edge(self, fair: float, executable_price: float) -> Optional[str]:
        """Net edge after fees and slippage."""
        edge = fair - executable_price
        fee_drag = self._fee_drag(executable_price, fair)
        net_edge = edge - fee_drag
        if net_edge < self.min_edge:
            return "insufficient_edge"
        return None

    def _gate_depth(self, ctx: MarketContext, size: float) -> Optional[str]:
        """Reject if the book cannot fill the intended size."""
        if ctx.book is None:
            return "no_book"
        vwap, fillable = ctx.book.vwap_price("BUY", size)
        if vwap is None or fillable < 1:
            return "no_depth"
        if fillable < size:
            return "partial_fill_only"
        # VWAP slippage guard
        ask = ctx.best_ask or 0.0
        if ask > 0 and (vwap - ask) / ask > 0.015:
            return "vwap_slippage_high"
        return None

    def _gate_cooldown(self, now: float) -> Optional[str]:
        """Reject if within cooldown window since last signal."""
        if now - self._last_signal_ts < self.cooldown_seconds:
            return "cooldown"
        return None

    def _gate_confirmation(self, signal_key: str) -> Optional[str]:
        """Require the same signal in N consecutive polling iterations."""
        if signal_key != self._last_signal_key:
            self._last_signal_key = signal_key
            self._confirmation_count = 0
            return "awaiting_confirmation"
        self._confirmation_count += 1
        if self._confirmation_count < self.confirmations_required:
            return "awaiting_confirmation"
        return None

    # ── fee model ─────────────────────────────────────────────

    @staticmethod
    def _fee_drag(price: float, fair: float) -> float:
        """Polymarket parabolic taker fee: fee = shares * 0.07 * p * (1-p).
        Fee drag in probability space = 0.07 * price * (1 - price)."""
        return 0.07 * price * (1.0 - price)

    # ── core step ─────────────────────────────────────────────

    def _compute_edge(self, ctx: MarketContext) -> Optional[dict]:
        """Override this in subclasses with the strategy's fair value model.
        Returns dict with 'fair', 'direction', 'signal_key' or None if no edge."""
        raise NotImplementedError("subclass must implement _compute_edge")

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        valid = [c for c in contexts if c.is_valid]
        if not valid:
            return []
        if self._has_position:
            return []

        ctx = valid[0]

        # Gate 1: warmup
        reason = self._gate_warmup(ctx)
        if reason:
            self.rejected[reason] += 1
            return []

        # Gate 2: window age
        reason = self._gate_age(ctx)
        if reason:
            self.rejected[reason] += 1
            return []

        # Gate 3: time remaining
        reason = self._gate_time_remaining(ctx)
        if reason:
            self.rejected[reason] += 1
            return []

        # Compute edge via subclass
        result = self._compute_edge(ctx)
        if result is None:
            self.rejected["no_signal"] += 1
            return []
        fair = result["fair"]
        direction = result["direction"]
        signal_key = result["signal_key"]

        # Gate 4: price band
        ask = ctx.best_ask or 0.0
        if ask <= 0:
            self.rejected["no_ask"] += 1
            return []
        reason = self._gate_price_band(ask)
        if reason:
            self.rejected[reason] += 1
            return []

        # Gate 5: spread
        reason = self._gate_spread(ctx)
        if reason:
            self.rejected[reason] += 1
            return []

        # Gate 6: net edge (fair - ask - fee_drag >= min_edge)
        reason = self._gate_edge(fair, ask)
        if reason:
            self.rejected[reason] += 1
            return []

        # Kelly sizing
        edge = fair - ask
        kelly_bet = self.bankroll * 0.25 * (edge / max(ask, 0.01))
        max_bet = self.bankroll * 0.025
        notional = min(kelly_bet, max_bet, self.max_notional_usdc)
        if notional < 5.0:
            self.rejected["kelly_too_small"] += 1
            return []
        size = notional / ask

        # Gate 7: depth / VWAP
        reason = self._gate_depth(ctx, size)
        if reason:
            self.rejected[reason] += 1
            return []

        # Recompute edge at VWAP price (double-pass)
        vwap, _ = ctx.book.vwap_price("BUY", size)
        vwap_edge = fair - vwap
        vwap_fee = self._fee_drag(vwap, fair)
        vwap_net = vwap_edge - vwap_fee
        if vwap_net < self.min_edge:
            self.rejected["edge_lost_at_vwap"] += 1
            return []

        # Gate 8: cooldown
        now = ctx.timestamp or 0.0
        reason = self._gate_cooldown(now)
        if reason:
            return []  # cooldown is not a rejection, just a wait

        # Gate 9: confirmation
        reason = self._gate_confirmation(signal_key)
        if reason:
            return []  # awaiting confirmation is not a rejection

        self._last_signal_ts = now
        self.signals += 1
        return [TradingSignal(
            token_id=ctx.token_id,
            side="BUY",
            price=vwap,
            size=size,
            strategy=self.name,
            tick_size=ctx.tick_size,
            neg_risk=ctx.neg_risk,
            order_type="FAK",
            edge=vwap_net,
            confidence=min(vwap_net / 0.10, 1.0),
            fair_value=fair,
            direction=direction,
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
            "rejected": dict(self.rejected),
            "has_position": self._has_position,
        }
```

Key differences from the minimal pattern:

- **9 gates in order** — warmup → age → time-remaining → price-band → spread → net-edge → depth/VWAP → cooldown → confirmation. Every gate records its rejection reason.
- **Double-pass VWAP** — size at `best_ask`, then recheck edge at actual VWAP fill price.
- **Fee drag modeled explicitly** — Polymarket charges `0.07 * p * (1-p)` per share. Net edge = `fair - vwap - fee_drag`.
- **Confirmation gating** — same signal must survive N consecutive polling iterations before emission (default 2).
- **Kelly sizing with hard caps** — quarter Kelly, max 2.5% of bankroll, max notional cap, $5 minimum.
- **snapshot() returns rejection breakdown** — use `dict(self.rejected)` to see which gate fires most often.

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

- Aggressive rolling entries: `side="BUY"`, executable VWAP price, `order_type="FAK"` or `FOK`.
- Passive market making: use explicit bid/ask quotes and `GTC` only when queue risk is intended.
- Exits: emit `SELL` only if the strategy deliberately manages exits; rolling settlement is handled by `UnifiedRunnerV2` in dry-run mode.
- `price` must be the VWAP fill price from the depth check, not `best_ask` and not model fair value.
- `size` is shares, not USDC notional. Convert: `size = notional / vwap_price`.
- `edge = fair_value - vwap_price - fee_drag` (net edge, not gross edge).
- Set `direction` to `UP`, `DOWN`, or a strategy-specific label when useful for analytics.
- Set `confidence = min(net_edge / 0.10, 1.0)` so the analytics layer can validate edge-to-outcome correlation.

## Fee Model

Polymarket charges a parabolic taker fee on crypto markets:

```
fee_drag = 0.07 * price * (1.0 - price)
```

This is the fee expressed in probability space. At `price = 0.50`, fee drag is `0.07 * 0.50 * 0.50 = 0.0175` (1.75pp). At `price = 0.20`, it is `0.07 * 0.20 * 0.80 = 0.0112` (1.12pp).

Always compute **net edge** — never rely on `fair - mid`:

```
net_edge = fair - vwap_price - fee_drag - max_vwap_slippage
```

A strategy that shows `edge = 0.05` at mid-price may have `net_edge < 0` after fees and VWAP slippage. This is the single most common reason strategies lose money despite a sound fair value model.

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

- `bankroll = 5_000` — small enough to survive a cold streak.
- `min_edge = 0.04` — net of fees. Lower = more trades, higher win rate.
- `min_price = 0.20, max_price = 0.55` — avoid tail bets and near-certainty.
- `max_spread = 0.08` — reject illiquid markets.
- `max_notional_usdc = 150` — hard cap, pipeline also enforces `$25` live cap.
- `cooldown_seconds = 10` — prevent rapid re-entry.
- `confirmations_required = 2` — filter one-off noise signals.
- Use quarter Kelly (`kelly_frac = 0.25`, `max_bet_pct = 0.025`).
- Minimum bet $5 — reject micro-positions that can't cover fees.
- Avoid live GTC unless the user explicitly wants maker behavior.
- Require positive net edge after fees and VWAP slippage.
- Reject weak depth — ask depth must be at least 3x notional.

## Replay Support

Add replay support when the strategy's edge depends on rolling-market timing, spot moves, or historical microstructure.

Use `backtest/replay.py` patterns:

- Create a replay strategy class with a stable `name`.
- Consume recorded JSONL records rather than live `MarketContext`.
- Recompute the same gates as live where practical.
- Add parser flags for tunable thresholds.
- Add a branch in the strategy dispatch near existing `momentum`, `momentum_v2`, `leadlag`, `convergence`, `snipe`, `portfolio`, `volconv`, `oracle`, `rl`, and `whale`.
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

## Hand-Off To Auto Improvement

After a strategy is created, wired, and passes compile + dry-run validation, use `auto-improvement` to evaluate it through the research orchestrator. This closes the loop:

```
strategy-creator creates strategy
  -> auto-improvement runs experiment variants
    -> promote_candidate / keep_testing / reject
      -> if code change needed, hand back to strategy-creator
```

Use `auto-improvement` when:

- a new strategy needs controlled experiment comparison against baselines;
- gate results (accuracy, profit factor, Brier, drawdown) should decide next steps;
- parameter tuning is needed via experiment variants in `research/experiments.yaml`;
- the question is "is this strategy ready to promote?" and not "does it compile?".

Do not claim a strategy is live-ready from compile + dry-run alone. Always run at least one research loop before promotion.

## Common Mistakes

- Adding a strategy outside `strategies/v2/` for active use.
- Calling execution code directly inside a strategy.
- Returning model fair values as order prices — always use VWAP from the depth check.
- Using `edge = fair - mid` instead of `net_edge = fair - vwap - fee_drag`.
- Forgetting Polymarket's 7% parabolic taker fee — edge must be net of fees.
- Sizing in USDC but assigning the value to `TradingSignal.size`, which expects shares.
- Skipping the double-pass VWAP check — edge computed at `best_ask` may evaporate at the actual fill price.
- Forgetting `on_fill()` state updates, causing repeated entries.
- Forgetting `on_cancel()` for rolling window resets.
- Adding a CLI strategy name but not adding it to `roll_strats` or static setup.
- Emitting signals without confirmation gating — noise trades bleed edge to fees.
- Using flat `self.rejected += 1` instead of a Counter by reason — makes gate tuning impossible.
- Letting a shadow/diagnostic strategy emit orders unintentionally.
