"""
Regime Portfolio Strategy (v2).

Wraps the tuned v2 strategies and only handles allocation, arbitration, and
portfolio-level exposure. Child strategies remain the source of all entry
logic; this module never reimplements their signal conditions.
"""

import copy
import logging
from typing import Dict, List, Optional

from data_pipeline.market_provider import MarketContext
from oms.position_manager import Fill
from pipeline.signal import TradingSignal
from strategies.base import BaseStrategy
from strategies.v2.last_seconds_snipe import LastSecondsSnipe
from strategies.v2.leadlag import LeadLag
from strategies.v2.momentum import Momentum
from strategies.v2.oracle_frontrun import OracleFrontrun

log = logging.getLogger(__name__)


class PortfolioRegimeStrategy(BaseStrategy):
    """Run tuned child strategies as a fixed-weight regime portfolio."""

    name = "portfolio"

    DEFAULT_WEIGHTS = {
        "warmup": {"oracle": 0.50, "leadlag": 0.50},
        "early_contested": {"momentum": 0.40, "oracle": 0.35, "leadlag": 0.25},
        "mid_shock": {"oracle": 0.40, "leadlag": 0.40, "momentum": 0.20},
        "endgame": {"snipe": 0.50, "oracle": 0.25, "leadlag": 0.25},
        "deadzone": {},
    }
    PRIORITY = {"snipe": 4, "oracle": 3, "leadlag": 2, "momentum": 1}
    STATE_KEYS = (
        "_has_position",
        "_last_trade_time",
        "_last_signal_time",
        "_signaled_window",
        "_pending_signal_key",
        "_pending_signal_count",
        "total_trades",
        "total_signals",
    )

    def __init__(
        self,
        asset: str = "btc",
        leader: str = "btc",
        bankroll: float = 10_000.0,
        include_leadlag: bool = False,
        single_window_cap_pct: float = 0.5,
        total_exposure_cap_pct: float = 1.0,
        weights: Optional[dict] = None,
        momentum_params: Optional[dict] = None,
        oracle_params: Optional[dict] = None,
        leadlag_params: Optional[dict] = None,
        snipe_params: Optional[dict] = None,
    ):
        self.asset = asset
        self.leader = leader
        self.bankroll = bankroll
        self.single_window_cap = bankroll * single_window_cap_pct
        self.total_exposure_cap = bankroll * total_exposure_cap_pct
        self.weights = weights or self.DEFAULT_WEIGHTS

        momentum_params = dict(momentum_params or {})
        oracle_params = dict(oracle_params or {})
        leadlag_params = dict(leadlag_params or {})
        snipe_params = dict(snipe_params or {})

        self.children: Dict[str, BaseStrategy] = {
            "momentum": Momentum(asset=asset, bankroll=bankroll * 0.40, **momentum_params),
            "oracle": OracleFrontrun(asset=asset, bankroll=bankroll * 0.40, **oracle_params),
            "snipe": LastSecondsSnipe(asset=asset, bankroll=bankroll * 0.30, **snipe_params),
        }
        if include_leadlag and asset != leader:
            self.children["leadlag"] = LeadLag(
                leader=leader,
                follower=asset,
                bankroll=bankroll * 0.40,
                **leadlag_params,
            )

        self._current_window = ""
        self._accepted_windows: set[str] = set()
        self._window_notional: Dict[str, float] = {}
        self._open_notional = 0.0

        self.accepted = 0
        self.rejected = 0

    def step(self, contexts: List[MarketContext]) -> List[TradingSignal]:
        contexts = [c for c in contexts if c and c.is_valid]
        if not contexts:
            return []

        window_key = contexts[0].condition_id or ""
        if window_key and window_key != self._current_window:
            self._current_window = window_key
            self._accepted_windows.clear()
            self._window_notional.clear()

        remaining = min((c.seconds_remaining for c in contexts), default=0.0)
        regime = self._regime(remaining)
        active_weights = self.weights.get(regime, {})
        if not active_weights:
            log.debug("PORTFOLIO REJECT regime: asset=%s regime=%s remaining=%.1f", self.asset, regime, remaining)
            return []

        if window_key in self._accepted_windows:
            log.debug("PORTFOLIO REJECT window: asset=%s window=%s already accepted", self.asset, window_key[:24])
            return []

        candidates: list[tuple[str, TradingSignal]] = []
        for key, child in self.children.items():
            if active_weights.get(key, 0.0) <= 0:
                continue
            before = self._capture_state(child)
            try:
                signals = child.step(contexts)
            except Exception as exc:
                log.warning("PORTFOLIO child %s failed: %s", key, exc)
                continue
            if signals:
                self._restore_state(child, before)
                for signal in signals:
                    candidates.append((key, signal))

        if not candidates:
            log.debug(
                "PORTFOLIO: asset=%s regime=%s weights=%s no child signals",
                self.asset, regime, self._fmt_weights(active_weights),
            )
            return []

        accepted = self._arbitrate(candidates, active_weights, regime, window_key)
        if accepted:
            self._accepted_windows.add(window_key)
            self.accepted += len(accepted)
            log.info(
                "PORTFOLIO[%s]: asset=%s accepted=%s weights=%s window_used=$%.0f open=$%.0f",
                regime,
                self.asset.upper(),
                ",".join(s.strategy for s in accepted),
                self._fmt_weights(active_weights),
                self._window_notional.get(window_key, 0.0),
                self._open_notional,
            )
        return accepted

    def on_fill(self, fill: Fill):
        if fill.side == "BUY":
            self._open_notional += fill.size * fill.price
        elif fill.side == "SELL":
            self._open_notional = max(0.0, self._open_notional - fill.size * fill.price)

        child = self._child_from_source(fill.source)
        if child:
            child.on_fill(fill)

    def on_cancel(self):
        self._accepted_windows.clear()
        self._window_notional.clear()
        self._open_notional = 0.0
        for child in self.children.values():
            child.on_cancel()
            if hasattr(child, "_has_position"):
                setattr(child, "_has_position", False)

    def snapshot(self) -> dict:
        return {
            "asset": self.asset,
            "bankroll": self.bankroll,
            "open_notional": self._open_notional,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "children": {name: child.snapshot() for name, child in self.children.items()},
        }

    def _arbitrate(
        self,
        candidates: list[tuple[str, TradingSignal]],
        active_weights: dict,
        regime: str,
        window_key: str,
    ) -> List[TradingSignal]:
        if regime == "endgame":
            snipe = [(k, s) for k, s in candidates if k == "snipe"]
            if snipe:
                candidates = snipe

        candidates.sort(
            key=lambda item: (
                self.PRIORITY.get(item[0], 0),
                item[1].edge * max(item[1].confidence, 0.01),
            ),
            reverse=True,
        )

        accepted: List[TradingSignal] = []
        used_direction = None
        for child_key, signal in candidates:
            if used_direction and signal.direction and signal.direction != used_direction:
                self._reject(child_key, signal, "direction_conflict")
                continue

            weight = active_weights.get(child_key, 0.0)
            sleeve_cap = self.bankroll * weight
            window_left = max(0.0, self.single_window_cap - self._window_notional.get(window_key, 0.0))
            total_left = max(0.0, self.total_exposure_cap - self._open_notional)
            allowed_notional = min(signal.notional, sleeve_cap, window_left, total_left)
            if signal.price <= 0 or allowed_notional < 5:
                self._reject(child_key, signal, "budget")
                continue

            final_signal = copy.copy(signal)
            if allowed_notional < signal.notional:
                final_signal.size = allowed_notional / signal.price
            final_signal.strategy = f"{self.name}:{signal.strategy}"
            self._window_notional[window_key] = self._window_notional.get(window_key, 0.0) + final_signal.notional
            accepted.append(final_signal)
            used_direction = signal.direction or used_direction
            break

        return accepted

    def _reject(self, child_key: str, signal: TradingSignal, reason: str) -> None:
        self.rejected += 1
        log.debug(
            "PORTFOLIO REJECT %s: child=%s dir=%s notional=$%.0f edge=%.4f conf=%.2f",
            reason, child_key, signal.direction, signal.notional, signal.edge, signal.confidence,
        )

    def _child_from_source(self, source: str) -> Optional[BaseStrategy]:
        if not source.startswith(f"{self.name}:"):
            return None
        raw = source.split(":", 1)[1]
        key = self._child_key(raw)
        return self.children.get(key)

    @classmethod
    def _child_key(cls, strategy_name: str) -> str:
        if strategy_name.startswith("btc5m_momentum"):
            return "momentum"
        if strategy_name.startswith("oracle_frontrun"):
            return "oracle"
        if strategy_name.startswith("leadlag"):
            return "leadlag"
        if strategy_name.startswith("btc5m_last_snipe"):
            return "snipe"
        if strategy_name.startswith("snipe"):
            return "snipe"
        return strategy_name.split("_", 1)[0]

    @staticmethod
    def _regime(remaining: float) -> str:
        if remaining < 12.0:
            return "deadzone"
        if remaining <= 60.0:
            return "endgame"
        if remaining <= 180.0:
            return "mid_shock"
        if remaining <= 240.0:
            return "early_contested"
        return "warmup"

    @classmethod
    def _capture_state(cls, child: BaseStrategy) -> dict:
        return {key: copy.copy(getattr(child, key)) for key in cls.STATE_KEYS if hasattr(child, key)}

    @staticmethod
    def _restore_state(child: BaseStrategy, state: dict) -> None:
        for key, value in state.items():
            setattr(child, key, value)

    @staticmethod
    def _fmt_weights(weights: dict) -> str:
        return ",".join(f"{k}={v:.0%}" for k, v in sorted(weights.items()))
