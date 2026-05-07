"""
Parameter Tuner — Heuristic suggestions based on actual performance.

Analyzes recent trade history and suggests parameter adjustments.
Never auto-applies — logs suggestions for human review.
"""

import logging
from typing import List

from analytics.performance import PerformanceTracker

log = logging.getLogger(__name__)


class ParameterTuner:

    def __init__(self, performance: PerformanceTracker):
        self.perf = performance
        self._last_suggestions: List[str] = []

    def suggest(self) -> List[str]:
        """Generate parameter tuning suggestions."""
        suggestions = []
        summary = self.perf.per_strategy_summary()

        for name, stats_dict in summary.items():
            trades = stats_dict["trades"]
            if trades < 5:
                continue  # not enough data

            win_rate = float(stats_dict["win_rate"].rstrip("%"))
            pnl = float(stats_dict["total_pnl"].lstrip("$"))
            pf = float(stats_dict["profit_factor"])

            # Low win rate
            if win_rate < 40:
                suggestions.append(
                    f"[{name}] Win rate {win_rate:.0f}% is low. "
                    f"Consider increasing min_edge threshold to filter weak signals."
                )

            # Losing money
            if pnl < 0 and trades >= 10:
                suggestions.append(
                    f"[{name}] Negative PnL (${pnl:.2f}) over {trades} trades. "
                    f"Consider pausing this strategy or widening spreads."
                )

            # Profit factor below 1 (losing)
            if pf < 1.0 and trades >= 10:
                suggestions.append(
                    f"[{name}] Profit factor {pf:.2f} < 1.0 — losses exceed gains. "
                    f"Review entry criteria."
                )

            # Very high win rate but low PnL (cutting winners too early)
            if win_rate > 80 and pnl < trades * 0.5:
                suggestions.append(
                    f"[{name}] High win rate ({win_rate:.0f}%) but low PnL. "
                    f"Avg wins may be too small — consider wider take-profit targets."
                )

            # No trades in a long time
            if trades == 0:
                suggestions.append(
                    f"[{name}] Zero trades. Entry thresholds may be too tight — "
                    f"consider lowering min_edge or widening the signal threshold."
                )

        # Portfolio-level suggestions
        dd = self.perf.max_drawdown_pct()
        if dd > 15:
            suggestions.append(
                f"[PORTFOLIO] Max drawdown {dd:.1f}% is high. "
                f"Consider reducing kelly_fraction or max_bet_pct."
            )

        sharpe = self.perf.sharpe_ratio()
        if sharpe < 0.5 and self.perf._trade_count >= 20:
            suggestions.append(
                f"[PORTFOLIO] Sharpe ratio {sharpe:.2f} is low. "
                f"Returns are noisy relative to risk. Consider reducing position sizes."
            )

        self._last_suggestions = suggestions
        return suggestions

    def log_suggestions(self):
        """Log suggestions if any."""
        suggestions = self.suggest()
        if suggestions:
            log.info("=== PARAMETER TUNING SUGGESTIONS ===")
            for s in suggestions:
                log.info("  %s", s)
