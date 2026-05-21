import os
import tempfile
import unittest

from backtest.replay import ReplayResult, ReplayTrade, replay_summary
from research.orchestrator import (
    REPLAY_MODE,
    SIGNAL_MODE,
    evaluate_gates,
    load_spec,
    validate_spec,
)


class ResearchOrchestratorTests(unittest.TestCase):
    def test_loads_and_validates_experiment_spec(self):
        text = """
run_id: unit
data_dir: data
assets: [btc, eth]
bankroll: 10000
gates:
  min_trades: 1
experiments:
  - name: momentum_default
    mode: replay
    strategy: momentum
    tags: [baseline]
    params:
      min_edge: 0.16
"""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(text)
            path = fh.name
        try:
            spec = load_spec(path)
            validate_spec(spec)
        finally:
            os.unlink(path)

        self.assertEqual(spec["run_id"], "unit")
        self.assertEqual(spec["assets"], ["btc", "eth"])
        self.assertEqual(spec["experiments"][0]["params"]["min_edge"], 0.16)

    def test_replay_summary_aggregates_synthetic_results(self):
        result = ReplayResult(asset="btc", strategy="unit", bankroll=10_000)
        result.wins = 1
        result.losses = 1
        result.total_pnl = 20.0
        result.total_fees = 1.5
        result.max_drawdown = 0.03
        result.trades = [
            ReplayTrade(
                strategy="unit",
                window_slug="w1",
                asset="btc",
                direction="UP",
                entry_price=0.4,
                fair_value=0.6,
                edge=0.2,
                size_usdc=100,
                shares=250,
                entry_ts=1,
                pnl=30,
                outcome="WIN",
            ),
            ReplayTrade(
                strategy="unit",
                window_slug="w2",
                asset="btc",
                direction="DOWN",
                entry_price=0.4,
                fair_value=0.3,
                edge=0.1,
                size_usdc=100,
                shares=250,
                entry_ts=2,
                pnl=-10,
                outcome="LOSS",
            ),
        ]

        summary = replay_summary({"btc": result})

        self.assertEqual(summary["trades"], 2)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["losses"], 1)
        self.assertAlmostEqual(summary["win_rate"], 0.5)
        self.assertAlmostEqual(summary["profit_factor"], 3.0)
        self.assertAlmostEqual(summary["max_drawdown"], 0.03)

    def test_replay_gates_fail_on_insufficient_trades(self):
        failures = evaluate_gates(
            {"trades": 0, "profit_factor": 1.2, "max_drawdown": 0.01, "total_pnl": 5.0},
            {"min_trades": 1, "min_profit_factor": 1.0, "max_drawdown": 0.10},
            REPLAY_MODE,
        )

        self.assertTrue(any(item.startswith("min_trades") for item in failures))
        self.assertIn("no_replay_trades", failures)

    def test_signal_gates_pass_forecast_metrics(self):
        failures = evaluate_gates(
            {"signals": 200, "accuracy": 0.56, "brier": 0.22, "log_loss": 0.65},
            {"min_signals": 100, "min_accuracy": 0.52, "max_brier": 0.25},
            SIGNAL_MODE,
        )

        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
