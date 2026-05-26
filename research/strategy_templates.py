"""Research strategy templates for prediction-market strategy generation.

Templates are deterministic constraints used by a planner. They are not live
trading strategies.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StrategyTemplate:
    family: str
    hypothesis: str
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...] = ()
    default_params: dict[str, Any] = field(default_factory=dict)
    labels: tuple[str, ...] = ()
    grading_track: str = "forecast"

    def candidate(self, available_fields: set[str]) -> dict[str, Any]:
        missing = sorted(set(self.required_fields) - available_fields)
        return {
            "candidate_id": f"{self.family}_default",
            "family": self.family,
            "hypothesis": self.hypothesis,
            "required_fields": list(self.required_fields),
            "optional_fields": list(self.optional_fields),
            "params": self.default_params,
            "labels": list(self.labels),
            "grading_track": self.grading_track,
            "disabled_reason": f"missing required fields: {', '.join(missing)}" if missing else None,
        }


TEMPLATES = [
    StrategyTemplate(
        family="momentum",
        hypothesis="Recent probability movement continues over a short forward horizon.",
        required_fields=("condition_id", "outcome", "bar_ts", "close"),
        optional_fields=("volume", "rel_spread", "secs_to_close", "category"),
        default_params={"lookback": 5, "horizon": 3, "price_band": [0.05, 0.95]},
        labels=("future_return", "direction"),
        grading_track="forecast",
    ),
    StrategyTemplate(
        family="mean_reversion",
        hypothesis="Large deviations from rolling mean partially reverse.",
        required_fields=("condition_id", "outcome", "bar_ts", "close"),
        optional_fields=("rel_spread", "depth_bid_1", "depth_ask_1", "secs_to_close"),
        default_params={"window": 20, "z_entry": 1.5, "horizon": 3},
        labels=("future_return",),
        grading_track="forecast",
    ),
    StrategyTemplate(
        family="volume_shock",
        hypothesis="Abnormal volume predicts future volatility and attention.",
        required_fields=("condition_id", "outcome", "bar_ts", "close", "volume"),
        optional_fields=("open_interest", "rel_spread", "depth_bid_1", "depth_ask_1"),
        default_params={"lookback": 30, "z_threshold": 2.0, "horizon": 6},
        labels=("future_abs_return", "future_volume"),
        grading_track="forecast",
    ),
    StrategyTemplate(
        family="early_attention",
        hypothesis="Early volume and price range predict market lifecycle activity.",
        required_fields=("condition_id", "bar_ts", "market_created_at", "volume"),
        optional_fields=("open_interest", "high", "low", "category"),
        default_params={"early_hours": 6, "label_days": 7},
        labels=("high_attention_label", "future_volume"),
        grading_track="forecast",
    ),
    StrategyTemplate(
        family="open_interest_growth",
        hypothesis="Open-interest growth predicts later market activity and volatility.",
        required_fields=("condition_id", "outcome", "bar_ts", "close", "open_interest"),
        optional_fields=("volume", "category"),
        default_params={"lookback": 60, "horizon": 60},
        labels=("future_volume", "future_abs_return"),
        grading_track="forecast",
    ),
    StrategyTemplate(
        family="volatility_regime",
        hypothesis="Realized volatility regime predicts future volatility and activity.",
        required_fields=("condition_id", "outcome", "bar_ts", "close"),
        optional_fields=("volume", "rel_spread", "open_interest"),
        default_params={"rv_window": 30, "horizon": 6},
        labels=("future_abs_return", "future_volume"),
        grading_track="forecast",
    ),
    StrategyTemplate(
        family="closing_time_behavior",
        hypothesis="Time-to-close buckets explain price, volatility, spread, and depth behavior.",
        required_fields=("condition_id", "outcome", "bar_ts", "close", "secs_to_close"),
        optional_fields=("rel_spread", "depth_bid_1", "depth_ask_1", "volume"),
        default_params={"horizon": 3},
        labels=("future_return", "future_abs_return", "spread_change"),
        grading_track="both",
    ),
]


def generate_candidates(available_fields: set[str]) -> list[dict[str, Any]]:
    return [template.candidate(available_fields) for template in TEMPLATES]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate research strategy candidates from available fields")
    parser.add_argument("--schema", default="", help="Schema probe JSON path")
    parser.add_argument("--fields", default="", help="Comma-separated available canonical fields")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    fields: set[str] = set()
    if args.schema:
        data = json.loads(Path(args.schema).read_text(encoding="utf-8"))
        fields.update(data.get("fields") or data.get("canonical_fields") or [])
        fields.update(data.get("canonical_matches", {}).keys())
    if args.fields:
        fields.update(part.strip() for part in args.fields.split(",") if part.strip())
    candidates = generate_candidates(fields)
    output = json.dumps(candidates, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
