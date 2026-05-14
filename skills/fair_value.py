"""
Fair Value Skills — the ONE thing that differs between strategies.

MomentumFairValueSkill: z-score from distance + time + volatility
OracleFairValueSkill:   BTC move magnitude → probability shift

Both answer: "What should the UP/DOWN probabilities be right now?"
"""

import math
from typing import Optional

from skills.types import PriceFeatures, RollingMarket, FairValue


class MomentumFairValueSkill:
    """
    Fair value from z-score: how far is BTC from strike,
    adjusted for time remaining, volatility, and momentum drift.

    z = distance / (price × volatility × √ticks_remaining)
    fair_up = Φ(z + drift)
    """

    def __init__(self, drift_weight: float = 0.2, min_vol: float = 0.00002):
        self.drift_weight = drift_weight
        self.min_vol = min_vol

    def estimate(self, features: PriceFeatures, market: RollingMarket) -> Optional[FairValue]:
        if features.current_price <= 0 or market.strike_price <= 0:
            return None
        if market.seconds_remaining <= 0:
            return None

        vol = max(features.volatility, self.min_vol)

        # How many ticks of uncertainty remain
        horizon_ticks = max(market.seconds_remaining / 0.5, 1.0)

        # Expected price range in remaining time
        horizon_sigma = features.current_price * vol * math.sqrt(horizon_ticks)

        # How far is BTC from strike in sigma units
        distance = features.current_price - market.strike_price
        z_score = distance / horizon_sigma if horizon_sigma > 0 else 0

        # Adjust for momentum drift
        mom_z = max(-2.0, min(2.0, features.momentum_z))
        adjusted_z = z_score + self.drift_weight * mom_z

        # Convert to probability via normal CDF
        fair_up = 0.5 * (1.0 + math.erf(adjusted_z / math.sqrt(2.0)))
        fair_up = max(0.05, min(0.95, fair_up))

        return FairValue(
            fair_up=fair_up,
            fair_down=1.0 - fair_up,
            reason=f"momentum: z={adjusted_z:.2f} dist=${distance:.0f} vol={vol:.6f} T={market.seconds_remaining:.0f}s",
        )


class OracleFairValueSkill:
    """
    Fair value from BTC move speed: a sharp move means the probability
    should have shifted, but Polymarket might not have caught up yet.

    prob_shift = min(|move_bps| / scale, max_shift)
    """

    def __init__(self, scale: float = 50.0, max_shift: float = 0.35):
        self.scale = scale
        self.max_shift = max_shift

    def estimate(self, features: PriceFeatures, market: RollingMarket) -> Optional[FairValue]:
        move_bps = features.move_bps
        prob_shift = min(abs(move_bps) / self.scale, self.max_shift)

        if move_bps > 0:
            fair_up = 0.50 + prob_shift
            fair_down = 1.0 - fair_up
        else:
            fair_down = 0.50 + prob_shift
            fair_up = 1.0 - fair_down

        return FairValue(
            fair_up=fair_up,
            fair_down=fair_down,
            reason=f"oracle: {move_bps:.1f}bps move → {prob_shift:.3f} prob shift",
        )


class LeadLagFairValueSkill:
    """
    Fair value from a LEADER asset's price move applied to a FOLLOWER asset.

    When BTC moves sharply, correlated assets (ETH, SOL, XRP) tend to follow
    with a 1-5 second lag. This skill uses the leader's PriceFeatures to
    estimate fair value on the follower's market.

    The follower's own price doesn't matter for fair value — only the
    leader's move matters, because the edge is in the speed of propagation.

    Portable: can be used with any leader/follower pair.
    """

    def __init__(self, scale: float = 50.0, max_shift: float = 0.35,
                 correlation_discount: float = 0.90):
        self.scale = scale
        self.max_shift = max_shift
        self.correlation_discount = correlation_discount  # dampen for imperfect correlation

    def estimate(self, leader_features: PriceFeatures, follower_market: RollingMarket) -> Optional[FairValue]:
        """
        Estimate fair value of a follower market based on leader's price move.

        Args:
            leader_features: PriceFeatures from the LEADER asset (e.g., BTC)
            follower_market: RollingMarket of the FOLLOWER asset (e.g., ETH)
        """
        move_bps = leader_features.move_bps
        prob_shift = min(abs(move_bps) / self.scale, self.max_shift)
        prob_shift *= self.correlation_discount

        if move_bps > 0:
            fair_up = 0.50 + prob_shift
            fair_down = 1.0 - fair_up
        else:
            fair_down = 0.50 + prob_shift
            fair_up = 1.0 - fair_down

        return FairValue(
            fair_up=fair_up,
            fair_down=fair_down,
            reason=f"leadlag: leader {move_bps:.1f}bps → follower shift {prob_shift:.3f} (corr={self.correlation_discount:.0%})",
        )
