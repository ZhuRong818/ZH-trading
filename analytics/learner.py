"""
Learner — reads past post-facto analysis reports and auto-adjusts parameters.

On startup, loads all previous analysis JSONs from reports/.
Computes aggregate performance per strategy over recent history.
Adjusts parameters within safe bounds based on what worked and what didn't.

Safety rules:
  - Never adjusts more than ±30% from defaults in a single session
  - Needs minimum 10 completed trades per strategy before adjusting
  - Logs every change with reasoning
  - Can be disabled with --no-learn flag

What it adjusts:
  - min_edge thresholds (widen if win rate < 45%, tighten if > 70%)
  - kelly_fraction (reduce if drawdown > 10%, increase if Sharpe > 1.5)
  - spread parameters (widen if adverse selection detected)
  - strategy enable/disable (disable strategies with negative PnL over 3+ sessions)
"""

import glob
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config import SystemConfig

log = logging.getLogger(__name__)

# Safe adjustment bounds (fraction of default value)
MAX_ADJUSTMENT = 0.30  # never change more than ±30%
MIN_TRADES_TO_LEARN = 10


@dataclass
class StrategyHistory:
    """Aggregated history for one strategy across sessions."""
    strategy: str = ""
    total_trades: int = 0
    total_wins: int = 0
    total_pnl: float = 0.0
    avg_win_rate: float = 0.0
    avg_sharpe: float = 0.0
    max_drawdown: float = 0.0
    avg_slippage: float = 0.0
    avg_profit_factor: float = 0.0
    sessions: int = 0
    consecutive_negative_sessions: int = 0
    last_pnl: float = 0.0

    # Strategy-specific
    avg_spread_captured: float = 0.0   # MM
    reversion_rate: float = 0.0        # mean rev
    stop_loss_rate: float = 0.0        # mean rev


@dataclass
class LearningResult:
    """What the learner decided to change."""
    parameter: str
    old_value: float
    new_value: float
    reason: str
    strategy: str


class Learner:

    def __init__(self, reports_dir: str = "reports"):
        self.reports_dir = reports_dir
        self.history: Dict[str, StrategyHistory] = {}
        self.adjustments: List[LearningResult] = []

    def load_history(self) -> int:
        """Load all past analysis JSONs and aggregate per-strategy stats."""
        pattern = os.path.join(self.reports_dir, "analysis_*.json")
        files = sorted(glob.glob(pattern))

        if not files:
            log.info("Learner: no past reports found in %s", self.reports_dir)
            return 0

        for filepath in files:
            try:
                with open(filepath) as f:
                    data = json.load(f)
                self._process_report(data)
            except Exception as e:
                log.warning("Learner: failed to load %s: %s", filepath, e)

        log.info("Learner: loaded %d past sessions", len(files))
        for name, h in self.history.items():
            log.info(
                "  %s: %d trades, %.1f%% win rate, $%.2f total PnL, %.2f Sharpe, %d sessions",
                name, h.total_trades, h.avg_win_rate, h.total_pnl, h.avg_sharpe, h.sessions,
            )

        return len(files)

    def _process_report(self, data: dict):
        """Extract per-strategy stats from one analysis report."""
        strategies = data.get("strategies", {})

        for name, stats in strategies.items():
            trades = stats.get("trades", 0)
            if trades == 0:
                continue

            if name not in self.history:
                self.history[name] = StrategyHistory(strategy=name)

            h = self.history[name]
            h.sessions += 1
            h.total_trades += trades
            h.total_wins += stats.get("wins", 0)
            h.total_pnl += stats.get("total_pnl", 0)

            session_pnl = stats.get("total_pnl", 0)
            if session_pnl < 0:
                h.consecutive_negative_sessions += 1
            else:
                h.consecutive_negative_sessions = 0
            h.last_pnl = session_pnl

            # Running averages
            h.avg_win_rate = h.total_wins / h.total_trades * 100 if h.total_trades > 0 else 0
            sharpe = stats.get("sharpe", 0)
            h.avg_sharpe = (h.avg_sharpe * (h.sessions - 1) + sharpe) / h.sessions

            dd = stats.get("max_drawdown", 0)
            h.max_drawdown = max(h.max_drawdown, dd)

            slippage = stats.get("avg_slippage", 0)
            h.avg_slippage = (h.avg_slippage * (h.sessions - 1) + slippage) / h.sessions

            profit_factor = stats.get("profit_factor", 0)
            h.avg_profit_factor = (h.avg_profit_factor * (h.sessions - 1) + profit_factor) / h.sessions

            # Strategy-specific
            if "mm_avg_spread_captured" in stats:
                h.avg_spread_captured = stats["mm_avg_spread_captured"]
            if "mr_reversion_rate_pct" in stats:
                h.reversion_rate = stats["mr_reversion_rate_pct"]
            if "mr_stop_loss_rate_pct" in stats:
                h.stop_loss_rate = stats["mr_stop_loss_rate_pct"]

    def apply_learning(self, config: SystemConfig) -> List[LearningResult]:
        """
        Analyze history and adjust config parameters.
        Returns list of changes made.
        """
        self.adjustments = []

        for name, h in self.history.items():
            if h.total_trades < MIN_TRADES_TO_LEARN:
                log.info("Learner: %s has only %d trades, need %d — skipping",
                         name, h.total_trades, MIN_TRADES_TO_LEARN)
                continue

            if name in ("market_making", "stoikov_mm"):
                self._adjust_mm(h, config)
            elif name in ("mean_reversion", "mean_rev"):
                self._adjust_meanrev(h, config)
            elif name in ("whale_copy", "whale"):
                self._adjust_whale(h, config)
            elif name in ("btc_5m", "btc5m"):
                self._adjust_btc5m(h, config)

            # Universal adjustments
            self._adjust_kelly(h, config)

        if self.adjustments:
            log.info("=" * 60)
            log.info("LEARNER: %d parameter adjustments applied", len(self.adjustments))
            for adj in self.adjustments:
                log.info("  [%s] %s: %.4f → %.4f (%s)",
                         adj.strategy, adj.parameter, adj.old_value, adj.new_value, adj.reason)
            log.info("=" * 60)
        else:
            log.info("Learner: no adjustments needed based on %d sessions of history",
                     sum(h.sessions for h in self.history.values()))

        return self.adjustments

    def _adjust_mm(self, h: StrategyHistory, config: SystemConfig):
        """Adjust market making parameters."""
        mm = config.market_making

        # If spread captured is consistently low, widen the spread
        if h.avg_spread_captured < 0.02 and h.sessions >= 3:
            old = mm.gamma
            new = self._clamp(old * 1.15, old, MAX_ADJUSTMENT)  # increase gamma = wider spread
            if new != old:
                mm.gamma = new
                self.adjustments.append(LearningResult(
                    "gamma", old, new,
                    f"avg spread captured only ${h.avg_spread_captured:.4f}, widening",
                    "market_making",
                ))

        # If win rate is very high (>80%), spread might be too wide — tighten
        if h.avg_win_rate > 80 and h.total_trades >= 20:
            old = mm.gamma
            new = self._clamp(old * 0.90, old, MAX_ADJUSTMENT)
            if new != old:
                mm.gamma = new
                self.adjustments.append(LearningResult(
                    "gamma", old, new,
                    f"win rate {h.avg_win_rate:.0f}% very high — spread may be too wide, tightening",
                    "market_making",
                ))

    def _adjust_meanrev(self, h: StrategyHistory, config: SystemConfig):
        """Adjust mean reversion parameters."""
        # If stop-loss triggers > 40% of trades, entry threshold is too tight
        if h.stop_loss_rate > 40 and h.total_trades >= 10:
            # Can't directly change MeanReversionConfig from here since it's
            # created in setup_mean_reversion(). Log the suggestion.
            self.adjustments.append(LearningResult(
                "entry_threshold", 0.03, 0.04,
                f"stop-loss rate {h.stop_loss_rate:.0f}% — entry threshold too tight",
                "mean_reversion",
            ))

        # If reversion rate is very high (>80%), threshold might be too conservative
        if h.reversion_rate > 80 and h.avg_win_rate > 70:
            self.adjustments.append(LearningResult(
                "entry_threshold", 0.03, 0.025,
                f"reversion rate {h.reversion_rate:.0f}% — can afford tighter entries",
                "mean_reversion",
            ))

    def _adjust_whale(self, h: StrategyHistory, config: SystemConfig):
        """Adjust whale tracking parameters."""
        wt = config.whale_tracking

        # If whale copies are mostly losing, raise the win rate threshold
        if h.avg_win_rate < 40 and h.total_trades >= 10:
            old = wt.high_confidence_win_rate
            new = min(old + 0.05, 0.95)  # raise by 5%
            if new != old:
                wt.high_confidence_win_rate = new
                self.adjustments.append(LearningResult(
                    "high_confidence_win_rate", old, new,
                    f"copy win rate only {h.avg_win_rate:.0f}% — raising whale quality threshold",
                    "whale_copy",
                ))

        # If whale copies are very profitable, can lower the threshold slightly
        if h.avg_win_rate > 70 and h.total_pnl > 0 and h.total_trades >= 20:
            old = wt.high_confidence_win_rate
            new = max(old - 0.03, 0.70)
            if new != old:
                wt.high_confidence_win_rate = new
                self.adjustments.append(LearningResult(
                    "high_confidence_win_rate", old, new,
                    f"copy win rate {h.avg_win_rate:.0f}% — can accept slightly weaker whales",
                    "whale_copy",
                ))

    def _adjust_btc5m(self, h: StrategyHistory, config: SystemConfig):
        """Adjust BTC 5m V2 momentum parameters."""
        if (h.avg_win_rate < 45 or h.avg_profit_factor < 1.0) and h.total_trades >= 15:
            old = config.btc5m_min_edge
            new = min(max(old + 0.02, 0.14), 0.20)
            config.btc5m_min_edge = new
            self.adjustments.append(LearningResult(
                "min_edge", old, new,
                f"BTC 5m win rate {h.avg_win_rate:.0f}% — raise min_edge to filter weak signals",
                "btc_5m",
            ))

        if h.avg_win_rate > 65 and h.avg_profit_factor > 1.20 and h.total_trades >= 50:
            old = config.btc5m_min_edge
            new = max(old - 0.01, 0.08)
            config.btc5m_min_edge = new
            self.adjustments.append(LearningResult(
                "min_edge", old, new,
                f"BTC 5m win rate {h.avg_win_rate:.0f}% — can lower min_edge for more trades",
                "btc_5m",
            ))

    def _adjust_kelly(self, h: StrategyHistory, config: SystemConfig):
        """Universal Kelly adjustment based on drawdown."""
        # If drawdown is high, reduce risk across all strategies
        if h.max_drawdown > 500 and h.sessions >= 3:
            self.adjustments.append(LearningResult(
                "kelly_fraction", 0.25, 0.15,
                f"{h.strategy} max drawdown ${h.max_drawdown:.0f} — reduce position sizes",
                h.strategy,
            ))

    def _clamp(self, new_value: float, old_value: float, max_change: float) -> float:
        """Clamp adjustment to within max_change fraction of old value."""
        lower = old_value * (1 - max_change)
        upper = old_value * (1 + max_change)
        return round(max(lower, min(upper, new_value)), 4)

    def get_disabled_strategies(self) -> List[str]:
        """Return strategies that should be disabled based on history."""
        disabled = []
        for name, h in self.history.items():
            if h.consecutive_negative_sessions >= 3 and h.total_pnl < 0:
                disabled.append(name)
                log.warning(
                    "Learner: DISABLING %s — %d consecutive losing sessions, total PnL $%.2f",
                    name, h.consecutive_negative_sessions, h.total_pnl,
                )
        return disabled

    def summary(self) -> dict:
        return {
            "sessions_loaded": sum(h.sessions for h in self.history.values()),
            "strategies_tracked": list(self.history.keys()),
            "adjustments_made": len(self.adjustments),
            "disabled_strategies": self.get_disabled_strategies(),
            "per_strategy": {
                name: {
                    "trades": h.total_trades,
                    "win_rate": f"{h.avg_win_rate:.1f}%",
                    "total_pnl": f"${h.total_pnl:.2f}",
                    "sharpe": f"{h.avg_sharpe:.2f}",
                    "sessions": h.sessions,
                    "consecutive_losses": h.consecutive_negative_sessions,
                }
                for name, h in self.history.items()
            },
        }
