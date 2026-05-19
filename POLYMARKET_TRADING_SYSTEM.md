# Polymarket Institutional Trading System
### A Modular, Open-Source Pipeline for Prediction Market Alpha

> **Status:** Paper-Trade → Live  
> **Strategies:** Market Making · Combinatorial Arbitrage · LLM Sentiment · Whale Tracking  
> **Philosophy:** Build cheap with open source, graduate to paid only when revenue justifies it.

---

## Table of Contents

1. [System Architecture Overview](#1-system-architecture-overview)
2. [Module 1 — Infrastructure & Connectivity](#2-module-1--infrastructure--connectivity)
3. [Module 2 — Data Pipeline & Market Data](#3-module-2--data-pipeline--market-data)
4. [Module 3 — Order Management System (OMS)](#4-module-3--order-management-system-oms)
5. [Module 4 — Execution Management System (EMS) & Smart Order Routing](#5-module-4--execution-management-system-ems--smart-order-routing)
6. [Module 5a — Market Making Strategy (Stoikov Model)](#6-module-5a--market-making-strategy-stoikov-model)
7. [Module 5b — Combinatorial Arbitrage (Bregman + Frank-Wolfe)](#7-module-5b--combinatorial-arbitrage-bregman--frank-wolfe)
8. [Module 5c — LLM Sentiment Pipeline (PolySwarm)](#8-module-5c--llm-sentiment-pipeline-polyswarm)
9. [Module 5d — Whale Tracking & Copy Trading](#9-module-5d--whale-tracking--copy-trading)
10. [Module 6 — Risk Management & Kill Switch](#10-module-6--risk-management--kill-switch)
11. [Module 7 — Backtesting & Paper Trading Engine](#11-module-7--backtesting--paper-trading-engine)
12. [Module 8 — Live Deployment Checklist](#12-module-8--live-deployment-checklist)
13. [Cost Breakdown (Open Source vs Paid)](#13-cost-breakdown-open-source-vs-paid)
14. [Full Stack Summary Table](#14-full-stack-summary-table)
15. [Repo Structure](#15-repo-structure)

---

## 1. System Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        EXTERNAL DATA SOURCES                        │
│   Polymarket CLOB API │ Polygon RPC │ X/Twitter │ News RSS │ Chain  │
└───────────────┬─────────────────────────┬───────────────────────────┘
                │                         │
     ┌──────────▼──────────┐   ┌──────────▼──────────┐
     │   MODULE 2          │   │   MODULE 5c          │
     │   Data Pipeline     │   │   LLM Sentiment      │
     │   (Market Data,     │   │   Pipeline           │
     │    Order Book,      │   │   (FinBERT → GPT-4o  │
     │    On-Chain State)  │   │    → Bayesian Score) │
     └──────────┬──────────┘   └──────────┬──────────┘
                │                         │
     ┌──────────▼─────────────────────────▼──────────┐
     │               MODULE 3: OMS                    │
     │   Position Tracker │ Token Lifecycle Manager   │
     │   Merge/Split Engine │ P&L Calculator          │
     └──────────────────────┬─────────────────────────┘
                            │
     ┌──────────────────────▼─────────────────────────┐
     │               MODULE 4: EMS + SOR              │
     │   Smart Order Routing │ Order Types (FOK/FAK)  │
     │   Rate Limit Manager │ Signing Server (EIP-712)│
     └───┬──────────┬───────────────┬─────────────────┘
         │          │               │
 ┌───────▼──┐ ┌─────▼──────┐ ┌─────▼────────────────┐
 │ MOD 5a   │ │ MOD 5b     │ │ MOD 5d               │
 │ Market   │ │ Combinat.  │ │ Whale Tracker        │
 │ Making   │ │ Arbitrage  │ │ Copy Trading         │
 └───────┬──┘ └─────┬──────┘ └─────┬────────────────┘
         │          │               │
     ┌───▼──────────▼───────────────▼───┐
     │         MODULE 6: RISK ENGINE    │
     │  Stop-Loss │ Kill Switch │ Limits│
     └───────────────────────────────────┘
                       │
     ┌─────────────────▼──────────────────┐
     │  MODULE 7: BACKTEST / PAPER TRADE  │
     │  VectorBT │ Tick-Level Simulation  │
     └────────────────────────────────────┘
```

**Core dual-clock constraint:** Polygon block time ≈ 2 seconds (on-chain settlement) vs millisecond off-chain order updates. Every module must be aware of this split.

---

## 2. Module 1 — Infrastructure & Connectivity

### What it does
Manages the physical and network layer: VPS selection, RPC nodes, wallet security (HSM/Signing Server), and environment bootstrapping.

### Open-Source Repos

| Tool | Repo | Purpose | Cost |
|------|------|---------|------|
| **py-clob-client** | [Polymarket/py-clob-client](https://github.com/Polymarket/py-clob-client) | Official Python client for the CLOB API (REST + WS) | Free |
| **web3.py** | [ethereum/web3.py](https://github.com/ethereum/web3.py) | Polygon RPC interaction, wallet signing, token ops | Free |
| **eth-account** | [ethereum/eth-account](https://github.com/ethereum/eth-account) | EIP-712 structured signing without a full node | Free |
| **Docker + Compose** | [docker/compose](https://github.com/docker/compose) | Containerize all modules for isolated deployment | Free |
| **HashiCorp Vault** | [hashicorp/vault](https://github.com/hashicorp/vault) | Open-source HSM substitute — stores private keys securely, never exposes them to the trading logic | Free (self-hosted) |

### Architecture: Signing Server (Critical)

```
Trading Bot (EMS)
      │  sends: {price, size, side, token_id}
      ▼
 Signing Server  ◄──── Vault (stores private key)
      │  returns: EIP-712 signature
      ▼
 CLOB API (submits signed order)
```

The bot **never** holds the private key. The Signing Server is the only process that touches Vault.

### RPC Node Strategy

```
# Paper trading: use free tiers
Alchemy:     https://polygon-mainnet.g.alchemy.com/v2/{KEY}   (300M compute units/month free)
Ankr:        https://rpc.ankr.com/polygon                     (completely free)

# Live trading: upgrade to dedicated
Chainstack:  ~$50/month for dedicated Polygon node
QuickNode:   ~$50/month
```

### Environment Setup

```bash
# 1. Clone the official client
git clone https://github.com/Polymarket/py-clob-client
cd py-clob-client && pip install -e .

# 2. Install dependencies
pip install web3 eth-account redis psycopg2-binary python-dotenv aiohttp

# 3. Environment variables (never commit these)
cat > .env << EOF
POLYGON_RPC_URL=https://rpc.ankr.com/polygon
CLOB_API_URL=https://clob.polymarket.com
WALLET_ADDRESS=0x...
VAULT_ADDR=http://127.0.0.1:8200
VAULT_TOKEN=...
EOF
```

### Paper Trading VPS vs Live VPS

| Phase | Provider | Spec | Cost |
|-------|----------|------|------|
| Paper | Local machine or free AWS t2.micro | 1 vCPU, 1GB RAM | $0 |
| Live | [QuantVPS](https://quantvps.com) or AWS c5.xlarge near CLOB DC | 4 vCPU, 8GB RAM | ~$80/month |

---

## 3. Module 2 — Data Pipeline & Market Data

### What it does
Collects, normalizes, and stores: (a) CLOB order book snapshots, (b) trade history OHLCV, (c) on-chain position state, (d) external market metadata. Feeds all strategy modules in real-time.

### Open-Source Repos

| Tool | Repo | Purpose | Cost |
|------|------|---------|------|
| **py-clob-client** | [Polymarket/py-clob-client](https://github.com/Polymarket/py-clob-client) | WebSocket streams for order book + trades | Free |
| **gamma-client** | [Polymarket/gamma-client](https://github.com/Polymarket/gamma-markets-py) | Gamma API — market metadata, resolution criteria | Free |
| **TimescaleDB** | [timescale/timescaledb](https://github.com/timescale/timescaledb) | Time-series PostgreSQL extension for OHLCV storage | Free (self-hosted) |
| **Redis** | [redis/redis](https://github.com/redis/redis) | Sub-millisecond in-memory cache for live order book state | Free |
| **Apache Kafka** | [apache/kafka](https://github.com/apache/kafka) | Event streaming bus between all modules | Free |

### Data Flow

```
WebSocket (CLOB)  ──► Kafka Topic: raw_orderbook
REST Poll (CLOB)  ──► Kafka Topic: trades
Polygon RPC       ──► Kafka Topic: on_chain_events
                         │
                  Kafka Consumers
                  ├── Redis (hot state: live book)
                  └── TimescaleDB (cold state: OHLCV history)
```

### Key Data Structures to Maintain

```python
# 1. Order Book (Redis, updated on every WebSocket tick)
{
  "market_id": "0xabc...",
  "token_id_yes": "0x111",
  "token_id_no":  "0x222",
  "bids_yes": [[0.62, 500], [0.61, 1200]],   # [price, size]
  "asks_yes": [[0.63, 300], [0.64, 800]],
  "timestamp_ms": 1716000000000
}

# 2. Position State (TimescaleDB + Redis)
{
  "condition_id": "0xdef...",
  "token_id": "0x111",
  "size": 1000,
  "avg_price": 0.58,
  "cur_price": 0.63,
  "pnl": 50.0,
  "mergeable": False,   # True if equal YES+NO held
  "redeemable": False   # True post-resolution
}
```

### OHLCV Normalization (Critical for Prediction Markets)

Standard OHLCV means nothing without normalization. A price of `$0.50` = 50% probability. Implement this transform before any technical analysis:

```python
def normalize_ohlcv(df):
    """
    Clamp prices to [0.01, 0.99] — prices of 0 or 1 are resolved markets.
    Interpret volume in USDC notional, not raw token count.
    Tag 'regime' based on price zone:
      - [0.01, 0.15] or [0.85, 0.99] → "tail" (resolution certainty zone)
      - [0.35, 0.65] → "contested" (mean-reverting likely)
      - else → "trending"
    """
    df['price'] = df['price'].clip(0.01, 0.99)
    df['notional'] = df['volume'] * df['price']
    df['regime'] = df['price'].apply(classify_regime)
    return df
```

---

## 4. Module 3 — Order Management System (OMS)

### What it does
Single source of truth for all positions. Tracks token lifecycles (active → mergeable → redeemable → closed), computes real P&L, detects merge opportunities, and reconciles on-chain vs off-chain state.

### Open-Source Repos

| Tool | Repo | Purpose | Cost |
|------|------|---------|------|
| **py-clob-client** | [Polymarket/py-clob-client](https://github.com/Polymarket/py-clob-client) | `/positions` and `/trades` endpoints | Free |
| **SQLAlchemy** | [sqlalchemy/sqlalchemy](https://github.com/sqlalchemy/sqlalchemy) | ORM for position persistence | Free |
| **APScheduler** | [agronholm/apscheduler](https://github.com/agronholm/apscheduler) | Periodic reconciliation jobs | Free |

### Core OMS Responsibilities

#### A. Token Lifecycle Manager

```
USDC
 │ split()
 ▼
YES tokens + NO tokens  ◄── trade in/out
 │
 ├── if holdings(YES) == holdings(NO) → mergeable → merge() → USDC
 │
 └── if oracle resolves → redeemable → redeem() → USDC
```

```python
class TokenLifecycleManager:
    def check_mergeable(self, condition_id: str) -> bool:
        yes_pos = self.get_position(condition_id, "YES")
        no_pos  = self.get_position(condition_id, "NO")
        return yes_pos.size == no_pos.size and yes_pos.size > 0

    def merge_position(self, condition_id: str):
        """Collapses equal YES+NO → USDC without waiting for resolution."""
        # Calls CTF merge() on-chain via web3.py
        ...

    def redeem_position(self, condition_id: str):
        """Post-resolution: redeems winning token for USDC."""
        ...
```

#### B. Reconciliation Loop (runs every 30s)

```python
async def reconcile():
    # 1. Pull on-chain balances from Polygon via web3.py
    # 2. Pull off-chain positions from CLOB /positions endpoint
    # 3. Diff the two — alert if divergence > threshold
    # 4. Auto-cancel any orphan orders for resolved markets
```

#### C. P&L Engine

```python
# Uses fields from Polymarket Data API:
# avgPrice, curPrice, initialValue, pnl, redeemable, mergeable

pnl_unrealized = (cur_price - avg_price) * size
pnl_realized   = sum(closed_trade_profits)
capital_locked  = size * avg_price   # USDC collateral at risk
```

---

## 5. Module 4 — Execution Management System (EMS) & Smart Order Routing

### What it does
The competitive edge layer. Receives signals from all strategy modules, applies Smart Order Routing (SOR) logic specific to prediction markets (exploiting YES/NO synthetic equality), manages order types, and enforces rate limits.

### Open-Source Repos

| Tool | Repo | Purpose | Cost |
|------|------|---------|------|
| **py-clob-client** | [Polymarket/py-clob-client](https://github.com/Polymarket/py-clob-client) | Order placement (limit, FOK, FAK, GTD) | Free |
| **Hummingbot** | [hummingbot/hummingbot](https://github.com/hummingbot/hummingbot) | Has a **Polymarket connector** built-in; use as EMS skeleton | Free |

> **Hummingbot is the most important open-source shortcut in this entire stack.** It already handles WebSocket reconnection, rate limiting, order lifecycle management, and has a Polymarket-specific connector. Build your custom strategies on top of it rather than from scratch.

### Smart Order Routing: The Synthetic Equality Exploit

The key insight from the document: **buying YES at $0.60 = selling NO at $0.40 + USDC split**.

```python
class SyntheticEqualitySOR:
    """
    For every desired YES acquisition, evaluate 3 paths:
    1. Direct:    Buy YES directly from the ask side
    2. Synthetic: Buy NO from ask side → split USDC → get YES
    3. LP path:   Post NO bid → get filled → merge for YES (passive)
    """

    def route_yes_buy(self, market, target_size_usdc):
        path1_cost = self.cost_direct_yes(market, target_size_usdc)
        path2_cost = self.cost_synthetic_yes(market, target_size_usdc)

        if path2_cost < path1_cost:
            return self.execute_synthetic(market, target_size_usdc)
        else:
            return self.execute_direct(market, target_size_usdc)

    def cost_synthetic_yes(self, market, size):
        # cost of buying NO + gas for split tx
        no_ask   = market.asks_no[0][0]
        gas_cost = self.estimate_polygon_gas()
        return (no_ask * size) + gas_cost
```

### Order Types Reference

| Order Type | When to Use | py-clob-client call |
|-----------|------------|-------------------|
| **Limit** | Standard market making | `create_order(price, size, side)` |
| **FOK** (Fill or Kill) | Arbitrage — must fill completely or cancel | `create_order(..., time_in_force="FOK")` |
| **FAK** (Fill and Kill) | Large orders in deep books — partial fill OK | `create_order(..., time_in_force="FAK")` |
| **GTD** (Good Till Date) | Hold position until market resolution date | `create_order(..., expiration=timestamp)` |

### Rate Limit Manager

```python
class RateLimitManager:
    """
    Polymarket CLOB enforces limits on order creations + cancellations per second.
    A naive bot that re-quotes on every tick will get throttled.
    Only update a quote if EV(price_change) > cost_of_update.
    """
    MIN_PRICE_DELTA_TO_REQUOTE = 0.003   # 0.3 cents
    MAX_ORDERS_PER_SECOND      = 10

    def should_requote(self, old_price, new_price):
        return abs(new_price - old_price) >= self.MIN_PRICE_DELTA_TO_REQUOTE
```

---

## 6. Module 5a — Market Making Strategy (Stoikov Model)

### What it does
Posts continuous bid/ask quotes around the market midpoint, collecting the spread as profit. Uses the Avellaneda-Stoikov model to skew quotes based on current inventory, volatility, and time-to-maturity, protecting against holding excess YES or NO shares.

### Open-Source Repos

| Tool | Repo | Purpose | Cost |
|------|------|---------|------|
| **Hummingbot** | [hummingbot/hummingbot](https://github.com/hummingbot/hummingbot) | Stoikov market making strategy built-in; Polymarket connector | Free |
| **market-maker-keeper** | [makerdao/market-maker-keeper](https://github.com/makerdao/market-maker-keeper) | Reference implementation of bands-based market making | Free |

### Stoikov Reservation Price Formula

```
P_r = P_mid - (q × γ × σ² × T)

Where:
  P_mid = midpoint price (avg of best bid and ask)
  q     = current inventory imbalance (positive = long YES, negative = short YES)
  γ     = risk aversion parameter (tune between 0.1 and 1.0)
  σ     = price volatility (rolling 1h standard deviation)
  T     = time to market resolution in hours
```

### Implementation Sketch

```python
class StoikovMarketMaker:
    gamma = 0.5      # risk aversion — tune this per market
    spread_k = 1.5   # spread scaling factor

    def reservation_price(self, mid, inventory, sigma, T):
        return mid - (inventory * self.gamma * sigma**2 * T)

    def optimal_spread(self, sigma, T):
        return self.gamma * sigma**2 * T + (2/self.gamma) * math.log(1 + self.gamma/self.spread_k)

    def compute_quotes(self, market):
        mid       = market.midprice()
        inventory = self.oms.net_position(market.id)  # +/- USDC notional
        sigma     = market.rolling_volatility(window="1h")
        T         = market.hours_to_resolution()

        r         = self.reservation_price(mid, inventory, sigma, T)
        half_s    = self.optimal_spread(sigma, T) / 2

        bid = r - half_s
        ask = r + half_s
        return bid, ask

    def skew_check(self, inventory, threshold=500):
        """If inventory imbalance > $500 USDC, skew quotes aggressively to reduce risk."""
        if abs(inventory) > threshold:
            self.gamma *= 1.5   # increase risk aversion temporarily
```

### Anti-Toxic-Flow: Adjusted Midpoint Logic

```python
def adjusted_midpoint(order_book, min_incentive_size=50):
    """
    Filter out bait orders from other bots trying to pin a fake price.
    Only use orders >= min_incentive_size USDC when computing the mid.
    """
    filtered_bids = [b for b in order_book.bids if b[1] >= min_incentive_size]
    filtered_asks = [a for a in order_book.asks if a[1] >= min_incentive_size]
    return (filtered_bids[0][0] + filtered_asks[0][0]) / 2
```

---

## 7. Module 5b — Combinatorial Arbitrage (Bregman + Frank-Wolfe)

### What it does
Detects pricing inconsistencies across related markets (e.g., Bitcoin hitting $80k, $90k, $100k must be monotonically priced). Extracts risk-free profit using Bregman Divergence to measure mispricing and Frank-Wolfe optimization to find the minimum-cost trade set.

### Open-Source Repos

| Tool | Repo | Purpose | Cost |
|------|------|---------|------|
| **CVXPY** | [cvxpy/cvxpy](https://github.com/cvxpy/cvxpy) | Convex optimization — implements Frank-Wolfe for finding optimal arbitrage trades | Free |
| **NumPy / SciPy** | [numpy/numpy](https://github.com/numpy/numpy) | Bregman divergence calculations, matrix operations | Free |
| **NetworkX** | [networkx/networkx](https://github.com/networkx/networkx) | Model market relationships as a graph for tournament arbitrage | Free |

### Types of Combinatorial Arbitrage on Polymarket

#### Type 1: Monotonic Price Constraint
```
Example: 3 Bitcoin markets
  "BTC hits $80k"  → priced at 0.70
  "BTC hits $90k"  → priced at 0.75  ← VIOLATION (must be ≤ 0.70)
  "BTC hits $100k" → priced at 0.40

Action: Short "BTC hits $90k" (buy NO), long "BTC hits $80k" (buy YES)
```

#### Type 2: Sum-to-One Constraint
```
Example: US Election winner (must sum to ≤ 1.00)
  Candidate A: 0.55
  Candidate B: 0.48
  Candidate C: 0.10
  Total:        1.13  ← OVER-PRICED by 0.13

Action: Sell all three (buy NO on each), collect the $0.13 excess.
```

### Implementation

```python
import cvxpy as cp
import numpy as np

class BregmanArbitrageDetector:

    def compute_bregman_divergence(self, market_prices: np.array, fair_prices: np.array):
        """
        Measures 'information distance' between current prices and arbitrage-free vector.
        KL divergence is the standard choice for probability distributions.
        """
        return np.sum(market_prices * np.log(market_prices / fair_prices) - market_prices + fair_prices)

    def find_arbitrage_trades(self, related_markets: list):
        """
        Frank-Wolfe: iteratively find the min-cost set of trades to restore
        arbitrage-free pricing. Typically converges in 50-150 iterations.
        """
        n = len(related_markets)
        prices = np.array([m.midprice() for m in related_markets])

        # Decision variables: how much to buy (+) or sell (-) in each market
        x = cp.Variable(n)

        # Objective: maximize profit (negative = minimize cost)
        profit = -prices @ x

        # Constraints
        constraints = [
            cp.sum(x) == 0,                    # dollar-neutral
            x >= -1000,                         # max short per market
            x <=  1000,                         # max long per market
        ]

        # Add monotonicity constraint if applicable
        for i in range(n - 1):
            constraints.append(prices[i] >= prices[i+1])

        prob = cp.Problem(cp.Minimize(profit), constraints)
        prob.solve(solver=cp.SCS, max_iters=150)

        return x.value if prob.status == "optimal" else None
```

### Execution: FOK Orders for Simultaneous Multi-Leg

```python
def execute_arbitrage(trade_vector, markets, ems):
    """
    All legs must fill simultaneously (FOK) or not at all.
    Otherwise you have a partial arb with directional risk.
    """
    orders = []
    for i, trade_size in enumerate(trade_vector):
        if trade_size > 0:
            orders.append(ems.create_order(markets[i], "buy", abs(trade_size), time_in_force="FOK"))
        elif trade_size < 0:
            orders.append(ems.create_order(markets[i], "sell", abs(trade_size), time_in_force="FOK"))

    # Submit all simultaneously
    return ems.batch_submit(orders)
```

---

## 8. Module 5c — LLM Sentiment Pipeline (PolySwarm)

### What it does
Multi-agent LLM framework that ingests millions of news/social events per day, filters to what's relevant to open markets, and outputs a calibrated probability score that can front-run market price movements.

### Open-Source Repos

| Tool | Repo | Purpose | Cost |
|------|------|---------|------|
| **FinBERT** | [ProsusAI/finbert](https://github.com/ProsusAI/finbert) | Stage 1: High-speed financial sentiment classifier (runs locally, free) | Free |
| **VADER** | [cjhutto/vaderSentiment](https://github.com/cjhutto/vaderSentiment) | Stage 1 fallback: rule-based sentiment (zero GPU needed) | Free |
| **CrewAI** | [crewAIInc/crewAI](https://github.com/crewAIInc/crewAI) | Multi-agent framework for PolySwarm personas | Free (framework) |
| **LangChain** | [langchain-ai/langchain](https://github.com/langchain-ai/langchain) | LLM orchestration, prompt chaining, tool use | Free (framework) |
| **Scrapy** | [scrapy/scrapy](https://github.com/scrapy/scrapy) | News/RSS scraping at scale | Free |
| **Tweepy** | [tweepy/tweepy](https://github.com/tweepy/tweepy) | X/Twitter API client for social signal ingestion | Free (Basic API tier) |
| **Haystack** | [deepset-ai/haystack](https://github.com/deepset-ai/haystack) | RAG pipeline to give LLMs context about market resolution criteria | Free |

### Pipeline Architecture (4 Stages)

```
Stage 1: INGESTION
  Sources: X API, RSS (Reuters/AP/Bloomberg RSS), Reddit API, Telegram
  Volume:  10M+ events/day
  Tool:    Scrapy + Tweepy + asyncio
  Output:  Kafka topic: raw_events

Stage 2: SCREENING (95% noise reduction)
  Tool:    FinBERT (local GPU) or VADER (CPU fallback)
  Filter:  Keep only events with |sentiment_score| > 0.6
           AND keyword match against active market titles
  Output:  Kafka topic: screened_events (~500k/day)

Stage 3: VALIDATION (Causality Check — 0.037% selection rate)
  Tool:    GPT-4o or Claude via API (only ~185 calls/day at this rate)
  Prompt:  "Given this market resolution criteria: {criteria}
            Does this news event causally affect the outcome? 
            Score relevance 0.0-1.0 and explain."
  Filter:  Keep relevance_score > 0.7
  Output:  Kafka topic: validated_signals (~3,700/day)

Stage 4: SCORING (Bayesian Ensemble)
  Tool:    CrewAI with 50+ LLM Personas
  Personas: Geopolitical Analyst, Polling Expert, Macroeconomist, 
            Legal Expert, Tech Policy Analyst, etc.
  Output:  Consensus probability with confidence interval
```

### CrewAI Persona Setup

```python
from crewai import Agent, Task, Crew

geopolitical_analyst = Agent(
    role="Geopolitical Risk Analyst",
    goal="Assess how this news event shifts the probability of the market outcome",
    backstory="Expert in international relations, political risk, and event forecasting",
    llm="gpt-4o",
    tools=[haystack_retrieval_tool]   # fetch market resolution criteria
)

polling_expert = Agent(
    role="Electoral Polling Expert",
    goal="Evaluate polling methodology and state-level implications",
    backstory="Expert in survey design, demographic weighting, and electoral modeling",
    llm="gpt-4o"
)

# ... define 48 more personas ...

scoring_task = Task(
    description="""
    Market: {market_title}
    Resolution criteria: {criteria}
    News event: {event_text}
    
    Provide: probability_estimate (0.0-1.0), confidence (0.0-1.0), reasoning
    """,
    expected_output="JSON: {probability, confidence, reasoning}",
    agents=[geopolitical_analyst, polling_expert, ...]
)

crew = Crew(agents=[...], tasks=[scoring_task], process="hierarchical")
```

### Bayesian Ensemble (Confidence-Weighted)

```python
def bayesian_ensemble(persona_outputs: list[dict]) -> dict:
    """
    Aggregate persona probability estimates, weighted by confidence.
    Suppresses idiosyncratic errors across the swarm.
    """
    probs       = np.array([p['probability'] for p in persona_outputs])
    confidences = np.array([p['confidence']  for p in persona_outputs])
    weights     = confidences / confidences.sum()

    consensus_prob = np.dot(weights, probs)
    uncertainty    = np.sqrt(np.dot(weights, (probs - consensus_prob)**2))

    return {
        "consensus_probability": consensus_prob,
        "confidence_interval": (consensus_prob - uncertainty, consensus_prob + uncertainty),
        "signal_strength": 1 - uncertainty   # high = high conviction
    }
```

### Signal → Trade Trigger

```python
def evaluate_signal(signal, market):
    market_price   = market.midprice()
    model_prob     = signal['consensus_probability']
    edge           = abs(model_prob - market_price)
    min_edge       = 0.04   # only trade if model disagrees by 4+ cents

    if edge > min_edge and signal['signal_strength'] > 0.6:
        direction = "buy_yes" if model_prob > market_price else "buy_no"
        size_usdc = kelly_criterion(edge, signal['signal_strength'])
        return TradingSignal(direction=direction, size=size_usdc, source="llm_sentiment")
    return None
```

---

## 9. Module 5d — Whale Tracking & Copy Trading

### What it does
Monitors on-chain transactions in real-time to detect when high-win-rate wallets make large conviction moves. Uses these as either direct copy-trade signals or as triggers to run a deeper sentiment scan on the relevant market.

### Open-Source Repos

| Tool | Repo | Purpose | Cost |
|------|------|---------|------|
| **web3.py** | [ethereum/web3.py](https://github.com/ethereum/web3.py) | Subscribe to Polygon logs for CTF Transfer events | Free |
| **Dune Analytics** | [duneanalytics/dune-client](https://github.com/duneanalytics/dune-client) | Query historical on-chain trade data to build whale scorecard | Free tier (25 queries/day) |
| **SQLite / PostgreSQL** | — | Whale wallet registry with historical win rates | Free |

### Whale Registry: Building the Scorecard

```python
# Schema: whale_wallets table
{
  "wallet":       "0xabc...",
  "total_trades": 147,
  "win_rate":     0.91,
  "avg_edge":     0.12,          # average edge vs market at time of trade
  "domains":      ["tech_policy", "elections", "crypto"],
  "last_active":  "2025-05-01",
  "trust_score":  0.85           # composite score
}
```

Build the initial registry using Dune Analytics SQL:
```sql
-- Query Polymarket CTF contract for large trades from repeat winners
SELECT 
    maker_address AS wallet,
    COUNT(*) AS total_trades,
    SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS win_rate,
    MAX(trade_size_usdc) AS max_trade_size
FROM polymarket_trades
WHERE trade_size_usdc > 1000
GROUP BY maker_address
HAVING total_trades > 20 AND win_rate > 0.70
ORDER BY win_rate DESC
```

### Real-Time On-Chain Monitor

```python
from web3 import Web3

class WhaleMonitor:
    WHALE_THRESHOLD_USDC = 10_000   # only alert on $10k+ moves
    HIGH_CONFIDENCE_WIN_RATE = 0.80

    def __init__(self, rpc_url, whale_registry, kafka_producer):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.registry = whale_registry
        self.kafka = kafka_producer

    async def monitor_ctf_transfers(self):
        """Subscribe to Polygon logs for large CTF token transfers."""
        ctf_contract = self.w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
        event_filter = ctf_contract.events.TransferSingle.create_filter(fromBlock='latest')

        while True:
            for event in event_filter.get_new_entries():
                await self.process_transfer(event)
            await asyncio.sleep(2)   # Polygon block time

    async def process_transfer(self, event):
        wallet = event['args']['from']
        token_id = event['args']['id']
        amount = event['args']['value']

        whale = self.registry.lookup(wallet)
        if whale and whale.trust_score > 0.75:
            usdc_value = self.estimate_usdc_value(token_id, amount)
            if usdc_value >= self.WHALE_THRESHOLD_USDC:
                signal = WhaleSignal(
                    wallet=wallet,
                    token_id=token_id,
                    size_usdc=usdc_value,
                    win_rate=whale.win_rate,
                    direction=self.infer_direction(token_id)
                )
                self.kafka.send("whale_signals", signal.to_dict())
```

### Copy Trade Decision Logic

```python
def evaluate_whale_signal(signal: WhaleSignal, market) -> TradingSignal | None:
    # Conservative: only copy if whale is highly trusted and market isn't overextended
    if signal.win_rate < 0.80:
        return None
    if market.midprice() > 0.90 or market.midprice() < 0.10:
        return None   # too close to resolution, no edge left

    # Size: copy at 10-20% of whale's position to limit exposure
    copy_size = min(signal.size_usdc * 0.15, MAX_COPY_TRADE_SIZE)

    # Optional: trigger LLM validation before copying
    if REQUIRE_LLM_CONFIRMATION:
        llm_signal = llm_pipeline.score_market(market)
        if not llm_signal or llm_signal.direction != signal.direction:
            return None

    return TradingSignal(
        direction=signal.direction,
        size=copy_size,
        source="whale_copy",
        metadata={"whale_wallet": signal.wallet, "whale_win_rate": signal.win_rate}
    )
```

---

## 10. Module 6 — Risk Management & Kill Switch

### What it does
Enforces hard limits at strategy, market, and portfolio level. Provides automatic stop-losses, a kill switch, circuit breakers against toxic flow, and manipulation detection.

### Open-Source Repos

| Tool | Repo | Purpose | Cost |
|------|------|---------|------|
| **Prometheus** | [prometheus/prometheus](https://github.com/prometheus/prometheus) | Real-time metrics collection for all risk thresholds | Free |
| **Grafana** | [grafana/grafana](https://github.com/grafana/grafana) | Risk dashboard: P&L, drawdown, exposure per strategy | Free |
| **Alertmanager** | [prometheus/alertmanager](https://github.com/prometheus/alertmanager) | Telegram/Slack alerts when thresholds are breached | Free |

### Risk Limits Configuration

```yaml
# risk_config.yaml

portfolio:
  max_total_exposure_usdc:  1_000_000    # total capital at risk
  max_drawdown_pct:         20           # portfolio shutdown if exceeded
  
per_market:
  max_position_size_usdc:   10_000       # single trade max
  max_concentration_pct:    5            # max % of portfolio in one market
  stop_loss_pct:            15           # auto-exit if position down 15%

per_strategy:
  market_making:
    max_inventory_imbalance_usdc: 5_000  # Stoikov skew kicks in above this
  arbitrage:
    max_leg_exposure_usdc:        20_000
  sentiment:
    max_signal_size_usdc:         5_000
  whale_copy:
    max_copy_size_usdc:           2_000
    
circuit_breakers:
  volatility_pause_threshold: 0.15      # pause quoting if σ > 15% in 5 min
  min_incentive_size_usdc:    50        # filter bait orders below this
```

### Kill Switch Implementation

```python
class KillSwitch:
    """
    Triggered manually (hot-key) or automatically (drawdown threshold).
    Cancels ALL open orders via cancelAll() and closes positions aggressively.
    """
    async def activate(self, reason: str):
        logger.critical(f"KILL SWITCH ACTIVATED: {reason}")

        # 1. Cancel all open orders on CLOB immediately
        await self.clob_client.cancel_all_orders()

        # 2. Close all open positions at market (FOK, cross the spread)
        for position in self.oms.get_all_open_positions():
            await self.ems.market_close(position, time_in_force="FAK")

        # 3. Alert team
        await self.notifier.send_urgent(f"Kill switch fired: {reason}")

        # 4. Lock system — require manual restart
        self.system_state.set("HALTED")
```

### Circuit Breaker

```python
class CircuitBreaker:
    def check_volatility(self, market, window_seconds=300):
        recent_prices = self.data.get_prices(market.id, window_seconds)
        sigma = np.std(recent_prices)
        if sigma > self.config.volatility_pause_threshold:
            self.ems.pause_quoting(market.id, duration_seconds=60)
            logger.warning(f"Circuit breaker triggered on {market.id}: σ={sigma:.3f}")
```

---

## 11. Module 7 — Backtesting & Paper Trading Engine

### What it does
Validates all strategies on historical data before risking capital. Uses tick-level order book simulation with realistic trading costs (spread, slippage, Polygon gas). Walk-forward validated to prevent overfitting.

### Open-Source Repos

| Tool | Repo | Purpose | Cost |
|------|------|---------|------|
| **VectorBT** | [polakowo/vectorbt](https://github.com/polakowo/vectorbt) | Fast vectorized backtesting engine | Free |
| **Backtrader** | [mementum/backtrader](https://github.com/mementum/backtrader) | Event-driven backtesting (better for complex order logic) | Free |
| **Nautilus Trader** | [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) | High-performance C-core backtester with HFT-grade tick simulation | Free |
| **pandas** | — | OHLCV data manipulation | Free |

> **Recommendation:** Use **Nautilus Trader** for market making (needs tick-level simulation) and **VectorBT** for arbitrage/sentiment (speed matters more than tick precision).

### Paper Trading Mode

```python
class PaperTradingEMS:
    """
    Identical interface to the live EMS.
    Routes orders to a simulated order book instead of the real CLOB.
    Swap this for LiveEMS when going live — no other code changes needed.
    """

    def __init__(self, historical_data):
        self.book = SimulatedOrderBook(historical_data)
        self.fills = []
        self.pnl = 0

    async def create_order(self, market, side, size, price, time_in_force):
        # Simulate realistic fill with slippage model
        fill_price = self.book.simulate_fill(side, size, price)
        gas_cost   = self.estimate_gas()
        spread_cost = abs(fill_price - price) * size

        self.pnl -= (gas_cost + spread_cost)
        self.fills.append(Fill(side, size, fill_price))
        return SimulatedOrderID()
```

### Realistic Cost Simulation

```python
def simulate_trading_costs(order_size_usdc, market_midprice, volatility):
    """
    Never assume midpoint fills. Use tick-level data.
    """
    half_spread   = 0.003                           # ~0.3 cents typical spread
    slippage      = volatility * 0.1 * order_size_usdc / 10_000   # size-dependent
    polygon_gas   = 0.02                            # ~$0.02 per tx on Polygon
    total_cost    = (half_spread * order_size_usdc) + slippage + polygon_gas
    return total_cost
```

### Walk-Forward Validation Setup

```
Training Window:  2022 US Midterms data (Oct-Nov 2022)
  └── Optimize: γ (risk aversion), spread_k, signal thresholds
  
Test Window 1:    2023 markets (out-of-sample)
  └── Validate: does alpha persist?

Test Window 2:    2024 US Primaries + Presidential Election
  └── Final validation before live deployment
```

### Target KPIs (from the document)

| KPI | Target | What it Means |
|-----|--------|--------------|
| Annualized Alpha | > 20% | Consistent informational edge |
| Max Drawdown | < 15% | Effective capital protection |
| t-statistic | > 3.0 | Signal is real, not noise |
| Execution Latency | < 100ms | Competitive in HFT context |
| Sharpe Ratio | > 1.0 | Risk-adjusted return is positive |
| Sortino Ratio | > 2.0 | Downside risk is controlled |

---

## 12. Module 8 — Live Deployment Checklist

Do **not** go live until every item below is checked:

```
INFRASTRUCTURE
[ ] Signing Server deployed, tested with testnet wallet
[ ] HashiCorp Vault in production mode (not dev mode)
[ ] Dedicated Polygon RPC node provisioned (not free tier)
[ ] VPS in low-latency proximity to CLOB matching engine
[ ] All secrets in .env and Vault — nothing hardcoded
[ ] Docker Compose tested on target VPS

WALLET & AUTH
[ ] EIP-712 signing tested — no L2_AUTH_NOT_AVAILABLE errors
[ ] Wallet type confirmed (MetaMask=0, Magic=1, Gnosis=2) — correct header sent
[ ] USDC allowance approved on CTF contract
[ ] Multi-sig (Gnosis Safe) configured if holding > $100k

OMS & RECONCILIATION
[ ] Reconciliation loop tested — correctly diffs on-chain vs off-chain state
[ ] Position merging tested — confirmed USDC recovery works on testnet
[ ] Redeemable position auto-claim tested

PAPER TRADE GATE (minimum 30 days)
[ ] All 4 strategies paper-traded for 30+ days
[ ] Sharpe > 1.0 and Max Drawdown < 15% in paper results
[ ] Walk-forward validation passed on 2024 election data
[ ] t-statistic > 3.0 for each strategy independently

RISK MANAGEMENT
[ ] Kill switch tested — confirmed cancelAll() fires correctly
[ ] All risk_config.yaml limits reviewed and confirmed
[ ] Alertmanager connected to team Telegram/Slack
[ ] Grafana dashboard live and displaying all KPIs
[ ] Circuit breaker tested with synthetic volatility spike

GO-LIVE SEQUENCE
[ ] Start with 5% of intended capital for first 2 weeks
[ ] Enable only ONE strategy at a time (suggest: whale tracking first)
[ ] Scale up strategies one by one after 2-week stable period
[ ] Reach full capital allocation only after 6 weeks of stable live trading
```

---

## 13. Cost Breakdown (Open Source vs Paid)

### Paper Trading Phase: ~$0–$30/month

| Item | Tool | Cost |
|------|------|------|
| Compute | Local machine or AWS t2.micro | $0 |
| RPC | Alchemy free tier / Ankr | $0 |
| Database | TimescaleDB (self-hosted) | $0 |
| LLM API (validation stage only ~185 calls/day) | GPT-4o mini | ~$5–10/month |
| X API (Basic tier) | Twitter/X | $100/month (biggest cost) |
| Everything else | Open source | $0 |

### Live Trading Phase: ~$300–$500/month

| Item | Tool | Cost |
|------|------|------|
| VPS (low-latency) | QuantVPS or AWS c5.xlarge | ~$80–150/month |
| Dedicated Polygon RPC | Chainstack or QuickNode | ~$50/month |
| LLM API (Validation + Scoring) | GPT-4o | ~$50–100/month |
| X API (Basic) | Twitter/X | $100/month |
| Monitoring | Grafana Cloud free tier | $0 |
| Everything else | Open source | $0 |

> **Total estimated live cost: ~$280–400/month.** A system generating $5k+/month covers this easily.

---

## 14. Full Stack Summary Table

| Module | Component | Open-Source Tool | Repo |
|--------|-----------|-----------------|------|
| Infrastructure | CLOB Client | py-clob-client | [Polymarket/py-clob-client](https://github.com/Polymarket/py-clob-client) |
| Infrastructure | Blockchain | web3.py | [ethereum/web3.py](https://github.com/ethereum/web3.py) |
| Infrastructure | Key Security | HashiCorp Vault | [hashicorp/vault](https://github.com/hashicorp/vault) |
| Infrastructure | Containerization | Docker Compose | [docker/compose](https://github.com/docker/compose) |
| Data Pipeline | Time-series DB | TimescaleDB | [timescale/timescaledb](https://github.com/timescale/timescaledb) |
| Data Pipeline | Cache / State | Redis | [redis/redis](https://github.com/redis/redis) |
| Data Pipeline | Event Bus | Apache Kafka | [apache/kafka](https://github.com/apache/kafka) |
| OMS | Framework | py-clob-client + SQLAlchemy | — |
| EMS + SOR | Framework | Hummingbot | [hummingbot/hummingbot](https://github.com/hummingbot/hummingbot) |
| Market Making | Strategy | Hummingbot (Stoikov built-in) | [hummingbot/hummingbot](https://github.com/hummingbot/hummingbot) |
| Arbitrage | Optimization | CVXPY | [cvxpy/cvxpy](https://github.com/cvxpy/cvxpy) |
| Arbitrage | Graph modelling | NetworkX | [networkx/networkx](https://github.com/networkx/networkx) |
| LLM Sentiment | Screening | FinBERT | [ProsusAI/finbert](https://github.com/ProsusAI/finbert) |
| LLM Sentiment | Agents | CrewAI | [crewAIInc/crewAI](https://github.com/crewAIInc/crewAI) |
| LLM Sentiment | Orchestration | LangChain | [langchain-ai/langchain](https://github.com/langchain-ai/langchain) |
| LLM Sentiment | Scraping | Scrapy | [scrapy/scrapy](https://github.com/scrapy/scrapy) |
| LLM Sentiment | RAG | Haystack | [deepset-ai/haystack](https://github.com/deepset-ai/haystack) |
| Whale Tracking | On-chain | web3.py | [ethereum/web3.py](https://github.com/ethereum/web3.py) |
| Whale Tracking | Historical | Dune Analytics Client | [duneanalytics/dune-client](https://github.com/duneanalytics/dune-client) |
| Risk Management | Metrics | Prometheus | [prometheus/prometheus](https://github.com/prometheus/prometheus) |
| Risk Management | Dashboard | Grafana | [grafana/grafana](https://github.com/grafana/grafana) |
| Risk Management | Alerts | Alertmanager | [prometheus/alertmanager](https://github.com/prometheus/alertmanager) |
| Backtesting | HFT-grade | Nautilus Trader | [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) |
| Backtesting | Vectorized | VectorBT | [polakowo/vectorbt](https://github.com/polakowo/vectorbt) |

---

## 15. Repo Structure

```
polymarket-trading-system/
│
├── infra/
│   ├── docker-compose.yml          # Spins up all services
│   ├── vault/                      # Vault config (key storage)
│   └── signing_server/             # EIP-712 signing service
│
├── data_pipeline/
│   ├── collectors/
│   │   ├── clob_collector.py       # WebSocket order book
│   │   ├── onchain_collector.py    # Polygon RPC events
│   │   └── news_scraper.py         # Scrapy spiders
│   ├── kafka_topics.py
│   └── timescale_schema.sql
│
├── oms/
│   ├── position_manager.py         # Position CRUD
│   ├── token_lifecycle.py          # Split / Merge / Redeem
│   ├── reconciliation.py           # On-chain vs off-chain diff
│   └── pnl_engine.py
│
├── ems/
│   ├── sor.py                      # Synthetic Equality routing
│   ├── order_types.py              # FOK / FAK / GTD wrappers
│   ├── rate_limit_manager.py
│   └── paper_trading_ems.py        # Swap for live EMS at go-live
│
├── strategies/
│   ├── market_making/
│   │   ├── stoikov_model.py
│   │   └── adjusted_midpoint.py
│   ├── arbitrage/
│   │   ├── bregman_detector.py
│   │   └── frank_wolfe_optimizer.py
│   ├── sentiment/
│   │   ├── ingestion.py
│   │   ├── finbert_screener.py
│   │   ├── crewai_personas.py
│   │   └── bayesian_ensemble.py
│   └── whale_tracking/
│       ├── whale_monitor.py
│       ├── whale_registry.py
│       └── copy_trade_logic.py
│
├── risk/
│   ├── risk_engine.py              # Limit enforcement
│   ├── kill_switch.py
│   ├── circuit_breaker.py
│   └── risk_config.yaml
│
├── backtest/
│   ├── nautilus_config.py          # Market making backtest
│   ├── vectorbt_runner.py          # Arb / sentiment backtest
│   ├── cost_simulator.py           # Realistic cost model
│   └── walk_forward.py
│
├── monitoring/
│   ├── prometheus.yml
│   └── grafana_dashboard.json
│
├── .env.example                    # Template (never commit .env)
├── requirements.txt
└── README.md                       # This file
```

---

*Built to paper-trade first. Graduate to live only after the paper KPIs are met. Every module is independently testable — develop them in isolation and integrate via Kafka.*

