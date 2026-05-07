"""
Performance Tracker — Sharpe, drawdown, win rate, per-strategy breakdown.

Works across all strategies using the Fill source field.
No external dependencies.
"""

import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Tuple

log = logging.getLogger(__name__)


@dataclass
class StrategyStats:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    returns: list = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        if self.trades == 0:
            return 0.0
        return self.wins / self.trades * 100

    @property
    def avg_win(self) -> float:
        return self.gross_profit / self.wins if self.wins > 0 else 0

    @property
    def avg_loss(self) -> float:
        return self.gross_loss / self.losses if self.losses > 0 else 0

    @property
    def profit_factor(self) -> float:
        return self.gross_profit / abs(self.gross_loss) if self.gross_loss != 0 else float('inf')


class PerformanceTracker:
    """
    Tracks performance metrics across all strategies.
    """

    def __init__(self, initial_capital: float = 100_000):
        self.initial_capital = initial_capital
        self._equity_curve: List[Tuple[float, float]] = [(time.time(), initial_capital)]
        self._strategy_stats: dict[str, StrategyStats] = defaultdict(StrategyStats)
        self._total_pnl = 0.0
        self._peak_equity = initial_capital
        self._max_drawdown = 0.0
        self._trade_count = 0

    def record_trade(self, pnl: float, strategy: str):
        """Record a completed trade (round-trip)."""
        self._trade_count += 1
        self._total_pnl += pnl

        stats = self._strategy_stats[self._normalize_strategy(strategy)]
        stats.trades += 1
        stats.total_pnl += pnl
        stats.returns.append(pnl)

        if pnl > 0:
            stats.wins += 1
            stats.gross_profit += pnl
        elif pnl < 0:
            stats.losses += 1
            stats.gross_loss += pnl

        # Update equity curve
        equity = self.initial_capital + self._total_pnl
        self._equity_curve.append((time.time(), equity))

        # Drawdown tracking
        if equity > self._peak_equity:
            self._peak_equity = equity
        dd = (self._peak_equity - equity) / self._peak_equity * 100 if self._peak_equity > 0 else 0
        if dd > self._max_drawdown:
            self._max_drawdown = dd

    def sharpe_ratio(self, annualization: float = 252) -> float:
        """Sharpe ratio from trade returns (annualized assuming ~1 trade/day)."""
        all_returns = []
        for stats in self._strategy_stats.values():
            all_returns.extend(stats.returns)
        if len(all_returns) < 2:
            return 0.0
        avg = sum(all_returns) / len(all_returns)
        variance = sum((r - avg) ** 2 for r in all_returns) / len(all_returns)
        std = math.sqrt(variance) if variance > 0 else 1e-10
        return (avg / std) * math.sqrt(annualization)

    def max_drawdown_pct(self) -> float:
        return self._max_drawdown

    def per_strategy_summary(self) -> dict:
        result = {}
        for name, stats in self._strategy_stats.items():
            result[name] = {
                "trades": stats.trades,
                "wins": stats.wins,
                "losses": stats.losses,
                "win_rate": f"{stats.win_rate:.1f}%",
                "total_pnl": f"${stats.total_pnl:.2f}",
                "avg_win": f"${stats.avg_win:.2f}",
                "avg_loss": f"${stats.avg_loss:.2f}",
                "profit_factor": f"{stats.profit_factor:.2f}",
            }
        return result

    def report(self) -> str:
        """One-line status for the main loop log."""
        equity = self.initial_capital + self._total_pnl
        return (
            f"PnL=${self._total_pnl:.2f} equity=${equity:.0f} "
            f"trades={self._trade_count} DD={self._max_drawdown:.1f}% "
            f"sharpe={self.sharpe_ratio():.2f}"
        )

    def full_report(self) -> str:
        """Multi-line report for shutdown."""
        lines = [
            "=" * 60,
            "PERFORMANCE REPORT",
            "=" * 60,
            f"  Total PnL:      ${self._total_pnl:.2f}",
            f"  Total Trades:   {self._trade_count}",
            f"  Max Drawdown:   {self._max_drawdown:.1f}%",
            f"  Sharpe Ratio:   {self.sharpe_ratio():.2f}",
            "",
            "  Per-Strategy Breakdown:",
        ]
        for name, stats in sorted(self._strategy_stats.items()):
            lines.append(
                f"    {name:20s}  trades={stats.trades:4d}  "
                f"win={stats.win_rate:5.1f}%  pnl=${stats.total_pnl:>8.2f}  "
                f"PF={stats.profit_factor:.2f}"
            )
        lines.append("=" * 60)
        return "\n".join(lines)

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
