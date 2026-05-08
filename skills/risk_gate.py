"""
RiskGateSkill — filters out trades with bad risk/reward.

Same logic, different thresholds per strategy.
"""

from dataclasses import dataclass

from skills.types import Edge, RollingMarket


@dataclass
class RiskGateConfig:
    min_edge: float = 0.03
    min_price: float = 0.05
    max_price: float = 0.65
    min_seconds_remaining: float = 30.0


class RiskGateSkill:

    def passes(self, edge: Edge, market: RollingMarket, config: RiskGateConfig,
               can_trade: bool = True) -> bool:
        if not can_trade:
            return False

        if edge.edge < config.min_edge:
            return False

        if edge.market_price < config.min_price:
            return False

        if edge.market_price > config.max_price:
            return False

        if market.seconds_remaining < config.min_seconds_remaining:
            return False

        return True
