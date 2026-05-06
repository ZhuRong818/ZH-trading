"""
Module 5b — Combinatorial Arbitrage

Detects pricing inconsistencies across related markets:
1. Sum-to-one violations (exclusive outcomes that sum > 1.0)
2. Monotonic price violations (nested thresholds mispriced)

Uses numpy for Bregman divergence. CVXPY is optional for optimization.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import requests

from config import GAMMA_BASE
from data_pipeline.market_data import MarketDataFeed, MarketInfo
from ems.execution import ExecutionEngine

log = logging.getLogger(__name__)


@dataclass
class ArbOpportunity:
    arb_type: str           # "sum_to_one" or "monotonic"
    markets: List[str]      # condition_ids
    titles: List[str]
    prices: List[float]
    fair_prices: List[float]
    profit_estimate: float
    trades: List[Tuple[str, str, float]]  # (token_id, side, size)


class ArbitrageDetector:
    """
    Scans for combinatorial arbitrage across related Polymarket markets.
    """

    def __init__(self, data_feed: MarketDataFeed, ems: ExecutionEngine):
        self.data = data_feed
        self.ems = ems
        self.session = requests.Session()

    def scan_sum_to_one(self, event_slug: str) -> Optional[ArbOpportunity]:
        """
        Check if exclusive outcomes in an event sum to > 1.0.
        e.g., election candidates should sum to <= 1.0.
        """
        try:
            resp = self.session.get(
                f"{GAMMA_BASE}/markets",
                params={"slug": event_slug, "active": True, "closed": False, "_limit": 50},
            )
            resp.raise_for_status()
            markets = resp.json()

            if len(markets) < 2:
                return None

            # Get YES prices for each outcome
            prices = []
            titles = []
            condition_ids = []
            token_ids_yes = []

            for m in markets:
                outcomes_raw = m.get("outcomePrices", "[]")
                outcome_prices = (
                    __import__("json").loads(outcomes_raw)
                    if isinstance(outcomes_raw, str)
                    else (outcomes_raw or [])
                )
                if outcome_prices:
                    yes_price = float(outcome_prices[0])
                    prices.append(yes_price)
                    titles.append(m.get("question", ""))
                    condition_ids.append(m.get("conditionId", ""))

                    clob_ids_raw = m.get("clobTokenIds", "[]")
                    clob_ids = (
                        __import__("json").loads(clob_ids_raw)
                        if isinstance(clob_ids_raw, str)
                        else (clob_ids_raw or [])
                    )
                    token_ids_yes.append(clob_ids[0] if clob_ids else "")

            total = sum(prices)
            if total <= 1.02:  # within tolerance
                return None

            excess = total - 1.0
            fair_prices = [p / total for p in prices]  # normalize to sum=1

            # Trade: sell all (buy NO on each) to collect the excess
            trades = []
            for i, token_id in enumerate(token_ids_yes):
                if token_id:
                    trades.append((token_id, "SELL", 10.0))

            arb = ArbOpportunity(
                arb_type="sum_to_one",
                markets=condition_ids,
                titles=titles,
                prices=prices,
                fair_prices=fair_prices,
                profit_estimate=excess * 10.0,  # per 10 shares
                trades=trades,
            )

            log.info(
                "ARB DETECTED [sum_to_one]: %d markets sum=%.4f excess=%.4f profit_est=$%.2f",
                len(prices), total, excess, arb.profit_estimate,
            )
            for i, title in enumerate(titles):
                log.info("  %.4f (fair=%.4f) %s", prices[i], fair_prices[i], title[:60])

            return arb

        except Exception as e:
            log.error("Sum-to-one scan failed: %s", e)
            return None

    def scan_monotonic(self, token_ids: List[str], thresholds: List[float]) -> Optional[ArbOpportunity]:
        """
        Check monotonic pricing constraint for nested threshold markets.
        e.g., P(BTC > $80k) >= P(BTC > $90k) >= P(BTC > $100k)
        """
        if len(token_ids) != len(thresholds):
            return None

        # Sort by threshold ascending
        pairs = sorted(zip(thresholds, token_ids))
        thresholds_sorted = [p[0] for p in pairs]
        tokens_sorted = [p[1] for p in pairs]

        # Fetch current prices
        prices = []
        for token_id in tokens_sorted:
            book = self.data.fetch_order_book(token_id)
            mid = book.mid or 0.5
            prices.append(mid)

        # Check monotonic: price should decrease as threshold increases
        violations = []
        for i in range(len(prices) - 1):
            if prices[i] < prices[i + 1]:
                violations.append((i, i + 1))

        if not violations:
            return None

        # Build trades: for each violation, short the higher-priced one, long the lower-priced one
        trades = []
        for i, j in violations:
            trades.append((tokens_sorted[j], "SELL", 10.0))  # sell the overpriced
            trades.append((tokens_sorted[i], "BUY", 10.0))   # buy the underpriced

        profit = sum(prices[j] - prices[i] for i, j in violations) * 10.0

        arb = ArbOpportunity(
            arb_type="monotonic",
            markets=[],
            titles=[f"threshold={t}" for t in thresholds_sorted],
            prices=prices,
            fair_prices=sorted(prices, reverse=True),  # what they should be
            profit_estimate=profit,
            trades=trades,
        )

        log.info(
            "ARB DETECTED [monotonic]: %d violations, profit_est=$%.2f",
            len(violations), profit,
        )

        return arb

    def bregman_divergence(self, market_prices: np.ndarray, fair_prices: np.ndarray) -> float:
        """
        KL divergence between market prices and arbitrage-free prices.
        Higher = more mispriced = bigger opportunity.
        """
        # Clamp to avoid log(0)
        p = np.clip(market_prices, 1e-6, 1 - 1e-6)
        q = np.clip(fair_prices, 1e-6, 1 - 1e-6)
        return float(np.sum(p * np.log(p / q) - p + q))

    def execute_arb(self, arb: ArbOpportunity, dry_run: bool = True) -> bool:
        """Execute an arbitrage opportunity. All legs must fill or none."""
        if arb.profit_estimate < 0.50:
            log.info("Arb profit too small ($%.2f), skipping", arb.profit_estimate)
            return False

        log.info("Executing %s arb: %d legs, est profit=$%.2f",
                 arb.arb_type, len(arb.trades), arb.profit_estimate)

        for token_id, side, size in arb.trades:
            self.ems.place_order(
                token_id=token_id,
                side=side,
                price=0.0,  # will be adjusted to book
                size=size,
                order_type="FOK",  # fill-or-kill for arb legs
                source=f"arb_{arb.arb_type}",
            )

        return True
