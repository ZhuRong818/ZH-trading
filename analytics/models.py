"""
Data models for post-session analysis.

TradeRecord captures the full round-trip lifecycle of a trade:
entry context → execution → exit → outcome.

MarketSnapshot captures market state at a point in time
for regime analysis.
"""

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TradeRecord:
    """Complete round-trip trade record for post-session analysis."""

    # Identity
    trade_id: str = ""
    token_id: str = ""
    strategy: str = ""

    # Entry
    entry_time: float = 0.0
    entry_price: float = 0.0
    entry_size: float = 0.0
    entry_side: str = ""           # BUY or SELL
    entry_edge: float = 0.0        # estimated edge at entry
    entry_fair_value: float = 0.0  # model's fair value at entry

    # Market context at entry
    entry_mid: float = 0.0
    entry_spread: float = 0.0
    entry_bid_depth: float = 0.0
    entry_ask_depth: float = 0.0
    entry_volume_24h: float = 0.0
    entry_regime: str = ""         # tail / contested / trending
    entry_hours_to_resolution: float = 0.0

    # Exit
    exit_time: float = 0.0
    exit_price: float = 0.0
    exit_size: float = 0.0
    exit_reason: str = ""          # target_hit / stop_loss / regime_exit / kill_switch / manual

    # Execution quality
    expected_fill_price: float = 0.0
    actual_fill_price: float = 0.0
    slippage: float = 0.0          # actual - expected (positive = worse)
    fees_paid: float = 0.0

    # Outcome
    pnl: float = 0.0
    pnl_pct: float = 0.0          # pnl / notional
    hold_time_seconds: float = 0.0

    # Status
    is_open: bool = True
    is_complete: bool = False

    @property
    def notional(self) -> float:
        return self.entry_size * self.entry_price

    def close(self, exit_price: float, exit_size: float, exit_reason: str):
        """Mark trade as closed and compute outcome."""
        self.exit_time = time.time()
        self.exit_price = exit_price
        self.exit_size = exit_size
        self.exit_reason = exit_reason
        self.hold_time_seconds = self.exit_time - self.entry_time
        self.is_open = False
        self.is_complete = True

        if self.entry_side == "BUY":
            self.pnl = (exit_price - self.entry_price) * self.entry_size - self.fees_paid
        else:
            self.pnl = (self.entry_price - exit_price) * self.entry_size - self.fees_paid

        self.pnl_pct = self.pnl / self.notional * 100 if self.notional > 0 else 0

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "token_id": self.token_id[:20],
            "strategy": self.strategy,
            "side": self.entry_side,
            "entry_time": self.entry_time,
            "entry_price": round(self.entry_price, 6),
            "exit_price": round(self.exit_price, 6),
            "size": round(self.entry_size, 2),
            "pnl": round(self.pnl, 4),
            "pnl_pct": round(self.pnl_pct, 2),
            "hold_time_s": round(self.hold_time_seconds, 1),
            "exit_reason": self.exit_reason,
            "slippage": round(self.slippage, 6),
            "entry_edge": round(self.entry_edge, 4),
            "entry_regime": self.entry_regime,
            "entry_spread": round(self.entry_spread, 4),
            "hours_to_resolution": round(self.entry_hours_to_resolution, 1),
            "is_complete": self.is_complete,
        }


@dataclass
class MarketSnapshot:
    """Point-in-time market state for regime analysis."""
    timestamp: float = 0.0
    token_id: str = ""
    mid: float = 0.0
    spread: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    bid_depth_5: float = 0.0
    ask_depth_5: float = 0.0
    volatility: float = 0.0
    regime: str = ""

    def to_dict(self) -> dict:
        return {
            "ts": round(self.timestamp, 1),
            "token": self.token_id[:20],
            "mid": round(self.mid, 4),
            "spread": round(self.spread, 4),
            "bid_depth": round(self.bid_depth_5, 0),
            "ask_depth": round(self.ask_depth_5, 0),
            "vol": round(self.volatility, 6),
            "regime": self.regime,
        }


@dataclass
class StrategySnapshot:
    """Point-in-time strategy state."""
    timestamp: float = 0.0
    strategy: str = ""
    state: dict = field(default_factory=dict)
