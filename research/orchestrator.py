"""
Report-only research orchestration for strategy experiments.

Runs replay and standardized signal-evaluation experiments from a spec file,
normalizes metrics, applies fixed gates, ranks candidates, and writes one
summary report. This module deliberately does not mutate live config or
strategy code.

Usage:
    python -m research.orchestrator --spec research/experiments.yaml
    python -m research.orchestrator --spec research/experiments.yaml --max-records 5000
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional


REPLAY_MODE = "replay"
SIGNAL_MODE = "signal_eval"
MODES = {REPLAY_MODE, SIGNAL_MODE}


@dataclass
class ExperimentResult:
    run_id: str
    experiment_name: str
    mode: str
    strategy: str
    params: dict[str, Any]
    assets: list[str]
    tags: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    artifact: str = ""
    returncode: int = 0
    stdout_tail: str = ""
    stderr_tail: str = ""
    trades: int = 0
    signals: int = 0
    total_pnl: float = 0.0
    fees: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    accuracy: Optional[float] = None
    brier: Optional[float] = None
    log_loss: Optional[float] = None
    calibration_ece: Optional[float] = None
    high_conf_precision: Optional[float] = None
    high_edge_win_rate_pct: float = 0.0
    low_edge_win_rate_pct: float = 0.0
    high_edge_trades: int = 0
    low_edge_trades: int = 0
    gate_status: str = "fail"
    failed_gates: list[str] = field(default_factory=list)
    rank: Optional[int] = None
    recommendation: str = "reject"
    score: float = 0.0


def load_spec(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()

    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return json.loads(text)

    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
    except ImportError:
        data = _parse_simple_yaml(text)

    if not isinstance(data, dict):
        raise ValueError("Experiment spec must be a mapping")
    return data


def _parse_simple_yaml(text: str) -> Any:
    """Small YAML subset parser used when PyYAML is unavailable.

    Supports mappings, lists, nested dictionaries, inline lists, booleans,
    numbers, nulls, and quoted strings. It is intended for research specs, not
    arbitrary YAML.
    """
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        content = _strip_comment(raw.rstrip())
        if content.strip():
            lines.append((len(content) - len(content.lstrip(" ")), content.strip()))

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(lines):
            return {}, index
        if lines[index][0] < indent:
            return {}, index
        if lines[index][1].startswith("- "):
            return parse_list(index, indent)
        return parse_mapping(index, indent)

    def parse_list(index: int, indent: int) -> tuple[list[Any], int]:
        items = []
        while index < len(lines):
            line_indent, text_line = lines[index]
            if line_indent != indent or not text_line.startswith("- "):
                break
            item_text = text_line[2:].strip()
            index += 1
            if not item_text:
                item, index = parse_block(index, indent + 2)
                items.append(item)
            elif ":" in item_text and not item_text.startswith(("'", '"')):
                item = {}
                key, value = _split_key_value(item_text)
                if value == "":
                    child, index = parse_block(index, indent + 2)
                    item[key] = child
                else:
                    item[key] = _parse_scalar(value)
                while index < len(lines) and lines[index][0] >= indent + 2:
                    child_indent, child_text = lines[index]
                    if child_indent != indent + 2 or child_text.startswith("- "):
                        break
                    key, value = _split_key_value(child_text)
                    index += 1
                    if value == "":
                        child, index = parse_block(index, child_indent + 2)
                        item[key] = child
                    else:
                        item[key] = _parse_scalar(value)
                items.append(item)
            else:
                items.append(_parse_scalar(item_text))
        return items, index

    def parse_mapping(index: int, indent: int) -> tuple[dict[str, Any], int]:
        mapping: dict[str, Any] = {}
        while index < len(lines):
            line_indent, text_line = lines[index]
            if line_indent != indent or text_line.startswith("- "):
                break
            key, value = _split_key_value(text_line)
            index += 1
            if value == "":
                child, index = parse_block(index, indent + 2)
                mapping[key] = child
            else:
                mapping[key] = _parse_scalar(value)
        return mapping, index

    parsed, end = parse_block(0, lines[0][0] if lines else 0)
    if end != len(lines):
        raise ValueError("Could not parse full YAML spec")
    return parsed


def _strip_comment(line: str) -> str:
    in_quote = ""
    for idx, char in enumerate(line):
        if char in ("'", '"') and (idx == 0 or line[idx - 1] != "\\"):
            in_quote = "" if in_quote == char else char if not in_quote else in_quote
        if char == "#" and not in_quote:
            return line[:idx].rstrip()
    return line


def _split_key_value(text: str) -> tuple[str, str]:
    if ":" not in text:
        raise ValueError(f"Expected key: value line, got: {text}")
    key, value = text.split(":", 1)
    key = key.strip()
    if not key:
        raise ValueError(f"Empty key in line: {text}")
    return key, value.strip()


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in ("", "null", "None", "~"):
        return None
    if value in ("true", "True"):
        return True
    if value in ("false", "False"):
        return False
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    if value.startswith("{") and value.endswith("}"):
        return json.loads(value)
    if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def validate_spec(spec: dict[str, Any]) -> None:
    experiments = spec.get("experiments")
    if not isinstance(experiments, list) or not experiments:
        raise ValueError("Spec must include a non-empty experiments list")

    for idx, exp in enumerate(experiments):
        if not isinstance(exp, dict):
            raise ValueError(f"Experiment {idx} must be a mapping")
        for key in ("name", "mode", "strategy"):
            if not exp.get(key):
                raise ValueError(f"Experiment {idx} missing required key: {key}")
        if exp["mode"] not in MODES:
            raise ValueError(f"Experiment {exp['name']} has unsupported mode: {exp['mode']}")
        params = exp.get("params", {})
        if params is not None and not isinstance(params, dict):
            raise ValueError(f"Experiment {exp['name']} params must be a mapping")


def run_orchestration(spec_path: str, max_records: int = 0, dry_run: bool = False) -> dict[str, Any]:
    spec = load_spec(spec_path)
    validate_spec(spec)

    run_id = str(spec.get("run_id") or datetime.now().strftime("%Y%m%d_%H%M%S"))
    output_dir = os.path.join("reports", "research_runs", run_id)
    os.makedirs(output_dir, exist_ok=True)

    results: list[ExperimentResult] = []
    for exp in spec["experiments"]:
        result = run_experiment(spec, exp, run_id, output_dir, max_records=max_records, dry_run=dry_run)
        results.append(result)

    apply_ranking(results)
    summary = {
        "schema_version": 1,
        "run_id": run_id,
        "spec_path": spec_path,
        "report_only": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "spec": spec,
        "results": [asdict(result) for result in results],
    }
    summary_json = os.path.join(output_dir, "summary.json")
    with open(summary_json, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)

    summary_md = os.path.join(output_dir, "summary.md")
    with open(summary_md, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(summary))

    summary["summary_json"] = summary_json
    summary["summary_md"] = summary_md
    return summary


def run_experiment(
    spec: dict[str, Any],
    exp: dict[str, Any],
    run_id: str,
    output_dir: str,
    max_records: int = 0,
    dry_run: bool = False,
) -> ExperimentResult:
    mode = str(exp["mode"])
    name = str(exp["name"])
    strategy = str(exp["strategy"])
    assets = _as_list(exp.get("assets", spec.get("assets", ["btc", "eth", "sol", "xrp"])))
    params = dict(exp.get("params") or {})
    tags = _as_list(exp.get("tags", []))
    artifact = os.path.join(output_dir, f"{mode}_{name}.json")
    gates = merge_gates(spec.get("gates", {}), exp.get("gates", {}))

    if mode == REPLAY_MODE:
        command = build_replay_command(spec, strategy, assets, params, artifact, max_records=max_records)
    else:
        command = build_signal_eval_command(spec, strategy, assets, params, artifact, max_records=max_records)

    result = ExperimentResult(
        run_id=run_id,
        experiment_name=name,
        mode=mode,
        strategy=strategy,
        params=params,
        assets=assets,
        tags=tags,
        command=command,
        artifact=artifact,
    )

    if dry_run:
        result.returncode = 0
        result.failed_gates = ["dry_run_no_metrics"]
        result.gate_status = "fail"
        return result

    completed = subprocess.run(command, capture_output=True, text=True)
    result.returncode = completed.returncode
    result.stdout_tail = _tail(completed.stdout)
    result.stderr_tail = _tail(completed.stderr)
    if completed.returncode != 0:
        result.failed_gates = [f"command_failed:{completed.returncode}"]
        result.gate_status = "fail"
        return result

    metrics = normalize_artifact(mode, strategy, artifact)
    for key, value in metrics.items():
        setattr(result, key, value)
    result.failed_gates = evaluate_gates(asdict(result), gates, mode)
    result.gate_status = "pass" if not result.failed_gates else "fail"
    result.score = score_result(asdict(result))
    return result


def build_replay_command(
    spec: dict[str, Any],
    strategy: str,
    assets: list[str],
    params: dict[str, Any],
    artifact: str,
    max_records: int = 0,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "backtest.replay",
        "--strategy",
        strategy,
        "--assets",
        ",".join(assets),
        "--bankroll",
        str(spec.get("bankroll", 10_000)),
        "--out-json",
        artifact,
    ]
    _add_data_args(command, spec)
    if max_records:
        command.extend(["--max-records", str(max_records)])
    _add_param_args(command, params)
    return command


def build_signal_eval_command(
    spec: dict[str, Any],
    strategy: str,
    assets: list[str],
    params: dict[str, Any],
    artifact: str,
    max_records: int = 0,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "research.signal_eval",
        "--strategies",
        strategy,
        "--assets",
        ",".join(assets),
        "--out-metrics",
        artifact,
    ]
    _add_data_args(command, spec)
    if max_records:
        command.extend(["--max-records", str(max_records)])
    _add_param_args(command, params)
    return command


def _add_data_args(command: list[str], spec: dict[str, Any]) -> None:
    if spec.get("file"):
        command.extend(["--file", str(spec["file"])])
    else:
        command.extend(["--data-dir", str(spec.get("data_dir", "data_v2"))])


def _add_param_args(command: list[str], params: dict[str, Any]) -> None:
    for key, value in params.items():
        flag = "--" + str(key).replace("_", "-")
        if isinstance(value, bool):
            if value:
                command.append(flag)
        elif value is not None:
            command.extend([flag, str(value)])


def normalize_artifact(mode: str, strategy: str, artifact: str) -> dict[str, Any]:
    with open(artifact, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if mode == REPLAY_MODE:
        run = payload.get("runs", [{}])[0]
        summary = run.get("summary", {})
        return {
            "trades": int(summary.get("trades", 0) or 0),
            "total_pnl": float(summary.get("total_pnl", 0) or 0),
            "fees": float(summary.get("fees", 0) or 0),
            "win_rate": float(summary.get("win_rate", 0) or 0),
            "profit_factor": float(summary.get("profit_factor", 0) or 0),
            "max_drawdown": float(summary.get("max_drawdown", 0) or 0),
            "high_edge_win_rate_pct": float(summary.get("high_edge_win_rate_pct", 0) or 0),
            "low_edge_win_rate_pct": float(summary.get("low_edge_win_rate_pct", 0) or 0),
            "high_edge_trades": int(summary.get("high_edge_trades", 0) or 0),
            "low_edge_trades": int(summary.get("low_edge_trades", 0) or 0),
        }

    rows = payload if isinstance(payload, list) else []
    row = _pick_signal_row(rows, strategy)
    return {
        "signals": int(row.get("n", 0) or 0),
        "accuracy": _optional_float(row.get("accuracy")),
        "brier": _optional_float(row.get("brier")),
        "log_loss": _optional_float(row.get("log_loss")),
        "calibration_ece": _optional_float(row.get("calibration_ece")),
        "high_conf_precision": _optional_float(row.get("high_conf_precision")),
    }


def _pick_signal_row(rows: list[dict[str, Any]], strategy: str) -> dict[str, Any]:
    if not rows:
        return {}
    strategy_lower = strategy.lower()
    for row in rows:
        name = str(row.get("strategy_name", "")).lower()
        if name == strategy_lower or name.startswith(strategy_lower) or strategy_lower in name:
            return row
    return rows[0]


def merge_gates(defaults: Any, overrides: Any) -> dict[str, Any]:
    merged = dict(defaults or {})
    merged.update(dict(overrides or {}))
    return merged


def evaluate_gates(result: dict[str, Any], gates: dict[str, Any], mode: str) -> list[str]:
    failures = []
    replay_checks = [
        ("min_trades", "trades", ">="),
        ("max_drawdown", "max_drawdown", "<="),
        ("min_pnl", "total_pnl", ">="),
        ("min_profit_factor", "profit_factor", ">="),
        ("min_win_rate", "win_rate", ">="),
    ]
    signal_checks = [
        ("min_signals", "signals", ">="),
        ("min_accuracy", "accuracy", ">="),
        ("max_brier", "brier", "<="),
        ("max_log_loss", "log_loss", "<="),
        ("max_calibration_ece", "calibration_ece", "<="),
        ("min_high_conf_precision", "high_conf_precision", ">="),
    ]
    checks = replay_checks if mode == REPLAY_MODE else signal_checks
    for gate_key, result_key, operator in checks:
        if gate_key not in gates:
            continue
        actual = result.get(result_key)
        threshold = gates[gate_key]
        if actual is None:
            failures.append(f"{gate_key}:missing")
            continue
        if operator == ">=" and float(actual) < float(threshold):
            failures.append(f"{gate_key}:{actual}<{threshold}")
        elif operator == "<=" and float(actual) > float(threshold):
            failures.append(f"{gate_key}:{actual}>{threshold}")

    if mode == REPLAY_MODE and not result.get("trades"):
        failures.append("no_replay_trades")
    if mode == SIGNAL_MODE and not result.get("signals"):
        failures.append("no_signal_rows")
    return failures


def score_result(result: dict[str, Any]) -> float:
    if result.get("mode") == REPLAY_MODE:
        return (
            float(result.get("total_pnl") or 0)
            + float(result.get("profit_factor") or 0) * 10.0
            + float(result.get("win_rate") or 0) * 10.0
            - float(result.get("max_drawdown") or 0) * 100.0
        )
    return (
        float(result.get("accuracy") or 0) * 100.0
        + float(result.get("high_conf_precision") or 0) * 25.0
        - float(result.get("brier") or 0) * 50.0
        - float(result.get("calibration_ece") or 0) * 25.0
        - float(result.get("log_loss") or 0) * 10.0
    )


def apply_ranking(results: list[ExperimentResult]) -> None:
    passing = sorted(
        [result for result in results if result.gate_status == "pass"],
        key=lambda item: item.score,
        reverse=True,
    )
    for idx, result in enumerate(passing, start=1):
        result.rank = idx
        result.recommendation = "promote_candidate" if idx == 1 else "keep_testing"
    for result in results:
        if result.gate_status != "pass":
            result.rank = None
            result.recommendation = "reject"


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# Research Run {summary['run_id']}",
        "",
        "Report-only orchestration. No live config or strategy files were modified.",
        "",
        "| Rank | Experiment | Mode | Strategy | Status | Recommendation | Trades | Signals | PnL | Win Rate | PF | DD | Score |",
        "|---:|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summary["results"]:
        lines.append(
            "| {rank} | {name} | {mode} | {strategy} | {status} | {rec} | {trades} | {signals} | "
            "{pnl:.2f} | {win:.3f} | {pf:.3f} | {dd:.3f} | {score:.2f} |".format(
                rank=item.get("rank") or "",
                name=item["experiment_name"],
                mode=item["mode"],
                strategy=item["strategy"],
                status=item["gate_status"],
                rec=item["recommendation"],
                trades=item.get("trades", 0),
                signals=item.get("signals", 0),
                pnl=float(item.get("total_pnl") or 0),
                win=float(item.get("win_rate") or 0),
                pf=float(item.get("profit_factor") or 0),
                dd=float(item.get("max_drawdown") or 0),
                score=float(item.get("score") or 0),
            )
        )
    lines.extend(["", "## Failed Gates", ""])
    for item in summary["results"]:
        failures = ", ".join(item.get("failed_gates") or []) or "-"
        lines.append(f"- {item['experiment_name']}: {failures}")
    lines.append("")
    return "\n".join(lines)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(item) for item in value]


def _optional_float(value: Any) -> Optional[float]:
    return None if value is None else float(value)


def _tail(text: str, lines: int = 40) -> str:
    parts = text.splitlines()
    return "\n".join(parts[-lines:])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run report-only research orchestration")
    parser.add_argument("--spec", default="research/experiments.yaml")
    parser.add_argument("--max-records", type=int, default=0, help="Optional smoke-test cap passed to experiments")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print commands without executing")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = run_orchestration(args.spec, max_records=args.max_records, dry_run=args.dry_run)
    print(f"Research summary JSON: {summary['summary_json']}")
    print(f"Research summary Markdown: {summary['summary_md']}")


if __name__ == "__main__":
    main()
