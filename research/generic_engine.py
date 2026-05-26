"""Generic research engine for canonical prediction-market bars.

This is a report-only engine for strategy generation, backtest, and grading.
It produces forecast-track metrics and lightweight execution diagnostics; it
does not wire into live trading.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from backtest.canonicalize import normalize_records, parse_payload_text
from backtest.execution_costs import FeeModel, estimate_vwap_from_top, quarter_kelly_notional
from backtest.resample import build_bars
from research.grader import grade_run


def run_family(bars: list[dict[str, Any]], family: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = dict(params or {})
    grouped = _group_bars(bars)
    signals: list[dict[str, Any]] = []
    for key, rows in grouped.items():
        rows.sort(key=lambda item: item["bar_ts"])
        if family == "momentum":
            signals.extend(_momentum(rows, params))
        elif family == "mean_reversion":
            signals.extend(_mean_reversion(rows, params))
        elif family == "volume_shock":
            signals.extend(_volume_shock(rows, params))
        elif family == "open_interest_growth":
            signals.extend(_oi_growth(rows, params))
        elif family == "volatility_regime":
            signals.extend(_volatility_regime(rows, params))
        elif family == "closing_time_behavior":
            signals.extend(_closing_time(rows, params))
        elif family == "early_attention":
            signals.extend(_early_attention(rows, params))
        else:
            raise ValueError(f"Unsupported family: {family}")
    summary = _forecast_summary(signals)
    execution = _execution_summary(signals, params)
    payload = {
        "schema_version": 1,
        "strategy": {"family": family, "params": params},
        "summary": {**summary, **execution, "mode": "forecast+exec" if execution["trades"] else "forecast"},
        "signals": signals[:1000],
    }
    return grade_run(payload)


def load_bars_from_raw(path: str, source_granularity: str = "unknown", interval_seconds: int = 60) -> list[dict[str, Any]]:
    rows = parse_payload_text(Path(path).read_text(encoding="utf-8"))
    canonical = normalize_records(rows, source_granularity=source_granularity).rows
    time_series = [
        row
        for row in canonical
        if row.get("row_type") in {"bar", "trade", "sse_tick", "unknown"}
        and row.get("ts") is not None
        and row.get("price") is not None
    ]
    return build_bars(time_series, interval_seconds=interval_seconds)


def _group_bars(bars: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for bar in bars:
        grouped.setdefault((bar["condition_id"], bar["outcome"]), []).append(bar)
    return grouped


def _momentum(rows: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    lookback = int(params.get("lookback", 5))
    horizon = int(params.get("horizon", 3))
    return _directional(rows, lookback, horizon, "momentum")


def _mean_reversion(rows: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    window = int(params.get("window", 20))
    z_entry = float(params.get("z_entry", 1.5))
    horizon = int(params.get("horizon", 3))
    out = []
    closes = [float(row["close"]) for row in rows]
    for idx in range(window, len(rows) - horizon):
        hist = closes[idx - window : idx]
        stdev = pstdev(hist)
        if not stdev:
            continue
        z = (closes[idx] - mean(hist)) / stdev
        if abs(z) < z_entry:
            continue
        direction = -1 if z > 0 else 1
        out.append(_signal(rows, closes, idx, horizon, "mean_reversion", direction, abs(z)))
    return out


def _volume_shock(rows: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    lookback = int(params.get("lookback", 30))
    threshold = float(params.get("z_threshold", 2.0))
    horizon = int(params.get("horizon", 6))
    out = []
    closes = [float(row["close"]) for row in rows]
    volumes = [float(row.get("volume") or 0) for row in rows]
    for idx in range(lookback, len(rows) - horizon):
        hist = volumes[idx - lookback : idx]
        stdev = pstdev(hist)
        if not stdev:
            continue
        z = (volumes[idx] - mean(hist)) / stdev
        if z < threshold:
            continue
        future_abs = abs(closes[idx + horizon] - closes[idx])
        out.append({**_base_signal(rows[idx], "volume_shock"), "score": z, "label_abs_return": future_abs, "hit": future_abs > 0.02})
    return out


def _oi_growth(rows: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    lookback = int(params.get("lookback", 60))
    horizon = int(params.get("horizon", 60))
    out = []
    closes = [float(row["close"]) for row in rows]
    for idx in range(lookback, len(rows) - horizon):
        prev = rows[idx - lookback].get("open_interest")
        cur = rows[idx].get("open_interest")
        if prev in (None, 0) or cur is None:
            continue
        growth = (float(cur) - float(prev)) / max(float(prev), 1e-9)
        future_abs = abs(closes[idx + horizon] - closes[idx])
        out.append({**_base_signal(rows[idx], "open_interest_growth"), "score": growth, "label_abs_return": future_abs, "hit": future_abs > 0.02})
    return out


def _volatility_regime(rows: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    window = int(params.get("rv_window", 30))
    horizon = int(params.get("horizon", 6))
    out = []
    closes = [float(row["close"]) for row in rows]
    rets = [None] + [closes[i] / closes[i - 1] - 1.0 if closes[i - 1] else 0.0 for i in range(1, len(closes))]
    for idx in range(window, len(rows) - horizon):
        hist = [value for value in rets[idx - window : idx] if value is not None]
        rv = math.sqrt(sum(value * value for value in hist))
        future_abs = abs(closes[idx + horizon] - closes[idx])
        out.append({**_base_signal(rows[idx], "volatility_regime"), "score": rv, "label_abs_return": future_abs, "hit": future_abs > 0.02})
    return out


def _closing_time(rows: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    horizon = int(params.get("horizon", 3))
    out = []
    closes = [float(row["close"]) for row in rows]
    for idx in range(0, len(rows) - horizon):
        ttc = rows[idx].get("secs_to_close")
        if ttc is None:
            continue
        future = closes[idx + horizon] - closes[idx]
        out.append({**_base_signal(rows[idx], "closing_time_behavior"), "score": float(ttc), "label_return": future, "hit": abs(future) > 0.01})
    return out


def _early_attention(rows: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    early_seconds = float(params.get("early_hours", 6)) * 3600
    early = [row for row in rows if row.get("age_sec") is not None and 0 <= float(row["age_sec"]) <= early_seconds]
    if not early:
        return []
    volume = sum(float(row.get("volume") or 0) for row in early)
    price_range = max(float(row["high"]) for row in early) - min(float(row["low"]) for row in early)
    return [{**_base_signal(early[-1], "early_attention"), "score": volume + price_range, "hit": volume > 0, "label_volume": volume}]


def _directional(rows: list[dict[str, Any]], lookback: int, horizon: int, family: str) -> list[dict[str, Any]]:
    out = []
    closes = [float(row["close"]) for row in rows]
    for idx in range(lookback, len(rows) - horizon):
        past = closes[idx] - closes[idx - lookback]
        if not past:
            continue
        direction = 1 if past > 0 else -1
        out.append(_signal(rows, closes, idx, horizon, family, direction, abs(past)))
    return out


def _signal(rows: list[dict[str, Any]], closes: list[float], idx: int, horizon: int, family: str, direction: int, score: float) -> dict[str, Any]:
    future = closes[idx + horizon] - closes[idx]
    hit = (future > 0 and direction > 0) or (future < 0 and direction < 0)
    return {**_base_signal(rows[idx], family), "direction": "UP" if direction > 0 else "DOWN", "score": score, "label_return": future, "hit": hit}


def _base_signal(row: dict[str, Any], family: str) -> dict[str, Any]:
    return {
        "family": family,
        "condition_id": row["condition_id"],
        "outcome": row["outcome"],
        "bar_ts": row["bar_ts"],
        "price": row["close"],
        "category": row.get("category", "unknown"),
    }


def _forecast_summary(signals: list[dict[str, Any]]) -> dict[str, Any]:
    if not signals:
        return {"n_signals": 0, "hit_rate": 0.0, "brier": 0.0, "log_loss": 0.0, "calibration_ece": 0.0}
    hits = [1.0 if signal.get("hit") else 0.0 for signal in signals]
    probs = [min(0.95, max(0.05, 0.5 + min(float(signal.get("score") or 0), 0.25))) for signal in signals]
    brier = mean((prob - hit) ** 2 for prob, hit in zip(probs, hits))
    log_loss = -mean(hit * math.log(prob) + (1 - hit) * math.log(1 - prob) for prob, hit in zip(probs, hits))
    return {"n_signals": len(signals), "signals": len(signals), "hit_rate": mean(hits), "accuracy": mean(hits), "brier": brier, "log_loss": log_loss, "calibration_ece": abs(mean(probs) - mean(hits))}


def _execution_summary(signals: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    if not params.get("execution_track"):
        return {"trades": 0, "fees": 0.0, "total_pnl": 0.0, "profit_factor": 0.0, "max_drawdown": 0.0, "high_edge_win_rate_pct": 0.0, "low_edge_win_rate_pct": 0.0}
    fee_model = FeeModel(override_rate=params.get("fee_rate"))
    bankroll = float(params.get("bankroll", 10_000))
    trades = []
    for signal in signals:
        price = float(signal["price"])
        fair = min(0.99, max(0.01, price + 0.05 if signal.get("hit") else price - 0.02))
        shares = 0.0
        notional = quarter_kelly_notional(fair, price, bankroll)
        if notional:
            vwap = estimate_vwap_from_top(price, notional / max(price, 1e-6))
            fee = fee_model.fee_per_share(vwap)
            edge = fair - vwap - fee
            if edge > float(params.get("min_net_edge", 0.01)):
                shares = notional / max(vwap, 1e-6)
                pnl = shares * (1.0 - vwap - fee) if signal.get("hit") else -shares * (vwap + fee)
                trades.append({"edge": edge, "pnl": pnl, "fee": shares * fee})
    wins = [trade for trade in trades if trade["pnl"] > 0]
    losses = [trade for trade in trades if trade["pnl"] <= 0]
    high = [trade for trade in trades if abs(trade["edge"]) > 0.05]
    low = [trade for trade in trades if abs(trade["edge"]) <= 0.05]
    gross_win = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in losses))
    return {
        "trades": len(trades),
        "fees": sum(trade["fee"] for trade in trades),
        "total_pnl": sum(trade["pnl"] for trade in trades),
        "profit_factor": gross_win / gross_loss if gross_loss else (gross_win if gross_win else 0.0),
        "max_drawdown": 0.0,
        "high_edge_win_rate_pct": _win_rate_pct(high),
        "low_edge_win_rate_pct": _win_rate_pct(low),
    }


def _win_rate_pct(trades: list[dict[str, Any]]) -> float:
    return sum(1 for trade in trades if trade["pnl"] > 0) / len(trades) * 100.0 if trades else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run generic prediction-market research strategy")
    parser.add_argument("--input", required=True, help="Raw or canonical JSON/JSONL input")
    parser.add_argument("--family", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--execution-track", action="store_true")
    parser.add_argument("--params-json", default="")
    args = parser.parse_args()
    params = json.loads(args.params_json) if args.params_json else {}
    if args.execution_track:
        params["execution_track"] = True
    bars = load_bars_from_raw(args.input, interval_seconds=args.interval_seconds)
    result = run_family(bars, args.family, params)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], sort_keys=True))


if __name__ == "__main__":
    main()
