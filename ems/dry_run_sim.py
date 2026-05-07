"""
Realistic Dry-Run Simulator

Replaces the instant-fill logic with probabilistic fills based on:
- Order book depth (VWAP walk)
- Order type (FOK must fill completely, GTC can be partial)
- Fill probability scaled by order size vs available depth

GTC orders that don't fill immediately go to a pending queue and
may fill on subsequent book updates.
"""

import logging
import random
import time
from dataclasses import dataclass
from typing import List, Optional

from data_pipeline.market_data import MarketDataFeed
from oms.position_manager import Fill

log = logging.getLogger(__name__)


@dataclass
class PendingOrder:
    token_id: str
    side: str
    price: float
    size: float
    order_type: str
    source: str
    created_at: float
    order_id: str


class DryRunSimulator:
    """
    Simulates order fills using real book depth instead of instant fills.
    """

    def __init__(self, data_feed: MarketDataFeed, base_fill_prob: float = 0.6):
        self.data = data_feed
        self.base_fill_prob = base_fill_prob  # base probability a GTC order fills
        self.pending: List[PendingOrder] = []
        self._order_counter = 0

    def _next_id(self) -> str:
        self._order_counter += 1
        return f"sim_{self._order_counter}_{int(time.time())}"

    def simulate_fill(
        self, token_id: str, side: str, price: float, size: float,
        order_type: str = "GTC", source: str = "",
    ) -> Optional[Fill]:
        """
        Simulate a fill based on current book depth.

        Returns a Fill if the order would execute, None if not.
        For GTC orders that don't fill, adds to pending queue.
        """
        book = self.data.get_book(token_id)
        if not book:
            log.debug("[SIM] No book for %s, %s pending", token_id[:16], "queued as" if order_type == "GTC" else "rejected")
            if order_type == "GTC":
                self._add_pending(token_id, side, price, size, order_type, source)
            return None

        # Walk the book to find what's available
        vwap, fillable = book.vwap_price(side, size)

        if vwap is None or fillable <= 0:
            log.debug("[SIM] No depth for %s %s %.1f", side, token_id[:16], size)
            if order_type == "GTC":
                self._add_pending(token_id, side, price, size, order_type, source)
            return None

        # Check if our price is competitive
        best = book.best_ask if side == "BUY" else book.best_bid
        if best is None:
            log.debug("[SIM] No best price for %s %s", side, token_id[:16])
            return None

        price_ok = (side == "BUY" and price >= best) or (side == "SELL" and price <= best)
        if not price_ok:
            log.debug("[SIM] Price not competitive: %s %.4f vs best %.4f", side, price, best)
            if order_type == "GTC":
                self._add_pending(token_id, side, price, size, order_type, source)
            return None

        # FOK: must fill completely
        if order_type == "FOK":
            if fillable < size:
                log.debug("FOK rejected: only %.1f of %.1f available", fillable, size)
                return None
            return Fill(
                token_id=token_id, side=side, size=size,
                price=vwap, timestamp=time.time(),
                order_id=self._next_id(), source=source,
            )

        # FAK: fill what's available, kill the rest
        if order_type == "FAK":
            fill_size = min(fillable, size)
            if fill_size < 1:
                return None
            return Fill(
                token_id=token_id, side=side, size=fill_size,
                price=vwap, timestamp=time.time(),
                order_id=self._next_id(), source=source,
            )

        # GTC: probabilistic fill based on depth ratio
        depth_ratio = min(fillable / size, 1.0) if size > 0 else 0
        fill_prob = self.base_fill_prob * depth_ratio

        if random.random() > fill_prob:
            # Didn't fill this time — add to pending
            self._add_pending(token_id, side, price, size, order_type, source)
            log.debug("GTC pending: %s %s %.1f @ %.4f (prob=%.2f)", source, side, size, price, fill_prob)
            return None

        # Fill at VWAP (not the requested price — more realistic)
        fill_size = min(fillable, size)
        return Fill(
            token_id=token_id, side=side, size=fill_size,
            price=vwap, timestamp=time.time(),
            order_id=self._next_id(), source=source,
        )

    def check_pending(self) -> List[Fill]:
        """
        Re-evaluate pending GTC orders against current book state.
        Called each iteration of the main loop.
        Returns list of fills for orders that now execute.
        """
        fills = []
        still_pending = []

        for order in self.pending:
            # Expire orders older than 5 minutes
            if time.time() - order.created_at > 300:
                continue

            book = self.data.get_book(order.token_id)
            if not book:
                still_pending.append(order)
                continue

            best = book.best_ask if order.side == "BUY" else book.best_bid
            if best is None:
                still_pending.append(order)
                continue

            # Check if market has moved to our price
            price_ok = (
                (order.side == "BUY" and best <= order.price)
                or (order.side == "SELL" and best >= order.price)
            )

            if not price_ok:
                still_pending.append(order)
                continue

            vwap, fillable = book.vwap_price(order.side, order.size)
            if vwap is None or fillable < 1:
                still_pending.append(order)
                continue

            # Probabilistic fill
            depth_ratio = min(fillable / order.size, 1.0)
            if random.random() > self.base_fill_prob * depth_ratio:
                still_pending.append(order)
                continue

            fill_size = min(fillable, order.size)
            fill = Fill(
                token_id=order.token_id, side=order.side, size=fill_size,
                price=vwap, timestamp=time.time(),
                order_id=order.order_id, source=order.source,
            )
            fills.append(fill)
            log.info("[SIM] Pending fill: %s %s %.1f @ %.4f", order.source, order.side, fill_size, vwap)

        self.pending = still_pending
        return fills

    def _add_pending(self, token_id, side, price, size, order_type, source):
        self.pending.append(PendingOrder(
            token_id=token_id, side=side, price=price, size=size,
            order_type=order_type, source=source,
            created_at=time.time(), order_id=self._next_id(),
        ))

    def cancel_all_pending(self):
        count = len(self.pending)
        self.pending.clear()
        return count
