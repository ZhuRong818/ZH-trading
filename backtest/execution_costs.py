"""Research-only execution cost helpers."""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_PARABOLIC_FEE_RATE = 0.07


@dataclass
class FeeModel:
    default_rate: float = DEFAULT_PARABOLIC_FEE_RATE
    override_rate: float | None = None
    metadata_rate: float | None = None

    def fee_per_share(self, price: float) -> float:
        rate = self.metadata_rate if self.metadata_rate is not None else self.override_rate
        if rate is None:
            rate = self.default_rate
        return float(rate) * float(price) * (1.0 - float(price))


def quarter_kelly_notional(
    fair_value: float,
    exec_price: float,
    bankroll: float,
    max_bankroll_fraction: float = 0.025,
    max_notional: float = 150.0,
    min_notional: float = 5.0,
) -> float:
    if exec_price <= 0 or exec_price >= 1 or fair_value <= exec_price or bankroll <= 0:
        return 0.0
    odds = (1.0 - exec_price) / exec_price
    kelly = max(0.0, (fair_value * odds - (1.0 - fair_value)) / odds)
    notional = min(0.25 * kelly * bankroll, max_bankroll_fraction * bankroll, max_notional)
    return notional if notional >= min_notional else 0.0


def estimate_vwap_from_top(
    best_price: float,
    shares: float,
    depth: float | None = None,
    rel_spread: float | None = None,
    realized_vol: float = 0.0,
) -> float:
    if shares <= 0:
        return best_price
    if depth is not None and depth > 0 and shares <= depth:
        return best_price
    missing_depth_ratio = 0.0 if not depth or depth <= 0 else max(0.0, shares - depth) / depth
    spread_component = 0.5 * (rel_spread or 0.0)
    impact_component = 0.02 * missing_depth_ratio
    vol_component = 0.10 * max(0.0, realized_vol)
    return min(0.999, max(0.001, best_price + spread_component + impact_component + vol_component))
