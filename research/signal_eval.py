"""
Research signal evaluator for recorded 5-minute Polymarket data.

This module does not affect live trading, EMS/OMS, or the existing replay
backtester. It reads JSONL market snapshots, emits standardized probability
forecast signals, and evaluates those forecasts against settled window labels.

Example:
    python -m research.signal_eval --file data_v2/market_data_2026-05-15.jsonl
    python -m research.signal_eval --data-dir data_v2 --out-signals reports/research_signals.jsonl
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable, Optional


EPS = 1e-6


@dataclass
class ResearchSignal:
    strategy_name: str
    market_id: str
    timestamp: float
    timestamp_iso: str
    asset: str
    predicted_prob_up: float
    confidence: str
    confidence_score: float
    reason: str
    seconds_remaining: float
    baseline_prob_up: float
    features: dict = field(default_factory=dict)


@dataclass
class MetricSummary:
    strategy_name: str
    n: int
    accuracy: float
    brier: float
    log_loss: float
    calibration_ece: float
    high_conf_n: int
    high_conf_precision: float
    avg_predicted_prob_up: float
    empirical_up_rate: float


def clamp_prob(value: float) -> float:
    return min(1.0 - EPS, max(EPS, float(value)))


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def confidence_from_prob(prob_up: float) -> tuple[str, float]:
    score = abs(prob_up - 0.5) * 2.0
    if score >= 0.50:
        label = "high"
    elif score >= 0.20:
        label = "medium"
    else:
        label = "low"
    return label, score


def market_prob_up(rec: dict) -> float:
    up_mid = float(rec.get("up_mid", 0) or 0)
    down_mid = float(rec.get("down_mid", 0) or 0)
    if 0 < up_mid < 1:
        return clamp_prob(up_mid)
    if 0 < down_mid < 1:
        return clamp_prob(1.0 - down_mid)
    return 0.5


def signed_distance_bps(rec: dict) -> float:
    price = float(rec.get("price", 0) or 0)
    strike = float(rec.get("strike", 0) or 0)
    if price <= 0 or strike <= 0:
        return 0.0
    return (price - strike) / price * 10_000


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


def load_records(data_dir: str, file_path: Optional[str], assets: set[str], max_records: int = 0) -> list[dict]:
    files = [file_path] if file_path else sorted(glob.glob(os.path.join(data_dir, "market_data_*.jsonl")))
    records: list[dict] = []
    for path in files:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                asset = str(rec.get("asset", "")).lower()
                if assets and asset not in assets:
                    continue
                records.append(rec)
                if max_records and len(records) >= max_records:
                    return sorted(records, key=lambda row: float(row.get("ts", 0) or 0))
    return sorted(records, key=lambda row: float(row.get("ts", 0) or 0))


def compute_outcomes(records: Iterable[dict]) -> dict[str, int]:
    windows: dict[str, list[dict]] = defaultdict(list)
    for rec in records:
        slug = str(rec.get("slug", ""))
        if slug:
            windows[slug].append(rec)

    outcomes: dict[str, int] = {}
    for slug, rows in windows.items():
        rows.sort(key=lambda row: float(row.get("ts", 0) or 0))
        last_rows = [row for row in rows if float(row.get("seconds_remaining", 999) or 999) < 10]
        final = (last_rows or rows)[-1]
        up_mid = float(final.get("up_mid", 0) or 0)
        if up_mid > 0.70:
            outcomes[slug] = 1
        elif up_mid < 0.30:
            outcomes[slug] = 0
        else:
            price = float(final.get("price", 0) or 0)
            strike = float(final.get("strike", 0) or 0)
            outcomes[slug] = 1 if price >= strike else 0
    return outcomes


class RollingState:
    def __init__(self):
        self.prices: dict[str, deque[tuple[float, float]]] = defaultdict(lambda: deque(maxlen=2000))
        self.last_emit_ts: dict[tuple[str, str], float] = {}

    def add(self, rec: dict) -> None:
        asset = str(rec.get("asset", "")).lower()
        ts = float(rec.get("ts", 0) or 0)
        price = float(rec.get("price", 0) or 0)
        if asset and price > 0:
            self.prices[asset].append((ts, price))

    def price_seconds_ago(self, asset: str, ts: float, seconds: float) -> Optional[float]:
        rows = self.prices.get(asset)
        if not rows:
            return None
        target = ts - seconds
        best = None
        for row_ts, price in rows:
            if row_ts <= target:
                best = price
            else:
                break
        return best if best is not None else rows[0][1]

    def should_emit(self, strategy: str, market_id: str, ts: float, sample_seconds: float) -> bool:
        if sample_seconds <= 0:
            return True
        key = (strategy, market_id)
        last = self.last_emit_ts.get(key)
        if last is not None and ts - last < sample_seconds:
            return False
        self.last_emit_ts[key] = ts
        return True


def make_signal(strategy: str, rec: dict, prob_up: float, reason: str, extra: Optional[dict] = None) -> ResearchSignal:
    prob_up = clamp_prob(prob_up)
    confidence, confidence_score = confidence_from_prob(prob_up)
    seconds_remaining = float(rec.get("seconds_remaining", 0) or 0)
    features = {
        "price": float(rec.get("price", 0) or 0),
        "strike": float(rec.get("strike", 0) or 0),
        "distance_bps": signed_distance_bps(rec),
        "up_mid": float(rec.get("up_mid", 0) or 0),
        "down_mid": float(rec.get("down_mid", 0) or 0),
        "up_spread": float(rec.get("up_spread", 0) or 0),
        "down_spread": float(rec.get("down_spread", 0) or 0),
        "regime": regime(seconds_remaining),
    }
    if extra:
        features.update(extra)
    return ResearchSignal(
        strategy_name=strategy,
        market_id=str(rec.get("slug", "")),
        timestamp=float(rec.get("ts", 0) or 0),
        timestamp_iso=str(rec.get("time", "")),
        asset=str(rec.get("asset", "")).lower(),
        predicted_prob_up=prob_up,
        confidence=confidence,
        confidence_score=confidence_score,
        reason=reason,
        seconds_remaining=seconds_remaining,
        baseline_prob_up=market_prob_up(rec),
        features=features,
    )


def forecast_baseline_50(rec: dict, state: RollingState) -> Optional[ResearchSignal]:
    return make_signal("baseline_50_50", rec, 0.5, "Constant 50/50 probability baseline")


def forecast_market_mid(rec: dict, state: RollingState) -> Optional[ResearchSignal]:
    return make_signal("baseline_market_mid", rec, market_prob_up(rec), "Polymarket UP midpoint implied probability")


def forecast_spot_vs_strike(rec: dict, state: RollingState) -> Optional[ResearchSignal]:
    distance = signed_distance_bps(rec)
    prob = 0.65 if distance >= 0 else 0.35
    return make_signal("baseline_spot_vs_strike", rec, prob, "Spot price is above/below strike")


def forecast_distance_model(rec: dict, state: RollingState) -> Optional[ResearchSignal]:
    distance = signed_distance_bps(rec)
    prob = sigmoid(distance / 12.0)
    return make_signal("baseline_distance_model", rec, prob, "Sigmoid transform of spot distance from strike")


def forecast_momentum(rec: dict, state: RollingState) -> Optional[ResearchSignal]:
    asset = str(rec.get("asset", "")).lower()
    ts = float(rec.get("ts", 0) or 0)
    current = float(rec.get("price", 0) or 0)
    old = state.price_seconds_ago(asset, ts, 30.0)
    if not old or current <= 0:
        return None
    momentum_bps = (current - old) / old * 10_000
    prob = market_prob_up(rec) + max(-0.20, min(0.20, momentum_bps / 100.0))
    reason = "Spot momentum over the last 30 seconds shifts market-implied probability"
    return make_signal("momentum_strategy", rec, prob, reason, {"momentum_bps_30s": momentum_bps})


def forecast_oracle(rec: dict, state: RollingState) -> Optional[ResearchSignal]:
    asset = str(rec.get("asset", "")).lower()
    ts = float(rec.get("ts", 0) or 0)
    current = float(rec.get("price", 0) or 0)
    old = state.price_seconds_ago(asset, ts, 5.0)
    if not old or current <= 0:
        return None
    move_bps = (current - old) / old * 10_000
    market = market_prob_up(rec)
    fair = market + max(-0.25, min(0.25, move_bps / 40.0))
    reason = "Recent external spot move adjusts stale Polymarket-implied probability"
    return make_signal("oracle_strategy", rec, fair, reason, {"spot_move_bps_5s": move_bps})


def forecast_leadlag(rec: dict, state: RollingState) -> Optional[ResearchSignal]:
    asset = str(rec.get("asset", "")).lower()
    if asset == "btc":
        return None
    ts = float(rec.get("ts", 0) or 0)
    btc_rows = state.prices.get("btc")
    if not btc_rows:
        return None
    btc_now = btc_rows[-1][1]
    btc_old = state.price_seconds_ago("btc", ts, 5.0)
    if not btc_old or btc_old <= 0:
        return None
    move_bps = (btc_now - btc_old) / btc_old * 10_000
    fair = market_prob_up(rec) + max(-0.18, min(0.18, move_bps / 55.0))
    reason = "BTC short-horizon move leads follower asset market probability"
    return make_signal("leadlag_strategy", rec, fair, reason, {"btc_move_bps_5s": move_bps})


def forecast_convergence(rec: dict, state: RollingState) -> Optional[ResearchSignal]:
    remaining = float(rec.get("seconds_remaining", 0) or 0)
    if remaining > 45:
        return None
    up_mid = market_prob_up(rec)
    if up_mid >= 0.88:
        prob = max(up_mid, 0.90)
        reason = "Near expiry, UP market is priced close to certain"
    elif up_mid <= 0.12:
        prob = min(up_mid, 0.10)
        reason = "Near expiry, DOWN market is priced close to certain"
    else:
        return None
    return make_signal("convergence_strategy", rec, prob, reason)


def forecast_snipe(rec: dict, state: RollingState) -> Optional[ResearchSignal]:
    remaining = float(rec.get("seconds_remaining", 0) or 0)
    if remaining > 60 or remaining < 12:
        return None
    distance = signed_distance_bps(rec)
    if abs(distance) < 5:
        return None
    prob = 0.95 if distance > 0 else 0.05
    reason = "Late window spot price is materially on one side of strike"
    return make_signal("snipe_strategy", rec, prob, reason)


FORECASTERS: dict[str, Callable[[dict, RollingState], Optional[ResearchSignal]]] = {
    "baseline_50_50": forecast_baseline_50,
    "baseline_market_mid": forecast_market_mid,
    "baseline_spot_vs_strike": forecast_spot_vs_strike,
    "baseline_distance_model": forecast_distance_model,
    "momentum": forecast_momentum,
    "oracle": forecast_oracle,
    "leadlag": forecast_leadlag,
    "convergence": forecast_convergence,
    "snipe": forecast_snipe,
}


def generate_signals(records: list[dict], strategies: list[str], sample_seconds: float) -> list[ResearchSignal]:
    state = RollingState()
    signals: list[ResearchSignal] = []
    for rec in records:
        state.add(rec)
        market_id = str(rec.get("slug", ""))
        ts = float(rec.get("ts", 0) or 0)
        for name in strategies:
            forecaster = FORECASTERS.get(name)
            if not forecaster:
                continue
            signal = forecaster(rec, state)
            if not signal:
                continue
            if not state.should_emit(signal.strategy_name, market_id, ts, sample_seconds):
                continue
            signals.append(signal)
    return signals


def evaluate(signals: Iterable[ResearchSignal], outcomes: dict[str, int], high_conf_threshold: float = 0.70) -> list[MetricSummary]:
    grouped: dict[str, list[tuple[ResearchSignal, int]]] = defaultdict(list)
    for signal in signals:
        actual = outcomes.get(signal.market_id)
        if actual is None:
            continue
        grouped[signal.strategy_name].append((signal, actual))

    summaries: list[MetricSummary] = []
    for strategy, rows in sorted(grouped.items()):
        n = len(rows)
        if n == 0:
            continue
        accuracy = sum((sig.predicted_prob_up >= 0.5) == bool(actual) for sig, actual in rows) / n
        brier = sum((sig.predicted_prob_up - actual) ** 2 for sig, actual in rows) / n
        log_loss = -sum(
            actual * math.log(clamp_prob(sig.predicted_prob_up))
            + (1 - actual) * math.log(clamp_prob(1.0 - sig.predicted_prob_up))
            for sig, actual in rows
        ) / n
        ece = calibration_error(rows)
        high_rows = [
            (sig, actual)
            for sig, actual in rows
            if max(sig.predicted_prob_up, 1.0 - sig.predicted_prob_up) >= high_conf_threshold
        ]
        high_precision = (
            sum((sig.predicted_prob_up >= 0.5) == bool(actual) for sig, actual in high_rows) / len(high_rows)
            if high_rows
            else 0.0
        )
        summaries.append(
            MetricSummary(
                strategy_name=strategy,
                n=n,
                accuracy=accuracy,
                brier=brier,
                log_loss=log_loss,
                calibration_ece=ece,
                high_conf_n=len(high_rows),
                high_conf_precision=high_precision,
                avg_predicted_prob_up=sum(sig.predicted_prob_up for sig, _ in rows) / n,
                empirical_up_rate=sum(actual for _, actual in rows) / n,
            )
        )
    return summaries


def calibration_error(rows: list[tuple[ResearchSignal, int]], bins: int = 10) -> float:
    total = len(rows)
    if total == 0:
        return 0.0
    ece = 0.0
    for idx in range(bins):
        lo = idx / bins
        hi = (idx + 1) / bins
        bucket = [
            (sig, actual)
            for sig, actual in rows
            if lo <= sig.predicted_prob_up < hi
            or (idx == bins - 1 and lo <= sig.predicted_prob_up <= hi)
        ]
        if not bucket:
            continue
        avg_pred = sum(sig.predicted_prob_up for sig, _ in bucket) / len(bucket)
        actual_rate = sum(actual for _, actual in bucket) / len(bucket)
        ece += len(bucket) / total * abs(avg_pred - actual_rate)
    return ece


def write_jsonl(path: str, rows: Iterable[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def write_metrics(path: str, summaries: list[MetricSummary]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = [asdict(item) for item in summaries]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate standardized research signals on recorded data")
    parser.add_argument("--data-dir", default="data_v2")
    parser.add_argument("--file", default=None)
    parser.add_argument("--assets", default="btc,eth,sol,xrp")
    parser.add_argument(
        "--strategies",
        default="baseline_50_50,baseline_market_mid,baseline_spot_vs_strike,baseline_distance_model,momentum,oracle,leadlag,convergence,snipe",
        help="Comma-separated strategy names or 'all'",
    )
    parser.add_argument("--sample-seconds", type=float, default=30.0, help="Minimum seconds between signals per strategy/window")
    parser.add_argument("--high-conf-threshold", type=float, default=0.70)
    parser.add_argument("--max-records", type=int, default=0, help="Optional cap for quick tests")
    parser.add_argument("--out-signals", default="", help="Optional JSONL path for standardized signals")
    parser.add_argument("--out-metrics", default="", help="Optional JSON path for metrics")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    assets = {asset.strip().lower() for asset in args.assets.split(",") if asset.strip()}
    requested = [name.strip() for name in args.strategies.split(",") if name.strip()]
    if requested == ["all"]:
        requested = list(FORECASTERS)
    unknown = [name for name in requested if name not in FORECASTERS]
    if unknown:
        print(f"Unknown strategies: {', '.join(unknown)}", file=sys.stderr)
        sys.exit(2)

    records = load_records(args.data_dir, args.file, assets, max_records=args.max_records)
    if not records:
        print("No records loaded", file=sys.stderr)
        sys.exit(1)

    outcomes = compute_outcomes(records)
    signals = generate_signals(records, requested, sample_seconds=args.sample_seconds)
    summaries = evaluate(signals, outcomes, high_conf_threshold=args.high_conf_threshold)

    print(f"Loaded records: {len(records):,}")
    print(f"Windows labeled: {len(outcomes):,}")
    print(f"Signals emitted: {len(signals):,}")
    print()
    print("strategy,n,accuracy,brier,log_loss,calibration_ece,high_conf_n,high_conf_precision,avg_prob_up,actual_up_rate")
    for item in summaries:
        print(
            f"{item.strategy_name},{item.n},{item.accuracy:.4f},{item.brier:.4f},"
            f"{item.log_loss:.4f},{item.calibration_ece:.4f},{item.high_conf_n},"
            f"{item.high_conf_precision:.4f},{item.avg_predicted_prob_up:.4f},{item.empirical_up_rate:.4f}"
        )

    if args.out_signals:
        write_jsonl(args.out_signals, (asdict(signal) for signal in signals))
        print(f"\nSignals: {args.out_signals}")
    if args.out_metrics:
        write_metrics(args.out_metrics, summaries)
        print(f"Metrics: {args.out_metrics}")


if __name__ == "__main__":
    main()
