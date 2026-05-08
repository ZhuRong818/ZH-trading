"""
Trading Signal — the universal message that flows through the pipeline.

Every strategy produces TradingSignals. Every pipeline stage processes them.
This enforces a single contract between all modules:

    Strategy → TradingSignal → RiskGate → CapitalGate → Executor → Tracker → Logger
"""

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TradingSignal:
    """
    Universal signal emitted by all strategies.
    Flows through: Risk → Capital → Execution → Tracking → Logging.
    """
    # What to trade
    token_id: str
    side: str              # "BUY" or "SELL"
    price: float           # target price (may be adjusted by executor)
    size: float            # desired size in shares

    # Who generated it
    strategy: str          # e.g. "stoikov_mm", "whale_copy", "mean_rev"

    # Market metadata
    tick_size: str = "0.01"
    neg_risk: bool = False
    order_type: str = "GTC"  # GTC, FOK, FAK

    # Signal quality
    edge: float = 0.0       # estimated edge (fair - market)
    confidence: float = 0.0  # 0-1 how confident the strategy is
    fair_value: float = 0.0  # strategy's fair value estimate
    direction: str = ""      # market outcome direction, e.g. UP or DOWN

    # Lifecycle
    timestamp: float = field(default_factory=time.time)
    signal_id: str = ""

    # Pipeline state (set by each stage)
    risk_approved: bool = False
    capital_approved: float = 0.0   # approved USDC amount
    order_id: Optional[str] = None  # set by executor
    fill_price: Optional[float] = None  # set after fill
    fill_size: Optional[float] = None
    rejected_by: Optional[str] = None   # which stage rejected it
    reject_reason: Optional[str] = None

    @property
    def notional(self) -> float:
        return self.size * self.price if self.price > 0 else 0

    @property
    def was_executed(self) -> bool:
        return self.order_id is not None

    @property
    def was_rejected(self) -> bool:
        return self.rejected_by is not None
