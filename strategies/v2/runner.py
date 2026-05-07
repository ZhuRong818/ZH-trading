"""
Unified Runner (v2) — runs all strategies through the same pipeline.

Every strategy:
  step(contexts) → List[TradingSignal]

The runner:
  1. Refreshes market data from provider
  2. Calls each strategy's step()
  3. Submits all signals through the pipeline (risk → capital → execute → log)
  4. Feeds fill results back to strategies
"""

import logging
import time
from typing import Dict, List, Optional

from strategies.base import BaseStrategy
from data_pipeline.market_provider import MarketProvider, MarketContext
from pipeline.engine import PipelineEngine
from pipeline.signal import TradingSignal
from ems.execution import ExecutionEngine
from oms.position_manager import Fill

log = logging.getLogger(__name__)


class UnifiedRunnerV2:
    """
    Runs any number of BaseStrategy instances on any MarketProvider.
    All signals go through the pipeline. No strategy touches EMS directly.
    """

    def __init__(
        self,
        provider: MarketProvider,
        pipeline: PipelineEngine,
        ems: ExecutionEngine,
    ):
        self.provider = provider
        self.pipeline = pipeline
        self.ems = ems
        self.strategies: List[BaseStrategy] = []
        self._last_window_key: str = ""
        self._cancel_on_roll: bool = True  # cancel orders when window changes

    def add(self, strategy: BaseStrategy):
        """Register a strategy."""
        self.strategies.append(strategy)
        log.info("Runner: registered %s", strategy.name)

    def step(self):
        """One iteration: refresh data → run strategies → submit signals."""
        # 1. Refresh market data
        self.provider.refresh()
        contexts = self.provider.all_contexts()

        if not contexts:
            return

        # 2. Check for window roll (rolling markets)
        window_key = contexts[0].condition_id if contexts else ""
        if window_key != self._last_window_key and self._last_window_key:
            if self._cancel_on_roll:
                self.ems.cancel_all()
            for s in self.strategies:
                s.on_cancel()
            log.info("Runner: window rolled to %s", window_key[:30])
        self._last_window_key = window_key

        # 3. Run each strategy
        all_signals: List[TradingSignal] = []
        for strategy in self.strategies:
            try:
                signals = strategy.step(contexts)
                all_signals.extend(signals)
            except Exception as e:
                log.warning("Runner: %s.step() failed: %s", strategy.name, e)

        # 4. Submit all signals through the pipeline
        for signal in all_signals:
            result = self.pipeline.submit(signal)

            # 5. Feed fill results back to the strategy
            if result.was_executed:
                fill = Fill(
                    token_id=result.token_id,
                    side=result.side,
                    size=result.fill_size or result.size,
                    price=result.fill_price or result.price,
                    timestamp=time.time(),
                    source=result.strategy,
                )
                # Find the strategy that generated this signal
                for s in self.strategies:
                    if result.strategy.startswith(s.name):
                        s.on_fill(fill)
                        break

    def status(self) -> dict:
        pstats = self.pipeline.stats()
        return {
            "strategies": [s.name for s in self.strategies],
            "active_contexts": len(self.provider.all_contexts()),
            "window": self._last_window_key[:30],
            "pipeline_signals": pstats["total_signals"],
            "pipeline_executed": pstats["executed"],
            "pipeline_fill_rate": pstats["fill_rate"],
        }
