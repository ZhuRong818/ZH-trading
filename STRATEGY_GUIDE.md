# Strategy Guide

## Overview

5 active strategies, each designed for specific market conditions on Polymarket.

| Strategy | Market Type | Signal Source | Execution Style |
|---|---|---|---|
| **Stoikov MM** | Long-dated prediction markets | Order book spread | Passive (post and wait) |
| **Mean Reversion** | Long-dated prediction markets | Price deviation from moving average | Aggressive (cross spread) |
| **Whale Copy** | Any Polymarket market | Leaderboard trader positions | Aggressive (cross spread) |
| **Momentum** | BTC 5-minute rolling | BTC price trend (Binance) | Aggressive (cross spread) |
| **Oracle Front-Run** | BTC 5-minute rolling | Binance-Polymarket price lag | Aggressive (cross spread) |

---

## 1. Stoikov Market Making

**File:** `strategies/v2/mm.py`

### How It Works

Posts bid and ask quotes around a mathematically computed reservation price. Earns the spread when both sides fill.

```
Reservation Price = mid - (inventory × gamma × volatility² × time_left)
Spread = gamma × volatility² × time_left + (2/gamma) × ln(1 + gamma/k)
```

- If holding inventory (long), shifts quotes DOWN to encourage selling.
- If flat, quotes symmetrically around mid.
- Widens spread in high volatility, narrows in low.
- Filters bait orders (<$50) when computing midpoint.
- Posts 3 levels per side with decreasing size.

### Parameters

| Parameter | Default | Effect |
|---|---|---|
| `gamma` | 0.5 | Risk aversion. Higher = wider spread, less risk. |
| `spread_k` | 1.5 | Spread scaling. Higher = tighter spread. |
| `num_levels` | 3 | Quote levels per side. |
| `order_size` | 20 | Shares per quote level. |

### Tested Performance

| Metric | Result | Context |
|---|---|---|
| Trades | 20 | Across 3 sessions on PSG Champions League |
| Win Rate | 5% | Only 1 of 20 round-trips profitable |
| PnL | -$9.12 | Net loss |
| Spread Captured | -$0.018 | Negative — buying high, selling low |

### Why It Lost

MM was tested on 5-minute rolling markets where token price **trends toward 0 or 1**. MM requires price oscillation. On long-dated contested markets (its intended environment), it should perform differently but **cannot be tested in dry-run** because the simulator doesn't model passive limit order fills.

### Limitations

- **Cannot test in dry-run.** Passive orders sit on the book waiting for counterparties. The simulator only fills aggressive orders that cross the spread.
- **Adverse selection on 5m markets.** People who trade against your quotes on a trending market know which way it's going. You're always on the wrong side.
- **Needs live testing.** The only way to validate is with real capital ($50-100) on a contested long-dated market.

### Best Suited For

- Long-dated markets with price in 0.35-0.65 range
- Volume >$200k/day
- Resolution >2 weeks away
- Example: "US Iran peace deal by June 30" at 0.50

### Command

```bash
python main.py --strategy mm --token TOKEN_ID --dry-run --no-learn
```

---

## 2. Mean Reversion

**File:** `strategies/v2/meanrev.py`

### How It Works

When price deviates from its recent moving average, bets that it will return to the mean.

```
Moving Average (20 observations) = 0.45
Current Price = 0.43 (deviation = -0.02, exceeds 1% threshold)
→ BUY at 0.43
→ Target: 0.45 (the mean)
→ Stop-loss: 0.39 (2× deviation below entry)
```

Exits when:
- Price reverts to the mean (target hit) → profit
- Price keeps moving against you (stop-loss) → cut loss + 120s cooldown
- Price leaves the 0.20-0.80 regime → exit

Sizes with fractional Kelly (0.25×).

### Parameters

| Parameter | Default | Effect |
|---|---|---|
| `entry_threshold` | 0.01 (1%) | Min deviation from mean to enter |
| `lookback` | 20 observations | Moving average window |
| `stop_multiple` | 2.0 | Stop-loss at 2× entry deviation |
| `min_price / max_price` | 0.20 / 0.80 | Only trade in this price range |
| `cooldown` | 120s | Wait after stop-loss before re-entry |

### Tested Performance

No completed trades on long-dated markets. The price of "Iran peace deal" didn't deviate 1% from its moving average in 3+ hours of testing.

On 5-minute rolling markets, mean reversion is **fundamentally wrong** — the token price trends toward 0 or 1, it doesn't oscillate.

### Limitations

- **Needs hours to trigger on long-dated markets.** Political markets move slowly. A 1% deviation might take a day.
- **Wrong model for 5m markets.** Binary outcome tokens trend, they don't mean-revert.
- **Cannot test in dry-run on long-dated markets.** Same passive order issue as MM.
- **News events break the assumption.** If price moves because of real news (ceasefire announced), it won't revert.

### Best Suited For

- Long-dated contested markets (0.30-0.70) during periods of **no news**
- Markets that have been oscillating in a range for days
- NOT suitable for 5-minute rolling markets

### Command

```bash
# Long-dated market
python main.py --strategy meanrev --token TOKEN_ID --dry-run --no-learn

# On 5m rolling (not recommended, but possible)
python main.py --strategy rolling,meanrev --dry-run --no-learn
```

---

## 3. Whale Copy Trading

**File:** `strategies/v2/whale.py`

### How It Works

1. Polls Polymarket leaderboard every 10 minutes for top traders by PnL
2. First poll: snapshots all whale positions (no signals — baseline)
3. Subsequent polls: compares current positions to baseline
4. If a whale increased a position by >$20 notional → copy signal
5. Sizes at 15% of the whale's delta, capped at $2,000

Safety features:
- First poll snapshot prevents false signals from empty baseline
- Max 3 signals per whale per cycle
- Max 10 signals total per cycle
- Win rate threshold: 60%+ to copy
- Skips extreme prices (>0.95 or <0.05)

### Parameters

| Parameter | Default | Effect |
|---|---|---|
| `high_confidence_win_rate` | 0.60 | Min whale win rate to copy |
| `copy_fraction` | 0.15 | Copy 15% of whale's position delta |
| `max_copy_size_usdc` | 2,000 | Max $2k per copy trade |
| `top_n_traders` | 20 | Track top 20 by PnL |

### Tested Performance

| Metric | Result |
|---|---|
| Signals detected | 391 (in 20 min session) |
| Signals executed | 0 |
| Fill rate | 0% |

Whale signals fired correctly — detected real whale moves (RN1 on tennis, surfandturf on Lakers, GamblingIsAllYouNeed on football). All failed at the executor because the dry-run simulator didn't have book data for the whale's target markets.

### Limitations

- **Dry-run can't fill whale signals.** The whale buys on a market we don't have book data for. The simulator needs a book to simulate fills. In live mode, the EMS just submits to the CLOB.
- **Detection lag.** Polling every 5 seconds means 5-15 seconds between whale's trade and our copy. The market may have already moved.
- **No exit logic.** Once copied, the position is held until market resolution. No stop-loss, no take-profit.
- **Whale might be wrong.** Even 80% win rate whales lose 20% of the time.

### Best Suited For

- **Live trading only.** Cannot be properly tested in dry-run.
- Any market where a trusted whale makes a large conviction bet
- Best on markets with >$100k daily volume (enough liquidity to fill)

### Command

```bash
python main.py --strategy whale --dry-run --no-learn
```

---

## 4. BTC Momentum

**File:** `strategies/v2/momentum.py`

### How It Works

Trades BTC 5-minute Up/Down markets by following short-term BTC price momentum.

1. Polls BTC price from Binance every 0.5 seconds
2. Computes z-score: `(current - strike) / (volatility × √time_remaining)`
3. Adjusts for momentum drift and trend strength
4. Converts z-score to probability via error function
5. Compares fair probability vs Polymarket odds
6. If edge > 3% and risk/reward favorable → BUY

Risk filters:
- Won't buy above $0.65 (bad risk/reward)
- Momentum-volatility ratio filter (ignore noise)
- Positive edge required

### Parameters

| Parameter | Default | Effect |
|---|---|---|
| `min_edge` | 0.03 (3%) | Min fair-market gap to trade |
| `kelly_frac` | 0.20 | 20% Kelly sizing |
| `max_price` | 0.65 | Won't buy above this |
| `min_mom_vol_ratio` | 0.5 | Momentum must exceed 0.5× noise |
| `momentum_window` | 20 ticks | ~10 seconds of price history |

### Tested Performance

#### Live Dry-Run Sessions

| Session | Trades | Wins | Losses | PnL | Win Rate |
|---|---|---|---|---|---|
| Session 1 (1.3 min) | 6 | 6 | 0 | +$564.61 | 100% |
| Session 2 (11 min) | 2 | 1 | 1 | -$116.91 | 50% |
| Session 3 (5.7 min) | 4 | 0 | 4 | -$443.57 | 0% |
| **Total** | **12** | **7** | **5** | **+$4.13** | **58%** |

#### Backtest with Real Polymarket Prices

| Period | Trades | Win Rate | PnL | Profit Factor | Sharpe | Max DD |
|---|---|---|---|---|---|---|
| 24 hours | 62 | 93.5% | +$11,370 | 12.02 | 24.70 | $516 |
| **7 days** | **389** | **92.0%** | **+$69,092** | **9.63** | **21.77** | **$517** |

Key findings from 7-day real-price backtest:
- **Late entries win more**: >=180s entry has 97.3% WR vs 81.2% for 120-180s
- **High edge trades dominate**: edge >=0.25 has 93.1% WR (363 of 389 trades)
- **Balanced UP/DOWN**: 92.3% WR down, 91.8% WR up — no directional bias
- **Fees included**: uses real Polymarket parabolic fee formula (fee = shares × 0.07 × p × (1-p))
- **Slippage included**: ~1% average adverse slippage per trade

### Limitations

- **Holds to settlement.** No mid-window exit. If BTC reverses after entry, rides the loss to $0.
- **Late-window entry bias.** The V2 momentum runner enters at 120-240s (2-4 min into window), by which point the direction is largely determined. This is a feature (high confidence) but means fewer trading opportunities per window.
- **Backtest uses 1-minute BTC granularity.** Live strategy polls every 0.5s — backtest can't fully replicate sub-minute signal dynamics.

### Best Suited For

- BTC 5-minute rolling markets in **all market conditions** (trending and contested)
- The V2 confirmation + trending filter makes it selective — trades ~19% of windows
- **Recommended as the primary strategy for live trading**

### Command

```bash
python main.py --strategy rolling,momentum --dry-run --no-learn

# Backtest with real prices
python -m backtest.run --strategy momentum --real-prices --hours 168
```

---

## 5. Oracle Front-Run

**File:** `strategies/v2/oracle_frontrun.py`

### How It Works

Exploits the 1-3 second lag between Binance BTC price and Polymarket 5m market odds.

```
Second 0: BTC drops $28 on Binance (3.5 bps)
Second 1: Polymarket still prices DOWN at $0.30 (stale)
           Fair value should be ~$0.57 based on the move
           Staleness gap: 27% → exceeds 8% threshold
Second 2: BUY DOWN at $0.30 (before Polymarket adjusts)
Second 5: Polymarket adjusts to reflect the move
Minute 5: Window resolves → if BTC ended down, payout = $1.00
```

Not a prediction — trades on something that **already happened** on Binance but hasn't been reflected on Polymarket yet.

### Parameters

| Parameter | Default | Effect |
|---|---|---|
| `move_threshold_bps` | 3.5 | Min BTC move to trigger (0.035%) |
| `staleness_threshold` | 0.08 | Polymarket must lag >8% behind fair |
| `lookback_ticks` | 5 | Compare price over last 5 ticks (~2.5s) |
| `max_price` | 0.60 | Won't buy above $0.60 |
| `cooldown_seconds` | 10 | Wait between trades |

### Tested Performance

#### Live Dry-Run Sessions

| Session | Trades | Wins | Losses | PnL | Win Rate |
|---|---|---|---|---|---|
| 209 min session | 22 | 15 | 7 | +$3,172 | 68.2% |
| 20 min session | 2 | 2 | 0 | +$1,349 | 100% |
| 60 min session | 7 | 3 | 4 | -$555 | 42.9% |
| **Total dry-run** | **31** | **20** | **11** | **+$3,966** | **64.5%** |

#### Backtest with Real Polymarket Prices

| Period | Trades | Win Rate | PnL | Profit Factor | Sharpe | Max DD |
|---|---|---|---|---|---|---|
| 24 hours | 11 | 36.4% | -$1,126 | 0.63 | -3.53 | $2,183 |
| **7 days** | **61** | **52.5%** | **+$2,191** | **1.16** | **1.14** | **$2,222** |

Key findings from 7-day real-price backtest:
- **Low edge trades are better**: edge <0.14 has 60% WR (+$2,624), edge 0.14-0.18 has 43% WR (-$446)
- **Entry at 0.45-0.50 is the sweet spot**: 78.9% WR vs 36.4% at 0.40-0.45
- **DOWN trades slightly better**: 54.5% WR vs 50.0% UP
- **Inverted edge problem**: the `bps/50` fair value model overestimates probability shifts — when it thinks there's a big edge, Polymarket has usually already adjusted

### Why Real Prices Are Different from Dry-Run

Dry-run sessions showed 64.5% WR because the simulator used fake Polymarket prices that were deliberately "stale." Real Polymarket prices track BTC much faster than assumed — the staleness the oracle detects is mostly noise, not real lag.

### Limitations

- **Marginal edge.** PF 1.16 over 7 days means the strategy barely breaks even after fees.
- **Polymarket is fast.** The assumed 1-3 second lag is smaller than expected — prices adjust within 1 second most of the time.
- **Fair value model is too aggressive.** `bps/50` overestimates how much a BTC move should shift Polymarket odds.
- **Needs WebSocket speed** to capture the brief staleness windows that do exist.

### Best Suited For

- BTC 5-minute markets during **high volatility** (large BTC moves where Polymarket genuinely lags)
- **Not recommended as primary strategy** — momentum is significantly better
- Useful as a supplementary signal when combined with momentum

### Command

```bash
python main.py --strategy rolling,oracle --dry-run --no-learn

# Backtest with real prices
python -m backtest.run --strategy oracle --real-prices --hours 168
```

---

## Strategy Comparison

### Backtested with Real Polymarket Prices (7 days)

| | Momentum | Oracle | MM | Mean Rev | Whale |
|---|---|---|---|---|---|
| **Market type** | 5m rolling | 5m rolling | Long-dated | Long-dated | Any |
| **7-day trades** | 389 | 61 | N/A | N/A | N/A |
| **Win rate** | **92.0%** | 52.5% | untested | untested | untested |
| **7-day PnL** | **+$69,092** | +$2,191 | N/A | N/A | N/A |
| **Profit factor** | **9.63** | 1.16 | N/A | N/A | N/A |
| **Sharpe** | **21.77** | 1.14 | N/A | N/A | N/A |
| **Max drawdown** | **$517** | $2,222 | N/A | N/A | N/A |
| **Live ready** | **Yes** | Marginal | Needs live test | Needs longer run | Needs live test |
| **Biggest risk** | Late entry bias | Noise trading | Adverse selection | News events | Whale is wrong |

### Key Takeaway

**Momentum is the clear winner.** With real Polymarket prices over 7 days:
- 92% win rate with 389 trades (statistically significant)
- $517 max drawdown on $10k bankroll (excellent risk control)
- Profit factor 9.63 (wins are 9.6x losses)
- Fees included (real Polymarket parabolic fee formula)

Oracle is marginally profitable (PF 1.16) but not reliable enough for primary use.

## Backtesting

Run backtests with real Polymarket historical prices:

```bash
# Momentum (recommended — takes ~15 min to load 7 days)
python -m backtest.run --strategy momentum --real-prices --hours 168

# Oracle
python -m backtest.run --strategy oracle --real-prices --hours 168

# Both
python -m backtest.run --strategy momentum,oracle --real-prices --hours 168

# Fast mode with simulated prices (less accurate but instant)
python -m backtest.run --strategy momentum --days 7
```

Note: `--real-prices` fetches actual Polymarket token prices from the CLOB API. Results are realistic but loading takes ~15 minutes per 7 days. Without the flag, simulated prices run in seconds but overestimate performance.

## Recommended Next Steps

1. **Live test momentum** on BTC 5m with small capital ($50-100). The 92% backtest win rate with real prices strongly suggests genuine alpha.
2. **Deprioritize oracle** — marginal edge (PF 1.16) doesn't justify the risk. Consider as supplementary signal only.
3. **Live test MM** on a contested long-dated market with $50-100 — can't be backtested (passive orders).
4. **Add mid-window exit** to momentum — cut losses if trade goes >30% against you within 60 seconds.
5. **Run longer backtests** (30+ days with real prices) to confirm momentum edge persists across different market conditions.
