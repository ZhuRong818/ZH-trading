"""
BaseStrategy — the common interface ALL strategies must implement.

Every strategy:
  - Receives List[MarketContext] as input
  - Returns List[TradingSignal] as output
  - Never touches EMS directly
  - Tracks its own state (positions, fills) via on_fill() callback

The runner handles: cancel old orders → submit signals → track fills → log
"""

from typing import List
from data_pipeline.market_provider import MarketContext
from pipeline.signal import TradingSignal
from oms.position_manager import Fill


class BaseStrategy:
    """All strategies inherit from this."""

    name: str = "base"

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        """
        Analyze markets and return desired trades.
        Called every loop iteration.
        MUST NOT call ems.place_order() — return TradingSignals instead.
        """
        raise NotImplementedError

    def on_fill(self, fill: Fill):
        """Called when one of this strategy's signals was executed."""
        pass

    def on_cancel(self):
        """Called when the runner cancels this strategy's orders."""
        pass

    def snapshot(self) -> dict:
        """Return current state for post-session analysis."""
        return {}
