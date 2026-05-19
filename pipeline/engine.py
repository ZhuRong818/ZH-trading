"""
Pipeline Engine — orchestrates all stages in sequence.

This is the single entry point for all trading activity.
No strategy should call EMS directly — everything goes through the pipeline.

Usage:
    pipeline = PipelineEngine(risk, capital, ems, oms, trade_log, performance)
    signal = TradingSignal(token_id=..., side="BUY", price=0.50, size=10, strategy="mm")
    result = pipeline.submit(signal)
    if result.was_executed:
        print(f"Order placed: {result.order_id}")
    elif result.was_rejected:
        print(f"Rejected by {result.rejected_by}: {result.reject_reason}")
"""

import logging
import time
from typing import List

from pipeline.signal import TradingSignal
from pipeline.stages import RiskGate, CapitalGate, Executor, Tracker, Logger
from risk.risk_engine import RiskEngine
from oms.capital_allocator import CapitalAllocator
from oms.position_manager import PositionManager
from ems.execution import ExecutionEngine
from analytics.trade_log import TradeLog
from analytics.performance import PerformanceTracker

log = logging.getLogger(__name__)


class PipelineEngine:
    """
    Chains all pipeline stages:
        Signal → RiskGate → CapitalGate → Executor → Tracker → Logger

    Every signal passes through every stage in order.
    If any stage rejects, subsequent stages see the rejection and skip.
    """

    def __init__(
        self,
        risk_engine: RiskEngine,
        capital_allocator: CapitalAllocator,
        ems: ExecutionEngine,
        oms: PositionManager,
        trade_log: TradeLog,
        performance: PerformanceTracker,
    ):
        self.risk_gate = RiskGate(risk_engine)
        self.capital_gate = CapitalGate(capital_allocator)
        self.executor = Executor(ems)
        self.tracker = Tracker(oms)
        self.logger = Logger(trade_log, performance)

        # References for status
        self._capital = capital_allocator
        self._ems = ems

    def _apply_live_order_cap(self, signal: TradingSignal) -> TradingSignal:
        """
        Live per-order caps are sizing constraints, not strategy vetoes.
        Apply them before risk/capital so those gates evaluate the actual order size.
        """
        if signal.was_rejected or self._ems.dry_run or signal.side != "BUY":
            return signal

        cap = float(getattr(self._ems, "live_max_order_usdc", 0.0) or 0.0)
        if cap <= 0 or signal.price <= 0 or signal.notional <= cap:
            return signal

        old_size = signal.size
        old_notional = signal.notional
        signal.size = cap / signal.price
        log.info(
            "Live cap resized signal: %s %s %.4f -> %.4f @ %.4f "
            "notional=$%.2f -> $%.2f",
            signal.strategy, signal.side, old_size, signal.size, signal.price,
            old_notional, signal.notional,
        )
        return signal

    def submit(self, signal: TradingSignal) -> TradingSignal:
        """
        Submit a signal through the full pipeline.
        Returns the signal with pipeline state filled in.
        """
        signal = self._apply_live_order_cap(signal)
        signal = self.risk_gate.process(signal)
        signal = self.capital_gate.process(signal)
        signal = self.executor.process(signal)
        signal = self.tracker.process(signal)
        signal = self.logger.process(signal)

        # Release capital if execution failed after capital was approved
        if signal.capital_approved > 0 and not signal.was_executed:
            self._capital.release_capital(
                signal.strategy, signal.token_id, signal.capital_approved,
            )

        return signal

    def submit_batch(self, signals: List[TradingSignal]) -> List[TradingSignal]:
        """Submit multiple signals. Useful for multi-leg arb."""
        return [self.submit(s) for s in signals]

    def stats(self) -> dict:
        """Pipeline statistics."""
        return self.logger.stats()
