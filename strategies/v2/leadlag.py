"""
Cross-Asset Lead-Lag Strategy — unified interface (v2).

Exploits the lag between BTC price moves and alt-asset Polymarket markets.
When BTC moves sharply, ETH/SOL/XRP Polymarket odds lag by 1-5 seconds.

How it works:
  1. Polls LEADER (BTC) price from Binance every 0.5 seconds
  2. Detects sharp moves (> threshold) in the last few ticks
  3. Checks if FOLLOWER (ETH/SOL/XRP) Polymarket odds haven't adjusted yet
  4. If BTC drops but follower DOWN token is still cheap → BUY DOWN
  5. Holds to settlement

The edge: BTC moves first, alt markets follow. You're trading the
propagation delay, not predicting direction.

Backtested results (22h real data, bps=5.0, staleness=0.10):
  XRP: 75% WR, +$6,232 | SOL: 70% WR, +$3,308 | ETH: 44% WR, +$162
  Combined: 60.5% WR, +$9,702 on 38 trades

Key parameters:
  - leader: asset to watch for moves (default: btc)
  - move_threshold_bps: minimum leader move to trigger (default: 5.0)
  - staleness_threshold: how much follower must lag behind fair value (default: 0.10)
  - correlation_discount: dampen fair value for imperfect correlation (default: 0.90)
"""

import logging
import time
from collections import deque
from typing import List, Optional

import requests

from strategies.base import BaseStrategy
from strategies.kelly import kelly_size
from data_pipeline.market_provider import MarketContext
from pipeline.signal import TradingSignal

log = logging.getLogger(__name__)

BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"


class LeadLag(BaseStrategy):
    name = "leadlag"

    def __init__(
        self,
        leader: str = "btc",
        follower: str = "eth",
        move_threshold_bps: float = 5.0,
        staleness_threshold: float = 0.10,
        lookback_ticks: int = 5,
        max_price: float = 0.55,
        min_price: float = 0.20,
        min_remaining_seconds: float = 60,
        kelly_frac: float = 0.25,
        max_bet_pct: float = 0.05,
        bankroll: float = 10_000,
        cooldown_seconds: float = 15.0,
        max_notional_usdc: float = 500.0,
        correlation_discount: float = 0.90,
    ):
        self.leader = leader
        self.follower = follower
        self.move_threshold_bps = move_threshold_bps
        self.staleness_threshold = staleness_threshold
        self.lookback_ticks = lookback_ticks
        self.max_price = max_price
        self.min_price = min_price
        self.min_remaining = min_remaining_seconds
        self.kelly_frac = kelly_frac
        self.max_bet_pct = max_bet_pct
        self.bankroll = bankroll
        self.cooldown_seconds = cooldown_seconds
        self.max_notional_usdc = max_notional_usdc
        self.correlation_discount = correlation_discount

        self._leader_prices: deque = deque(maxlen=200)
        self._session = requests.Session()
        self._has_position = False
        self._last_trade_time = 0.0

        # Stats
        self.total_trades = 0
        self.signals_detected = 0
        self.signals_stale = 0
        self.signals_already_priced = 0

    def on_fill(self, fill):
        if fill.side == "BUY":
            self._has_position = True
        elif fill.side == "SELL":
            self._has_position = False

    def on_cancel(self):
        self._has_position = False

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        """
        Step receives FOLLOWER market contexts.
        Leader price is polled internally.
        """
        ctx_map = {c.token_id: c for c in contexts if c and c.is_valid}

        # Poll LEADER price
        self._poll_leader()

        if self._has_position:
            return []

        if len(self._leader_prices) < self.lookback_ticks + 1:
            return []

        # Cooldown
        if time.time() - self._last_trade_time < self.cooldown_seconds:
            return []

        # Detect sharp LEADER move
        move = self._detect_leader_move()
        if move is None:
            return []

        direction, move_bps, fair_prob_up = move

        # Check follower contexts for staleness
        for ctx in ctx_map.values():
            if ctx.seconds_remaining < self.min_remaining:
                continue
            s = self._check_follower_staleness(ctx, ctx_map, direction, fair_prob_up, move_bps)
            if s:
                self._has_position = True
                self._last_trade_time = time.time()
                return [s]

        return []

    def _poll_leader(self):
        """Poll the LEADER asset's price from Binance."""
        try:
            symbol = f"{self.leader.upper()}USDT"
            resp = self._session.get(BINANCE_TICKER, params={"symbol": symbol}, timeout=3)
            price = float(resp.json()["price"])
            self._leader_prices.append((time.time(), price))
        except Exception:
            pass

    def _detect_leader_move(self) -> Optional[tuple]:
        """Detect a sharp move in the LEADER asset."""
        if len(self._leader_prices) < self.lookback_ticks + 1:
            return None

        current_time, current_price = self._leader_prices[-1]
        old_time, old_price = self._leader_prices[-self.lookback_ticks - 1]

        if old_price <= 0:
            return None

        move_pct = (current_price - old_price) / old_price
        move_bps = abs(move_pct) * 10_000

        if move_bps < self.move_threshold_bps:
            return None

        self.signals_detected += 1

        # Convert leader move to fair probability, dampened by correlation
        prob_shift = min(move_bps / 50, 0.35) * self.correlation_discount

        if move_pct > 0:
            direction = "UP"
            fair_prob_up = 0.50 + prob_shift
        else:
            direction = "DOWN"
            fair_prob_up = 0.50 - prob_shift

        return direction, move_bps, fair_prob_up

    def _check_follower_staleness(
        self,
        ctx: MarketContext,
        ctx_map: dict[str, MarketContext],
        direction: str,
        fair_prob_up: float,
        move_bps: float,
    ) -> Optional[TradingSignal]:
        """Check if FOLLOWER Polymarket odds are stale relative to leader move."""
        up_ctx, down_ctx = self._resolve_up_down_contexts(ctx, ctx_map)
        if not up_ctx or not down_ctx:
            return None

        market_up = up_ctx.best_ask or 0.0
        market_down = down_ctx.best_ask or 0.0
        if market_up <= 0 or market_down <= 0:
            return None

        if direction == "UP":
            fair = fair_prob_up
            market_price = market_up
            token_id = up_ctx.token_id
            staleness = fair - market_price
        else:
            fair = 1.0 - fair_prob_up
            market_price = market_down
            token_id = down_ctx.token_id
            staleness = fair - market_price

        # Is the follower stale enough?
        if staleness < self.staleness_threshold:
            self.signals_already_priced += 1
            log.debug(
                "LEADLAG [%s→%s]: %s move %.1fbps but follower already priced (stale=%.4f < %.4f)",
                self.leader, self.follower, direction, move_bps,
                staleness, self.staleness_threshold,
            )
            return None

        self.signals_stale += 1

        # Risk/reward gate
        if market_price > self.max_price or market_price < self.min_price:
            return None

        edge = fair - market_price
        if edge <= 0:
            return None

        # Kelly sizing
        kelly = kelly_size(
            fair_prob=fair,
            market_price=market_price,
            bankroll=self.bankroll,
            kelly_fraction=self.kelly_frac,
            max_bet_pct=self.max_bet_pct,
            min_edge=self.staleness_threshold,
        )
        if kelly.direction == "NONE" or kelly.size_usdc < 5:
            return None

        capped_usdc = min(kelly.size_usdc, self.max_notional_usdc)
        size = capped_usdc / market_price if market_price > 0 else 0

        self.total_trades += 1
        self._last_trade_time = time.time()

        log.info(
            "LEADLAG [%s→%s]: %s %.1f shares @ %.4f ($%.0f) | "
            "leader_move=%.1fbps stale=%.4f fair=%.3f mkt=%.3f edge=%.4f",
            self.leader, self.follower,
            direction, size, market_price, capped_usdc,
            move_bps, staleness, fair, market_price, edge,
        )

        return TradingSignal(
            token_id=token_id,
            side="BUY",
            price=market_price,
            size=size,
            strategy=f"{self.name}_{self.follower}",
            edge=edge,
            fair_value=fair,
            confidence=min(staleness / 0.20, 1.0),
            direction=direction,
            tick_size=ctx.tick_size,
        )

    def _resolve_up_down_contexts(
        self, ctx: MarketContext, ctx_map: dict[str, MarketContext]
    ) -> tuple[Optional[MarketContext], Optional[MarketContext]]:
        up_ctx = None
        down_ctx = None

        q = (ctx.question or "").upper()
        if q.endswith(" UP"):
            up_ctx = ctx
            down_ctx = ctx_map.get(ctx.token_id_other)
        elif q.endswith(" DOWN"):
            down_ctx = ctx
            up_ctx = ctx_map.get(ctx.token_id_other)

        if not up_ctx or not down_ctx:
            other = ctx_map.get(ctx.token_id_other)
            if other:
                up_ctx = up_ctx or ctx
                down_ctx = down_ctx or other

        return up_ctx, down_ctx

    def snapshot(self) -> dict:
        return {
            "leader": self.leader,
            "follower": self.follower,
            "leader_price": self._leader_prices[-1][1] if self._leader_prices else 0,
            "total_trades": self.total_trades,
            "signals_detected": self.signals_detected,
            "signals_stale": self.signals_stale,
            "signals_already_priced": self.signals_already_priced,
            "stale_rate": f"{self.signals_stale / max(self.signals_detected, 1) * 100:.0f}%",
            "has_position": self._has_position,
        }
