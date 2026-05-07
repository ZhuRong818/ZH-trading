"""
Base analyzer — shared metrics that every strategy gets.
Per-strategy analyzers inherit and add their own KPIs.
"""

import math
from typing import List
from analytics.models import TradeRecord


class BaseAnalyzer:
    """Computes universal metrics for a list of trades."""

    def __init__(self, strategy_name: str):
        self.strategy_name = strategy_name

    def analyze(self, trades: List[TradeRecord]) -> dict:
        """Run analysis. Override in subclass to add strategy-specific metrics."""
        if not trades:
            return {"strategy": self.strategy_name, "trades": 0}

        completed = [t for t in trades if t.is_complete]
        if not completed:
            return {"strategy": self.strategy_name, "trades": 0, "open": len(trades)}

        wins = [t for t in completed if t.pnl > 0]
        losses = [t for t in completed if t.pnl <= 0]
        pnls = [t.pnl for t in completed]

        gross_profit = sum(t.pnl for t in wins)
        gross_loss = sum(t.pnl for t in losses)
        total_pnl = sum(pnls)

        hold_times = [t.hold_time_seconds for t in completed]
        slippages = [t.slippage for t in completed if t.slippage != 0]

        # Sharpe
        if len(pnls) >= 2:
            avg = sum(pnls) / len(pnls)
            var = sum((p - avg) ** 2 for p in pnls) / len(pnls)
            sharpe = (avg / math.sqrt(var)) * math.sqrt(252) if var > 0 else 0
        else:
            sharpe = 0

        # Max drawdown from trade sequence
        peak = 0
        max_dd = 0
        cumulative = 0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        # Exit reason distribution
        exit_reasons = {}
        for t in completed:
            exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

        # Regime distribution
        regime_pnl = {}
        regime_count = {}
        for t in completed:
            r = t.entry_regime or "unknown"
            regime_pnl[r] = regime_pnl.get(r, 0) + t.pnl
            regime_count[r] = regime_count.get(r, 0) + 1

        result = {
            "strategy": self.strategy_name,
            "trades": len(completed),
            "open": len(trades) - len(completed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(completed) * 100, 1),
            "total_pnl": round(total_pnl, 4),
            "avg_pnl": round(total_pnl / len(completed), 4),
            "avg_win": round(gross_profit / len(wins), 4) if wins else 0,
            "avg_loss": round(gross_loss / len(losses), 4) if losses else 0,
            "profit_factor": round(gross_profit / abs(gross_loss), 2) if gross_loss != 0 else float("inf"),
            "sharpe": round(sharpe, 2),
            "max_drawdown": round(max_dd, 4),
            "avg_hold_time_s": round(sum(hold_times) / len(hold_times), 1),
            "max_hold_time_s": round(max(hold_times), 1),
            "avg_slippage": round(sum(slippages) / len(slippages), 6) if slippages else 0,
            "total_slippage": round(sum(abs(s) for s in slippages), 4),
            "exit_reasons": exit_reasons,
            "regime_pnl": {k: round(v, 4) for k, v in regime_pnl.items()},
            "regime_trades": regime_count,
        }

        # Add strategy-specific metrics
        result.update(self.strategy_metrics(trades, completed))
        return result

    def strategy_metrics(self, all_trades: List[TradeRecord], completed: List[TradeRecord]) -> dict:
        """Override in subclass for strategy-specific KPIs."""
        return {}
