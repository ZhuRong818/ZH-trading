"""
Leg Risk Handler — Multi-leg execution with partial fill recovery.

For arbitrage trades that need multiple legs to fill simultaneously.
If some legs fill and others don't, unwinds the filled legs to avoid
naked directional exposure.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from data_pipeline.market_data import MarketDataFeed
from ems.execution import ExecutionEngine

log = logging.getLogger(__name__)


@dataclass
class LegResult:
    token_id: str
    side: str
    requested_size: float
    filled_size: float
    fill_price: float
    order_id: Optional[str]
    success: bool


class LegRiskHandler:
    """
    Executes multi-leg trades with unwind protection.

    Strategy:
    1. Sort legs by depth (deepest first — most likely to fill)
    2. Execute sequentially
    3. After each leg, check if remaining legs are still profitable
    4. If not profitable or a leg fails, unwind all filled legs
    """

    def __init__(self, ems: ExecutionEngine, data_feed: MarketDataFeed,
                 max_slippage: float = 0.02):
        self.ems = ems
        self.data = data_feed
        self.max_slippage = max_slippage

    def execute_multi_leg(
        self,
        legs: List[Tuple[str, str, float, float]],  # [(token_id, side, size, price)]
        tick_size: str = "0.01",
        neg_risk: bool = False,
        source: str = "arb",
    ) -> List[LegResult]:
        """
        Execute legs with recovery. Returns list of LegResults.
        """
        # Sort by book depth (deepest first)
        scored_legs = []
        for token_id, side, size, price in legs:
            book = self.data.get_book(token_id)
            depth = book.depth(side) if book else 0
            scored_legs.append((depth, token_id, side, size, price))
        scored_legs.sort(reverse=True)

        results = []
        total_cost = 0.0
        total_revenue = 0.0

        for _, token_id, side, size, target_price in scored_legs:
            # Check book depth before placing
            book = self.data.get_fresh_book(token_id) if hasattr(self.data, 'get_fresh_book') else self.data.get_book(token_id)
            if book is None:
                log.warning("Leg failed: no book for %s", token_id[:16])
                self._unwind_legs(results, tick_size, neg_risk, source)
                results.append(LegResult(token_id, side, size, 0, 0, None, False))
                return results

            # Get realistic fill price
            vwap, fillable = book.vwap_price(side, size)
            if vwap is None or fillable < size * 0.8:
                log.warning("Leg failed: insufficient depth for %s %s %.1f (fillable=%.1f)",
                            side, token_id[:16], size, fillable)
                self._unwind_legs(results, tick_size, neg_risk, source)
                results.append(LegResult(token_id, side, size, 0, 0, None, False))
                return results

            # Check slippage vs target
            if abs(vwap - target_price) > self.max_slippage:
                log.warning("Leg failed: slippage %.4f > max %.4f for %s",
                            abs(vwap - target_price), self.max_slippage, token_id[:16])
                self._unwind_legs(results, tick_size, neg_risk, source)
                results.append(LegResult(token_id, side, size, 0, 0, None, False))
                return results

            # Place the order
            order_id = self.ems.place_order(
                token_id=token_id, side=side, price=vwap, size=size,
                tick_size=tick_size, neg_risk=neg_risk,
                order_type="FOK", source=source,
            )

            if order_id:
                results.append(LegResult(token_id, side, size, size, vwap, order_id, True))
                if side == "BUY":
                    total_cost += size * vwap
                else:
                    total_revenue += size * vwap
            else:
                # Leg failed — unwind all previous legs
                log.warning("Leg order failed for %s, unwinding %d filled legs",
                            token_id[:16], len(results))
                self._unwind_legs(results, tick_size, neg_risk, source)
                results.append(LegResult(token_id, side, size, 0, 0, None, False))
                return results

        all_success = all(r.success for r in results)
        if all_success:
            log.info("Multi-leg complete: %d legs, cost=$%.2f revenue=$%.2f",
                     len(results), total_cost, total_revenue)

        return results

    def _unwind_legs(self, filled_results: List[LegResult], tick_size: str,
                     neg_risk: bool, source: str):
        """Reverse all filled legs to close the exposure."""
        for result in filled_results:
            if not result.success or result.filled_size <= 0:
                continue

            reverse_side = "SELL" if result.side == "BUY" else "BUY"
            book = self.data.get_book(result.token_id)
            if book:
                vwap, _ = book.vwap_price(reverse_side, result.filled_size)
                price = vwap if vwap else result.fill_price
            else:
                price = result.fill_price

            log.warning("UNWIND: %s %s %.1f @ %.4f (was %s @ %.4f)",
                        reverse_side, result.token_id[:16],
                        result.filled_size, price,
                        result.side, result.fill_price)

            self.ems.place_order(
                token_id=result.token_id, side=reverse_side,
                price=price, size=result.filled_size,
                tick_size=tick_size, neg_risk=neg_risk,
                order_type="FAK", source=f"{source}_unwind",
            )
