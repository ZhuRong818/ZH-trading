"""
Resolution Fade Strategy

Core idea: As a market approaches its resolution date, prices near the
extremes (>0.85 or <0.15) become increasingly "locked in". Traders panic
and pay premiums to exit. We provide liquidity to these panicking traders
and earn the premium.

Why this has low drawdown:
1. Maximum loss per trade is bounded. Buying NO at $0.05 can only lose $0.05.
2. You're trading with time decay on your side — as resolution approaches,
   certainty increases, and extreme prices get more extreme (in your favor).
3. You're the liquidity provider, not the liquidity taker. You're earning
   the spread between panic sellers and fair value.

Three sub-strategies:

A. "Certainty Fade" (primary):
   When a market is >0.85 with >3 days left, the YES price includes a
   "uncertainty premium" — buy YES because it should drift toward 1.0
   as resolution approaches and uncertainty shrinks.
   Mirror: when <0.15 with >3 days left, buy NO (sell YES).

B. "Last-Minute Liquidity":
   In the final 24 hours, spreads widen because MMs pull out. Post tight
   quotes to earn the widened spread.

C. "Resolution Convergence":
   Markets approaching resolution with price 0.90+ are very likely YES.
   Buy YES and hold to resolution for the last 5-10% of upside.
   Only when days_left < 3 and price > 0.90.

Position sizing: Kelly criterion with extra-conservative 0.15x multiplier.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from data_pipeline.market_data import MarketDataFeed
from ems.execution import ExecutionEngine
from oms.position_manager import PositionManager
from strategies.kelly import kelly_size

log = logging.getLogger(__name__)


@dataclass
class ResolutionFadeConfig:
    # Certainty fade
    certainty_min_price: float = 0.82     # minimum YES price to trigger
    certainty_max_price: float = 0.97     # don't buy above this (too expensive)
    certainty_min_days_left: float = 3.0  # need at least 3 days left
    certainty_fair_premium: float = 0.03  # assume fair is 3% above market

    # Last-minute liquidity
    last_minute_hours: float = 24.0       # activate within final 24h
    last_minute_spread: float = 0.04      # our spread in last-minute mode
    last_minute_size: float = 10.0

    # Resolution convergence
    convergence_max_days: float = 3.0     # final 3 days
    convergence_min_price: float = 0.90   # need 90%+ certainty
    convergence_fair_boost: float = 0.05  # assume fair is 5% above market

    # Sizing
    kelly_fraction: float = 0.15          # very conservative 0.15x Kelly
    max_bet_pct: float = 0.02             # max 2% of bankroll per trade
    min_edge: float = 0.02
    bankroll: float = 10_000.0

    # Risk
    max_positions: int = 5                # max concurrent positions


@dataclass
class FadePosition:
    token_id: str
    sub_strategy: str   # "certainty", "convergence"
    side: str
    entry_price: float
    size: float
    days_left_at_entry: float
    entry_time: float


class ResolutionFadeStrategy:
    """
    Trades time decay and certainty premium in markets approaching resolution.
    """

    def __init__(
        self,
        config: ResolutionFadeConfig,
        data_feed: MarketDataFeed,
        ems: ExecutionEngine,
        oms: PositionManager,
        markets: list[dict],  # [{token_id, end_date, tick_size, neg_risk, question}]
    ):
        self.config = config
        self.data = data_feed
        self.ems = ems
        self.oms = oms
        self.markets = markets
        self._positions: dict[str, FadePosition] = {}
        self.total_trades = 0

    def _days_left(self, end_date: str) -> float:
        """Days until market resolution."""
        if not end_date:
            return 999.0
        try:
            end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return max((end - now).total_seconds() / 86400, 0)
        except Exception:
            return 999.0

    def step(self):
        """One iteration across all tracked markets."""
        for mkt in self.markets:
            token_id = mkt["token_id"]
            end_date = mkt.get("end_date", "")
            days_left = self._days_left(end_date)

            # Fetch book
            book = self.data.fetch_order_book(token_id)
            mid = book.mid
            if mid is None:
                continue

            # Already have a position in this market?
            if token_id in self._positions:
                self._manage_position(token_id, mid, days_left)
                continue

            # Max positions check
            if len(self._positions) >= self.config.max_positions:
                continue

            # Try each sub-strategy
            self._check_certainty_fade(mkt, mid, days_left)
            self._check_convergence(mkt, mid, days_left)
            self._check_last_minute_liquidity(mkt, mid, days_left)

    # ---- Sub-Strategy A: Certainty Fade ----

    def _check_certainty_fade(self, mkt: dict, mid: float, days_left: float):
        """
        When price is 0.82-0.97 and >3 days left, the market is paying an
        uncertainty premium. Buy YES — it should drift toward 1.0.
        Mirror for low prices.
        """
        cfg = self.config
        token_id = mkt["token_id"]

        if days_left < cfg.certainty_min_days_left:
            return

        if cfg.certainty_min_price <= mid <= cfg.certainty_max_price:
            # High price — buy YES (expect drift to 1.0)
            fair = min(mid + cfg.certainty_fair_premium, 0.99)
            self._enter_fade(mkt, mid, fair, "BUY", "certainty", days_left)

        elif (1 - cfg.certainty_max_price) <= mid <= (1 - cfg.certainty_min_price):
            # Low price (0.03-0.18) — sell YES / buy NO (expect drift to 0.0)
            fair = max(mid - cfg.certainty_fair_premium, 0.01)
            self._enter_fade(mkt, mid, fair, "SELL", "certainty", days_left)

    # ---- Sub-Strategy B: Last-Minute Liquidity ----

    def _check_last_minute_liquidity(self, mkt: dict, mid: float, days_left: float):
        """
        Final 24 hours — MMs pull out, spreads widen. Post tight quotes.
        This is essentially market-making but only near resolution.
        """
        cfg = self.config
        token_id = mkt["token_id"]

        hours_left = days_left * 24
        if hours_left > cfg.last_minute_hours or hours_left < 1:
            return

        # Don't do this if price is extreme (let convergence handle it)
        if mid > 0.90 or mid < 0.10:
            return

        tick = mkt.get("tick_size", "0.01")
        neg = mkt.get("neg_risk", False)
        half_spread = cfg.last_minute_spread / 2

        self.ems.place_order(
            token_id=token_id, side="BUY",
            price=mid - half_spread, size=cfg.last_minute_size,
            tick_size=tick, neg_risk=neg,
            source="fade_lastmin",
        )
        self.ems.place_order(
            token_id=token_id, side="SELL",
            price=mid + half_spread, size=cfg.last_minute_size,
            tick_size=tick, neg_risk=neg,
            source="fade_lastmin",
        )

    # ---- Sub-Strategy C: Resolution Convergence ----

    def _check_convergence(self, mkt: dict, mid: float, days_left: float):
        """
        Final 3 days + price >0.90 = very likely YES. Buy and hold to resolution.
        Maximum loss = 1 - price (e.g., buy at 0.92, max loss = $0.08/share).
        Expected gain = 1.0 - 0.92 = $0.08/share if it resolves YES.
        """
        cfg = self.config
        token_id = mkt["token_id"]

        if days_left > cfg.convergence_max_days:
            return

        if mid >= cfg.convergence_min_price:
            # Buy YES — expect resolution at $1.0
            fair = min(mid + cfg.convergence_fair_boost, 0.99)
            self._enter_fade(mkt, mid, fair, "BUY", "convergence", days_left)

        elif mid <= (1 - cfg.convergence_min_price):
            # Price <0.10, buy NO / sell YES — expect resolution at $0.0
            fair = max(mid - cfg.convergence_fair_boost, 0.01)
            self._enter_fade(mkt, mid, fair, "SELL", "convergence", days_left)

    # ---- Trade Execution ----

    def _enter_fade(
        self, mkt: dict, mid: float, fair: float,
        side: str, sub_strategy: str, days_left: float,
    ):
        """Enter a fade trade with Kelly sizing."""
        token_id = mkt["token_id"]
        cfg = self.config

        if token_id in self._positions:
            return

        kelly = kelly_size(
            fair_prob=fair if side == "BUY" else 1 - fair,
            market_price=mid if side == "BUY" else 1 - mid,
            bankroll=cfg.bankroll,
            kelly_fraction=cfg.kelly_fraction,
            max_bet_pct=cfg.max_bet_pct,
            min_edge=cfg.min_edge,
        )

        if kelly.direction == "NONE" or kelly.size_usdc < 1:
            return

        size_shares = kelly.size_usdc / mid if mid > 0 else 0
        if size_shares < 1:
            return

        tick = mkt.get("tick_size", "0.01")
        neg = mkt.get("neg_risk", False)

        order_id = self.ems.place_order(
            token_id=token_id, side=side,
            price=mid, size=size_shares,
            tick_size=tick, neg_risk=neg,
            source=f"fade_{sub_strategy}",
        )

        if order_id:
            self._positions[token_id] = FadePosition(
                token_id=token_id,
                sub_strategy=sub_strategy,
                side=side,
                entry_price=mid,
                size=size_shares,
                days_left_at_entry=days_left,
                entry_time=time.time(),
            )
            self.total_trades += 1

            log.info(
                "FADE ENTRY [%s]: %s %s %.1f @ %.4f | fair=%.4f days_left=%.1f kelly=$%.0f",
                sub_strategy, side, token_id[:16], size_shares, mid,
                fair, days_left, kelly.size_usdc,
            )

    def _manage_position(self, token_id: str, mid: float, days_left: float):
        """Manage existing fade positions."""
        pos = self._positions.get(token_id)
        if not pos:
            return

        # Convergence positions: hold to resolution
        if pos.sub_strategy == "convergence":
            if days_left <= 0:
                self._close_position(token_id, mid, reason="resolved")
            return

        # Certainty fade: exit if price reversed too much (stop-loss)
        if pos.sub_strategy == "certainty":
            if pos.side == "BUY":
                # Stop if price drops more than 5% below entry
                if mid < pos.entry_price - 0.05:
                    self._close_position(token_id, mid, reason="stop_loss")
                    return
                # Take profit if price rose by our expected premium
                if mid > pos.entry_price + self.config.certainty_fair_premium:
                    self._close_position(token_id, mid, reason="take_profit")
                    return
            elif pos.side == "SELL":
                if mid > pos.entry_price + 0.05:
                    self._close_position(token_id, mid, reason="stop_loss")
                    return
                if mid < pos.entry_price - self.config.certainty_fair_premium:
                    self._close_position(token_id, mid, reason="take_profit")
                    return

    def _close_position(self, token_id: str, price: float, reason: str):
        """Close a fade position."""
        pos = self._positions.pop(token_id, None)
        if not pos:
            return

        close_side = "SELL" if pos.side == "BUY" else "BUY"
        mkt = next((m for m in self.markets if m["token_id"] == token_id), {})
        tick = mkt.get("tick_size", "0.01")
        neg = mkt.get("neg_risk", False)

        pnl = (price - pos.entry_price) * pos.size if pos.side == "BUY" else (pos.entry_price - price) * pos.size

        self.ems.place_order(
            token_id=token_id, side=close_side,
            price=price, size=pos.size,
            tick_size=tick, neg_risk=neg,
            source=f"fade_{pos.sub_strategy}_exit",
        )

        log.info(
            "FADE EXIT [%s/%s]: %s %.1f @ %.4f | entry=%.4f pnl=$%.2f held=%.0fs",
            pos.sub_strategy, reason, close_side, pos.size, price,
            pos.entry_price, pnl, time.time() - pos.entry_time,
        )

    def status(self) -> dict:
        return {
            "active_positions": len(self._positions),
            "total_trades": self.total_trades,
            "positions": {
                tid: {
                    "sub_strategy": p.sub_strategy,
                    "side": p.side,
                    "entry": p.entry_price,
                    "days_at_entry": p.days_left_at_entry,
                }
                for tid, p in self._positions.items()
            },
        }
