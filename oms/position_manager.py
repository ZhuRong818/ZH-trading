"""
Module 3 — Order Management System (OMS)

Single source of truth for all positions. Tracks token lifecycles,
computes real P&L, and reconciles on-chain vs off-chain state.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

from config import DATA_API_BASE

log = logging.getLogger(__name__)


@dataclass
class Position:
    token_id: str
    condition_id: str
    outcome: str
    size: float = 0.0
    avg_price: float = 0.0
    cur_price: float = 0.0
    realized_pnl: float = 0.0

    @property
    def unrealized_pnl(self) -> float:
        if self.size == 0:
            return 0.0
        return self.size * (self.cur_price - self.avg_price)

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def notional(self) -> float:
        return abs(self.size * self.avg_price)


@dataclass
class Fill:
    token_id: str
    side: str
    size: float
    price: float
    timestamp: float
    order_id: str = ""
    source: str = ""  # which strategy generated this
    edge: float = 0.0       # estimated edge at time of signal
    fair_value: float = 0.0  # model's fair value at time of signal


class PositionManager:
    """
    Tracks all positions and fills. Updates from trade events.
    In production: backed by SQLAlchemy + TimescaleDB.
    """

    def __init__(self):
        self.positions: Dict[str, Position] = {}  # keyed by token_id
        self.fills: List[Fill] = []
        self.total_realized_pnl: float = 0.0

    def get_position(self, token_id: str) -> Optional[Position]:
        return self.positions.get(token_id)

    def net_position_usdc(self, token_id: str) -> float:
        pos = self.positions.get(token_id)
        if not pos:
            return 0.0
        return pos.size * pos.cur_price

    def total_exposure_usdc(self) -> float:
        return sum(abs(p.notional) for p in self.positions.values())

    def record_fill(self, fill: Fill) -> float:
        """Update position state from a fill event. Returns realized PnL."""
        self.fills.append(fill)
        pos = self.positions.get(fill.token_id)

        if pos is None:
            pos = Position(
                token_id=fill.token_id,
                condition_id="",
                outcome="",
            )
            self.positions[fill.token_id] = pos

        realized = 0.0

        if fill.side == "BUY":
            # Increase position
            total_cost = pos.avg_price * pos.size + fill.price * fill.size
            pos.size += fill.size
            pos.avg_price = total_cost / pos.size if pos.size > 0 else 0.0
            # Seed mark price so risk checks don't see a zero price on new positions
            pos.cur_price = fill.price
        elif fill.side == "SELL":
            if pos.size > 0:
                # Realize PnL on the sold portion
                realized = fill.size * (fill.price - pos.avg_price)
                pos.realized_pnl += realized
                self.total_realized_pnl += realized
                pos.size -= fill.size
                if pos.size <= 0:
                    pos.size = 0
                    pos.avg_price = 0

        log.info(
            "FILL %s %s %.1f @ %.4f | pos=%.1f avg=%.4f pnl=%.2f",
            fill.side, fill.token_id[:12] + "...", fill.size, fill.price,
            pos.size, pos.avg_price, pos.total_pnl,
        )

        return realized

    def update_mark_prices(self, token_prices: Dict[str, float]):
        """Update current market prices for all positions."""
        for token_id, price in token_prices.items():
            if token_id in self.positions:
                self.positions[token_id].cur_price = price

    def check_mergeable(self, condition_id: str, yes_token: str, no_token: str) -> bool:
        """Check if equal YES + NO holdings can be merged back to USDC."""
        yes_pos = self.positions.get(yes_token)
        no_pos = self.positions.get(no_token)
        if yes_pos and no_pos:
            return yes_pos.size > 0 and yes_pos.size == no_pos.size
        return False

    def get_all_open(self) -> List[Position]:
        return [p for p in self.positions.values() if p.size > 0]

    def portfolio_summary(self) -> dict:
        positions = self.get_all_open()
        total_unrealized = sum(p.unrealized_pnl for p in positions)
        total_notional = sum(p.notional for p in positions)
        return {
            "num_positions": len(positions),
            "total_notional_usdc": total_notional,
            "unrealized_pnl": total_unrealized,
            "realized_pnl": self.total_realized_pnl,
            "total_pnl": total_unrealized + self.total_realized_pnl,
        }

    # ---- Reconciliation with Polymarket Data API ----

    def sync_from_api(self, proxy_wallet: str):
        """
        Pull positions from Polymarket Data API and reconcile.
        This is the on-chain truth — use to detect drift.
        """
        try:
            resp = requests.get(
                f"{DATA_API_BASE}/positions",
                params={"user": proxy_wallet, "sizeThreshold": 0, "limit": 500},
            )
            resp.raise_for_status()
            api_positions = resp.json()

            for ap in api_positions:
                token_id = ap.get("asset", "")
                if not token_id:
                    continue

                pos = self.positions.get(token_id)
                api_size = float(ap.get("size", 0))
                api_avg = float(ap.get("avgPrice", 0))
                api_cur = float(ap.get("curPrice", 0))

                if pos is None and api_size > 0:
                    # Position exists on-chain but not tracked locally
                    self.positions[token_id] = Position(
                        token_id=token_id,
                        condition_id=ap.get("conditionId", ""),
                        outcome=ap.get("outcome", ""),
                        size=api_size,
                        avg_price=api_avg,
                        cur_price=api_cur,
                    )
                    log.warning("Reconcile: found untracked position %s size=%.1f", token_id[:16], api_size)
                elif pos is not None and abs(pos.size - api_size) > 0.01:
                    log.warning(
                        "Reconcile: size mismatch for %s local=%.1f api=%.1f",
                        token_id[:16], pos.size, api_size,
                    )
                    # Trust on-chain
                    pos.size = api_size
                    pos.avg_price = api_avg
                    pos.cur_price = api_cur

            log.info("Reconciliation complete: %d positions synced", len(api_positions))
        except Exception as e:
            log.error("Reconciliation failed: %s", e)
