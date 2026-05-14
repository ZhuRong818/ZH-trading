"""
Thin strategy wrappers — orchestration only.

Each strategy:
  1. Extracts price history + market context
  2. Calls skills in sequence
  3. Returns a TradeDecision or None

The actual logic lives in skills/. Strategies just compose them.
"""

import logging
from typing import List, Optional

import requests

from strategies.base import BaseStrategy
from data_pipeline.market_provider import MarketContext
from pipeline.signal import TradingSignal

from skills.types import PriceFeatures, RollingMarket, TradeDecision
from skills.price_features import PriceFeatureSkill
from skills.fair_value import MomentumFairValueSkill, OracleFairValueSkill, LeadLagFairValueSkill
from skills.edge import EdgeSkill
from skills.risk_gate import RiskGateSkill, RiskGateConfig
from skills.sizing import PositionSizerSkill, SizingConfig
from skills.entry_lock import EntryLock

log = logging.getLogger(__name__)

BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"


class _PriceFeedMixin:
    """Shared BTC price polling. Both strategies inherit this."""

    def _init_feed(self, asset: str = "btc"):
        self._asset = asset
        self._prices: list = []
        self._session = requests.Session()

    def _poll(self):
        try:
            symbol = f"{self._asset.upper()}USDT"
            resp = self._session.get(BINANCE_TICKER, params={"symbol": symbol}, timeout=3)
            self._prices.append(float(resp.json()["price"]))
            if len(self._prices) > 200:
                self._prices = self._prices[-100:]
        except Exception:
            pass

    def _build_market(self, contexts: List[MarketContext]) -> Optional[RollingMarket]:
        """Resolve UP/DOWN contexts into a RollingMarket."""
        ctx_map = {c.token_id: c for c in contexts if c and c.is_valid}
        up_ctx = down_ctx = None

        for ctx in ctx_map.values():
            q = (ctx.question or "").upper()
            if q.endswith(" UP"):
                up_ctx = ctx
            elif q.endswith(" DOWN"):
                down_ctx = ctx

        if not up_ctx or not down_ctx:
            for ctx in ctx_map.values():
                other = ctx_map.get(ctx.token_id_other)
                if other:
                    up_ctx = up_ctx or ctx
                    down_ctx = down_ctx or other
                    break

        if not up_ctx or not down_ctx:
            return None

        return RollingMarket(
            strike_price=up_ctx.strike_price,
            seconds_remaining=up_ctx.seconds_remaining,
            up_price=up_ctx.best_ask or 0.0,
            down_price=down_ctx.best_ask or 0.0,
            up_token_id=up_ctx.token_id,
            down_token_id=down_ctx.token_id,
        )


class MomentumStrategy(BaseStrategy, _PriceFeedMixin):
    """
    Momentum = PriceFeatures + MomentumFairValue + Edge + RiskGate + Sizer
    """
    name = "btc5m_momentum"

    def __init__(self, asset: str = "btc", bankroll: float = 5_000,
                 kelly_frac: float = 0.20, max_bet_pct: float = 0.05,
                 min_edge: float = 0.03, max_price: float = 0.65,
                 cooldown: float = 5.0):
        self._init_feed(asset)
        self.features = PriceFeatureSkill(lookback_ticks=5, momentum_ticks=20)
        self.fair_value = MomentumFairValueSkill()
        self.edge_skill = EdgeSkill()
        self.risk_gate = RiskGateSkill()
        self.risk_config = RiskGateConfig(min_edge=min_edge, max_price=max_price, min_seconds_remaining=30)
        self.sizer = PositionSizerSkill()
        self.sizing_config = SizingConfig(kelly_fraction=kelly_frac, max_bet_pct=max_bet_pct, bankroll=bankroll)
        self.lock = EntryLock(cooldown)
        self.total_trades = 0

    def on_fill(self, fill):
        if fill.side == "BUY":
            self.lock.on_fill_buy()
        elif fill.side == "SELL":
            self.lock.on_fill_sell()

    def on_cancel(self):
        self.lock.on_cancel()

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        self._poll()

        if not self.lock.can_enter():
            return []

        pf = self.features.compute(self._prices)
        if pf is None:
            return []

        market = self._build_market(contexts)
        if market is None or market.seconds_remaining < 30:
            return []

        fair = self.fair_value.estimate(pf, market)
        if fair is None:
            return []

        edge = self.edge_skill.best_edge(fair, market)

        if not self.risk_gate.passes(edge, market, self.risk_config, self.lock.can_enter()):
            return []

        size_usdc = self.sizer.size(edge, self.sizing_config)
        if size_usdc <= 0:
            return []

        size_shares = size_usdc / edge.market_price

        self.lock.on_signal()
        self.total_trades += 1

        log.info("MOMENTUM: %s %.1f @ %.4f edge=%.4f | %s",
                 edge.direction, size_shares, edge.market_price, edge.edge, edge.reason)

        return [TradingSignal(
            token_id=edge.token_id, side="BUY", price=edge.market_price,
            size=size_shares, strategy=self.name, edge=edge.edge,
            fair_value=edge.fair, direction=edge.direction, tick_size="0.01",
        )]

    def snapshot(self) -> dict:
        return {"btc_price": self._prices[-1] if self._prices else 0,
                "total_trades": self.total_trades, "has_position": self.lock.has_position}


class OracleFrontrunStrategy(BaseStrategy, _PriceFeedMixin):
    """
    Oracle = PriceFeatures + move filter + OracleFairValue + Edge + staleness check + RiskGate + Sizer
    """
    name = "oracle_frontrun"

    def __init__(self, asset: str = "btc", bankroll: float = 10_000,
                 kelly_frac: float = 0.25, max_bet_pct: float = 0.05,
                 move_threshold_bps: float = 6.0, staleness_threshold: float = 0.15,
                 max_price: float = 0.55, min_price: float = 0.20,
                 cooldown: float = 10.0, min_remaining: float = 60.0,
                 max_notional_usdc: float = 500.0):
        self._init_feed(asset)
        self.features = PriceFeatureSkill(lookback_ticks=5, momentum_ticks=20)
        self.fair_value = OracleFairValueSkill()
        self.edge_skill = EdgeSkill()
        self.risk_gate = RiskGateSkill()
        self.risk_config = RiskGateConfig(min_edge=staleness_threshold, max_price=max_price, min_price=min_price, min_seconds_remaining=min_remaining)
        self.sizer = PositionSizerSkill()
        self.sizing_config = SizingConfig(kelly_fraction=kelly_frac, max_bet_pct=max_bet_pct, bankroll=bankroll)
        self.lock = EntryLock(cooldown)
        self.move_threshold_bps = move_threshold_bps
        self.staleness_threshold = staleness_threshold
        self.max_notional_usdc = max_notional_usdc
        self.total_trades = 0
        self.signals_detected = 0
        self.signals_stale = 0
        self.signals_already_priced = 0

    def on_fill(self, fill):
        if fill.side == "BUY":
            self.lock.on_fill_buy()
        elif fill.side == "SELL":
            self.lock.on_fill_sell()

    def on_cancel(self):
        self.lock.on_cancel()

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        self._poll()

        if not self.lock.can_enter():
            return []

        pf = self.features.compute(self._prices)
        if pf is None:
            return []

        # Oracle-specific: only act on sharp moves
        if abs(pf.move_bps) < self.move_threshold_bps:
            return []

        self.signals_detected += 1

        market = self._build_market(contexts)
        if market is None or market.seconds_remaining < self.risk_config.min_seconds_remaining:
            return []

        fair = self.fair_value.estimate(pf, market)
        if fair is None:
            return []

        edge = self.edge_skill.best_edge(fair, market)

        # Oracle-specific: staleness check
        if edge.edge < self.staleness_threshold:
            self.signals_already_priced += 1
            return []

        self.signals_stale += 1

        if not self.risk_gate.passes(edge, market, self.risk_config, self.lock.can_enter()):
            return []

        size_usdc = self.sizer.size(edge, self.sizing_config)
        if size_usdc <= 0:
            return []

        # Cap notional to prevent oversized positions on low-price tokens
        capped_usdc = min(size_usdc, self.max_notional_usdc)
        size_shares = capped_usdc / edge.market_price

        self.lock.on_signal()
        self.total_trades += 1

        log.info("ORACLE FRONTRUN: %s %.1f @ %.4f ($%.0f) | move=%.1fbps stale=%.4f edge=%.4f | %s",
                 edge.direction, size_shares, edge.market_price, capped_usdc,
                 pf.move_bps, edge.edge, edge.edge, edge.reason)

        return [TradingSignal(
            token_id=edge.token_id, side="BUY", price=edge.market_price,
            size=size_shares, strategy=self.name, edge=edge.edge,
            fair_value=edge.fair, direction=edge.direction, tick_size="0.01",
        )]

    def snapshot(self) -> dict:
        return {
            "btc_price": self._prices[-1] if self._prices else 0,
            "total_trades": self.total_trades,
            "signals_detected": self.signals_detected,
            "signals_stale": self.signals_stale,
            "signals_already_priced": self.signals_already_priced,
            "has_position": self.lock.has_position,
        }


class LeadLagStrategy(BaseStrategy, _PriceFeedMixin):
    """
    Lead-Lag = Leader PriceFeatures + move filter + LeadLagFairValue
             + Edge + staleness check + RiskGate + Sizer

    Polls the LEADER asset (e.g., BTC) for sharp moves, then trades
    FOLLOWER assets (e.g., ETH, SOL, XRP) whose Polymarket odds
    haven't adjusted yet.

    Architecture:
      - PriceFeedMixin polls the LEADER's Binance price
      - LeadLagFairValueSkill maps leader move → follower fair value
      - The strategy receives FOLLOWER market contexts to trade
      - One EntryLock per instance (one position at a time per follower)

    Usage:
      # Create one instance per follower asset
      eth_leadlag = LeadLagStrategy(leader="btc", follower="eth")
      sol_leadlag = LeadLagStrategy(leader="btc", follower="sol")

    Backtested results (21h real data):
      XRP: 75% WR, +$6,232 | SOL: 67% WR, +$2,785 | ETH: 47% WR, +$680
      Combined: 61% WR, +$9,697 on 36 trades
    """
    name = "leadlag"

    def __init__(
        self,
        leader: str = "btc",
        follower: str = "eth",
        bankroll: float = 10_000,
        kelly_frac: float = 0.25,
        max_bet_pct: float = 0.05,
        move_threshold_bps: float = 5.0,
        staleness_threshold: float = 0.10,
        max_price: float = 0.55,
        min_price: float = 0.20,
        cooldown: float = 15.0,
        min_remaining: float = 60.0,
        max_notional_usdc: float = 500.0,
        correlation_discount: float = 0.90,
    ):
        self._init_feed(leader)  # poll LEADER's price
        self.follower = follower
        self.features = PriceFeatureSkill(lookback_ticks=5, momentum_ticks=20)
        self.fair_value = LeadLagFairValueSkill(
            correlation_discount=correlation_discount,
        )
        self.edge_skill = EdgeSkill()
        self.risk_gate = RiskGateSkill()
        self.risk_config = RiskGateConfig(
            min_edge=staleness_threshold,
            max_price=max_price,
            min_price=min_price,
            min_seconds_remaining=min_remaining,
        )
        self.sizer = PositionSizerSkill()
        self.sizing_config = SizingConfig(
            kelly_fraction=kelly_frac,
            max_bet_pct=max_bet_pct,
            bankroll=bankroll,
        )
        self.lock = EntryLock(cooldown)
        self.move_threshold_bps = move_threshold_bps
        self.staleness_threshold = staleness_threshold
        self.max_notional_usdc = max_notional_usdc
        self.total_trades = 0
        self.signals_detected = 0
        self.signals_stale = 0
        self.signals_already_priced = 0

    def on_fill(self, fill):
        if fill.side == "BUY":
            self.lock.on_fill_buy()
        elif fill.side == "SELL":
            self.lock.on_fill_sell()

    def on_cancel(self):
        self.lock.on_cancel()

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        """
        Step with FOLLOWER market contexts.

        The leader price is polled internally via _PriceFeedMixin.
        Contexts should be for the FOLLOWER asset's Polymarket market.
        """
        # Poll LEADER price (e.g., BTC)
        self._poll()

        if not self.lock.can_enter():
            return []

        # Compute LEADER price features
        leader_features = self.features.compute(self._prices)
        if leader_features is None:
            return []

        # Only act on sharp leader moves
        if abs(leader_features.move_bps) < self.move_threshold_bps:
            return []

        self.signals_detected += 1

        # Build FOLLOWER market from contexts
        market = self._build_market(contexts)
        if market is None or market.seconds_remaining < self.risk_config.min_seconds_remaining:
            return []

        # Estimate FOLLOWER fair value from LEADER move
        fair = self.fair_value.estimate(leader_features, market)
        if fair is None:
            return []

        edge = self.edge_skill.best_edge(fair, market)

        # Staleness check: is the follower market stale enough?
        if edge.edge < self.staleness_threshold:
            self.signals_already_priced += 1
            return []

        self.signals_stale += 1

        if not self.risk_gate.passes(edge, market, self.risk_config, self.lock.can_enter()):
            return []

        size_usdc = self.sizer.size(edge, self.sizing_config)
        if size_usdc <= 0:
            return []

        capped_usdc = min(size_usdc, self.max_notional_usdc)
        size_shares = capped_usdc / edge.market_price

        self.lock.on_signal()
        self.total_trades += 1

        log.info(
            "LEADLAG [%s→%s]: %s %.1f @ %.4f ($%.0f) | leader_move=%.1fbps stale=%.4f edge=%.4f | %s",
            self._asset, self.follower,
            edge.direction, size_shares, edge.market_price, capped_usdc,
            leader_features.move_bps, edge.edge, edge.edge, edge.reason,
        )

        return [TradingSignal(
            token_id=edge.token_id, side="BUY", price=edge.market_price,
            size=size_shares, strategy=f"{self.name}_{self.follower}",
            edge=edge.edge, fair_value=edge.fair, direction=edge.direction,
            tick_size="0.01",
        )]

    def snapshot(self) -> dict:
        return {
            "leader": self._asset,
            "follower": self.follower,
            "leader_price": self._prices[-1] if self._prices else 0,
            "total_trades": self.total_trades,
            "signals_detected": self.signals_detected,
            "signals_stale": self.signals_stale,
            "signals_already_priced": self.signals_already_priced,
            "has_position": self.lock.has_position,
        }
