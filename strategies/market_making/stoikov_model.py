"""
Module 5a — Market Making Strategy (Avellaneda-Stoikov Model)

Posts continuous bid/ask quotes around a reservation price,
collecting the spread as profit. Skews quotes based on inventory,
volatility, and time-to-maturity.

Key formula:
    P_reservation = P_mid - (q * gamma * sigma^2 * T)
    optimal_spread = gamma * sigma^2 * T + (2/gamma) * ln(1 + gamma/k)
"""

import logging
import math
import time
from datetime import datetime, timezone
from typing import Optional

from config import MarketMakingConfig
from data_pipeline.market_data import MarketDataFeed
from ems.execution import ExecutionEngine
from oms.position_manager import PositionManager

log = logging.getLogger(__name__)


class StoikovMarketMaker:
    """
    Avellaneda-Stoikov market making with prediction market adaptations:
    - Adjusted midpoint (anti-toxic-flow)
    - Time-to-resolution decay
    - Regime-aware spread widening
    """

    def __init__(
        self,
        config: MarketMakingConfig,
        data_feed: MarketDataFeed,
        ems: ExecutionEngine,
        oms: PositionManager,
        token_id: str,
        tick_size: str = "0.01",
        neg_risk: bool = False,
        end_date: str = "",
    ):
        self.config = config
        self.data = data_feed
        self.ems = ems
        self.oms = oms
        self.token_id = token_id
        self.tick_size = tick_size
        self.neg_risk = neg_risk
        self.end_date = end_date
        self._last_bid = 0.0
        self._last_ask = 0.0

    def hours_to_resolution(self) -> float:
        """Time remaining until market resolves, in hours."""
        if not self.end_date:
            return 720.0  # default 30 days
        try:
            end = datetime.fromisoformat(self.end_date.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            hours = max((end - now).total_seconds() / 3600, 1.0)
            return hours
        except Exception:
            return 720.0

    def reservation_price(self, mid: float, inventory: float, sigma: float, T: float) -> float:
        """
        Stoikov reservation price — shifts mid away from inventory risk.
        Long inventory -> lower reservation (encourages selling).
        """
        return mid - (inventory * self.config.gamma * sigma ** 2 * T)

    def optimal_spread(self, sigma: float, T: float) -> float:
        """
        Stoikov optimal spread — widens with volatility and time.
        """
        gamma = self.config.gamma
        k = self.config.spread_k
        return gamma * sigma ** 2 * T + (2 / gamma) * math.log(1 + gamma / k)

    def step(self):
        """One iteration of the market making loop."""
        # 1. Fetch fresh order book
        book = self.data.fetch_order_book(self.token_id)
        if book.mid is None:
            log.warning("No mid price, skipping")
            return

        # 2. Use adjusted midpoint (anti-toxic-flow)
        mid = self.data.adjusted_midpoint(self.token_id, min_incentive_size=50.0)
        if mid is None:
            mid = book.mid

        # 3. Calculate Stoikov parameters
        pos = self.oms.get_position(self.token_id)
        inventory = pos.size if pos else 0.0
        sigma = self.data.rolling_volatility(self.token_id, window=self.config.volatility_window)
        T = self.hours_to_resolution()

        # 4. Dynamic risk aversion — increase if inventory too large
        gamma = self.config.gamma
        if abs(inventory * mid) > 5000:  # mm_max_inventory_imbalance_usdc
            gamma *= 1.5
            log.info("Inventory skew active: gamma=%.2f inventory=%.1f", gamma, inventory)

        # 5. Compute reservation price and spread
        r = self.reservation_price(mid, inventory, sigma, T)
        raw_spread = gamma * sigma ** 2 * T + (2 / gamma) * math.log(1 + gamma / self.config.spread_k)

        # 6. Regime-aware spread widening
        regime = MarketDataFeed.classify_regime(mid)
        if regime == "tail":
            raw_spread *= 2.0  # widen in tail regime (near 0 or 1)
        elif regime == "contested":
            raw_spread *= 0.8  # tighten in contested zone (mean-reverting)

        half_spread = max(raw_spread / 2, float(self.tick_size))

        bid = r - half_spread
        ask = r + half_spread

        # 7. Rate limit check — only requote if prices moved enough
        should_update = (
            self.ems.rate_limiter.should_requote(self.token_id + "_bid", bid)
            or self.ems.rate_limiter.should_requote(self.token_id + "_ask", ask)
        )

        if not should_update:
            return

        # 8. Cancel existing quotes
        self.ems.cancel_all()

        # 9. Place layered quotes
        for level in range(self.config.num_levels):
            offset = level * float(self.tick_size) * 2
            size = self.config.order_size * (1.0 - 0.2 * level)

            bid_price = bid - offset
            ask_price = ask + offset

            self.ems.place_order(
                self.token_id, "BUY", bid_price, size,
                tick_size=self.tick_size, neg_risk=self.neg_risk,
                source="stoikov_mm",
            )
            self.ems.place_order(
                self.token_id, "SELL", ask_price, size,
                tick_size=self.tick_size, neg_risk=self.neg_risk,
                source="stoikov_mm",
            )

        self._last_bid = bid
        self._last_ask = ask

        log.info(
            "MM quotes: mid=%.4f r=%.4f bid=%.4f ask=%.4f spread=%.4f "
            "sigma=%.4f T=%.0fh inv=%.1f regime=%s",
            mid, r, bid, ask, ask - bid, sigma, T, inventory, regime,
        )
