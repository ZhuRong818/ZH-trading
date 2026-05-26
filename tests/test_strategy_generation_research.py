import json
import unittest

from backtest.canonicalize import normalize_records, parse_payload_text
from backtest.execution_costs import FeeModel, quarter_kelly_notional
from backtest.loader_kvrun_prediction import KvRunPredictionClient, normalize_payload
from backtest.resample import build_bars
from backtest.stream_kvrun import canonicalize_tick, parse_sse_blocks
from research.generic_engine import run_family
from research.grader import grade_summary
from research.kvrun_peer_graph import build_peer_edges
from research.strategy_templates import generate_candidates


class StrategyGenerationResearchTests(unittest.TestCase):
    def _sample_rows(self):
        rows = []
        base = 1_800_000_000
        for idx in range(12):
            rows.append(
                {
                    "slug": "btc-test-window",
                    "asset": "btc",
                    "ts": base + idx * 60,
                    "window_end_ts": base + 3600,
                    "up_mid": 0.40 + idx * 0.01,
                    "down_mid": 0.60 - idx * 0.01,
                    "up_buy": 0.41 + idx * 0.01,
                    "up_sell": 0.39 + idx * 0.01,
                    "up_bid_depth": 100,
                    "up_ask_depth": 100,
                    "down_buy": 0.61 - idx * 0.01,
                    "down_sell": 0.59 - idx * 0.01,
                    "down_bid_depth": 100,
                    "down_ask_depth": 100,
                    "seconds_remaining": 3600 - idx * 60,
                }
            )
        return rows

    def test_json_and_jsonl_payloads_parse(self):
        rows = [{"id": "m1"}, {"id": "m2"}]
        self.assertEqual(parse_payload_text(json.dumps({"data": rows})), rows)
        self.assertEqual(parse_payload_text("\n".join(json.dumps(row) for row in rows)), rows)

    def test_wide_record_canonicalizes_to_two_sides(self):
        result = normalize_records(self._sample_rows()[:1], source_granularity="snapshot")
        self.assertEqual(len(result.rows), 2)
        outcomes = {row["outcome"] for row in result.rows}
        self.assertEqual(outcomes, {"YES", "NO"})
        self.assertTrue(all(0 <= row["price"] <= 1 for row in result.rows))

    def test_bars_and_momentum_engine_run(self):
        canonical = normalize_records(self._sample_rows(), source_granularity="snapshot").rows
        bars = build_bars(canonical)
        result = run_family(bars, "momentum", {"lookback": 2, "horizon": 1})
        self.assertGreater(result["summary"]["n_signals"], 0)
        self.assertIn(result["grading"]["grade"], {"A", "B", "C", "Reject"})

    def test_missing_open_interest_disables_oi_family(self):
        fields = {"condition_id", "outcome", "bar_ts", "close"}
        candidates = {item["family"]: item for item in generate_candidates(fields)}
        self.assertIsNotNone(candidates["open_interest_growth"]["disabled_reason"])
        self.assertIsNone(candidates["momentum"]["disabled_reason"])

    def test_fee_and_sizing_are_conservative(self):
        fee = FeeModel(metadata_rate=0.05, default_rate=0.07).fee_per_share(0.5)
        self.assertAlmostEqual(fee, 0.0125)
        notional = quarter_kelly_notional(0.65, 0.50, 10_000, max_notional=150)
        self.assertGreater(notional, 0)
        self.assertLessEqual(notional, 150)

    def test_grader_rejects_dead_edge_model(self):
        grade = grade_summary(
            {
                "mode": "execution",
                "trades": 120,
                "profit_factor": 1.3,
                "max_drawdown": 0.05,
                "high_edge_win_rate_pct": 40,
                "low_edge_win_rate_pct": 55,
            }
        )
        self.assertNotEqual(grade["recommendation"], "promote_candidate")
        self.assertTrue(grade["reasons"])

    def test_kvrun_prediction_client_builds_concrete_paths(self):
        client = KvRunPredictionClient(base_url="https://kv.run:5000", api_key=None, rpm=0)
        search_url = client.build_url(
            "/prediction-markets/markets/search",
            {"q": "bitcoin", "venue": "polymarket", "status": "open", "limit": 50},
        )
        self.assertIn("/prediction-markets/markets/search?", search_url)
        self.assertIn("q=bitcoin", search_url)
        self.assertIn("venue=polymarket", search_url)
        candles_url = client.build_url("/prediction-markets/candles/polymarket/abc", {"interval": 1, "limit": 5000})
        self.assertEqual(candles_url, "https://kv.run:5000/prediction-markets/candles/polymarket/abc?interval=1&limit=5000")

    def test_kvrun_market_rows_do_not_need_price(self):
        rows, audit = normalize_payload(
            {"data": [{"id": "0xabc", "question": "Will BTC close above 100k?", "slug": "btc-100k"}]},
            "market",
            {"venue": "polymarket"},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["row_type"], "market")
        self.assertEqual(rows[0]["condition_id"], "0xabc")
        self.assertEqual(rows[0]["market_id"], "0xabc")
        self.assertEqual(audit, [])

    def test_kvrun_candles_pass_through_as_bars(self):
        rows, _audit = normalize_payload(
            {
                "data": [
                    {
                        "bucket_ts": "2026-05-20T00:00:00Z",
                        "open": 0.40,
                        "high": 0.45,
                        "low": 0.39,
                        "close": 0.44,
                        "volume": 120.5,
                        "trades": 4,
                    }
                ]
            },
            "bar",
            {"venue": "polymarket", "market_id": "cond1", "condition_id": "cond1"},
        )
        bars = build_bars(rows)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["close"], 0.44)
        self.assertEqual(bars[0]["volume"], 120.5)

    def test_sse_blocks_and_tick_canonicalization(self):
        events = list(
            parse_sse_blocks(
                [
                    "event: heartbeat",
                    'data: {"ok": true}',
                    "",
                    "event: tick",
                    'data: {"condition_id": "cond1", "ts": "2026-05-20T00:00:00Z", "price": 0.51}',
                    "",
                ]
            )
        )
        self.assertEqual(events[0]["event"], "heartbeat")
        tick = canonicalize_tick(events[1])
        self.assertIsNotNone(tick)
        self.assertEqual(tick["row_type"], "sse_tick")
        self.assertEqual(tick["condition_id"], "cond1")

    def test_peer_graph_uses_deterministic_edges(self):
        edges = build_peer_edges(
            [
                {"condition_id": "a", "event_id": "ev1", "question": "Will Bitcoin price hit 100k?", "end_date": "2026-06-01T00:00:00Z"},
                {"condition_id": "b", "event_id": "ev1", "question": "Will Bitcoin price hit 90k?", "end_date": "2026-06-01T00:00:00Z"},
            ]
        )
        relations = {edge["relation"] for edge in edges}
        self.assertIn("same_event", relations)
        self.assertIn("title_token_overlap", relations)


if __name__ == "__main__":
    unittest.main()
