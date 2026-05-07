"""
Post-Session Analyzer — orchestrates all analysis on shutdown.

Collects data during the session, runs per-strategy analyzers,
generates reports in multiple formats.

Usage:
    analyzer = PostSessionAnalyzer(data_feed, config)

    # During session: register components and record fills
    analyzer.register_token("token_123")
    analyzer.register_strategy(mm_instance, "stoikov_mm")
    analyzer.on_fill(fill)          # call on every fill
    analyzer.collect_snapshots()    # call each loop iteration

    # On shutdown: run analysis and generate reports
    analyzer.run_analysis()
"""

import logging
import time
from collections import defaultdict
from typing import Dict, List

from analytics.models import TradeRecord
from analytics.trade_recorder import TradeRecorder
from analytics.snapshot_collector import SnapshotCollector
from analytics.strategy_analyzers.analyzers import get_analyzer
from analytics.reporters.json_reporter import JsonReporter
from analytics.reporters.csv_reporter import CsvReporter
from analytics.reporters.log_reporter import LogReporter
from data_pipeline.market_data import MarketDataFeed
from oms.position_manager import Fill

log = logging.getLogger(__name__)


class PostSessionAnalyzer:

    def __init__(
        self,
        data_feed: MarketDataFeed,
        output_dir: str = "reports",
        snapshot_interval: float = 30.0,
    ):
        self.data = data_feed
        self.output_dir = output_dir
        self.start_time = time.time()

        # Data collectors
        self.trade_recorder = TradeRecorder(data_feed)
        self.snapshot_collector = SnapshotCollector(data_feed, interval=snapshot_interval)

        # Reporters
        self.json_reporter = JsonReporter(output_dir)
        self.csv_reporter = CsvReporter(output_dir)
        self.log_reporter = LogReporter()

        # Analysis result cache
        self._last_analysis: Dict = {}

    # ---- Registration ----

    def register_token(self, token_id: str):
        self.snapshot_collector.register_token(token_id)

    def register_strategy(self, strategy_obj, name: str):
        self.snapshot_collector.register_strategy(strategy_obj, name)

    # ---- Data Collection (called during session) ----

    def on_fill(self, fill: Fill):
        """Record every fill for round-trip tracking."""
        self.trade_recorder.on_fill(fill)

    def collect_snapshots(self):
        """Call each loop iteration — collects if interval elapsed."""
        self.snapshot_collector.collect_if_due()

    # ---- Analysis (called on shutdown) ----

    def run_analysis(self) -> dict:
        """Run full post-session analysis and generate all reports."""
        log.info("Running post-session analysis...")

        all_trades = self.trade_recorder.get_all_trades()
        duration_seconds = time.time() - self.start_time

        # Group trades by strategy
        strategy_trades: Dict[str, List[TradeRecord]] = defaultdict(list)
        for trade in all_trades:
            key = self._normalize_strategy(trade.strategy)
            strategy_trades[key].append(trade)

        # Run per-strategy analyzers
        strategy_results = {}
        for strategy_name, trades in strategy_trades.items():
            analyzer = get_analyzer(strategy_name)
            strategy_results[strategy_name] = analyzer.analyze(trades)

        # Session-level aggregates
        completed = [t for t in all_trades if t.is_complete]
        total_pnl = sum(t.pnl for t in completed)
        wins = sum(1 for t in completed if t.pnl > 0)

        # Sharpe from completed trades
        import math
        pnls = [t.pnl for t in completed]
        if len(pnls) >= 2:
            avg = sum(pnls) / len(pnls)
            var = sum((p - avg) ** 2 for p in pnls) / len(pnls)
            sharpe = (avg / math.sqrt(var)) * math.sqrt(252) if var > 0 else 0
        else:
            sharpe = 0

        # Max drawdown
        peak = 0
        max_dd = 0
        cum = 0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)

        analysis = {
            "session": {
                "start_time": self.start_time,
                "duration_minutes": round(duration_seconds / 60, 1),
                "total_trades": len(completed),
                "open_trades": len(all_trades) - len(completed),
                "total_pnl": round(total_pnl, 4),
                "wins": wins,
                "losses": len(completed) - wins,
                "win_rate_pct": round(wins / len(completed) * 100, 1) if completed else 0,
                "sharpe": round(sharpe, 2),
                "max_drawdown": round(max_dd, 4),
            },
            "strategies": strategy_results,
            "regime_distribution": self.snapshot_collector.get_regime_distribution(),
            "market_snapshots_count": len(self.snapshot_collector.market_snapshots),
            "strategy_snapshots_count": len(self.snapshot_collector.strategy_snapshots),
        }

        self._last_analysis = analysis

        # Generate reports
        self._generate_reports(analysis, all_trades)

        return analysis

    def _generate_reports(self, analysis: dict, trades: List[TradeRecord]):
        """Generate all report formats."""
        # Log report (always)
        self.log_reporter.write(analysis, trades)

        # JSON report
        try:
            path = self.json_reporter.write(analysis)
            log.info("JSON report: %s", path)
        except Exception as e:
            log.warning("JSON report failed: %s", e)

        # CSV report
        try:
            path = self.csv_reporter.write(trades)
            log.info("CSV report: %s", path)
        except Exception as e:
            log.warning("CSV report failed: %s", e)

    def _normalize_strategy(self, source: str) -> str:
        s = source.lower()
        if "stoikov" in s or s.startswith("mm"):
            return "market_making"
        if "whale" in s:
            return "whale_copy"
        if "arb" in s:
            return "arbitrage"
        if "mean_rev" in s:
            return "mean_reversion"
        if "fade" in s:
            return "resolution_fade"
        if "btc5m" in s:
            return "btc_5m"
        if "stop_loss" in s or "kill" in s:
            return "risk_exit"
        return source
