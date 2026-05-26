"""A/B/C/Reject grading on top of deterministic research metrics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def grade_summary(summary: dict[str, Any]) -> dict[str, Any]:
    mode = str(summary.get("mode") or "")
    n_signals = int(summary.get("n_signals") or summary.get("signals") or summary.get("n") or 0)
    trades = int(summary.get("trades") or 0)
    brier = _float(summary.get("brier"))
    ece = _float(summary.get("ece") or summary.get("calibration_ece"))
    profit_factor = _float(summary.get("profit_factor"))
    max_drawdown = _float(summary.get("max_drawdown"))
    high_edge = _float(summary.get("high_edge_win_rate_pct"))
    low_edge = _float(summary.get("low_edge_win_rate_pct"))
    total_pnl = _float(summary.get("total_pnl"))

    reasons: list[str] = []
    if "execution" in mode or trades:
        if trades >= 100 and profit_factor >= 1.20 and max_drawdown <= 0.10 and high_edge > low_edge:
            grade = "A"
        elif trades >= 50 and profit_factor >= 1.05 and max_drawdown <= 0.15:
            grade = "B"
        elif trades > 0 and (profit_factor >= 1.0 or total_pnl > 0):
            grade = "C"
        else:
            grade = "Reject"
        if high_edge <= low_edge and trades:
            reasons.append("edge model did not outperform in high-edge bucket")
    else:
        if n_signals >= 500 and brier and brier <= 0.22 and (ece is None or ece <= 0.05):
            grade = "A"
        elif n_signals >= 200 and brier and brier <= 0.25:
            grade = "B"
        elif n_signals > 0:
            grade = "C"
        else:
            grade = "Reject"

    if grade == "A":
        recommendation = "promote_candidate"
    elif grade in ("B", "C"):
        recommendation = "keep_testing"
    else:
        recommendation = "reject"
    if reasons and recommendation == "promote_candidate":
        recommendation = "keep_testing"

    return {"grade": grade, "recommendation": recommendation, "reasons": reasons}


def grade_run(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary", payload)
    grade = grade_summary(summary)
    return {**payload, "grading": grade}


def _float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Grade a strategy-generation research metrics JSON")
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    result = grade_run(json.loads(Path(args.input).read_text(encoding="utf-8")))
    output = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
