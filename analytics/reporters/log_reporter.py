"""Log Reporter — enhanced terminal report on shutdown."""

import logging
from typing import Dict, List

from analytics.models import TradeRecord

log = logging.getLogger(__name__)


class LogReporter:

    def write(self, analysis: dict, trades: List[TradeRecord]):
        """Print comprehensive analysis to the log."""
        lines = []
        lines.append("")
        lines.append("=" * 70)
        lines.append("POST-SESSION ANALYSIS REPORT")
        lines.append("=" * 70)

        # Session summary
        session = analysis.get("session", {})
        lines.append(f"  Duration:        {session.get('duration_minutes', 0):.1f} minutes")
        lines.append(f"  Total Trades:    {session.get('total_trades', 0)}")
        lines.append(f"  Total PnL:       ${session.get('total_pnl', 0):.4f}")
        lines.append(f"  Sharpe Ratio:    {session.get('sharpe', 0):.2f}")
        lines.append(f"  Max Drawdown:    ${session.get('max_drawdown', 0):.4f}")
        lines.append(f"  Win Rate:        {session.get('win_rate_pct', 0):.1f}%")

        # Regime distribution
        regimes = analysis.get("regime_distribution", {})
        if regimes:
            lines.append("")
            lines.append("  Market Regimes Observed:")
            for regime, pct in regimes.items():
                lines.append(f"    {regime:12s}  {pct}%")

        # Per-strategy breakdown
        strategies = analysis.get("strategies", {})
        if strategies:
            lines.append("")
            lines.append("-" * 70)
            lines.append("  PER-STRATEGY BREAKDOWN")
            lines.append("-" * 70)

            for name, stats in strategies.items():
                t = stats.get("trades", 0)
                if t == 0:
                    continue
                lines.append(f"")
                lines.append(f"  [{name}]")
                lines.append(f"    Trades: {t}  Wins: {stats.get('wins',0)}  Losses: {stats.get('losses',0)}")
                lines.append(f"    Win Rate: {stats.get('win_rate_pct',0)}%  PnL: ${stats.get('total_pnl',0):.4f}")
                lines.append(f"    Avg Win: ${stats.get('avg_win',0):.4f}  Avg Loss: ${stats.get('avg_loss',0):.4f}")
                lines.append(f"    Profit Factor: {stats.get('profit_factor',0):.2f}  Sharpe: {stats.get('sharpe',0):.2f}")
                lines.append(f"    Avg Hold: {stats.get('avg_hold_time_s',0):.0f}s  Avg Slippage: {stats.get('avg_slippage',0):.6f}")

                # Exit reasons
                exits = stats.get("exit_reasons", {})
                if exits:
                    lines.append(f"    Exit Reasons: {exits}")

                # Regime P&L
                rpnl = stats.get("regime_pnl", {})
                if rpnl:
                    lines.append(f"    Regime PnL: {rpnl}")

                # Strategy-specific metrics
                for key, val in stats.items():
                    if key.startswith(("mm_", "arb_", "whale_", "mr_", "fade_", "btc5m_")):
                        lines.append(f"    {key}: {val}")

        # Top trades
        if trades:
            completed = sorted([t for t in trades if t.is_complete], key=lambda t: t.pnl, reverse=True)
            if completed:
                lines.append("")
                lines.append("-" * 70)
                lines.append("  TOP TRADES")
                lines.append("-" * 70)

                best = completed[:3]
                worst = completed[-3:] if len(completed) > 3 else []

                lines.append("  Best:")
                for t in best:
                    lines.append(f"    ${t.pnl:>8.4f}  {t.strategy:20s}  {t.entry_side} @ {t.entry_price:.4f}→{t.exit_price:.4f}  ({t.exit_reason})")

                if worst:
                    lines.append("  Worst:")
                    for t in reversed(worst):
                        lines.append(f"    ${t.pnl:>8.4f}  {t.strategy:20s}  {t.entry_side} @ {t.entry_price:.4f}→{t.exit_price:.4f}  ({t.exit_reason})")

        lines.append("")
        lines.append("=" * 70)

        report = "\n".join(lines)
        log.info(report)
        return report
