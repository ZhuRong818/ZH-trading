"""
PriceFeatureSkill — computes features from raw price history.

Pure computation, no I/O. Takes a list of prices, returns PriceFeatures.
Both momentum and oracle use this.
"""

from typing import Optional
from skills.types import PriceFeatures


class PriceFeatureSkill:

    def __init__(self, lookback_ticks: int = 5, momentum_ticks: int = 20):
        self.lookback_ticks = lookback_ticks
        self.momentum_ticks = momentum_ticks

    def compute(self, price_history: list) -> Optional[PriceFeatures]:
        """Compute features from raw price list. Returns None if not enough data."""
        min_needed = max(self.lookback_ticks, self.momentum_ticks) + 1
        if len(price_history) < min_needed:
            return None

        current = price_history[-1]
        if current <= 0:
            return None

        # Move over lookback window (for oracle: "did BTC spike in last N ticks?")
        old_lookback = price_history[-1 - self.lookback_ticks]
        move_bps = (current / old_lookback - 1.0) * 10_000 if old_lookback > 0 else 0.0

        # Momentum over longer window (for momentum: "what's the trend?")
        old_momentum = price_history[-1 - self.momentum_ticks]
        momentum = (current / old_momentum - 1.0) if old_momentum > 0 else 0.0

        # Volatility: std dev of tick-to-tick returns
        returns = []
        n = min(self.momentum_ticks, len(price_history) - 1)
        for i in range(-n, 0):
            prev = price_history[i - 1]
            curr = price_history[i]
            if prev > 0:
                returns.append(curr / prev - 1.0)

        volatility = self._std(returns)

        # Signal-to-noise ratio
        momentum_z = momentum / max(volatility, 1e-9)

        return PriceFeatures(
            current_price=current,
            move_bps=move_bps,
            momentum=momentum,
            volatility=volatility,
            momentum_z=momentum_z,
        )

    @staticmethod
    def _std(xs: list) -> float:
        if len(xs) < 2:
            return 0.0
        mean = sum(xs) / len(xs)
        var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
        return var ** 0.5
