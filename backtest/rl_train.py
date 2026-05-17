"""Train the lightweight tabular RL model on replay JSONL data."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.replay import compute_max_drawdown, compute_window_outcomes, load_records
from backtest.rl_env import ACTIONS, split_records_by_time, train_model


log = logging.getLogger(__name__)


def _evaluate_split(model, records_by_asset, bankroll: float, min_price: float, max_price: float) -> dict:
    from backtest.replay import RLReplayStrategy

    strategy = RLReplayStrategy(model=model, min_price=min_price, max_price=max_price)
    results = strategy.run_multi(records_by_asset, bankroll=bankroll)
    total_trades = sum(len(r.trades) for r in results.values())
    total_pnl = sum(r.total_pnl for r in results.values())
    total_wins = sum(r.wins for r in results.values())
    total_losses = sum(r.losses for r in results.values())
    max_dd = max((r.max_drawdown for r in results.values()), default=0.0)
    win_rate = total_wins / max(total_wins + total_losses, 1)
    return {
        "trades": total_trades,
        "pnl": total_pnl,
        "wins": total_wins,
        "losses": total_losses,
        "win_rate": win_rate,
        "max_drawdown": max_dd,
    }


def main():
    parser = argparse.ArgumentParser(description="Train lightweight RL model on replay data")
    parser.add_argument("--data-dir", type=str, default="data_v2")
    parser.add_argument("--file", type=str, default=None)
    parser.add_argument("--assets", type=str, default="btc,eth,sol,xrp")
    parser.add_argument("--bankroll", type=float, default=10_000)
    parser.add_argument("--min-price", type=float, default=0.20)
    parser.add_argument("--max-price", type=float, default=0.95)
    parser.add_argument("--model-out", type=str, default="reports/rl_model.json")
    parser.add_argument("--report-out", type=str, default="reports/rl_training_report.json")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    assets = [a.strip().lower() for a in args.assets.split(",") if a.strip()]
    all_records = load_records(data_dir=args.data_dir, file_path=args.file, assets=assets)
    if not all_records:
        print("No data found!")
        sys.exit(1)

    train_records, val_records, test_records = split_records_by_time(all_records)
    outcomes = {asset: compute_window_outcomes(records) for asset, records in train_records.items()}
    model = train_model(
        train_records,
        outcomes,
        bankroll=args.bankroll,
        min_price=args.min_price,
        max_price=args.max_price,
    )
    model.metadata.update({
        "assets": assets,
        "actions": list(ACTIONS),
        "split": "time_ordered_70_15_15",
    })
    model.save(args.model_out)

    report = {
        "model": args.model_out,
        "metadata": model.metadata,
        "train": _evaluate_split(model, train_records, args.bankroll, args.min_price, args.max_price),
        "validation": _evaluate_split(model, val_records, args.bankroll, args.min_price, args.max_price),
        "test": _evaluate_split(model, test_records, args.bankroll, args.min_price, args.max_price),
    }
    os.makedirs(os.path.dirname(args.report_out) or ".", exist_ok=True)
    with open(args.report_out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)

    print(f"Saved RL model: {args.model_out}")
    print(f"Saved training report: {args.report_out}")
    for split in ("train", "validation", "test"):
        item = report[split]
        print(
            f"{split:10s} trades={item['trades']:5d} pnl=${item['pnl']:+.2f} "
            f"wr={item['win_rate']*100:.1f}% maxDD={item['max_drawdown']*100:.1f}%"
        )


if __name__ == "__main__":
    main()
