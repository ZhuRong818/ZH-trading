"""
Per-strategy analyzers — each adds strategy-specific KPIs
on top of the base metrics.
"""

from typing import List
from analytics.models import TradeRecord
from analytics.strategy_analyzers.base import BaseAnalyzer


class MarketMakingAnalyzer(BaseAnalyzer):
    def __init__(self):
        super().__init__("market_making")

    def strategy_metrics(self, all_trades, completed) -> dict:
        if not completed:
            return {}

        # Spread captured: avg of (exit_price - entry_price) for wins
        spreads = []
        for t in completed:
            if t.entry_side == "BUY":
                spreads.append(t.exit_price - t.entry_price)
            else:
                spreads.append(t.entry_price - t.exit_price)

        # Both-side fill rate: how often did we get a round trip
        total_entries = len(all_trades)
        round_trips = len(completed)

        # Inventory accumulation: how many open trades (unfilled one side)
        open_trades = [t for t in all_trades if t.is_open]

        return {
            "mm_avg_spread_captured": round(sum(spreads) / len(spreads), 4) if spreads else 0,
            "mm_max_spread_captured": round(max(spreads), 4) if spreads else 0,
            "mm_round_trip_rate_pct": round(round_trips / total_entries * 100, 1) if total_entries > 0 else 0,
            "mm_open_inventory": len(open_trades),
            "mm_positive_spreads_pct": round(sum(1 for s in spreads if s > 0) / len(spreads) * 100, 1) if spreads else 0,
        }


class ArbitrageAnalyzer(BaseAnalyzer):
    def __init__(self):
        super().__init__("arbitrage")

    def strategy_metrics(self, all_trades, completed) -> dict:
        if not completed:
            return {}

        # Actual vs expected profit
        edges = [t.entry_edge for t in completed if t.entry_edge > 0]
        actual_pnls = [t.pnl for t in completed]

        # Multi-leg success: trades that had unwind in exit reason
        unwind_count = sum(1 for t in completed if "unwind" in t.exit_reason)

        return {
            "arb_avg_expected_edge": round(sum(edges) / len(edges), 4) if edges else 0,
            "arb_avg_actual_pnl": round(sum(actual_pnls) / len(actual_pnls), 4) if actual_pnls else 0,
            "arb_edge_capture_pct": round(
                (sum(actual_pnls) / sum(edges) * 100) if edges and sum(edges) > 0 else 0, 1
            ),
            "arb_unwind_count": unwind_count,
            "arb_unwind_rate_pct": round(unwind_count / len(completed) * 100, 1) if completed else 0,
        }


class WhaleCopyAnalyzer(BaseAnalyzer):
    def __init__(self):
        super().__init__("whale_copy")

    def strategy_metrics(self, all_trades, completed) -> dict:
        if not completed:
            return {}

        # Group by whale (extract from strategy name like "whale_copy_Theo4")
        whale_stats = {}
        for t in completed:
            whale = t.strategy.replace("whale_copy_", "") if "whale_copy_" in t.strategy else "unknown"
            if whale not in whale_stats:
                whale_stats[whale] = {"trades": 0, "wins": 0, "pnl": 0.0}
            whale_stats[whale]["trades"] += 1
            whale_stats[whale]["pnl"] += t.pnl
            if t.pnl > 0:
                whale_stats[whale]["wins"] += 1

        # Per-whale win rate
        for whale, stats in whale_stats.items():
            stats["win_rate_pct"] = round(stats["wins"] / stats["trades"] * 100, 1) if stats["trades"] > 0 else 0
            stats["pnl"] = round(stats["pnl"], 4)

        # Copy latency: time between trade creation and fill (approximation)
        return {
            "whale_per_whale_stats": whale_stats,
            "whale_best_whale": max(whale_stats.items(), key=lambda x: x[1]["pnl"])[0] if whale_stats else "none",
            "whale_worst_whale": min(whale_stats.items(), key=lambda x: x[1]["pnl"])[0] if whale_stats else "none",
        }


class MeanReversionAnalyzer(BaseAnalyzer):
    def __init__(self):
        super().__init__("mean_reversion")

    def strategy_metrics(self, all_trades, completed) -> dict:
        if not completed:
            return {}

        # Reversion accuracy: what % of trades hit target vs stop
        target_hits = sum(1 for t in completed if t.exit_reason == "target_hit")
        stop_losses = sum(1 for t in completed if t.exit_reason == "stop_loss")

        # Hold time distribution
        short_holds = sum(1 for t in completed if t.hold_time_seconds < 60)
        medium_holds = sum(1 for t in completed if 60 <= t.hold_time_seconds < 300)
        long_holds = sum(1 for t in completed if t.hold_time_seconds >= 300)

        # Edge at entry vs actual PnL correlation
        edges = [t.entry_edge for t in completed]
        pnls = [t.pnl for t in completed]

        return {
            "mr_reversion_rate_pct": round(target_hits / len(completed) * 100, 1),
            "mr_stop_loss_rate_pct": round(stop_losses / len(completed) * 100, 1),
            "mr_target_hits": target_hits,
            "mr_stop_losses": stop_losses,
            "mr_hold_distribution": {
                "short_under_1m": short_holds,
                "medium_1m_5m": medium_holds,
                "long_over_5m": long_holds,
            },
        }


class ResolutionFadeAnalyzer(BaseAnalyzer):
    def __init__(self):
        super().__init__("resolution_fade")

    def strategy_metrics(self, all_trades, completed) -> dict:
        if not completed:
            return {}

        # Group by sub-strategy
        sub_stats = {}
        for t in completed:
            sub = "unknown"
            src = t.strategy.lower()
            if "certainty" in src:
                sub = "certainty"
            elif "convergence" in src:
                sub = "convergence"
            elif "lastmin" in src:
                sub = "last_minute"

            if sub not in sub_stats:
                sub_stats[sub] = {"trades": 0, "wins": 0, "pnl": 0.0}
            sub_stats[sub]["trades"] += 1
            sub_stats[sub]["pnl"] += t.pnl
            if t.pnl > 0:
                sub_stats[sub]["wins"] += 1

        for sub, stats in sub_stats.items():
            stats["win_rate_pct"] = round(stats["wins"] / stats["trades"] * 100, 1) if stats["trades"] > 0 else 0
            stats["pnl"] = round(stats["pnl"], 4)

        # Days-to-resolution vs PnL
        resolution_buckets = {"0-3d": [], "3-7d": [], "7-14d": [], "14d+": []}
        for t in completed:
            h = t.entry_hours_to_resolution
            if h <= 72:
                resolution_buckets["0-3d"].append(t.pnl)
            elif h <= 168:
                resolution_buckets["3-7d"].append(t.pnl)
            elif h <= 336:
                resolution_buckets["7-14d"].append(t.pnl)
            else:
                resolution_buckets["14d+"].append(t.pnl)

        resolution_pnl = {k: round(sum(v), 4) for k, v in resolution_buckets.items() if v}

        return {
            "fade_sub_strategy_stats": sub_stats,
            "fade_resolution_timing_pnl": resolution_pnl,
        }


class BTC5mAnalyzer(BaseAnalyzer):
    def __init__(self):
        super().__init__("btc_5m")

    def strategy_metrics(self, all_trades, completed) -> dict:
        if not completed:
            return {}

        # Direction accuracy. Direction is the selected UP/DOWN token; side is usually BUY.
        up_trades = [t for t in completed if t.entry_direction == "UP"]
        down_trades = [t for t in completed if t.entry_direction == "DOWN"]
        up_wins = sum(1 for t in up_trades if t.pnl > 0)
        down_wins = sum(1 for t in down_trades if t.pnl > 0)

        # Edge magnitude vs outcome correlation
        high_edge = [t for t in completed if abs(t.entry_edge) > 0.05]
        low_edge = [t for t in completed if abs(t.entry_edge) <= 0.05]

        return {
            "btc5m_up_trades": len(up_trades),
            "btc5m_up_win_rate_pct": round(up_wins / len(up_trades) * 100, 1) if up_trades else 0,
            "btc5m_down_trades": len(down_trades),
            "btc5m_down_win_rate_pct": round(down_wins / len(down_trades) * 100, 1) if down_trades else 0,
            "btc5m_high_edge_win_rate_pct": round(
                sum(1 for t in high_edge if t.pnl > 0) / len(high_edge) * 100, 1
            ) if high_edge else 0,
            "btc5m_low_edge_win_rate_pct": round(
                sum(1 for t in low_edge if t.pnl > 0) / len(low_edge) * 100, 1
            ) if low_edge else 0,
        }


# Registry of all analyzers
STRATEGY_ANALYZERS = {
    "market_making": MarketMakingAnalyzer,
    "stoikov_mm": MarketMakingAnalyzer,
    "arb": ArbitrageAnalyzer,
    "arbitrage": ArbitrageAnalyzer,
    "whale_copy": WhaleCopyAnalyzer,
    "whale": WhaleCopyAnalyzer,
    "mean_rev": MeanReversionAnalyzer,
    "mean_reversion": MeanReversionAnalyzer,
    "fade": ResolutionFadeAnalyzer,
    "resolution_fade": ResolutionFadeAnalyzer,
    "btc5m": BTC5mAnalyzer,
    "btc_5m": BTC5mAnalyzer,
}


def get_analyzer(strategy_name: str) -> BaseAnalyzer:
    """Get the appropriate analyzer for a strategy name."""
    # Normalize
    key = strategy_name.lower()
    for prefix, cls in STRATEGY_ANALYZERS.items():
        if key.startswith(prefix):
            return cls()
    return BaseAnalyzer(strategy_name)
