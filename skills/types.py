"""
Shared data objects — clean contracts between skills.

Every skill inputs/outputs these typed dataclasses.
No raw floats or dicts passed between components.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class PriceFeatures:
    """Output of PriceFeatureSkill — everything computed from price history."""
    current_price: float
    move_bps: float        # price change over lookback ticks in basis points
    momentum: float        # price change over momentum window (fractional)
    volatility: float      # std dev of returns
    momentum_z: float = 0.0  # momentum / volatility (signal-to-noise)


@dataclass
class RollingMarket:
    """Snapshot of a 5-minute rolling market at this moment."""
    strike_price: float
    seconds_remaining: float
    up_price: float          # best ask on UP token
    down_price: float        # best ask on DOWN token
    up_token_id: str = ""
    down_token_id: str = ""


@dataclass
class FairValue:
    """Output of a fair value skill — estimated true probabilities."""
    fair_up: float
    fair_down: float
    reason: str = ""


@dataclass
class Edge:
    """Output of EdgeSkill — the best tradeable edge found."""
    direction: str         # "UP" or "DOWN"
    fair: float            # our fair value for the chosen side
    market_price: float    # what the market is offering
    edge: float            # fair - market_price
    token_id: str = ""
    reason: str = ""


@dataclass
class TradeDecision:
    """Final output — a sized, validated trade ready to submit."""
    strategy: str
    direction: str
    token_id: str
    size: float
    price: float
    fair: float
    edge: float
    reason: str = ""
