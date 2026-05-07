# ZH Trading

Updated: 2026-05-07

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
- Persistent JSONL trade logs plus runtime performance reports and heuristic tuning suggestions.

## Pipeline Flow

```text
Polymarket Gamma API
Polymarket CLOB API
Polymarket Data API
Binance BTC/USDT feed for btc_5m
        |
        v
data_pipeline/market_data.py
market search, book snapshots, midpoint history, regime classification
        |
        v
strategies/
mm, arb, whale, meanrev, fade, btc_5m
        |
        v
risk/risk_engine.py + oms/capital_allocator.py
drawdown, exposure, stops, circuit breakers, budgets
        |
        v
ems/execution.py
dry-run simulator or signed live CLOB orders
        |
        v
oms/position_manager.py + analytics/
fills, positions, reconciliation, trade logs, performance
```

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
|   `-- market_data.py                   # Gamma/CLOB polling and market state
|-- ems/
|   |-- execution.py                     # Auth, live orders, rate limit, SOR
|   |-- dry_run_sim.py                   # Book-aware dry-run fill simulator
|   `-- leg_handler.py                   # Leg execution helper
|-- oms/
|   |-- position_manager.py              # Fills, positions, PnL, reconciliation
|   `-- capital_allocator.py             # Cross-strategy capital budgets
|-- risk/
|   `-- risk_engine.py                   # Limits, stops, kill switch
|-- analytics/
|   |-- trade_log.py                     # Daily JSONL fill logs
|   |-- performance.py                   # PnL, drawdown, Sharpe, win rate
|   `-- tuner.py                         # Human-review tuning suggestions
|-- strategies/
|   |-- arbitrage/arb_detector.py        # Sum-to-one and monotonic arb checks
|   |-- btc_5m/btc_5m.py                 # Standalone BTC 5-minute runner
|   |-- market_making/stoikov_model.py   # Stoikov market maker
|   |-- mean_reversion/mean_reversion.py # Contested-zone mean reversion
|   |-- resolution_fade/resolution_fade.py
|   |-- whale_tracking/whale_tracker.py
|   `-- kelly.py                         # Fractional Kelly helper
`-- logs/                                # Runtime logs and trade JSONL files
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
| `--strategy` | `mm`, `whale`, `arb`, `meanrev`, `fade`, `all`, or comma-separated values |
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

## BTC 5-Minute Runner

The BTC 5-minute strategy is standalone and is not wired through `main.py`.

It discovers rolling Polymarket BTC up/down 5-minute markets, polls Binance BTC/USDT as the price reference, computes momentum and trend strength, trades when estimated edge clears the configured threshold, and can attempt a buy-both-legs trade when `UP + DOWN < 1.0`.

```powershell
python strategies\btc_5m\btc_5m.py --dry-run
python strategies\btc_5m\btc_5m.py --dry-run --verbose
python strategies\btc_5m\btc_5m.py --dry-run --bankroll 5000 --kelly 0.20 --min-edge 0.03 --deadline 180
```

BTC 5-minute flags:

| Flag | Purpose |
| --- | --- |
| `--dry-run` | Paper mode |
| `--bankroll` | Strategy bankroll in USDC; default `5000` |
| `--kelly` | Kelly fraction; default `0.20` |
| `--min-edge` | Minimum edge required to trade; default `0.03` |
| `--deadline` | Entry cutoff in seconds before market end; default `180` |
| `--min-entry-age` | Seconds after market open before allowing entries; default `20` |
| `--max-adverse-bps` | Max adverse strike distance in bps; default `2.0` |
| `--verbose` | Debug logging |

## Fee Configuration

The system applies different fees based on market type, configured via `FeeConfig` in `config/settings.py`:

| Market Type | Fee |
|---|---|
| Standard markets (maker) | 0% |
| Standard markets (taker) | 1% (100 bps) |
| Crypto 5-minute markets | 7.2% (720 bps) |

The BTC 5-minute strategy fee of 7.2% is **not yet deducted** from dry-run PnL calculations — keep this in mind when evaluating simulated results.

## Strategies

### Market Making

`strategies/market_making/stoikov_model.py`

- Uses fresh CLOB order books for each selected token.
- Computes adjusted midpoint after filtering small bait orders.
- Falls back to Gamma market price when CLOB spread is too wide.
- Computes Stoikov reservation price and spread.
- Widens or tightens quotes based on market regime.
- Cancels existing quotes and posts layered bid/ask quotes through the EMS.

### Mean Reversion

`strategies/mean_reversion/mean_reversion.py`

- Trades only in contested markets.
- Uses recent midpoint history to compute a moving average.
- Buys dips below the mean and sells rips above the mean.
- Sizes with fractional Kelly.
- Exits on target, stop loss, or regime exit.

### Resolution Fade

`strategies/resolution_fade/resolution_fade.py`

- Trades certainty premium and time decay near resolution.
- Includes certainty fade, last-minute liquidity, and convergence logic.
- Uses conservative Kelly sizing and limits concurrent positions.

### Arbitrage

`strategies/arbitrage/arb_detector.py`

- `main.py` currently calls `scan_sum_to_one(event_slug)`.
- Detects exclusive outcome groups whose YES prices sum above the configured threshold.
- Estimates excess and submits FOK legs through the EMS.
- The detector also contains monotonic threshold checks, but those are not exposed through the main CLI.

### Whale Tracking

`strategies/whale_tracking/whale_tracker.py`

- Builds a registry from Polymarket leaderboard data.
- Enriches profiles with trade history and closed-position win rate.
- Tracks trusted wallets for new entries.
- Sizes copy trades by configured copy fraction and routes through the SOR when paired market metadata is available.

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

The EMS also checks capital allocation, depth, price ticks, and a simple per-second rate limit before submitting orders.

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

`analytics/trade_log.py` writes daily JSONL files:

```text
logs/trades_YYYY-MM-DD.jsonl
```

`analytics/performance.py` reports total PnL, equity, trade count, drawdown, Sharpe ratio, and per-strategy win/PnL stats. `analytics/tuner.py` logs human-review parameter suggestions periodically; it does not auto-change strategy settings.

## Live Trading

After setting environment variables, omit `--dry-run`:

```powershell
python main.py --strategy mm --token TOKEN_ID
```

Live mode submits real Polymarket CLOB orders. Validate in dry-run first, confirm token IDs and wallet/proxy configuration, and start with small sizes.

## Current Limitations

- No persistent database for positions, market state, or performance.
- No automatic `.env` loader.
- No `requirements.txt` or automated test suite.
- Market data is REST-polled instead of streamed over WebSockets.
- In-memory state is lost on restart except for JSONL trade logs.
- Dry-run fills are more realistic than instant fills, but still do not model queue priority, network latency, adverse selection, or complete exchange matching behavior.
- Main-runner arbitrage only exposes sum-to-one event scans.
- BTC 5-minute strategy is a separate runner.
- The target architecture document includes Kafka, Redis, TimescaleDB, LLM sentiment, and a signing server, but those are not implemented in this lightweight path.
