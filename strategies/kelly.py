"""
Kelly Criterion Position Sizing

Computes the optimal bet size given your estimated edge and confidence.
Used by other strategies to size their trades — not a standalone strategy.

Formula:
    kelly_fraction = (p * b - q) / b

    where:
        p = probability of winning (your model's estimate)
        b = odds (payout ratio: how much you win per $1 risked)
        q = 1 - p (probability of losing)

For prediction markets:
    Buying YES at price $X:
        b = (1 - X) / X     (you pay X, win 1-X)
        p = your estimated true probability

    kelly_fraction = (p * (1-X)/X - (1-p)) / ((1-X)/X)
                   = (p - X) / (1 - X)

    This simplifies to: fraction of bankroll to bet = (p - X) / (1 - X)
    where p is your true probability and X is the market price.

We use fractional Kelly (default 0.25x) to reduce variance and drawdown.
Full Kelly is mathematically optimal but has ~50% drawdown in practice.
Quarter Kelly cuts expected return by 25% but cuts drawdown by 75%.
"""

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class KellyResult:
    fraction: float       # fraction of bankroll to bet (0 to 1)
    size_usdc: float      # dollar amount to bet
    edge: float           # estimated edge (p - market_price)
    direction: str        # "BUY" or "SELL" or "NONE"
    confidence: float     # how confident we are in the edge


def kelly_size(
    fair_prob: float,
    market_price: float,
    bankroll: float,
    kelly_fraction: float = 0.25,
    max_bet_pct: float = 0.05,
    min_edge: float = 0.02,
) -> KellyResult:
    """
    Compute optimal position size using fractional Kelly criterion.

    Args:
        fair_prob:      Your model's estimated true probability (0-1)
        market_price:   Current market price (0-1)
        bankroll:       Total available capital in USDC
        kelly_fraction: Kelly multiplier (0.25 = quarter Kelly, safest)
        max_bet_pct:    Max fraction of bankroll per single bet (hard cap)
        min_edge:       Minimum edge required to trade (filter noise)

    Returns:
        KellyResult with direction, size, and edge
    """
    edge = fair_prob - market_price

    # No edge — don't trade
    if abs(edge) < min_edge:
        return KellyResult(
            fraction=0, size_usdc=0, edge=edge,
            direction="NONE", confidence=0,
        )

    if edge > 0:
        # Underpriced — buy YES
        # Kelly: f = (p - X) / (1 - X)
        denom = 1.0 - market_price
        if denom <= 0:
            return KellyResult(fraction=0, size_usdc=0, edge=edge, direction="NONE", confidence=0)
        full_kelly = edge / denom
        direction = "BUY"
    else:
        # Overpriced — buy NO (sell YES)
        # Equivalent to buying NO at price (1 - market_price)
        # Edge for NO = (1 - fair_prob) - (1 - market_price) = market_price - fair_prob = -edge
        no_price = 1.0 - market_price
        denom = 1.0 - no_price  # = market_price
        if denom <= 0:
            return KellyResult(fraction=0, size_usdc=0, edge=edge, direction="NONE", confidence=0)
        full_kelly = abs(edge) / denom
        direction = "SELL"

    # Apply fractional Kelly
    f = full_kelly * kelly_fraction

    # Hard cap at max_bet_pct of bankroll
    f = min(f, max_bet_pct)

    # Clamp to [0, 1]
    f = max(0.0, min(1.0, f))

    size = f * bankroll

    # Confidence: how many multiples of min_edge is our edge
    confidence = min(abs(edge) / min_edge, 3.0) / 3.0

    log.debug(
        "Kelly: fair=%.4f mkt=%.4f edge=%.4f dir=%s full_k=%.4f frac_k=%.4f size=$%.0f",
        fair_prob, market_price, edge, direction, full_kelly, f, size,
    )

    return KellyResult(
        fraction=f,
        size_usdc=size,
        edge=edge,
        direction=direction,
        confidence=confidence,
    )
