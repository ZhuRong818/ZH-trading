# ZH Trading

Updated: 2026-05-25

ZH Trading is a lightweight Python trading pipeline for Polymarket. It discovers markets, polls CLOB order books, runs strategy modules, sends all strategy output through a shared risk/capital/execution pipeline, and records fills, positions, logs, reports, and replay data.

`POLYMARKET_TRADING_SYSTEM.md` is the larger target architecture. This README describes the local code that exists in this repository today.

## Current Capabilities

- Market discovery and filtering through the Polymarket Gamma API.
- Order book polling through the Polymarket CLOB API.
- Static long-dated market trading by token ID or interactive search.
- Rolling 5-minute crypto markets for `btc`, `eth`, `sol`, and `xrp`.
- Volatility convexity arbitrage strategy for rolling 5-minute markets near strike.
- Shared `MarketContext -> BaseStrategy.step() -> TradingSignal` strategy interface.
- Book-aware dry-run execution with VWAP fills, FOK/FAK/GTC handling, and pending GTC simulation.
- Live CLOB order signing/submission with explicit live-risk acknowledgement.
- Live preflight checks for balance/allowance and optional open-order cleanup.
- Shared OMS for fills, positions, realized PnL, unrealized PnL, and live reconciliation.
- Capital allocation by system reserve, strategy budget, market concentration, and locked collateral.
- Risk gates for exposure, drawdown, per-position stops, volatility pauses, and kill switch shutdown.
- Runtime JSONL trade logs plus post-session JSON/CSV analysis reports.
- Replay tooling for recorded rolling-market data, including oracle, momentum, lead-lag, convergence, snipe, portfolio, and RL replay modes.
- Report-only research orchestration for replay/signal experiments, gates, rankings, and promotion recommendations.
- Tabular RL training and live `rl_shadow` logging mode.

## Pipeline

```text
Polymarket Gamma/CLOB + external price feeds
        |
data_pipeline/
        |
MarketProvider -> MarketContext[]
        |
strategies/v2/* -> TradingSignal[]
        |
PipelineEngine
  RiskGate -> CapitalGate -> Executor -> Tracker -> Logger
        |
OMS / analytics / logs / reports
```

Strategies never place orders directly. They only return `TradingSignal` objects. The pipeline decides whether a signal is allowed, sized, executed, tracked, and logged.

## Repository Layout

```text
.
|-- main.py                         # Main runner and CLI wiring
|-- search.py                       # Market search and MM candidate ranking
|-- STRATEGY_GUIDE.md               # Extra strategy notes
|-- POLYMARKET_TRADING_SYSTEM.md    # Aspirational architecture/spec
|-- .env.example                    # Live-trading env var reference
|-- config/
|   `-- settings.py                 # Config dataclasses and API constants
|-- data_pipeline/
|   |-- market_data.py              # CLOB/Gamma data feed and book snapshots
|   |-- market_provider.py          # StaticProvider, RollingProvider, MarketContext
|   |-- price_feeds.py              # BTC/ETH/SOL/XRP price feeds
|   `-- oracle.py                   # Rolling-window settlement oracle
|-- ems/
|   |-- execution.py                # Dry-run/live execution, auth, rate limits, SOR
|   `-- dry_run_sim.py              # Book-aware dry-run simulator
|-- pipeline/
|   |-- signal.py                   # TradingSignal contract
|   |-- stages.py                   # Risk, capital, execution, tracking, logging stages
|   `-- engine.py                   # PipelineEngine
|-- oms/
|   |-- position_manager.py         # Fills, positions, PnL, reconciliation
|   `-- capital_allocator.py        # Strategy budgets and capital locks
|-- risk/
|   `-- risk_engine.py              # Exposure, drawdown, stops, kill switch
|-- analytics/
|   |-- trade_log.py                # Runtime JSONL fill logs
|   |-- performance.py              # PnL, drawdown, Sharpe, win-rate stats
|   |-- post_session.py             # Post-session analysis orchestration
|   |-- reporters/                  # JSON, CSV, terminal reporters
|   `-- strategy_analyzers/         # Per-strategy diagnostics
|-- backtest/
|   |-- recorder.py                 # Live rolling-market JSONL recorder
|   |-- replay.py                   # Replay engine, replay strategies, structured JSON output
|   |-- rl_env.py                   # Tabular RL environment/model helpers
|   `-- rl_train.py                 # Train tabular RL model from recorded data
|-- research/
|   |-- signal_eval.py              # Standardized probability forecast evaluation
|   |-- orchestrator.py             # Report-only experiment orchestration
|   `-- experiments.yaml            # Default research experiment spec
|-- strategies/
|   |-- base.py                     # BaseStrategy interface
|   |-- kelly.py                    # Fractional Kelly helper
|   |-- v2/                         # Active unified strategies
|   |   |-- mm.py                   # Stoikov market making
|   |   |-- meanrev.py              # Mean reversion
|   |   |-- whale.py                # Whale copy trading
|   |   |-- momentum.py             # Rolling 5m momentum
|   |   |-- oracle_frontrun.py      # Binance/Polymarket lag strategy
|   |   |-- leadlag.py              # Cross-asset lead-lag
|   |   |-- last_seconds_snipe.py   # Final-window high-odds snipe
|   |   |-- vol_convexity.py        # Volatility convexity arb
|   |   |-- portfolio.py            # Regime portfolio wrapper
|   |   |-- rl_shadow.py            # Observational RL policy logger
|   |   |-- skills.py               # Shared strategy helpers
|   |   `-- runner.py               # UnifiedRunnerV2
|   `-- ...                         # Legacy/reference strategies
|-- data/                           # Recorder output JSONL files
|-- logs/                           # Runtime trade logs
|-- reports/                        # Analysis and RL model outputs
`-- .codex/skills/                  # Repo-specific Codex skills
```

Legacy strategies outside `strategies/v2/` are kept for reference. `main.py` uses the v2 interface.

## Setup

Use Python 3.10+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install requests numpy aiohttp eth-account
```

For live trading, also install the official Polymarket v2 CLOB client:

```powershell
python -m pip install py-clob-client-v2
```

There is no `requirements.txt` yet.

## Environment

Dry-run mode does not require private keys. Live mode reads credentials from process environment variables:

```powershell
$env:POLYMARKET_PRIVATE_KEY="your_private_key"
$env:POLYMARKET_FUNDER="your_polymarket_proxy_or_deposit_wallet"
$env:POLYMARKET_SIG_TYPE="3"
$env:POLYMARKET_LIVE_MAX_ORDER_USDC="25"
$env:POLYMARKET_LIVE_MIN_BALANCE_USDC="1"
$env:POLYMARKET_LIVE_FORCE_ORDER_TYPE="FAK"
$env:POLYMARKET_CANCEL_OPEN_ON_START="1"
$env:POLYMARKET_LIVE_POLL_INTERVAL="2"
```

`POLYMARKET_SIG_TYPE` defaults to `1`.

- `0`: direct EOA wallet
- `1`: Polymarket proxy wallet account
- `3`: Polymarket deposit wallet / `POLY_1271`
- `POLYMARKET_CANCEL_OPEN_ON_START=0` keeps existing open orders at live startup.
- `POLYMARKET_LIVE_POLL_INTERVAL` controls how often live fill polling runs (seconds).

`.env.example` is only a reference. The code does not auto-load `.env`.

## Market Search

```powershell
python search.py
python search.py bitcoin
python search.py iran --min-volume 100000
python search.py --mm --limit 10
```

`search.py --mm` ranks market-making candidates by contested price, volume, liquidity, and time to resolution.

## Main Runner

Common dry-run examples:

```powershell
# Static market making through interactive search
python main.py --strategy mm --search bitcoin --dry-run

# Static token IDs
python main.py --strategy mm --token TOKEN1,TOKEN2 --dry-run

# Static mean reversion on searched markets
python main.py --strategy meanrev --search bitcoin --dry-run

# Whale copy trading
python main.py --strategy whale --dry-run

# Rolling 5m default: momentum + oracle
python main.py --strategy rolling --rolling-asset btc --dry-run --no-learn

# Rolling strategy combinations
python main.py --strategy rolling,momentum --rolling-asset eth --dry-run --no-learn --verbose
python main.py --strategy rolling,oracle --rolling-asset btc --dry-run --no-learn
python main.py --strategy rolling,snipe --rolling-asset sol --dry-run --no-learn
python main.py --strategy rolling,volconv --rolling-asset btc --dry-run --no-learn
python main.py --strategy rolling,portfolio --rolling-assets btc,eth,sol,xrp --dry-run --no-learn
python main.py --strategy rolling,rl_shadow --rolling-asset btc --dry-run --no-learn

# Alias for rolling momentum
python main.py --strategy btc5m --rolling-asset btc --dry-run --no-learn
```

Important flags:

| Flag | Purpose |
| --- | --- |
| `--strategy` | `mm`, `whale`, `meanrev`, `btc5m`, `rolling`, `oracle`, `snipe`, `volconv`, `portfolio`, `rl_shadow`, `all`, or comma-separated |
| `--search` | Search markets interactively by keyword |
| `--token` | Comma-separated CLOB token IDs; skips market search |
| `--dry-run` | Paper mode; no real orders |
| `--live` | Real-money mode; requires `--i-understand-live-risk` |
| `--rolling-asset` | One rolling asset: `btc`, `eth`, `sol`, or `xrp` |
| `--rolling-assets` | Multiple rolling assets, e.g. `btc,eth,sol,xrp` |
| `--no-learn` | Disable auto-learner |
| `--verbose` | Debug logging |
| `--max-position` | Max position per market in USDC |
| `--max-drawdown` | Drawdown percentage before kill switch |
| `--live-max-order-usdc` | Hard cap per live order |
| `--live-min-balance-usdc` | Minimum live collateral balance required at startup |
| `--live-order-type` | Force live order type; default is `FAK` |
| `--allow-live-gtc` | Allow strategy order types in live mode |
| `--keep-open-orders` | Keep existing open orders at live startup |
| `--live-check-only` | Authenticate and run live preflight, then exit |
| `--reconcile-interval` | Seconds between live position reconciliation |
| `--rl-*` | Configure `rl_shadow` model path and gates |
| `--volconv-*` | Tune volatility convexity entry gates |

## Strategies

All active strategies inherit from `strategies/base.py`:

```python
class BaseStrategy:
    name: str = "base"

    def step(self, contexts: list[MarketContext]) -> list[TradingSignal]:
        raise NotImplementedError

    def on_fill(self, fill: Fill):
        pass

    def on_cancel(self):
        pass

    def snapshot(self) -> dict:
        return {}
```

`MarketContext` includes token IDs, best bid/ask, spread, book snapshot, seconds remaining, regime, volatility, condition ID, question, external price, and rolling-window strike. `TradingSignal` includes token, side, price, size, strategy name, order type, edge, confidence, fair value, and direction.

Active v2 strategies:

| Strategy | File | CLI use | Notes |
| --- | --- | --- | --- |
| Stoikov MM | `strategies/v2/mm.py` | `mm` | Passive bid/ask quotes, inventory skew, static or rolling |
| Mean Reversion | `strategies/v2/meanrev.py` | `meanrev` | Contested-market dip/reversion logic |
| Whale Copy | `strategies/v2/whale.py` | `whale` | Leaderboard polling and copy signals |
| Momentum | `strategies/v2/momentum.py` | `btc5m`, `rolling,momentum` | Rolling 5m z-score/momentum entries |
| Oracle Front-Run | `strategies/v2/oracle_frontrun.py` | `rolling,oracle` | Trades stale Polymarket odds after external price moves |
| Lead-Lag | `strategies/v2/leadlag.py` | via `portfolio` live, direct in replay | BTC-led follower asset signals |
| Last Seconds Snipe | `strategies/v2/last_seconds_snipe.py` | `rolling,snipe` | Late-window high-odds UP/DOWN entries |
| Volatility Convexity | `strategies/v2/vol_convexity.py` | `rolling,volconv` | Short-window convexity arb near strike |
| Portfolio | `strategies/v2/portfolio.py` | `rolling,portfolio` | Regime wrapper over momentum/oracle/leadlag/snipe |
| RL Shadow | `strategies/v2/rl_shadow.py` | `rolling,rl_shadow` | Logs tabular RL intended actions; emits no orders |

When `--strategy rolling` is used without a child strategy, `main.py` defaults to rolling `momentum` plus `oracle`.

## Rolling 5-Minute Markets

`RollingProvider` auto-discovers the current 5-minute market for each asset, refreshes both UP and DOWN books, attaches external spot price and strike price, and rotates windows. `UnifiedRunnerV2` handles window roll, cancels stale orders, and in dry-run mode settles rolling positions using `SettlementOracle`.

Examples:

```powershell
python main.py --strategy rolling --rolling-asset btc --dry-run --no-learn
python main.py --strategy rolling,snipe --rolling-asset xrp --dry-run --no-learn --verbose
python main.py --strategy rolling,volconv --rolling-asset btc --dry-run --no-learn
python main.py --strategy rolling,portfolio --rolling-assets btc,eth,sol,xrp --dry-run --no-learn
```

## Execution Model

Dry-run mode:

- Uses `DryRunSimulator` when a data feed is attached.
- Walks current book depth for VWAP-style fills.
- Supports `FOK`, `FAK`, and `GTC`.
- Keeps unfilled GTC orders pending for later loop iterations.
- Settles rolling-market positions at window roll.

Live mode:

- Requires `--live --i-understand-live-risk`.
- Requires `POLYMARKET_PRIVATE_KEY`; proxy/deposit wallets also require `POLYMARKET_FUNDER`.
- Runs startup preflight for signer, funder, balance/allowance, order size, and open orders.
- Cancels existing open orders on startup by default (use `--keep-open-orders` or `POLYMARKET_CANCEL_OPEN_ON_START=0` to skip).
- Forces `FAK` by default to avoid stale GTC exposure.
- Polls accepted live orders for real fills before recording them.
- Cancels known open orders on shutdown and attempts real close orders for tracked positions.

## Capital And Risk

`CapitalAllocator` enforces:

- 20% system reserve by default.
- Per-strategy budgets from `CapitalConfig.strategy_budgets`.
- Per-market concentration limits.
- Locked collateral accounting.

`RiskEngine` enforces:

- Max portfolio exposure.
- Equity-based drawdown kill switch.
- Per-position stop losses.
- Per-market position-size checks.
- Volatility circuit breakers.

Drawdown is measured from peak equity, where equity is initial capital plus cumulative realized PnL.

## Backtesting, Recording, And RL

Record live rolling-market data:

```powershell
python -m backtest.recorder
python -m backtest.recorder --assets btc,eth,sol,xrp --interval 0.5
```

Replay collected JSONL data. `backtest.replay` and `backtest.rl_train` default to `data_v2`, while `backtest.recorder` writes to `data/`, so pass `--data-dir data` when replaying recorder output:

```powershell
python -m backtest.replay --strategy oracle --assets btc,eth,sol,xrp --data-dir data
python -m backtest.replay --strategy momentum --assets btc --data-dir data
python -m backtest.replay --strategy leadlag --assets btc,eth,sol,xrp --data-dir data
python -m backtest.replay --strategy convergence --conv-threshold 0.88 --conv-window 45 --data-dir data
python -m backtest.replay --strategy snipe --assets btc,eth,sol,xrp --data-dir data
python -m backtest.replay --strategy portfolio --assets btc,eth,sol,xrp --data-dir data
python -m backtest.replay --strategy rl --assets btc,eth,sol,xrp --data-dir data --rl-model reports/rl_model.json
```

Write machine-readable replay output for research orchestration:

```powershell
python -m backtest.replay --strategy volconv --assets btc,eth --data-dir data --out-json reports/tmp_replay.json
```

Run report-only research orchestration. The orchestrator reads `research/experiments.yaml`, runs replay and/or `research.signal_eval` experiments, applies gates, ranks variants, and writes summaries without changing live config or strategy code:

```powershell
python -m research.orchestrator --spec research/experiments.yaml

# Quick mechanics check
python -m research.orchestrator --spec research/experiments.yaml --max-records 5000
```

Train the tabular RL model:

```powershell
python -m backtest.rl_train --assets btc,eth,sol,xrp --data-dir data --model-out reports/rl_model.json
```

## Logs And Reports

Runtime fills are written to:

```text
logs/trades_YYYY-MM-DD.jsonl
```

Post-session analysis writes:

```text
reports/analysis_YYYY-MM-DD_HH-MM-SS.json
reports/trades_YYYY-MM-DD_HH-MM-SS.csv
```

RL training writes, by default:

```text
reports/rl_model.json
reports/rl_training_report.json
```

Research orchestration writes:

```text
reports/research_runs/<run_id>/summary.json
reports/research_runs/<run_id>/summary.md
reports/research_runs/<run_id>/<mode>_<experiment>.json
```

Repo-specific Codex skills:

- `strategy-creator`: create and wire candidate strategies under the v2 architecture.
- `auto-improvement`: report-only research loop that reads orchestration summaries, creates the next experiment variants, runs `research.orchestrator`, and recommends `promote_candidate`, `keep_testing`, or `reject`.

## Live Trading

Run a live preflight first:

```powershell
python main.py `
  --live `
  --i-understand-live-risk `
  --live-check-only
```

Then start small:

```powershell
python main.py `
  --strategy rolling,snipe `
  --rolling-asset btc `
  --live `
  --i-understand-live-risk `
  --live-max-order-usdc 10 `
  --max-position 25 `
  --max-drawdown 2 `
  --no-learn
```

Live mode submits real Polymarket CLOB orders. Validate in dry-run first, use a small dedicated wallet, and confirm wallet/proxy/deposit-wallet configuration.

## Current Limitations

- No `requirements.txt` or automated test suite.
- No persistent database for positions, market state, or performance.
- No automatic `.env` loader.
- REST polling is used instead of WebSockets.
- In-memory OMS/risk state is lost on restart except for logs/reports.
- Dry-run passive maker fills are only an approximation; queue position is not modeled.
- Some replay modes use separate replay wrappers rather than the exact live strategy class.
- The target architecture document includes Kafka, Redis, TimescaleDB, LLM sentiment, and a signing server; those are not implemented in this lightweight path.

## Useful Validation Commands

```powershell
python -m py_compile main.py config/settings.py data_pipeline/market_provider.py pipeline/signal.py strategies/base.py strategies/v2/runner.py
python -m py_compile strategies/v2/mm.py strategies/v2/meanrev.py strategies/v2/whale.py strategies/v2/momentum.py strategies/v2/oracle_frontrun.py strategies/v2/leadlag.py strategies/v2/last_seconds_snipe.py strategies/v2/vol_convexity.py strategies/v2/portfolio.py strategies/v2/rl_shadow.py
python -m py_compile backtest/replay.py backtest/recorder.py backtest/rl_env.py backtest/rl_train.py
```
