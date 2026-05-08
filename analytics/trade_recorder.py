"""
Trade Recorder — builds TradeRecords from fill events.

Tracks open entries and matches them with exits to create
complete round-trip TradeRecords for post-session analysis.
"""

import logging
import time
from typing import Dict, List, Optional

from analytics.models import TradeRecord
from data_pipeline.market_data import MarketDataFeed
from oms.position_manager import Fill

log = logging.getLogger(__name__)

_trade_counter = 0


def _next_trade_id() -> str:
    global _trade_counter
    _trade_counter += 1
    return f"T{_trade_counter:05d}"


class TradeRecorder:

    def __init__(self, data_feed: MarketDataFeed):
        self.data = data_feed
        self._open_trades: Dict[str, TradeRecord] = {}  # token_id -> TradeRecord
        self.completed_trades: List[TradeRecord] = []

    def on_fill(self, fill: Fill):
        """
        Process a fill event. Opens new trade on BUY, closes on SELL
        (or vice versa for short entries).
        """
        token_id = fill.token_id
        existing = self._open_trades.get(token_id)

        if existing is None:
            # New entry
            self._open_entry(fill)
        elif existing.entry_side == fill.side:
            # Adding to existing position — update size/avg price
            total_cost = existing.entry_price * existing.entry_size + fill.price * fill.size
            existing.entry_size += fill.size
            existing.entry_price = total_cost / existing.entry_size
        else:
            # Opposite side — this is an exit
            self._close_trade(existing, fill)

    def _open_entry(self, fill: Fill):
        """Create a new TradeRecord from an entry fill."""
        book = self.data.get_book(fill.token_id)

        record = TradeRecord(
            trade_id=_next_trade_id(),
            token_id=fill.token_id,
            strategy=fill.source,
            entry_time=fill.timestamp,
            entry_price=fill.price,
            entry_size=fill.size,
            entry_side=fill.side,
            actual_fill_price=fill.price,
            entry_edge=fill.edge,
            entry_fair_value=fill.fair_value,
        )

        # Capture market context at entry
        if book:
            record.entry_mid = book.mid or fill.price
            record.entry_spread = book.spread or 0
            record.entry_bid_depth = book.depth("BUY", 5)
            record.entry_ask_depth = book.depth("SELL", 5)
            record.entry_regime = MarketDataFeed.classify_regime(book.mid or fill.price)

            # Compute slippage
            if fill.side == "BUY" and book.best_ask:
                record.expected_fill_price = book.best_ask
                record.slippage = fill.price - book.best_ask
            elif fill.side == "SELL" and book.best_bid:
                record.expected_fill_price = book.best_bid
                record.slippage = book.best_bid - fill.price

        vol = self.data.rolling_volatility(fill.token_id)
        record.entry_spread = book.spread if book and book.spread else 0

        self._open_trades[fill.token_id] = record

    def _close_trade(self, record: TradeRecord, fill: Fill):
        """Close an open trade with an exit fill."""
        exit_size = min(fill.size, record.entry_size)

        # Determine exit reason from fill source
        exit_reason = "unknown"
        src = fill.source.lower()
        if "stop_loss" in src:
            exit_reason = "stop_loss"
        elif "target" in src or "exit" in src:
            exit_reason = "target_hit"
        elif "kill" in src:
            exit_reason = "kill_switch"
        elif "unwind" in src:
            exit_reason = "unwind"
        elif "fade" in src and "exit" in src:
            exit_reason = "take_profit"
        else:
            exit_reason = "signal_exit"

        record.close(fill.price, exit_size, exit_reason)
        self.completed_trades.append(record)

        # Remove from open trades
        if fill.token_id in self._open_trades:
            if fill.size >= record.entry_size:
                del self._open_trades[fill.token_id]
            else:
                # Partial close — keep remainder open
                remaining = record.entry_size - fill.size
                self._open_trades[fill.token_id] = TradeRecord(
                    trade_id=_next_trade_id(),
                    token_id=fill.token_id,
                    strategy=record.strategy,
                    entry_time=record.entry_time,
                    entry_price=record.entry_price,
                    entry_size=remaining,
                    entry_side=record.entry_side,
                    entry_mid=record.entry_mid,
                    entry_spread=record.entry_spread,
                    entry_regime=record.entry_regime,
                )

        log.debug(
            "Trade closed: %s %s %s %.1f @ %.4f→%.4f pnl=$%.2f (%s)",
            record.trade_id, record.strategy, record.entry_side,
            record.entry_size, record.entry_price, record.exit_price,
            record.pnl, exit_reason,
        )

    def get_open_trades(self) -> List[TradeRecord]:
        return list(self._open_trades.values())

    def get_all_trades(self) -> List[TradeRecord]:
        return self.completed_trades + self.get_open_trades()
