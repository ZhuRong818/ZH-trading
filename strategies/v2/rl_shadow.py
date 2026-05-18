"""
RL Shadow Strategy.

Loads the lightweight tabular replay model and logs intended actions without
returning TradingSignals. This keeps live/dry-run behavior observational only.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from backtest.rl_env import (
    ACTIONS,
    TabularQModel,
    action_direction,
    action_fraction,
    ask_depth_for_direction,
    feature_key,
    price_for_action,
    regime,
    rl_gate_thresholds,
    state_summary,
    spread_for_direction,
)
from data_pipeline.market_provider import MarketContext
from pipeline.signal import TradingSignal
from strategies.base import BaseStrategy

log = logging.getLogger(__name__)


class RLShadowStrategy(BaseStrategy):
    name = "rl_shadow"

    def __init__(
        self,
        asset: str = "btc",
        bankroll: float = 10_000.0,
        model_path: str = "reports/rl_model.json",
        min_price: float = 0.20,
        max_price: float = 0.95,
        min_q: float = 5.0,
        min_edge: float = 0.02,
        max_spread: float = 0.10,
        q_scale: float = 0.0,
        fee_edge_multiplier: float = 0.25,
        min_depth: float = 50.0,
        depth_buffer: float = 1.25,
        log_every: int = 1,
    ):
        self.asset = asset
        self.bankroll = bankroll
        self.model_path = model_path
        self.min_price = min_price
        self.max_price = max_price
        self.min_q = min_q
        self.min_edge = min_edge
        self.max_spread = max_spread
        self.q_scale = q_scale
        self.fee_edge_multiplier = fee_edge_multiplier
        self.min_depth = min_depth
        self.depth_buffer = depth_buffer
        self.log_every = max(1, int(log_every))
        self.model: Optional[TabularQModel] = None
        self._last_price = 0.0
        self._ticks = 0
        self._load_error = ""
        self._load_model()

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        self._ticks += 1
        rec = self._record_from_contexts(contexts)
        if not rec:
            return []

        if not self.model:
            if self._ticks % self.log_every == 0:
                log.info(
                    "RL_SHADOW: asset=%s action=HOLD reason=model_unavailable error=%s",
                    self.asset.upper(),
                    self._load_error,
                )
            return []

        action, q_values, reason = self.model.choose(
            rec,
            prev_price=self._last_price,
            min_price=self.min_price,
            max_price=self.max_price,
        )
        summary = state_summary(rec, prev_price=self._last_price)
        direction = action_direction(action)
        action_idx = ACTIONS.index(action) if action in ACTIONS else 0
        action_q = q_values[action_idx] if q_values and action_idx < len(q_values) else 0.0
        base_notional = self.bankroll * action_fraction(action)
        edge = action_q / max(base_notional, 1.0)
        entry_price = price_for_action(rec, action)
        spread = spread_for_direction(rec, direction)
        thresholds = rl_gate_thresholds(
            rec,
            price=entry_price,
            base_min_q=self.min_q,
            base_min_edge=self.min_edge,
            base_max_spread=self.max_spread,
            fee_edge_multiplier=self.fee_edge_multiplier,
        )
        shares = base_notional / entry_price if entry_price > 0 else 0.0
        depth = ask_depth_for_direction(rec, direction)
        required_depth = max(self.min_depth, shares * self.depth_buffer) if action != "HOLD" else 0.0
        gate_reason = reason or self._gate_reason(
            action, action_q, edge, spread, depth, required_depth, thresholds
        )
        if gate_reason:
            size_multiplier = 0.0
        elif self.q_scale <= 0:
            size_multiplier = 1.0
        else:
            size_multiplier = min(1.0, max(0.25, action_q / self.q_scale))
        intended_notional = base_notional * size_multiplier
        q_map = ",".join(f"{ACTIONS[i]}={q_values[i]:.2f}" for i in range(len(ACTIONS)))

        if self._ticks % self.log_every == 0:
            log.info(
                "RL_SHADOW: asset=%s window=%s regime=%s action=%s direction=%s notional=$%.2f "
                "q=%.2f edge=%.4f min_edge=%.4f fee_edge=%.4f spread=%.4f max_spread=%.4f "
                "depth=%.1f req_depth=%.1f size_mult=%.2f reason=%s rem=%.1f price=%.2f strike=%.2f dist=%.2f dist_bps=%.1f q_values=%s",
                self.asset.upper(),
                str(rec.get("slug", ""))[:32],
                summary["regime"],
                action,
                direction or "-",
                intended_notional,
                action_q,
                edge,
                thresholds["min_edge"],
                thresholds["fee_edge"],
                spread,
                thresholds["max_spread"],
                depth,
                required_depth,
                size_multiplier,
                gate_reason or "model",
                summary["remaining"],
                summary["price"],
                summary["strike"],
                summary["distance"],
                summary["distance_bps"],
                q_map,
            )

        self._last_price = float(rec.get("price", 0) or self._last_price)
        return []

    def snapshot(self) -> dict:
        return {
            "asset": self.asset,
            "bankroll": self.bankroll,
            "model_path": self.model_path,
            "model_loaded": self.model is not None,
            "load_error": self._load_error,
            "ticks": self._ticks,
        }

    def _gate_reason(
        self,
        action: str,
        action_q: float,
        edge: float,
        spread: float,
        depth: float,
        required_depth: float,
        thresholds: dict,
    ) -> str:
        if action == "HOLD":
            return ""
        if action_q < thresholds["min_q"]:
            return "min_q"
        if edge < thresholds["min_edge"]:
            return "min_edge"
        if thresholds["max_spread"] > 0 and spread > thresholds["max_spread"]:
            return "spread"
        if depth < required_depth:
            return "depth"
        return ""

    def _load_model(self) -> None:
        try:
            self.model = TabularQModel.load(self.model_path)
            self._load_error = ""
            log.info("RL_SHADOW: loaded model %s states=%s", self.model_path, self.model.metadata.get("states", "?"))
        except Exception as exc:
            self.model = None
            self._load_error = str(exc)
            log.warning("RL_SHADOW: model unavailable at %s: %s", self.model_path, exc)

    def _record_from_contexts(self, contexts: List[MarketContext]) -> dict:
        valid = [c for c in contexts if c and c.is_valid]
        if not valid:
            return {}

        up_ctx = down_ctx = None
        for ctx in valid:
            q = (ctx.question or "").upper()
            if q.endswith(" UP"):
                up_ctx = ctx
            elif q.endswith(" DOWN"):
                down_ctx = ctx

        if not up_ctx or not down_ctx:
            if len(valid) >= 2:
                up_ctx, down_ctx = valid[0], valid[1]
            else:
                return {}

        price = up_ctx.external_price or down_ctx.external_price or 0.0
        strike = up_ctx.strike_price or down_ctx.strike_price or 0.0
        remaining = min(up_ctx.seconds_remaining, down_ctx.seconds_remaining)
        return {
            "asset": self.asset,
            "slug": up_ctx.condition_id or down_ctx.condition_id or "",
            "ts": 0.0,
            "price": price,
            "strike": strike,
            "seconds_remaining": remaining,
            "up_mid": up_ctx.mid_price,
            "down_mid": down_ctx.mid_price,
            "up_buy": up_ctx.best_bid,
            "down_buy": down_ctx.best_bid,
            "up_sell": up_ctx.best_ask,
            "down_sell": down_ctx.best_ask,
            "up_spread": up_ctx.spread,
            "down_spread": down_ctx.spread,
            "up_ask_depth": up_ctx.book.depth("BUY", levels=5) if up_ctx.book else 0.0,
            "down_ask_depth": down_ctx.book.depth("BUY", levels=5) if down_ctx.book else 0.0,
            "regime": regime(remaining),
        }
