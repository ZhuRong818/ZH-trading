"""
Persistent Trade Log — JSONL format, survives restarts.

One JSON object per line, append-only. New file per day.
No external dependencies (no databases, no Redis).
"""

import json
import logging
import os
import time
from datetime import datetime

from oms.position_manager import Fill

log = logging.getLogger(__name__)


class TradeLog:

    def __init__(self, log_dir: str = "logs"):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self._file = None
        self._current_date = None
        self._rotate()

    def _rotate(self):
        """Open a new log file if the date changed."""
        today = datetime.now().strftime("%Y-%m-%d")
        if today == self._current_date and self._file:
            return
        if self._file:
            self._file.close()
        self._current_date = today
        path = os.path.join(self.log_dir, f"trades_{today}.jsonl")
        self._file = open(path, "a")
        log.info("Trade log: %s", path)

    def record_fill(self, fill: Fill):
        """Append a fill to the log file."""
        self._rotate()
        entry = {
            "ts": fill.timestamp,
            "time": datetime.fromtimestamp(fill.timestamp).isoformat(),
            "token": fill.token_id[:20],
            "side": fill.side,
            "size": round(fill.size, 2),
            "price": round(fill.price, 6),
            "source": fill.source,
            "order_id": fill.order_id[:20] if fill.order_id else "",
            "edge": round(fill.edge, 6),
            "fair_value": round(fill.fair_value, 6),
        }
        self._file.write(json.dumps(entry) + "\n")
        self._file.flush()

    def load_today(self) -> list:
        """Load today's trades."""
        today = datetime.now().strftime("%Y-%m-%d")
        return self._load_file(f"trades_{today}.jsonl")

    def load_history(self, days: int = 30) -> list:
        """Load trades from the last N days."""
        all_trades = []
        files = sorted(os.listdir(self.log_dir))
        for f in files[-days:]:
            if f.startswith("trades_") and f.endswith(".jsonl"):
                all_trades.extend(self._load_file(f))
        return all_trades

    def _load_file(self, filename: str) -> list:
        path = os.path.join(self.log_dir, filename)
        if not os.path.exists(path):
            return []
        trades = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    trades.append(json.loads(line))
        return trades

    def close(self):
        if self._file:
            self._file.close()
            self._file = None
