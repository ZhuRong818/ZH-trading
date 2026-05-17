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
    feature_key,
    regime,
    state_summary,
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
        log_every: int = 1,
    ):
        self.asset = asset
        self.bankroll = bankroll
        self.model_path = model_path
        self.min_price = min_price
        self.max_price = max_price
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
        intended_notional = self.bankroll * action_fraction(action)
        best_q = max(q_values) if q_values else 0.0
        q_map = ",".join(f"{ACTIONS[i]}={q_values[i]:.2f}" for i in range(len(ACTIONS)))

        if self._ticks % self.log_every == 0:
            log.info(
                "RL_SHADOW: asset=%s window=%s regime=%s action=%s direction=%s notional=$%.2f "
                "q=%.2f reason=%s rem=%.1f price=%.2f strike=%.2f dist=%.2f dist_bps=%.1f q_values=%s",
                self.asset.upper(),
                str(rec.get("slug", ""))[:32],
                summary["regime"],
                action,
                direction or "-",
                intended_notional,
                best_q,
                reason or "model",
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
            "regime": regime(remaining),
        }
