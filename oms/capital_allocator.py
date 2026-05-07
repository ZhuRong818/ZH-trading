"""
Capital Allocator — Cross-strategy capital budgeting.

Each strategy gets a budget. The allocator enforces:
  1. Per-strategy budget (e.g., MM gets max 30% of total)
  2. Per-market concentration (max 10% in any single market)
  3. System reserve (keep 20% uninvested for opportunities)
  4. Collateral lockup tracking for short-side trades

All strategies call request_capital() before placing orders.
"""

import logging
from collections import defaultdict
from typing import Optional

from config import CapitalConfig
from oms.position_manager import PositionManager

log = logging.getLogger(__name__)


class CapitalAllocator:

    def __init__(self, config: CapitalConfig, oms: PositionManager):
        self.config = config
        self.oms = oms
        self._strategy_deployed: dict[str, float] = defaultdict(float)
        self._market_deployed: dict[str, float] = defaultdict(float)
        self._locked_collateral: float = 0.0

    @property
    def total_capital(self) -> float:
        return self.config.total_capital_usdc

    @property
    def deployed(self) -> float:
        return sum(self._strategy_deployed.values())

    @property
    def available(self) -> float:
        reserve = self.total_capital * self.config.reserve_pct
        return max(0, self.total_capital - self.deployed - self._locked_collateral - reserve)

    def request_capital(self, strategy: str, market_token: str, amount_usdc: float) -> float:
        """
        Request capital. Returns approved amount (may be less than requested).
        Zero means rejected.
        """
        if amount_usdc <= 0:
            return 0.0

        # Map strategy source name to budget key
        budget_key = self._strategy_key(strategy)

        # Check 1: system-wide available (respects reserve)
        limit_system = self.available

        # Check 2: per-strategy budget
        budget_frac = self.config.strategy_budgets.get(budget_key, 0.20)
        budget_limit = self.total_capital * budget_frac
        already_used = self._strategy_deployed.get(budget_key, 0)
        limit_strategy = max(0, budget_limit - already_used)

        # Check 3: per-market concentration
        market_limit = self.total_capital * self.config.max_per_market_pct
        market_used = self._market_deployed.get(market_token, 0)
        limit_market = max(0, market_limit - market_used)

        # Approved = minimum of all limits
        approved = min(amount_usdc, limit_system, limit_strategy, limit_market)

        if approved < 1.0:  # below minimum useful amount
            return 0.0

        # Book it
        self._strategy_deployed[budget_key] += approved
        self._market_deployed[market_token] += approved

        if approved < amount_usdc:
            log.info(
                "Capital reduced: requested=$%.0f approved=$%.0f strategy=%s "
                "(sys=%.0f strat=%.0f mkt=%.0f)",
                amount_usdc, approved, budget_key,
                limit_system, limit_strategy, limit_market,
            )

        return approved

    def release_capital(self, strategy: str, market_token: str, amount_usdc: float):
        """Called when a position is closed."""
        budget_key = self._strategy_key(strategy)
        self._strategy_deployed[budget_key] = max(0, self._strategy_deployed.get(budget_key, 0) - amount_usdc)
        self._market_deployed[market_token] = max(0, self._market_deployed.get(market_token, 0) - amount_usdc)

    def lock_collateral(self, amount: float):
        """Track collateral locked for short-side trades."""
        self._locked_collateral += amount

    def unlock_collateral(self, amount: float):
        """Release collateral when position resolves."""
        self._locked_collateral = max(0, self._locked_collateral - amount)

    def strategy_remaining(self, strategy: str) -> float:
        budget_key = self._strategy_key(strategy)
        budget_frac = self.config.strategy_budgets.get(budget_key, 0.20)
        budget_limit = self.total_capital * budget_frac
        used = self._strategy_deployed.get(budget_key, 0)
        return max(0, budget_limit - used)

    def _strategy_key(self, source: str) -> str:
        """Map fill source names to budget keys."""
        s = source.lower()
        if "stoikov" in s or s == "mm":
            return "stoikov_mm"
        if "whale" in s:
            return "whale_copy"
        if "arb" in s:
            return "arb"
        if "mean_rev" in s:
            return "mean_rev"
        if "fade" in s:
            return "fade"
        if "btc5m" in s:
            return "btc5m"
        return s

    def summary(self) -> dict:
        return {
            "total_capital": self.total_capital,
            "deployed": self.deployed,
            "locked_collateral": self._locked_collateral,
            "available": self.available,
            "utilization_pct": self.deployed / self.total_capital * 100 if self.total_capital > 0 else 0,
            "by_strategy": dict(self._strategy_deployed),
        }
