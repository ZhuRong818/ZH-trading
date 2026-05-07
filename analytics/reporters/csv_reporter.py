"""CSV Reporter — exports per-trade CSV for spreadsheet analysis."""

import csv
import os
from datetime import datetime
from typing import List

from analytics.models import TradeRecord


class CsvReporter:
    def __init__(self, output_dir: str = "reports"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def write(self, trades: List[TradeRecord]) -> str:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = os.path.join(self.output_dir, f"trades_{ts}.csv")

        if not trades:
            return path

        fields = [
            "trade_id", "strategy", "token_id", "side",
            "entry_price", "exit_price", "size",
            "pnl", "pnl_pct", "hold_time_s",
            "exit_reason", "slippage", "entry_edge",
            "entry_regime", "entry_spread", "hours_to_resolution",
            "entry_time", "exit_time",
        ]

        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for trade in trades:
                row = trade.to_dict()
                # Format timestamps
                row["entry_time"] = datetime.fromtimestamp(trade.entry_time).isoformat() if trade.entry_time else ""
                row["exit_time"] = datetime.fromtimestamp(trade.exit_time).isoformat() if trade.exit_time else ""
                writer.writerow(row)

        return path
