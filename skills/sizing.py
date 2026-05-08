"""
PositionSizerSkill — Kelly criterion sizing.
"""

from dataclasses import dataclass

from skills.types import Edge


@dataclass
class SizingConfig:
    kelly_fraction: float = 0.20
    max_bet_pct: float = 0.05
    bankroll: float = 10_000
    min_bet_usdc: float = 5.0


class PositionSizerSkill:

    def size(self, edge: Edge, config: SizingConfig) -> float:
        """Returns size in USDC, or 0 if too small."""
        fair = edge.fair
        price = edge.market_price

        if fair <= price or price <= 0:
            return 0.0

        # Kelly: f = (p - X) / (1 - X)
        denom = 1.0 - price
        if denom <= 0:
            return 0.0

        raw_kelly = (fair - price) / denom
        scaled = raw_kelly * config.kelly_fraction
        capped = min(scaled, config.max_bet_pct)
        capped = max(0.0, capped)

        size_usdc = config.bankroll * capped

        if size_usdc < config.min_bet_usdc:
            return 0.0

        return size_usdc
