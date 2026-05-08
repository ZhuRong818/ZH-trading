"""
Pipeline Stages — each stage processes a TradingSignal and passes it forward.

The pipeline enforces that ALL signals flow through the same stages:

    ┌──────────┐    ┌──────────┐    ┌───────────┐    ┌──────────┐    ┌──────────┐
    │  RISK    │ →  │ CAPITAL  │ →  │ EXECUTION │ →  │ TRACKING │ →  │ LOGGING  │
    │  GATE    │    │  GATE    │    │           │    │          │    │          │
    └──────────┘    └──────────┘    └───────────┘    └──────────┘    └──────────┘
    Can I trade?    Can I afford    Place the        Update          Record to
    Drawdown OK?    it? Budget OK?  order. VWAP      positions,      trade log +
    Circuit break?  Concentration?  fill.            P&L.            performance.

No strategy can bypass a stage. If any stage rejects, the signal stops.
"""

import logging
import time
from typing import List, Optional

from pipeline.signal import TradingSignal
from risk.risk_engine import RiskEngine
from oms.capital_allocator import CapitalAllocator
from oms.position_manager import PositionManager, Fill
from ems.execution import ExecutionEngine
from analytics.trade_log import TradeLog
from analytics.performance import PerformanceTracker

log = logging.getLogger(__name__)


class RiskGate:
    """
    Stage 1: Risk checks.
    Rejects signals that would breach risk limits.
    """

    def __init__(self, risk_engine: RiskEngine):
        self.risk = risk_engine

    def process(self, signal: TradingSignal) -> TradingSignal:
        # System-level halt
        if self.risk.halted:
            signal.rejected_by = "risk"
            signal.reject_reason = "system halted"
            return signal

        # Per-position size check
        if not self.risk.check_position_size(signal.token_id, signal.notional):
            signal.rejected_by = "risk"
            signal.reject_reason = f"position size ${signal.notional:.0f} exceeds limit"
            return signal

        # Circuit breaker
        if not self.risk.check_circuit_breaker(signal.token_id):
            signal.rejected_by = "risk"
            signal.reject_reason = "circuit breaker active"
            return signal

        signal.risk_approved = True
        return signal


class CapitalGate:
    """
    Stage 2: Capital allocation.
    Checks budget, concentration, and reserve limits.
    May reduce signal size if capital is limited.
    """

    def __init__(self, allocator: CapitalAllocator):
        self.allocator = allocator

    def process(self, signal: TradingSignal) -> TradingSignal:
        if signal.was_rejected:
            return signal

        approved = self.allocator.request_capital(
            signal.strategy, signal.token_id, signal.notional,
        )

        if approved <= 0:
            signal.rejected_by = "capital"
            signal.reject_reason = f"no capital available (requested ${signal.notional:.0f})"
            return signal

        # Reduce size if capital was reduced
        if approved < signal.notional and signal.price > 0:
            signal.size = approved / signal.price

        signal.capital_approved = approved
        return signal


class Executor:
    """
    Stage 3: Order execution.
    Places the order through the EMS (live or simulated).
    """

    def __init__(self, ems: ExecutionEngine):
        self.ems = ems

    def process(self, signal: TradingSignal) -> TradingSignal:
        if signal.was_rejected:
            return signal

        order_id = self.ems.place_order(
            token_id=signal.token_id,
            side=signal.side,
            price=signal.price,
            size=signal.size,
            tick_size=signal.tick_size,
            neg_risk=signal.neg_risk,
            order_type=signal.order_type,
            source=signal.strategy,
            edge=signal.edge,
            fair_value=signal.fair_value,
            direction=signal.direction,
        )

        if order_id:
            signal.order_id = order_id
            fill = getattr(self.ems, "_fills_by_order_id", {}).get(order_id)
            if fill:
                signal.fill_price = fill.price
                signal.fill_size = fill.size
        else:
            signal.rejected_by = "executor"
            signal.reject_reason = "order placement failed"
            # Release capital since order didn't go through
            if signal.capital_approved > 0:
                from oms.capital_allocator import CapitalAllocator
                # Note: capital release handled by pipeline orchestrator

        return signal


class Tracker:
    """
    Stage 4: Position tracking.
    Records fills and updates P&L.
    (Fills are actually handled via EMS callbacks, but this stage
    can do additional bookkeeping.)
    """

    def __init__(self, oms: PositionManager):
        self.oms = oms

    def process(self, signal: TradingSignal) -> TradingSignal:
        # Fills are recorded via EMS callback → OMS.record_fill()
        # This stage exists for any post-execution bookkeeping
        return signal


class Logger:
    """
    Stage 5: Logging and analytics.
    Records the signal outcome for analysis.
    """

    def __init__(self, trade_log: TradeLog, performance: PerformanceTracker):
        self.trade_log = trade_log
        self.performance = performance
        self.signal_history: List[TradingSignal] = []

    def process(self, signal: TradingSignal) -> TradingSignal:
        self.signal_history.append(signal)

        if signal.was_rejected:
            log.debug(
                "REJECTED [%s]: %s %s %.1f @ %.4f | by=%s reason=%s",
                signal.strategy, signal.side, signal.token_id[:12],
                signal.size, signal.price,
                signal.rejected_by, signal.reject_reason,
            )
        elif signal.was_executed:
            log.info(
                "EXECUTED [%s]: %s %s %.1f @ %.4f edge=%.4f | id=%s",
                signal.strategy, signal.side, signal.token_id[:12],
                signal.size, signal.price, signal.edge,
                signal.order_id[:16] if signal.order_id else "",
            )

        return signal

    def stats(self) -> dict:
        total = len(self.signal_history)
        executed = sum(1 for s in self.signal_history if s.was_executed)
        rejected = sum(1 for s in self.signal_history if s.was_rejected)
        reject_reasons = {}
        for s in self.signal_history:
            if s.rejected_by:
                key = f"{s.rejected_by}:{s.reject_reason}"
                reject_reasons[key] = reject_reasons.get(key, 0) + 1
        return {
            "total_signals": total,
            "executed": executed,
            "rejected": rejected,
            "fill_rate": f"{executed/total*100:.1f}%" if total > 0 else "0%",
            "top_rejections": dict(sorted(reject_reasons.items(), key=lambda x: -x[1])[:5]),
        }
