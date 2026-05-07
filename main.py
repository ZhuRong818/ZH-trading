"""
Polymarket Institutional Trading System — Main Entry Point

Wires up all modules and runs the trading loop:
  Data Pipeline -> OMS -> EMS -> Strategies -> Risk Engine

Usage:
    # Paper trade with market making on an interactive market
    python main.py --strategy mm --search "bitcoin" --dry-run

    # Paper trade whale copy-trading
    python main.py --strategy whale --dry-run

    # Paper trade arbitrage scanning
    python main.py --strategy arb --arb-events "election,bitcoin" --dry-run

    # Run all strategies
    python main.py --strategy all --search "election" --dry-run

    # BTC 5-minute trading (auto-discovers rolling markets)
    python main.py --strategy btc5m --dry-run

    # Multi-market market making
    python main.py --strategy mm --token TOKEN1,TOKEN2,TOKEN3 --dry-run

    # Live trading (requires POLYMARKET_PRIVATE_KEY env var)
    python main.py --strategy mm --token TOKEN_ID
"""

import argparse
import json
import logging
import signal
import sys
import threading
import time

from config import SystemConfig, MarketMakingConfig, WhaleTrackingConfig, RiskConfig
from data_pipeline.market_data import MarketDataFeed, MarketInfo
from oms.position_manager import PositionManager, Fill
from oms.capital_allocator import CapitalAllocator
from ems.execution import ExecutionEngine, ClobAuth, SyntheticEqualitySOR
from strategies.market_making.stoikov_model import StoikovMarketMaker
from strategies.whale_tracking.whale_tracker import WhaleTracker
from strategies.arbitrage.arb_detector import ArbitrageDetector
from strategies.mean_reversion.mean_reversion import MeanReversionStrategy, MeanReversionConfig
from strategies.resolution_fade.resolution_fade import ResolutionFadeStrategy, ResolutionFadeConfig
from strategies.btc_5m.btc_5m import BTC5mStrategy, BTC5mConfig
from risk.risk_engine import RiskEngine
from analytics.trade_log import TradeLog
from analytics.performance import PerformanceTracker
from analytics.tuner import ParameterTuner
from analytics.post_session import PostSessionAnalyzer
from analytics.learner import Learner
from pipeline.signal import TradingSignal
from pipeline.engine import PipelineEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


class TradingSystem:
    """
    Orchestrates all modules into a single trading loop.
    """

    def __init__(self, config: SystemConfig):
        self.config = config
        self.running = False

        # Module 2: Data Pipeline
        self.data_feed = MarketDataFeed()

        # Module 3: OMS
        self.oms = PositionManager()

        # Capital Allocator
        self.capital_allocator = CapitalAllocator(config.capital, self.oms)

        # Module 4: EMS (with data feed for VWAP + simulator, and capital allocator)
        self.auth = None
        self.ems = ExecutionEngine(
            dry_run=config.dry_run,
            data_feed=self.data_feed,
            capital_allocator=self.capital_allocator,
        )

        # Module 4: SOR (Smart Order Router)
        self.sor = SyntheticEqualitySOR(self.data_feed, self.ems)

        # Module 6: Risk Engine
        self.risk = RiskEngine(config.risk, self.data_feed, self.ems, self.oms)

        # Analytics
        self.trade_log = TradeLog()
        self.performance = PerformanceTracker(config.capital.total_capital_usdc)
        self.tuner = ParameterTuner(self.performance)

        # Post-session analysis
        self.post_analyzer = PostSessionAnalyzer(self.data_feed)

        # Learner — reads past reports and adjusts parameters
        self.learner = Learner()

        # Pipeline — the single entry point for all trading
        self.pipeline = PipelineEngine(
            risk_engine=self.risk,
            capital_allocator=self.capital_allocator,
            ems=self.ems,
            oms=self.oms,
            trade_log=self.trade_log,
            performance=self.performance,
        )

        # Strategies
        self.market_makers: list[StoikovMarketMaker] = []
        self.whale_tracker = None
        self.arb_detector = None
        self.mean_reversion = None
        self.resolution_fade = None
        self.btc5m = None
        self._btc5m_thread = None

        # Market info for each MM token
        self.mm_markets: list[dict] = []  # [{token_id, tick_size, neg_risk, end_date, question}]

        # Arb config
        self.arb_event_slugs: list[str] = []
        self.arb_scan_interval = 30  # seconds between arb scans
        self._last_arb_scan = 0.0

        # Live reconciliation
        self._last_reconcile = 0.0
        self.reconcile_interval = 30  # seconds

        # Wire up fill callbacks
        self.ems.on_fill(self._on_fill)
        self.ems.on_fill(self.trade_log.record_fill)
        self.ems.on_fill(self.post_analyzer.on_fill)

    def _on_fill(self, fill: Fill):
        """Handle fill events — update OMS and performance tracker."""
        release_amount = 0.0
        if fill.side == "SELL":
            pos = self.oms.get_position(fill.token_id)
            if pos and pos.size > 0:
                release_amount = min(pos.size, fill.size) * pos.avg_price

        realized = self.oms.record_fill(fill)

        if release_amount > 0:
            self.capital_allocator.release_capital(fill.source, fill.token_id, release_amount)
        # Track realized PnL per trade for performance
        if fill.side == "SELL" and realized != 0.0:
            self.performance.record_trade(realized, fill.source)

    # ---- Authentication ----

    def connect(self):
        """Initialize CLOB authentication (live mode only)."""
        if self.config.dry_run:
            log.info("DRY RUN mode — no authentication needed")
            return

        if not self.config.private_key:
            raise RuntimeError(
                "Set POLYMARKET_PRIVATE_KEY env var for live trading"
            )

        self.auth = ClobAuth(
            private_key=self.config.private_key,
            chain_id=137,
            sig_type=self.config.sig_type,
            funder=self.config.funder,
        )
        self.auth.derive_api_creds()
        self.ems = ExecutionEngine(
            auth=self.auth, dry_run=False,
            data_feed=self.data_feed,
            capital_allocator=self.capital_allocator,
        )
        self.ems.on_fill(self._on_fill)
        self.ems.on_fill(self.trade_log.record_fill)
        self.ems.on_fill(self.post_analyzer.on_fill)
        # Rebuild SOR and risk with new EMS
        self.sor = SyntheticEqualitySOR(self.data_feed, self.ems)
        self.risk.ems = self.ems
        log.info("Authenticated and ready for live trading")

    # ---- Market Selection ----

    def select_markets_interactive(self, query: str = "", multi: bool = False):
        """Interactive market selection. Returns list of market dicts."""
        import requests as req
        if query:
            markets = self.data_feed.search_markets(query, limit=20)
        else:
            resp = req.get(
                "https://gamma-api.polymarket.com/markets",
                params={
                    "_limit": 20, "active": True, "closed": False,
                    "order": "volume24hr", "ascending": False,
                },
            )
            resp.raise_for_status()
            markets = [self.data_feed._parse_market(m) for m in resp.json()]

        if not markets:
            log.error("No markets found")
            sys.exit(1)

        print("\nAvailable markets:\n")
        for i, m in enumerate(markets):
            prices_str = " / ".join(
                f"{o}={p:.3f}" for o, p in zip(m.outcomes, m.prices)
            )
            print(f"  [{i:2d}] {m.question}")
            print(f"       {prices_str}  |  24h vol: ${m.volume_24h:,.0f}")

        if multi:
            raw = input("\nSelect market numbers (comma-separated, e.g. 0,2,5): ")
            indices = [int(x.strip()) for x in raw.split(",")]
        else:
            indices = [int(input("\nSelect market number: "))]

        for idx in indices:
            market = markets[idx]
            print(f"\n  {market.question}")
            for i, (outcome, token_id, price) in enumerate(
                zip(market.outcomes, market.token_ids, market.prices)
            ):
                print(f"    [{i}] {outcome} (price={price:.3f}, token={token_id[:20]}...)")

            tidx = int(input("  Select outcome: "))

            self.mm_markets.append({
                "token_id": market.token_ids[tidx],
                "tick_size": market.tick_size,
                "neg_risk": market.neg_risk,
                "end_date": market.end_date,
                "question": market.question,
                "outcome": market.outcomes[tidx],
                "condition_id": market.condition_id,
                "all_token_ids": market.token_ids,
                "gamma_price": market.prices[tidx],
            })

            log.info(
                "Selected: %s [%s] tick=%s neg_risk=%s",
                market.question, market.outcomes[tidx],
                market.tick_size, market.neg_risk,
            )

    def set_tokens(self, token_ids_csv: str, tick_size: str = "0.01", neg_risk: bool = False):
        """Set tokens from comma-separated CLI arg."""
        for token_id in token_ids_csv.split(","):
            token_id = token_id.strip()
            if token_id:
                self.mm_markets.append({
                    "token_id": token_id,
                    "tick_size": tick_size,
                    "neg_risk": neg_risk,
                    "end_date": "",
                    "question": f"token {token_id[:16]}...",
                    "outcome": "?",
                    "condition_id": "",
                    "all_token_ids": [],
                })

    # ---- Strategy Setup ----

    def setup_market_making(self):
        """Initialize Stoikov market makers for all selected markets."""
        for mkt in self.mm_markets:
            mm = StoikovMarketMaker(
                config=self.config.market_making,
                data_feed=self.data_feed,
                ems=self.ems,
                oms=self.oms,
                token_id=mkt["token_id"],
                tick_size=mkt["tick_size"],
                neg_risk=mkt["neg_risk"],
                end_date=mkt["end_date"],
                gamma_price=mkt.get("gamma_price"),
            )
            self.market_makers.append(mm)
            self.post_analyzer.register_token(mkt["token_id"])
            self.post_analyzer.register_strategy(mm, f"stoikov_mm_{mkt['outcome']}")
            log.info("MM initialized: %s [%s]", mkt["question"][:50], mkt["outcome"])
        log.info("Market Making: %d market(s) active", len(self.market_makers))

    def setup_whale_tracking(self):
        """Initialize the whale tracker with SOR."""
        self.whale_tracker = WhaleTracker(
            config=self.config.whale_tracking,
            data_feed=self.data_feed,
            ems=self.ems,
            oms=self.oms,
            sor=self.sor,
        )
        self.whale_tracker.initialize()
        self.post_analyzer.register_strategy(self.whale_tracker, "whale_copy")
        log.info("Whale Tracking strategy initialized (with SOR)")

    def setup_arbitrage(self, event_slugs: list[str]):
        """Initialize the arbitrage detector."""
        self.arb_detector = ArbitrageDetector(
            data_feed=self.data_feed,
            ems=self.ems,
        )
        self.arb_event_slugs = event_slugs
        self.post_analyzer.register_strategy(self.arb_detector, "arbitrage")
        log.info("Arbitrage detector initialized, scanning %d event(s): %s",
                 len(event_slugs), ", ".join(event_slugs))

    def setup_mean_reversion(self):
        """Initialize mean reversion on all selected MM markets."""
        if not self.mm_markets:
            log.warning("Mean reversion needs markets — use --token or --search")
            return
        token_ids = [m["token_id"] for m in self.mm_markets]
        tick_sizes = {m["token_id"]: m["tick_size"] for m in self.mm_markets}
        neg_risks = {m["token_id"]: m["neg_risk"] for m in self.mm_markets}

        self.mean_reversion = MeanReversionStrategy(
            config=MeanReversionConfig(bankroll=self.config.risk.max_total_exposure_usdc),
            data_feed=self.data_feed,
            ems=self.ems,
            oms=self.oms,
            token_ids=token_ids,
            tick_sizes=tick_sizes,
            neg_risks=neg_risks,
        )
        self.post_analyzer.register_strategy(self.mean_reversion, "mean_reversion")
        log.info("Mean Reversion strategy initialized on %d market(s)", len(token_ids))

    def setup_resolution_fade(self):
        """Initialize resolution fade on all selected MM markets."""
        if not self.mm_markets:
            log.warning("Resolution fade needs markets — use --token or --search")
            return
        fade_markets = [
            {
                "token_id": m["token_id"],
                "end_date": m["end_date"],
                "tick_size": m["tick_size"],
                "neg_risk": m["neg_risk"],
                "question": m["question"],
            }
            for m in self.mm_markets
        ]
        self.resolution_fade = ResolutionFadeStrategy(
            config=ResolutionFadeConfig(bankroll=self.config.risk.max_total_exposure_usdc),
            data_feed=self.data_feed,
            ems=self.ems,
            oms=self.oms,
            markets=fade_markets,
        )
        self.post_analyzer.register_strategy(self.resolution_fade, "resolution_fade")
        log.info("Resolution Fade strategy initialized on %d market(s)", len(fade_markets))

    def setup_btc_5m(self):
        """Initialize BTC 5-minute strategy (runs in background daemon thread)."""
        self.btc5m = BTC5mStrategy(
            config=BTC5mConfig(bankroll=self.config.risk.max_total_exposure_usdc),
            ems=self.ems,
            oms=self.oms,
            data_feed=self.data_feed,
        )
        self.post_analyzer.register_strategy(self.btc5m, "btc_5m")
        self.btc5m.running = True
        self._btc5m_thread = threading.Thread(target=self.btc5m.run, daemon=True)
        self._btc5m_thread.start()
        log.info("BTC 5-Minute strategy started (background thread)")

    # ---- Heartbeat ----

    def _heartbeat_loop(self):
        while self.running and self.auth:
            try:
                self.auth.post("/heartbeat", {"heartbeat_id": ""})
            except Exception:
                pass
            time.sleep(self.config.heartbeat_interval)

    # ---- Live Reconciliation ----

    def _reconcile_positions(self):
        """Poll Polymarket Data API to sync positions (live mode only)."""
        if self.config.dry_run or not self.auth:
            return

        now = time.time()
        if now - self._last_reconcile < self.reconcile_interval:
            return

        self._last_reconcile = now
        funder = self.auth.funder
        if funder:
            self.oms.sync_from_api(funder)

    # ---- Strategy Steps ----

    def _step_market_making(self):
        """Run one step for all market makers."""
        for mm in self.market_makers:
            if self.risk.check_circuit_breaker(mm.token_id):
                mm.step()

    def _step_whale_tracking(self):
        """Run one whale tracking step. Signals go through pipeline."""
        if not self.whale_tracker:
            return

        whale_signals = self.whale_tracker.step()
        for sig in whale_signals:
            # Convert whale signal to pipeline TradingSignal
            trading_signal = TradingSignal(
                token_id=sig.token_id,
                side=sig.side,
                price=sig.price,
                size=min(sig.size * self.config.whale_tracking.copy_fraction,
                         self.config.whale_tracking.max_copy_size_usdc / sig.price if sig.price > 0 else 0),
                strategy=f"whale_copy_{sig.username or sig.wallet[:8]}",
                edge=0.0,
                confidence=sig.win_rate,
            )
            self.pipeline.submit(trading_signal)

    def _step_arbitrage(self):
        """Run arbitrage scan. Signals go through pipeline."""
        if not self.arb_detector:
            return

        now = time.time()
        if now - self._last_arb_scan < self.arb_scan_interval:
            return
        self._last_arb_scan = now

        for slug in self.arb_event_slugs:
            arb = self.arb_detector.scan_sum_to_one(slug)
            if arb and arb.profit_estimate >= 0.50:
                log.info("Arb opportunity: %s profit_est=$%.2f", arb.arb_type, arb.profit_estimate)
                # Each arb leg goes through the pipeline
                for token_id, side, size in arb.trades:
                    signal = TradingSignal(
                        token_id=token_id,
                        side=side,
                        price=0.0,  # executor will use book price
                        size=size,
                        strategy=f"arb_{arb.arb_type}",
                        edge=arb.profit_estimate / len(arb.trades),
                        order_type="FOK",
                    )
                    self.pipeline.submit(signal)

    def _step_mean_reversion(self):
        """Run mean reversion step."""
        if self.mean_reversion:
            self.mean_reversion.step()

    def _step_resolution_fade(self):
        """Run resolution fade step."""
        if self.resolution_fade:
            self.resolution_fade.step()

    # ---- Main Loop ----

    def run(self, strategies: list[str]):
        """Main trading loop."""
        self.running = True

        # Learn from past sessions (adjust parameters before strategy setup)
        if not self.config.no_learn:
            sessions = self.learner.load_history()
            if sessions > 0:
                self.learner.apply_learning(self.config)
                disabled = self.learner.get_disabled_strategies()
                for d in disabled:
                    # Remove disabled strategies
                    for key in ["market_making", "stoikov_mm", "mm"]:
                        if d == key and "mm" in strategies:
                            strategies.remove("mm")
                            log.warning("Learner disabled strategy: mm")
                    if d in ("whale_copy", "whale") and "whale" in strategies:
                        strategies.remove("whale")
                        log.warning("Learner disabled strategy: whale")

        # Setup requested strategies
        if "mm" in strategies and self.mm_markets:
            self.setup_market_making()
        if "whale" in strategies:
            self.setup_whale_tracking()
        if "arb" in strategies and self.arb_event_slugs:
            self.setup_arbitrage(self.arb_event_slugs)
        if "meanrev" in strategies and self.mm_markets:
            self.setup_mean_reversion()
        if "fade" in strategies and self.mm_markets:
            self.setup_resolution_fade()
        if "btc5m" in strategies:
            self.setup_btc_5m()

        # Start heartbeat for live mode
        if not self.config.dry_run and self.auth:
            threading.Thread(target=self._heartbeat_loop, daemon=True).start()

        active_strats = []
        if self.market_makers:
            active_strats.append(f"mm({len(self.market_makers)} markets)")
        if self.whale_tracker:
            active_strats.append("whale")
        if self.arb_detector:
            active_strats.append(f"arb({len(self.arb_event_slugs)} events)")
        if self.mean_reversion:
            active_strats.append(f"meanrev({len(self.mean_reversion.token_ids)} markets)")
        if self.resolution_fade:
            active_strats.append(f"fade({len(self.resolution_fade.markets)} markets)")
        if self.btc5m:
            active_strats.append("btc5m")

        log.info("=" * 60)
        log.info("Trading system started")
        log.info("  Strategies: %s", ", ".join(active_strats) or "none")
        log.info("  Dry run: %s", self.config.dry_run)
        log.info("  Reconciliation: every %ds (live only)", self.reconcile_interval)
        log.info("=" * 60)

        iteration = 0
        try:
            while self.running:
                iteration += 1

                # Risk check
                if not self.risk.check_all():
                    log.warning("Risk check failed — pausing")
                    if self.risk.halted:
                        break
                    time.sleep(10)
                    continue

                # Live position reconciliation
                self._reconcile_positions()

                # Process pending dry-run orders
                self.ems.check_pending_dry_run()

                # Collect market/strategy snapshots for post-session analysis
                self.post_analyzer.collect_snapshots()

                # Strategy steps
                self._step_market_making()
                self._step_whale_tracking()
                self._step_arbitrage()
                self._step_mean_reversion()
                self._step_resolution_fade()

                # Periodic status
                if iteration % 12 == 0:  # every ~60s at 5s interval
                    cap = self.capital_allocator.summary()
                    pstats = self.pipeline.stats()
                    log.info(
                        "STATUS: %s | capital=$%.0f deployed=$%.0f (%.0f%%) | "
                        "signals=%d executed=%d fill_rate=%s",
                        self.performance.report(),
                        cap["total_capital"], cap["deployed"], cap["utilization_pct"],
                        pstats["total_signals"], pstats["executed"], pstats["fill_rate"],
                    )

                # Parameter tuning suggestions every ~10 min
                if iteration % 120 == 0 and iteration > 0:
                    self.tuner.log_suggestions()

                time.sleep(self.config.market_making.refresh_interval)

        except KeyboardInterrupt:
            log.info("Interrupted by user")
        finally:
            self.shutdown()

    def shutdown(self):
        self.running = False
        log.info("Shutting down...")
        self.ems.cancel_all()

        # Stop BTC 5m background thread
        if self.btc5m:
            self.btc5m.running = False
            if self._btc5m_thread and self._btc5m_thread.is_alive():
                self._btc5m_thread.join(timeout=5)

        # Run post-session analysis (replaces the old simple report)
        self.post_analyzer.run_analysis()

        # Pipeline stats
        pstats = self.pipeline.stats()
        log.info("Pipeline: signals=%d executed=%d rejected=%d fill_rate=%s",
                 pstats["total_signals"], pstats["executed"],
                 pstats["rejected"], pstats["fill_rate"])
        if pstats.get("top_rejections"):
            for reason, count in pstats["top_rejections"].items():
                log.info("  Rejection: %s (%d times)", reason, count)

        # Capital summary
        cap = self.capital_allocator.summary()
        log.info("Capital: total=$%.0f deployed=$%.0f available=$%.0f",
                 cap["total_capital"], cap["deployed"], cap["available"])

        # Final tuning suggestions
        self.tuner.log_suggestions()

        # Close trade log
        self.trade_log.close()
        log.info("Reports saved to reports/. System stopped.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Institutional Trading System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Strategies (comma-separated or 'all'):
  mm       Stoikov market making (requires --token or --search)
  whale    Whale tracking & copy trading (no token needed)
  arb      Combinatorial arbitrage (requires --arb-events)
  meanrev  Mean reversion — buy dips, sell rips in contested markets
  fade     Resolution fade — earn time decay premium near resolution
  btc5m    BTC 5-minute rolling markets — momentum + arbitrage (auto)
  all      Run all strategies

Low-drawdown combo:
  python main.py --strategy meanrev,fade --search "bitcoin" --dry-run

Examples:
  python main.py --strategy mm --search "bitcoin" --dry-run
  python main.py --strategy meanrev --token TOKEN1,TOKEN2 --dry-run
  python main.py --strategy fade --search "election" --dry-run
  python main.py --strategy whale --dry-run
  python main.py --strategy arb --arb-events "election" --dry-run
  python main.py --strategy all --search "election" --arb-events "election" --dry-run
        """,
    )
    parser.add_argument("--strategy", type=str, default="mm",
                        help="Strategy: mm, whale, arb, all (comma-separated)")
    parser.add_argument("--search", type=str, default="",
                        help="Search for market(s) interactively")
    parser.add_argument("--token", type=str,
                        help="Token ID(s) to trade (comma-separated for multi-market)")
    parser.add_argument("--arb-events", type=str, default="",
                        help="Event slugs for arb scanning (comma-separated)")
    parser.add_argument("--arb-interval", type=float, default=30.0,
                        help="Seconds between arb scans (default: 30)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Paper trading mode (no real orders)")
    parser.add_argument("--gamma", type=float, default=0.5,
                        help="Stoikov risk aversion (default: 0.5)")
    parser.add_argument("--spread-k", type=float, default=1.5,
                        help="Stoikov spread scaling (default: 1.5)")
    parser.add_argument("--size", type=float, default=20.0,
                        help="Order size in shares (default: 20)")
    parser.add_argument("--levels", type=int, default=3,
                        help="Quote levels per side (default: 3)")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="Refresh interval in seconds (default: 5)")
    parser.add_argument("--max-position", type=float, default=10_000,
                        help="Max position per market in USD (default: 10000)")
    parser.add_argument("--max-drawdown", type=float, default=20.0,
                        help="Max drawdown %% before kill switch (default: 20)")
    parser.add_argument("--reconcile-interval", type=float, default=30.0,
                        help="Seconds between position reconciliation (default: 30)")
    parser.add_argument("--no-learn", action="store_true",
                        help="Disable learning from past sessions")
    parser.add_argument("--verbose", action="store_true",
                        help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Build config
    config = SystemConfig.from_env()
    config.dry_run = args.dry_run
    config.no_learn = args.no_learn
    config.market_making.gamma = args.gamma
    config.market_making.spread_k = args.spread_k
    config.market_making.order_size = args.size
    config.market_making.num_levels = args.levels
    config.market_making.refresh_interval = args.interval
    config.risk.max_position_size_usdc = args.max_position
    config.risk.max_drawdown_pct = args.max_drawdown

    # Parse strategies
    if args.strategy == "all":
        strategies = ["mm", "whale", "arb", "meanrev", "fade", "btc5m"]
    else:
        strategies = [s.strip() for s in args.strategy.split(",")]

    # Initialize system
    system = TradingSystem(config)
    system.reconcile_interval = args.reconcile_interval
    system.arb_scan_interval = args.arb_interval

    # Handle Ctrl+C
    signal.signal(signal.SIGINT, lambda *_: setattr(system, 'running', False))

    # Market selection for strategies that need tokens
    needs_market = any(s in strategies for s in ["mm", "meanrev", "fade"])
    if needs_market:
        if args.token:
            system.set_tokens(args.token)
        else:
            multi = input("Multi-market mode? (y/N): ").strip().lower() == "y" if not args.search else False
            system.select_markets_interactive(args.search, multi=multi)

    # Arb event slugs
    if "arb" in strategies:
        if args.arb_events:
            system.arb_event_slugs = [s.strip() for s in args.arb_events.split(",") if s.strip()]
        else:
            raw = input("Enter event slugs for arb scanning (comma-separated): ")
            system.arb_event_slugs = [s.strip() for s in raw.split(",") if s.strip()]

    # Connect for live trading
    if not args.dry_run:
        system.connect()

    # Run
    system.run(strategies)


if __name__ == "__main__":
    main()
