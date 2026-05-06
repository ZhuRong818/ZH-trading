"""
Module 6 — Risk Management & Kill Switch

Enforces hard limits at strategy, market, and portfolio level.
Provides kill switch, circuit breakers, and stop-loss automation.
"""

import logging
import time
from typing import List, Optional

import numpy as np

from config import RiskConfig
from data_pipeline.market_data import MarketDataFeed
from ems.execution import ExecutionEngine
from oms.position_manager import PositionManager

log = logging.getLogger(__name__)


class RiskEngine:
    """
    Central risk enforcement. Checked before every strategy step.
    Can halt the entire system if limits are breached.
    """

    def __init__(
        self,
        config: RiskConfig,
        data_feed: MarketDataFeed,
        ems: ExecutionEngine,
        oms: PositionManager,
    ):
        self.config = config
        self.data = data_feed
        self.ems = ems
        self.oms = oms
        self.halted = False
        self._peak_portfolio_value = 0.0
        self._volatility_paused_until: dict = {}  # token_id -> resume_time

    def check_all(self) -> bool:
        """
        Run all risk checks. Returns True if trading is allowed.
        If any check fails, returns False (caller should stop trading).
        """
        if self.halted:
            return False

        if not self.check_drawdown():
            return False

        if not self.check_exposure():
            return False

        if not self.check_position_stops():
            return False

        return True

    # ---- Portfolio-Level Checks ----

    def check_drawdown(self) -> bool:
        """Check if portfolio drawdown exceeds max allowed."""
        summary = self.oms.portfolio_summary()
        total_value = summary["total_notional_usdc"] + summary["total_pnl"]

        if total_value > self._peak_portfolio_value:
            self._peak_portfolio_value = total_value

        if self._peak_portfolio_value > 0:
            drawdown_pct = (
                (self._peak_portfolio_value - total_value) / self._peak_portfolio_value * 100
            )
            if drawdown_pct > self.config.max_drawdown_pct:
                log.critical(
                    "DRAWDOWN BREACH: %.1f%% > %.1f%% max | peak=$%.0f current=$%.0f",
                    drawdown_pct, self.config.max_drawdown_pct,
                    self._peak_portfolio_value, total_value,
                )
                self.kill_switch("Max drawdown exceeded")
                return False

        return True

    def check_exposure(self) -> bool:
        """Check total portfolio exposure."""
        total = self.oms.total_exposure_usdc()
        if total > self.config.max_total_exposure_usdc:
            log.warning(
                "EXPOSURE BREACH: $%.0f > $%.0f max",
                total, self.config.max_total_exposure_usdc,
            )
            return False
        return True

    # ---- Per-Position Checks ----

    def check_position_stops(self) -> bool:
        """Check individual position stop-losses."""
        all_ok = True
        for pos in self.oms.get_all_open():
            if pos.avg_price <= 0 or pos.size <= 0:
                continue

            loss_pct = -pos.unrealized_pnl / pos.notional * 100 if pos.notional > 0 else 0

            if loss_pct > self.config.stop_loss_pct:
                log.warning(
                    "STOP LOSS: %s loss=%.1f%% > %.1f%% | closing position",
                    pos.token_id[:16], loss_pct, self.config.stop_loss_pct,
                )
                # Close position by selling
                book = self.data.get_book(pos.token_id)
                if book and book.best_bid:
                    self.ems.place_order(
                        pos.token_id, "SELL", book.best_bid, pos.size,
                        order_type="FOK",
                        source="stop_loss",
                    )
                all_ok = False

        return all_ok

    def check_position_size(self, token_id: str, additional_usdc: float) -> bool:
        """Pre-trade check: would this trade exceed per-market limits?"""
        pos = self.oms.get_position(token_id)
        current = pos.notional if pos else 0
        if current + additional_usdc > self.config.max_position_size_usdc:
            log.warning(
                "Position size check failed: current=$%.0f + new=$%.0f > max=$%.0f",
                current, additional_usdc, self.config.max_position_size_usdc,
            )
            return False
        return True

    def check_concentration(self, token_id: str) -> bool:
        """Check if a single position is too large relative to portfolio."""
        pos = self.oms.get_position(token_id)
        if not pos:
            return True
        total = self.oms.total_exposure_usdc()
        if total <= 0:
            return True
        concentration = pos.notional / total * 100
        if concentration > self.config.max_concentration_pct:
            log.warning(
                "Concentration check failed: %s at %.1f%% > %.1f%% max",
                token_id[:16], concentration, self.config.max_concentration_pct,
            )
            return False
        return True

    # ---- Circuit Breaker ----

    def check_circuit_breaker(self, token_id: str) -> bool:
        """Pause trading if volatility spikes."""
        # Check if currently paused
        resume_time = self._volatility_paused_until.get(token_id, 0)
        if time.time() < resume_time:
            return False

        sigma = self.data.rolling_volatility(token_id, window=30)
        if sigma > self.config.volatility_pause_threshold:
            pause_until = time.time() + 60  # pause for 60 seconds
            self._volatility_paused_until[token_id] = pause_until
            log.warning(
                "CIRCUIT BREAKER: %s sigma=%.4f > %.4f | pausing 60s",
                token_id[:16], sigma, self.config.volatility_pause_threshold,
            )
            return False

        return True

    # ---- Kill Switch ----

    def kill_switch(self, reason: str):
        """
        Emergency shutdown. Cancels ALL orders and marks system as halted.
        Requires manual restart.
        """
        log.critical("KILL SWITCH ACTIVATED: %s", reason)
        self.halted = True

        # Cancel all orders
        self.ems.cancel_all()

        # Close all positions at market
        for pos in self.oms.get_all_open():
            book = self.data.get_book(pos.token_id)
            if book and book.best_bid and pos.size > 0:
                self.ems.place_order(
                    pos.token_id, "SELL", book.best_bid, pos.size,
                    order_type="FAK",
                    source="kill_switch",
                )

        log.critical("Kill switch complete. System HALTED. Manual restart required.")

    def reset(self):
        """Manual reset after kill switch."""
        self.halted = False
        self._volatility_paused_until.clear()
        log.info("Risk engine reset. Trading resumed.")

    def status(self) -> dict:
        summary = self.oms.portfolio_summary()
        return {
            "halted": self.halted,
            "total_exposure_usdc": summary["total_notional_usdc"],
            "total_pnl": summary["total_pnl"],
            "peak_value": self._peak_portfolio_value,
            "num_positions": summary["num_positions"],
            "open_orders": len(self.ems.open_order_ids),
        }
