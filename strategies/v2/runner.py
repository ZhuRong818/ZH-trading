"""
Unified Runner (v2) — runs all strategies through the same pipeline.

Every strategy:
  step(contexts) → List[TradingSignal]

The runner:
  1. Refreshes market data from provider
  2. Calls each strategy's step()
  3. Submits all signals through the pipeline (risk → capital → execute → log)
  4. Feeds fill results back to strategies
  5. On window roll: settles open positions, computes PnL, frees capital
"""

import logging
import time
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from strategies.base import BaseStrategy
from data_pipeline.market_provider import MarketProvider, MarketContext, RollingProvider
from pipeline.engine import PipelineEngine
from pipeline.signal import TradingSignal
from ems.execution import ExecutionEngine
from oms.position_manager import Fill, PositionManager

log = logging.getLogger(__name__)


@dataclass
class OpenEntry:
    """Tracks an open position from a fill for settlement purposes."""
    token_id: str
    side: str       # "BUY"
    size: float
    price: float
    strategy: str
    is_up_token: bool = True  # True if this is the "Up" side


class UnifiedRunnerV2:
    """
    Runs any number of BaseStrategy instances on any MarketProvider.
    All signals go through the pipeline. No strategy touches EMS directly.
    Handles settlement when rolling windows expire.
    """

    def __init__(
        self,
        provider: MarketProvider,
        pipeline: PipelineEngine,
        ems: ExecutionEngine,
        oms: PositionManager = None,
    ):
        self.provider = provider
        self.pipeline = pipeline
        self.ems = ems
        self.oms = oms
        self.strategies: List[BaseStrategy] = []
        self._last_window_key: str = ""
        self._last_up_token: str = ""
        self._last_down_token: str = ""
        self._is_rolling = isinstance(provider, RollingProvider)

        # Track open entries for settlement
        self._open_entries: List[OpenEntry] = []

        # Settlement stats
        self.total_settlements = 0
        self.settlement_wins = 0
        self.settlement_losses = 0
        self.settlement_pnl = 0.0

    def add(self, strategy: BaseStrategy):
        """Register a strategy."""
        self.strategies.append(strategy)
        log.info("Runner: registered %s", strategy.name)

    def step(self):
        """One iteration: refresh data → settle if needed → run strategies → submit signals."""
        # 1. Refresh market data
        self.provider.refresh()
        contexts = self.provider.all_contexts()

        if not contexts:
            return

        # 2. Check for window roll (rolling markets)
        window_key = contexts[0].condition_id if contexts else ""
        if window_key != self._last_window_key and self._last_window_key:
            self._on_window_roll()
            log.info("Runner: window rolled to %s", window_key[:30])
        self._last_window_key = window_key

        # Save current provider tokens so they're available for OMS
        # reconciliation on the NEXT window roll (refresh() already
        # overwrites provider.up_token / provider.down_token).
        if self._is_rolling:
            self._last_up_token = self.provider.up_token
            self._last_down_token = self.provider.down_token

        # 3. Skip if too little time left
        if self._is_rolling:
            remaining = contexts[0].seconds_remaining if contexts else 0
            if remaining <= 0:
                return

        # 4. Run each strategy
        all_signals: List[TradingSignal] = []
        for strategy in self.strategies:
            try:
                signals = strategy.step(contexts)
                all_signals.extend(signals)
            except Exception as e:
                log.warning("Runner: %s.step() failed: %s", strategy.name, e)

        # 5. Submit all signals through the pipeline
        for signal in all_signals:
            result = self.pipeline.submit(signal)

            # 6. Feed fill results back and track for settlement
            if result.was_executed:
                fill = Fill(
                    token_id=result.token_id,
                    side=result.side,
                    size=result.fill_size or result.size,
                    price=result.fill_price or result.price,
                    timestamp=time.time(),
                    source=result.strategy,
                )
                # Notify the originating strategy
                for s in self.strategies:
                    if result.strategy.startswith(s.name):
                        s.on_fill(fill)
                        break

                # Track BUY fills for settlement
                if result.side == "BUY" and self._is_rolling:
                    is_up = self._is_up_token(result.token_id)
                    self._open_entries.append(OpenEntry(
                        token_id=result.token_id,
                        side="BUY",
                        size=fill.size,
                        price=fill.price,
                        strategy=result.strategy,
                        is_up_token=is_up,
                    ))

    def _is_up_token(self, token_id: str) -> bool:
        """Check if token is the 'Up' side of the current window."""
        if isinstance(self.provider, RollingProvider):
            return token_id == self.provider.up_token
        return True

    def _on_window_roll(self):
        """
        Window expired. Settle all open positions.
        For 5m markets: determine if BTC went up or down,
        then emit settlement SELL fills at $1.00 (won) or $0.00 (lost).
        """
        # Cancel any unfilled orders
        self.ems.cancel_all()
        for s in self.strategies:
            s.on_cancel()

        # ── Reconcile untracked positions from OMS ──────────────────
        # Pending fills that fired via check_pending_dry_run() bypass
        # the runner's _open_entries tracking.  Catch them here so they
        # still get settled when the window rolls.
        #
        # NOTE: provider.refresh() already switched up/down tokens to
        # the NEW window, so we use the saved _last_up/down_token.
        if self._is_rolling and self.oms:
            up_token = self._last_up_token
            down_token = self._last_down_token
            tracked_tokens = {e.token_id for e in self._open_entries}
            for pos in self.oms.get_all_open():
                if pos.token_id in (up_token, down_token) and pos.token_id not in tracked_tokens:
                    is_up = pos.token_id == up_token
                    self._open_entries.append(OpenEntry(
                        token_id=pos.token_id,
                        side="BUY",
                        size=pos.size,
                        price=pos.avg_price,
                        strategy="settlement",
                        is_up_token=is_up,
                    ))
                    log.info("Reconciled untracked rolling position: %s %.1f @ %.4f",
                             "UP" if is_up else "DOWN", pos.size, pos.avg_price)

        if not self._open_entries:
            return

        # Determine outcome from the rolling provider
        if isinstance(self.provider, RollingProvider):
            current_price = 0.0
            if self.provider.price_feed:
                try:
                    current_price = self.provider.price_feed()
                except Exception:
                    pass

            strike = self.provider.strike
            btc_went_up = current_price >= strike if (current_price > 0 and strike > 0) else None

            if btc_went_up is None:
                log.warning("Settlement: can't determine outcome (price=%.2f strike=%.2f)",
                            current_price, strike)
                self._open_entries.clear()
                return

            log.info("Settlement: strike=$%.2f end=$%.2f → %s",
                     strike, current_price, "UP" if btc_went_up else "DOWN")

            # Settle each open position
            for entry in self._open_entries:
                # Determine payout
                if entry.is_up_token:
                    payout = 1.0 if btc_went_up else 0.0
                else:
                    payout = 0.0 if btc_went_up else 1.0

                pnl = (payout - entry.price) * entry.size
                won = pnl > 0

                self.total_settlements += 1
                self.settlement_pnl += pnl
                if won:
                    self.settlement_wins += 1
                else:
                    self.settlement_losses += 1

                # Emit settlement SELL fill through EMS callbacks
                settlement_fill = Fill(
                    token_id=entry.token_id,
                    side="SELL",
                    size=entry.size,
                    price=payout,
                    timestamp=time.time(),
                    order_id=f"settlement_{int(time.time())}",
                    source="settlement",
                )
                # Fire through EMS callbacks (updates OMS, trade log, performance)
                self.ems._fire_fill(settlement_fill)

                # Notify the strategy
                for s in self.strategies:
                    if entry.strategy.startswith(s.name):
                        s.on_fill(settlement_fill)
                        break

                result = "WIN" if won else "LOSS"
                log.info(
                    "SETTLED [%s]: %s %s %.1f @ %.4f → $%.2f pnl=$%.2f | "
                    "record=%d-%d ($%.2f total)",
                    result, entry.strategy, "UP" if entry.is_up_token else "DOWN",
                    entry.size, entry.price, payout, pnl,
                    self.settlement_wins, self.settlement_losses, self.settlement_pnl,
                )

        self._open_entries.clear()

    def status(self) -> dict:
        pstats = self.pipeline.stats()
        return {
            "strategies": [s.name for s in self.strategies],
            "active_contexts": len(self.provider.all_contexts()),
            "window": self._last_window_key[:30],
            "pipeline_signals": pstats["total_signals"],
            "pipeline_executed": pstats["executed"],
            "pipeline_fill_rate": pstats["fill_rate"],
            "open_entries": len(self._open_entries),
            "settlements": self.total_settlements,
            "settlement_wins": self.settlement_wins,
            "settlement_losses": self.settlement_losses,
            "settlement_pnl": round(self.settlement_pnl, 2),
        }
