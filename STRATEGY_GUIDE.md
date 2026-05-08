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

| Session | Trades | Wins | Losses | PnL | Win Rate |
|---|---|---|---|---|---|
| Session 1 (1.3 min) | 6 | 6 | 0 | +$564.61 | 100% |
| Session 2 (11 min) | 2 | 1 | 1 | -$116.91 | 50% |
| Session 3 (5.7 min) | 4 | 0 | 4 | -$443.57 | 0% |
| **Total** | **12** | **7** | **5** | **+$4.13** | **58%** |

### Limitations

- **Momentum is noisy on short timeframes.** A 2-second BTC move can reverse in the next 2 seconds.
- **7.2% taker fee not deducted.** The reported PnL doesn't account for Polymarket's 7.2% fee on crypto 5m markets. After fees, most winning trades become breakeven or losers.
- **Pending fill pile-up (fixed).** Previously, old pending orders from the simulator would fill after the strategy stopped emitting signals, causing position blowup. Now cancelled on fill.
- **Holds to settlement.** No mid-window exit. If BTC reverses after entry, rides the loss to $0.

### Best Suited For

- BTC 5-minute rolling markets during **volatile periods** (news, liquidations)
- NOT suited for flat/quiet markets (false signals)

### Command

```bash
python main.py --strategy rolling,momentum --dry-run --no-learn
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

| Session | Trades | Wins | Losses | PnL | Win Rate |
|---|---|---|---|---|---|
| Session 1 (8.8 min, 3.0bps threshold) | 2 | 1 | 1 | +$236.46 | 50% |
| Session 2 (9.3 min, 3.0bps threshold) | 2 | 1 | 1 | -$374.34 | 50% |
| Session 3 (15 min, 3.5bps threshold) | 1 | 0 | 1 | -$526.35 | 0% |
| Session 4 (15 min, 8.0bps threshold) | 0 | 0 | 0 | $0.00 | N/A |
| Session 5 (22.5 min, 3.0bps threshold) | 2 | 0 | 2 | -$1,539.87 | 0% |
| **Total** | **7** | **2** | **5** | **-$2,204.10** | **29%** |

### Why It's Losing

1. **3.0-3.5 bps moves are noise.** A $28 BTC move reverses constantly. The "lag" the oracle detects is often just random ticks, not real momentum.
2. **Polymarket might not be stale.** The odds might already reflect the move — the 5% staleness threshold was too low. Now at 8%.
3. **Holds to settlement.** Even if the signal was right at entry, BTC can reverse in the remaining 4 minutes.
4. **Asymmetric losses.** Wins pay $0.60-$0.70 per share, losses cost $0.30-$0.45 per share. Needs >40% win rate to break even.

### What Would Improve It

- **Higher threshold (15-30 bps)** would only trigger on real moves, but needs volatile markets (news events, liquidations). In quiet periods, zero trades.
- **Mid-window exit** if the move reverses within 30 seconds of entry.
- **Volume confirmation** — check if the BTC move was on high volume (more likely to persist).
- **Multiple timeframe confirmation** — 5-tick and 20-tick momentum must agree.

### Limitations

- **Needs BTC volatility.** In flat markets (range <10 bps over 5 min), no signals fire.
- **Edge may not exist at current thresholds.** 29% win rate suggests the oracle is not detecting real staleness — it's trading on noise.
- **7.2% fee not deducted.** Reported PnL is pre-fee.
- **Single exchange price source.** Uses Binance only. Chainlink (the actual resolution oracle) may differ slightly.

### Best Suited For

- BTC 5-minute markets during **high volatility** (>50 bps range per 5-min window)
- News events, liquidation cascades, large market moves
- NOT suited for flat/quiet markets

### Command

```bash
python main.py --strategy rolling,oracle --dry-run --no-learn
```

---

## Strategy Comparison

| | MM | Mean Rev | Whale | Momentum | Oracle |
|---|---|---|---|---|---|
| **Market type** | Long-dated | Long-dated | Any | 5m rolling | 5m rolling |
| **Execution** | Passive | Aggressive | Aggressive | Aggressive | Aggressive |
| **Dry-run testable** | No | Partially | No | Yes | Yes |
| **Tested trades** | 20 | 0 | 0 (391 signals) | 12 | 7 |
| **Win rate** | 5% | N/A | N/A | 58% | 29% |
| **Live ready** | Needs live test | Needs longer run | Needs live test | Close | Needs tuning |
| **Biggest risk** | Adverse selection | News events | Whale is wrong | Momentum reversal | Noise trading |

## Recommended Next Steps

1. **Live test MM** on a contested long-dated market with $50-100. This is the most proven model — only fails in dry-run due to simulator limitations.
2. **Raise oracle threshold** to 15+ bps and wait for volatile periods. Current 3.5 bps is noise.
3. **Add mid-window exit** to momentum and oracle — if the trade goes >20% against you within 60 seconds, sell and cut losses.
4. **Run mean reversion overnight** on a long-dated market to see if it triggers during quieter periods.
5. **Live test whale copy** — signals are real, just need live execution to fill them.
