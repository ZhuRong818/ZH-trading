# ZH Trading

Updated: 2026-05-08 (v3 — unified strategy interface)

ZH Trading is a lightweight Python trading pipeline for Polymarket. It discovers markets, polls live order books, runs strategy modules, applies portfolio and capital controls, routes orders through a shared execution layer, and tracks fills, positions, logs, and basic performance in memory.

This README documents the code that exists in this repository today. `POLYMARKET_TRADING_SYSTEM.md` is the larger target architecture; it describes components such as Kafka, Redis, TimescaleDB, WebSockets, a signing server, and LLM sentiment that are not implemented in this local pipeline yet.

## Current Capabilities

- Market discovery and filtering through the Polymarket Gamma API.
- Order book polling through the Polymarket CLOB API.
- In-memory market state, midpoint history, volatility, and regime classification.
- Dry-run execution with book-aware VWAP fills, pending GTC orders, and FOK/FAK behavior.
- Live CLOB order signing and submission with EIP-712 order signatures and CLOB API authentication.
- Shared OMS for fills, positions, realized PnL, unrealized PnL, and live position reconciliation.
- Capital allocation by strategy budget, market concentration, reserve, and locked collateral.
- Risk checks for exposure, drawdown, stop losses, volatility pauses, and kill switch shutdown.
- Strategy modules for Stoikov market making, sum-to-one arbitrage, whale copy trading, mean reversion, resolution fade, and BTC 5-minute markets.
- Modular pipeline with enforced stages: Risk Gate, Capital Gate, Executor, Tracker, Logger.
- Persistent JSONL trade logs plus runtime performance reports and heuristic tuning suggestions.

## Pipeline Flow

```text
┌─────────────────────────────────────────────────────────────┐
│                     DATA SOURCES                             │
│  Polymarket CLOB API │ Gamma API │ Data API │ Binance BTC   │
└───────────────────────────┬──────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│              DATA PIPELINE  (data_pipeline/)                 │
│  Book snapshots, VWAP pricing, midpoint history,            │
│  volatility, staleness checks, regime classification        │
└───────────────────────────┬──────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│              STRATEGIES  (strategies/)                        │
│  Stoikov MM │ Whale Copy │ Arb │ Mean Rev │ Fade │ BTC 5m   │
│                                                              │
│  Each strategy emits → TradingSignal                        │
└───────────────────────────┬──────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│              PIPELINE ENGINE  (pipeline/)                     │
│                                                              │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐                 │
│  │ 1. RISK  │→ │2. CAPITAL│→ │3. EXECUTOR│                 │
│  │   GATE   │  │   GATE   │  │           │                 │
│  │Drawdown? │  │Budget OK?│  │VWAP fill  │                 │
│  │Stop-loss?│  │Reserve?  │  │Depth check│                 │
│  │Circuit?  │  │Concentr? │  │Sim / Live │                 │
│  └──────────┘  └──────────┘  └─────┬─────┘                 │
│                                    │                        │
│  ┌──────────┐  ┌──────────┐        │                        │
│  │5. LOGGER │← │4. TRACKER│← ─────┘                        │
│  │Trade log │  │OMS update│                                 │
│  │Perf stats│  │P&L calc  │                                 │
│  │Fill rate │  │Collateral│                                 │
│  └──────────┘  └──────────┘                                 │
└───────────────────────────┬──────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│              ANALYTICS  (analytics/)                         │
│  JSONL trade log │ Sharpe/drawdown │ Parameter tuner        │
└──────────────────────────────────────────────────────────────┘
```

Every signal flows through: **Risk Gate → Capital Gate → Executor → Tracker → Logger**. No strategy can bypass a stage. If any stage rejects, the signal stops and the rejection reason is logged.

The pipeline tracks fill rate, rejection breakdown, and per-strategy signal stats. On shutdown it prints a full report including top rejection reasons.

## Repository Layout

```text
.
|-- main.py                              # Main multi-strategy runner
|-- search.py                            # Market search and MM candidate ranking
|-- POLYMARKET_TRADING_SYSTEM.md         # Aspirational architecture/spec
|-- .gitignore                           # Git ignore rules
|-- .env.example                         # Live-trading env var reference
|-- config/
|   `-- settings.py                      # API URLs and config dataclasses
|-- data_pipeline/
|   |-- market_data.py                   # Gamma/CLOB polling and market state
|   |-- market_provider.py               # MarketContext, StaticProvider, RollingProvider
|   |-- price_feeds.py                   # Binance BTC/ETH price feeds
|   `-- oracle.py                        # Chainlink oracle integration
|-- ems/
|   |-- execution.py                     # Auth, live orders, rate limit, SOR
|   |-- dry_run_sim.py                   # Book-aware dry-run fill simulator
|   `-- leg_handler.py                   # Leg execution helper
|-- pipeline/
|   |-- signal.py                        # TradingSignal — universal message
|   |-- stages.py                        # RiskGate, CapitalGate, Executor, Tracker, Logger
|   `-- engine.py                        # PipelineEngine — chains all stages
|-- oms/
|   |-- position_manager.py              # Fills, positions, PnL, reconciliation
|   `-- capital_allocator.py             # Cross-strategy capital budgets
|-- risk/
|   `-- risk_engine.py                   # Limits, stops, kill switch
|-- analytics/
|   |-- models.py                        # TradeRecord, MarketSnapshot, StrategySnapshot
|   |-- trade_log.py                     # Daily JSONL fill logs
|   |-- trade_recorder.py               # Round-trip trade builder from fills
|   |-- snapshot_collector.py            # Periodic market/strategy state capture
|   |-- performance.py                   # PnL, drawdown, Sharpe, win rate
|   |-- tuner.py                         # Human-review tuning suggestions
|   |-- learner.py                       # Auto-learner (reads past reports, adjusts params)
|   |-- post_session.py                  # PostSessionAnalyzer orchestrator
|   |-- strategy_analyzers/
|   |   |-- base.py                      # Universal trade metrics
|   |   `-- analyzers.py                 # 6 per-strategy analyzers (MM, arb, whale, etc.)
|   `-- reporters/
|       |-- json_reporter.py             # Full structured JSON dump
|       |-- csv_reporter.py              # Per-trade CSV export
|       `-- log_reporter.py              # Enhanced terminal report
|-- strategies/
|   |-- base.py                          # BaseStrategy interface (step → TradingSignal)
|   |-- kelly.py                         # Fractional Kelly helper
|   |-- unified_runner.py               # Legacy runner for rolling markets
|   |-- v2/                              # Unified strategies (all same interface)
|   |   |-- mm.py                        # Stoikov market making
|   |   |-- meanrev.py                   # Mean reversion
|   |   |-- fade.py                      # Resolution fade
|   |   |-- whale.py                     # Whale copy trading
|   |   |-- arb.py                       # Combinatorial arbitrage
|   |   |-- momentum.py                  # BTC/ETH momentum (5m markets)
|   |   `-- runner.py                    # UnifiedRunnerV2 — runs all strategies
|   |-- arbitrage/arb_detector.py        # Legacy arb detector
|   |-- btc_5m/btc_5m.py                 # Legacy BTC 5-minute runner
|   |-- market_making/stoikov_model.py   # Legacy Stoikov market maker
|   |-- mean_reversion/mean_reversion.py # Legacy mean reversion
|   |-- resolution_fade/resolution_fade.py  # Legacy resolution fade
|   `-- whale_tracking/whale_tracker.py  # Legacy whale tracker
|-- logs/                                # Runtime JSONL trade logs
`-- reports/                             # Post-session JSON, CSV, and analysis reports
```

## Setup

Use Python 3.10+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install requests numpy eth-account
```

There is no `requirements.txt` yet. The install command above reflects the imports used by the current code.

## Environment

Dry-run mode does not require private keys.

Live mode reads credentials from shell environment variables:

```powershell
$env:POLYMARKET_PRIVATE_KEY="your_private_key"
$env:POLYMARKET_FUNDER="your_polymarket_proxy_or_funder_wallet"
$env:POLYMARKET_SIG_TYPE="1"
```

`POLYMARKET_SIG_TYPE` defaults to `1`.

- `0`: direct EOA wallet
- `1`: Polymarket proxy wallet account

`.env.example` is only a reference. The code currently does not auto-load `.env`, so variables must be present in the process environment before live startup.

## Market Search

List high-volume active markets:

```powershell
python search.py
```

Search by keyword:

```powershell
python search.py bitcoin
python search.py iran --min-volume 100000
```

Rank market-making candidates:

```powershell
python search.py --mm --limit 10
```

`search.py --mm` scores markets by contested price, 24-hour volume, liquidity, and time to resolution. It filters out tail prices, low volume, near-expiry markets, and markets without usable outcome prices.

## Main Runner

`main.py` wires together the shared pipeline:

```powershell
python main.py --strategy mm --search bitcoin --dry-run
```

Common examples:

```powershell
# Interactive market making search
python main.py --strategy mm --search bitcoin --dry-run

# Trade known token IDs without interactive search
python main.py --strategy mm --token TOKEN_ID --dry-run

# Multi-market market making
python main.py --strategy mm --token TOKEN1,TOKEN2,TOKEN3 --dry-run

# Lower-drawdown strategy combo on selected markets
python main.py --strategy meanrev,fade --search bitcoin --dry-run

# Whale copy-trading dry-run
python main.py --strategy whale --dry-run

# Arbitrage scan by Gamma event slug
python main.py --strategy arb --arb-events "event-slug" --dry-run

# Run all shared-runner strategies
python main.py --strategy all --search election --arb-events "event-slug" --dry-run
```

Important flags:

| Flag | Purpose |
| --- | --- |
| `--strategy` | `mm`, `whale`, `arb`, `meanrev`, `fade`, `btc5m`, `rolling`, `all`, or comma-separated |
| `--rolling-asset` | Asset for rolling 5m markets: `btc` or `eth` (default: `btc`) |
| `--no-learn` | Disable auto-learner (skip loading past reports) |
| `--search` | Search markets interactively by keyword |
| `--token` | Comma-separated CLOB token IDs; skips market search |
| `--arb-events` | Comma-separated Gamma event slugs for arbitrage scans |
| `--arb-interval` | Seconds between arbitrage scans; default `30` |
| `--dry-run` | Paper mode; no real orders |
| `--gamma` | Stoikov risk aversion |
| `--spread-k` | Stoikov spread scaling |
| `--size` | Base order size in shares |
| `--levels` | Quote levels per side |
| `--interval` | Main loop refresh interval in seconds |
| `--max-position` | Max position size per market in USDC |
| `--max-drawdown` | Portfolio drawdown percentage before kill switch |
| `--reconcile-interval` | Live position reconciliation interval |
| `--verbose` | Debug logging |

## BTC 5-Minute / Rolling Markets

BTC 5-minute is now integrated into `main.py` via the `rolling` strategy and `RollingProvider`. It auto-discovers rolling 5-minute markets, rotates tokens every window, and settles positions when windows expire.

```bash
# BTC 5m momentum only
python main.py --strategy btc5m --dry-run --no-learn

# All strategies on BTC 5m rolling market (MM + meanrev + fade + momentum)
python main.py --strategy rolling --dry-run --no-learn

# Combined: rolling + momentum
python main.py --strategy rolling,btc5m --dry-run --no-learn

# ETH 5m instead of BTC
python main.py --strategy rolling --rolling-asset eth --dry-run --no-learn
```

Rolling-specific flags:

| Flag | Purpose |
| --- | --- |
| `--rolling-asset` | Asset for rolling markets: `btc` or `eth` (default: `btc`) |

## Fee Configuration

The system applies different fees based on market type, configured via `FeeConfig` in `config/settings.py`:

| Market Type | Fee |
|---|---|
| Standard markets (maker) | 0% |
| Standard markets (taker) | 1% (100 bps) |
| Crypto 5-minute markets | 7.2% (720 bps) |

The BTC 5-minute strategy fee of 7.2% is **not yet deducted** from dry-run PnL calculations — keep this in mind when evaluating simulated results.

## Strategies

All v2 strategies (`strategies/v2/`) share the same interface and work on both long-dated and 5-minute rolling markets.

### Market Making (`strategies/v2/mm.py`)

- Posts bid/ask quotes around a Stoikov reservation price.
- Skews quotes based on inventory to reduce directional risk.
- Adjusts spread by regime (wider in tail, tighter in contested).
- Filters bait orders when computing midpoint.
- Works on long-dated (static tokens) and 5m rolling markets.

### Mean Reversion (`strategies/v2/meanrev.py`)

- Buys when price drops 1%+ below moving average, sells when it reverts.
- Only trades in the 0.20-0.80 price range.
- Stop-loss at 2x entry deviation, 120s cooldown after stop.
- Sizes with fractional Kelly (0.25x).
- On 5m markets: uses shorter lookback (20 vs 30) and tighter threshold.

### Resolution Fade (`strategies/v2/fade.py`)

- Three sub-strategies: certainty fade, last-minute liquidity, convergence.
- Earns time decay premium as markets approach resolution.
- Max 5 concurrent positions, 0.15x Kelly sizing.

### Arbitrage (`strategies/v2/arb.py`)

- Scans event slugs for sum-to-one violations (exclusive outcomes priced > $1 total).
- Submits FOK legs through the pipeline.
- Scans every 30 seconds (configurable).

### Whale Copy (`strategies/v2/whale.py`)

- Monitors Polymarket leaderboard for top traders.
- First poll snapshots only (no false signals from empty baseline).
- Max 3 signals per whale, max 10 per cycle (prevents rate limit flood).
- Win rate threshold: 60%+ to copy.

### BTC Momentum (`strategies/v2/momentum.py`)

- Trades BTC 5-minute Up/Down markets using short-term momentum.
- Z-score model: distance-to-strike + momentum drift + volatility.
- Risk/reward gate: won't buy above $0.65.
- Positive edge required before trading.

## Execution Model

`ExecutionEngine` supports dry-run and live modes.

Dry-run mode:

- Uses `DryRunSimulator` when a data feed is attached.
- Walks the current book for VWAP-style fills.
- Supports FOK, FAK, and GTC behavior.
- Keeps unfilled GTC orders pending for later loop iterations.
- Falls back to immediate fills only if no simulator/data feed is available.

Live mode:

- Derives or creates CLOB API credentials.
- Signs CLOB auth messages and EIP-712 orders with `eth-account`.
- Posts signed orders to `/order`.
- Tracks open order IDs and can cancel individual orders or all known orders.

The EMS checks depth, price ticks, and a per-second rate limit before submitting orders. Capital allocation is handled by the pipeline's CapitalGate (not the EMS) to avoid double-booking.

## OMS, Capital, And Risk

The OMS stores fills and positions in memory. It updates average price, realized PnL, unrealized PnL, portfolio exposure, and can reconcile live positions against the Polymarket Data API by proxy wallet.

The capital allocator enforces:

- 20% system reserve by default.
- Per-strategy budgets from `CapitalConfig.strategy_budgets`.
- 10% max capital per market by default.
- Locked collateral accounting for short-side trades.

The risk engine enforces:

- Max portfolio exposure.
- Max drawdown kill switch.
- Per-position stop losses.
- Per-market position-size checks for whale copy trades.
- Volatility circuit breakers that pause a token.

When the kill switch fires, the EMS cancels known orders and attempts to close open positions using current best bid liquidity.

## Logs And Analytics

### Runtime Logging

`analytics/trade_log.py` writes daily JSONL files during the session:

```text
logs/trades_YYYY-MM-DD.jsonl
```

`analytics/performance.py` tracks total PnL, equity, trade count, drawdown, Sharpe ratio, and per-strategy win/PnL stats in memory. `analytics/tuner.py` logs human-review parameter suggestions every 10 minutes; it does not auto-change strategy settings.

### Post-Session Analysis

On shutdown, `PostSessionAnalyzer` runs a full diagnostic across all strategies and generates reports:

```text
reports/analysis_YYYY-MM-DD_HH-MM-SS.json    # Full structured analysis
reports/trades_YYYY-MM-DD_HH-MM-SS.csv       # Per-trade CSV for spreadsheets
```

The analysis includes:

- **Session summary**: duration, total trades, PnL, Sharpe, max drawdown, win rate.
- **Per-strategy breakdown** with strategy-specific KPIs:
  - Market Making: spread captured, round-trip fill rate, inventory accumulation.
  - Arbitrage: expected vs actual profit, multi-leg unwind rate.
  - Whale Copy: per-whale win rate, best/worst whale identified.
  - Mean Reversion: reversion accuracy, stop-loss trigger rate, hold time distribution.
  - Resolution Fade: per-sub-strategy PnL, days-to-resolution vs outcome.
  - BTC 5m: up/down direction accuracy, edge magnitude vs win rate.
- **Regime analysis**: PnL per market regime (tail/contested/trending).
- **Top and worst trades** ranked by PnL with full context.
- **Market snapshots** captured during the session for replay analysis.

The terminal log also prints an enhanced report on shutdown with all of the above.

## Live Trading

After setting environment variables, omit `--dry-run`:

```powershell
python main.py --strategy mm --token TOKEN_ID
```

Live mode submits real Polymarket CLOB orders. Validate in dry-run first, confirm token IDs and wallet/proxy configuration, and start with small sizes.

## V2 Strategy Interface

All strategies in `strategies/v2/` implement the same `BaseStrategy` interface:

```python
class BaseStrategy:
    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]
    def on_fill(self, fill: Fill)
    def snapshot(self) -> dict
```

Every strategy receives the same input (`MarketContext` with token, price, spread, time remaining, volatility, regime) and returns the same output (`TradingSignal` with token, side, price, size, edge). No strategy touches the EMS directly — all signals go through the pipeline.

This makes strategies flexible across market types:
- Long-dated markets use `StaticProvider` (fixed tokens)
- 5-minute rolling markets use `RollingProvider` (auto-rotating tokens)
- Same strategy code works on both

```bash
# 5-minute rolling market (all strategies)
python main.py --strategy rolling,btc5m --dry-run --no-learn

# Long-dated market
python main.py --strategy mm,meanrev,fade,whale --token TOKEN --dry-run --no-learn
```

`main.py` now uses v2 strategies exclusively. Legacy strategies in `strategies/` (outside `v2/`) are kept for reference.

## Auto-Learner

On startup, the learner reads past `reports/analysis_*.json` files and adjusts parameters:
- Widens spreads if spread capture is low
- Raises whale quality threshold if copy trades are losing
- Disables strategies with 3+ consecutive negative sessions
- Adjusts within ±30% of defaults, needs 10+ trades before acting

Disable with `--no-learn`. Clear old data with `rm reports/analysis_*.json`.

## Shutdown Behavior

On Ctrl+C, the system:
1. Cancels all open orders.
2. Closes all open positions at current best bid (sells everything).
3. Settles any rolling market positions (BTC 5m windows).
4. Runs post-session analysis and generates reports.
5. Prints pipeline stats (signals, executed, rejected, fill rate, rejection reasons).
6. Logs tuner suggestions.

## Current Limitations

- Dry-run simulator rejects most passive limit orders (MM quotes) because it can't model queue-based fills. Live mode would work correctly.
- Whale copy signals fire for markets the bot doesn't have book data for — these fail at the executor in dry-run mode.
- Legacy strategies in `strategies/` (outside v2/) are kept for reference but no longer used by main.py.
- No persistent database for positions, market state, or performance.
- No automatic `.env` loader.
- No `requirements.txt` or automated test suite.
- Market data is REST-polled instead of streamed over WebSockets.
- In-memory state is lost on restart except for JSONL trade logs.
- The target architecture document includes Kafka, Redis, TimescaleDB, LLM sentiment, and a signing server, but those are not implemented in this lightweight path.

## V2 Roadmap

1. **Risk-adjusted threshold**: Replace static edge threshold with a signal scoring system that weighs edge, confidence, downside risk, liquidity, and portfolio correlation. Take high-Sharpe signals even if raw edge is small; skip low-Sharpe signals even if edge looks big.
2. **Arb scoring and capital allocation**: When multiple arb opportunities exist, rank by edge magnitude, duration, depth, capital efficiency, and competition. Allocate capital top-down by score instead of first-come-first-served.
3. **Regime-based strategy analysis**: Use post-session trade data tagged with market regime to determine when each strategy works best. Analyze P&L by volatility regime, volume regime, time-of-day, and news events.
4. **Queue position model**: Estimate queue depth at each price level, expected time-to-fill from historical flow, and auto-cancel orders when expected fill time exceeds estimated arb duration. Requires WebSocket data for real-time queue tracking.
