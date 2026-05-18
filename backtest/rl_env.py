"""
Lightweight RL utilities for 5-minute rolling replay data.

The first implementation is intentionally small: tabular, binned, and
deployment-friendly. It learns entry actions from replay ticks while settlement
and fee accounting stay aligned with backtest.replay.
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np


ASSETS = ("btc", "eth", "sol", "xrp")
REGIMES = ("warmup", "early_contested", "mid_shock", "endgame", "deadzone")
ACTIONS = (
    "HOLD",
    "BUY_UP_SMALL",
    "BUY_UP_MED",
    "BUY_UP_LARGE",
    "BUY_DOWN_SMALL",
    "BUY_DOWN_MED",
    "BUY_DOWN_LARGE",
)
ACTION_SPECS = {
    "HOLD": ("", 0.0),
    "BUY_UP_SMALL": ("UP", 0.005),
    "BUY_UP_MED": ("UP", 0.010),
    "BUY_UP_LARGE": ("UP", 0.020),
    "BUY_DOWN_SMALL": ("DOWN", 0.005),
    "BUY_DOWN_MED": ("DOWN", 0.010),
    "BUY_DOWN_LARGE": ("DOWN", 0.020),
}
FEE_RATE = 0.07

ASSET_GATE_MULTIPLIERS = {
    "btc": {"q": 1.00, "edge": 1.00, "spread": 1.00},
    "eth": {"q": 1.00, "edge": 1.05, "spread": 0.95},
    "sol": {"q": 1.05, "edge": 1.05, "spread": 0.95},
    "xrp": {"q": 1.15, "edge": 1.15, "spread": 0.85},
}

REGIME_GATE_MULTIPLIERS = {
    "warmup": {"q": 1.15, "edge": 1.15, "spread": 0.85},
    "early_contested": {"q": 1.05, "edge": 1.05, "spread": 0.90},
    "mid_shock": {"q": 0.90, "edge": 0.90, "spread": 1.00},
    "endgame": {"q": 1.20, "edge": 1.20, "spread": 0.80},
    "deadzone": {"q": 999.0, "edge": 999.0, "spread": 0.00},
}


def regime(seconds_remaining: float) -> str:
    if seconds_remaining < 12:
        return "deadzone"
    if seconds_remaining <= 60:
        return "endgame"
    if seconds_remaining <= 180:
        return "mid_shock"
    if seconds_remaining <= 240:
        return "early_contested"
    return "warmup"


def action_direction(action: str) -> str:
    return ACTION_SPECS.get(action, ("", 0.0))[0]


def action_fraction(action: str) -> float:
    return ACTION_SPECS.get(action, ("", 0.0))[1]


def price_for_action(rec: dict, action: str) -> float:
    direction = action_direction(action)
    if direction == "UP":
        return float(rec.get("up_sell", 0) or rec.get("up_ask", 0) or rec.get("up_mid", 0) or 0)
    if direction == "DOWN":
        return float(rec.get("down_sell", 0) or rec.get("down_ask", 0) or rec.get("down_mid", 0) or 0)
    return 0.0


def spread_for_direction(rec: dict, direction: str) -> float:
    if direction == "UP":
        return float(rec.get("up_spread", 0) or 0)
    if direction == "DOWN":
        return float(rec.get("down_spread", 0) or 0)
    return 0.0


def ask_depth_for_direction(rec: dict, direction: str) -> float:
    if direction == "UP":
        return float(rec.get("up_ask_depth", 0) or 0)
    if direction == "DOWN":
        return float(rec.get("down_ask_depth", 0) or 0)
    return 0.0


def fee_edge_for_price(price: float) -> float:
    if price <= 0 or price >= 1:
        return 1.0
    return FEE_RATE * (1 - price)


def rl_gate_thresholds(
    rec: dict,
    price: float,
    base_min_q: float = 5.0,
    base_min_edge: float = 0.02,
    base_max_spread: float = 0.10,
    fee_edge_multiplier: float = 0.25,
) -> dict:
    asset = str(rec.get("asset", "btc")).lower()
    reg = regime(float(rec.get("seconds_remaining", 0) or 0))
    asset_mult = ASSET_GATE_MULTIPLIERS.get(asset, {"q": 1.10, "edge": 1.10, "spread": 0.90})
    regime_mult = REGIME_GATE_MULTIPLIERS.get(reg, {"q": 1.10, "edge": 1.10, "spread": 0.90})
    fee_edge = fee_edge_for_price(price) * fee_edge_multiplier
    return {
        "asset": asset,
        "regime": reg,
        "min_q": base_min_q * asset_mult["q"] * regime_mult["q"],
        "min_edge": base_min_edge * asset_mult["edge"] * regime_mult["edge"] + fee_edge,
        "max_spread": base_max_spread * asset_mult["spread"] * regime_mult["spread"] if base_max_spread > 0 else 0.0,
        "fee_edge": fee_edge,
    }


def valid_action_reason(
    rec: dict,
    action: str,
    min_price: float = 0.20,
    max_price: float = 0.95,
) -> str:
    if action == "HOLD":
        return ""
    if float(rec.get("seconds_remaining", 0) or 0) < 12:
        return "deadzone"
    if float(rec.get("strike", 0) or 0) <= 0:
        return "missing_strike"
    price = price_for_action(rec, action)
    if price <= 0:
        return "missing_price"
    if price < min_price or price > max_price:
        return "price_bounds"
    return ""


def trade_reward(
    rec: dict,
    action: str,
    outcome: str,
    bankroll: float,
    min_price: float = 0.20,
    max_price: float = 0.95,
) -> Tuple[float, float, float, float]:
    """Return reward, size_usdc, shares, fee for an action at a tick."""
    reason = valid_action_reason(rec, action, min_price=min_price, max_price=max_price)
    if action == "HOLD":
        return 0.0, 0.0, 0.0, 0.0
    if reason:
        return -1.0, 0.0, 0.0, 0.0

    price = price_for_action(rec, action)
    direction = action_direction(action)
    size_usdc = bankroll * action_fraction(action)
    shares = size_usdc / price if price > 0 else 0.0
    fee = shares * FEE_RATE * price * (1 - price)
    if direction == outcome:
        pnl = shares - shares * price - fee
    else:
        pnl = -(shares * price) - fee
    return pnl, size_usdc, shares, fee


def _bucket(value: float, edges: Iterable[float]) -> int:
    for idx, edge in enumerate(edges):
        if value <= edge:
            return idx
    return len(tuple(edges))


def feature_key(rec: dict, prev_price: float = 0.0, position: Optional[dict] = None) -> str:
    price = float(rec.get("price", 0) or 0)
    strike = float(rec.get("strike", 0) or 0)
    remaining = float(rec.get("seconds_remaining", 0) or 0)
    asset = str(rec.get("asset", "btc")).lower()
    up_mid = float(rec.get("up_mid", 0) or 0)
    down_mid = float(rec.get("down_mid", 0) or 0)
    up_sell = float(rec.get("up_sell", 0) or rec.get("up_ask", 0) or up_mid)
    down_sell = float(rec.get("down_sell", 0) or rec.get("down_ask", 0) or down_mid)
    up_buy = float(rec.get("up_buy", 0) or rec.get("up_bid", 0) or up_mid)
    down_buy = float(rec.get("down_buy", 0) or rec.get("down_bid", 0) or down_mid)

    distance = price - strike if price > 0 and strike > 0 else 0.0
    distance_bps = abs(distance) / price * 10_000 if price > 0 else 0.0
    momentum_bps = (price - prev_price) / prev_price * 10_000 if prev_price > 0 else 0.0
    spread = max(up_sell - up_buy, down_sell - down_buy, 0.0)
    implied = up_mid if up_mid > 0 else (1.0 - down_mid if down_mid > 0 else 0.5)
    pos = position or {}

    parts = [
        f"a={asset if asset in ASSETS else 'other'}",
        f"r={regime(remaining)}",
        f"t={_bucket(remaining, (12, 30, 60, 120, 180, 240))}",
        f"d={_bucket(distance_bps, (2, 5, 10, 20, 40, 80))}",
        f"sgn={1 if distance >= 0 else -1}",
        f"mom={_bucket(momentum_bps, (-10, -4, -1, 1, 4, 10))}",
        f"u={_bucket(up_mid, (0.2, 0.35, 0.5, 0.65, 0.8))}",
        f"spr={_bucket(spread, (0.01, 0.02, 0.04, 0.08))}",
        f"imp={_bucket(implied, (0.2, 0.35, 0.5, 0.65, 0.8))}",
        f"pos={1 if pos.get('direction') else 0}",
        f"pdir={pos.get('direction', '') or 'NONE'}",
    ]
    return "|".join(parts)


def state_summary(rec: dict, prev_price: float = 0.0) -> dict:
    price = float(rec.get("price", 0) or 0)
    strike = float(rec.get("strike", 0) or 0)
    distance = price - strike if price > 0 and strike > 0 else 0.0
    return {
        "asset": str(rec.get("asset", "btc")).lower(),
        "slug": rec.get("slug", ""),
        "remaining": float(rec.get("seconds_remaining", 0) or 0),
        "regime": regime(float(rec.get("seconds_remaining", 0) or 0)),
        "price": price,
        "strike": strike,
        "distance": distance,
        "distance_bps": abs(distance) / price * 10_000 if price > 0 else 0.0,
        "momentum_bps": (price - prev_price) / prev_price * 10_000 if prev_price > 0 else 0.0,
        "up_mid": float(rec.get("up_mid", 0) or 0),
        "down_mid": float(rec.get("down_mid", 0) or 0),
    }


@dataclass
class TabularQModel:
    q: Dict[str, List[float]] = field(default_factory=dict)
    counts: Dict[str, List[int]] = field(default_factory=dict)
    default_q: List[float] = field(default_factory=lambda: [0.0] * len(ACTIONS))
    metadata: dict = field(default_factory=dict)

    def values(self, key: str) -> List[float]:
        return list(self.q.get(key, self.default_q))

    def update_average(self, key: str, action_idx: int, reward: float) -> None:
        values = self.q.setdefault(key, list(self.default_q))
        counts = self.counts.setdefault(key, [0] * len(ACTIONS))
        counts[action_idx] += 1
        values[action_idx] += (reward - values[action_idx]) / counts[action_idx]

    def choose(
        self,
        rec: dict,
        prev_price: float = 0.0,
        min_price: float = 0.20,
        max_price: float = 0.95,
    ) -> Tuple[str, List[float], str]:
        key = feature_key(rec, prev_price=prev_price)
        q_values = self.values(key)
        ranked = sorted(range(len(ACTIONS)), key=lambda idx: q_values[idx], reverse=True)
        for idx in ranked:
            action = ACTIONS[idx]
            reason = valid_action_reason(rec, action, min_price=min_price, max_price=max_price)
            if not reason:
                return action, q_values, ""
        return "HOLD", q_values, "all_actions_blocked"

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        payload = {
            "actions": list(ACTIONS),
            "q": self.q,
            "counts": self.counts,
            "default_q": self.default_q,
            "metadata": self.metadata,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)

    @classmethod
    def load(cls, path: str) -> "TabularQModel":
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if tuple(payload.get("actions", [])) != ACTIONS:
            raise ValueError("RL model action space does not match current code")
        return cls(
            q={str(k): [float(x) for x in v] for k, v in payload.get("q", {}).items()},
            counts={str(k): [int(x) for x in v] for k, v in payload.get("counts", {}).items()},
            default_q=[float(x) for x in payload.get("default_q", [0.0] * len(ACTIONS))],
            metadata=dict(payload.get("metadata", {})),
        )


def split_records_by_time(all_records: Dict[str, List[dict]]) -> tuple[dict, dict, dict]:
    train: dict[str, list[dict]] = {}
    val: dict[str, list[dict]] = {}
    test: dict[str, list[dict]] = {}
    for asset, records in all_records.items():
        rows = sorted(records, key=lambda r: float(r.get("ts", 0) or 0))
        n = len(rows)
        i = int(n * 0.70)
        j = int(n * 0.85)
        train[asset] = rows[:i]
        val[asset] = rows[i:j]
        test[asset] = rows[j:]
    return train, val, test


def train_model(
    all_records: Dict[str, List[dict]],
    outcomes_by_asset: Dict[str, dict],
    bankroll: float = 10_000,
    min_price: float = 0.20,
    max_price: float = 0.95,
) -> TabularQModel:
    model = TabularQModel()
    ts_values = []
    for asset, records in all_records.items():
        prev_price = 0.0
        outcomes = outcomes_by_asset.get(asset, {})
        for rec in sorted(records, key=lambda r: float(r.get("ts", 0) or 0)):
            ts_values.append(float(rec.get("ts", 0) or 0))
            key = feature_key(rec, prev_price=prev_price)
            outcome = outcomes.get(rec.get("slug"))
            if outcome:
                for idx, action in enumerate(ACTIONS):
                    reward, _, _, _ = trade_reward(
                        rec,
                        action,
                        outcome,
                        bankroll=bankroll,
                        min_price=min_price,
                        max_price=max_price,
                    )
                    model.update_average(key, idx, reward)
            prev_price = float(rec.get("price", 0) or prev_price)

    all_action_rewards = np.array([v for values in model.q.values() for v in values], dtype=float)
    model.metadata.update({
        "bankroll": bankroll,
        "min_price": min_price,
        "max_price": max_price,
        "states": len(model.q),
        "samples": int(sum(sum(c) for c in model.counts.values())),
        "start_ts": min(ts_values) if ts_values else 0,
        "end_ts": max(ts_values) if ts_values else 0,
        "mean_q": float(all_action_rewards.mean()) if all_action_rewards.size else 0.0,
    })
    return model
