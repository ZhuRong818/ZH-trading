"""
Polymarket Institutional Trading System — Main Entry Point

Wires up all modules and runs the trading loop:
  Data Pipeline -> OMS -> EMS -> Strategies -> Risk Engine

Usage:
    # Paper trade with market making on an interactive market
    python main.py --strategy mm --search "bitcoin" --dry-run

    # Paper trade whale copy-trading
    python main.py --strategy whale --dry-run

    # Run all strategies
    python main.py --strategy all --search "election" --dry-run

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
from ems.execution import ExecutionEngine, ClobAuth
from strategies.market_making.stoikov_model import StoikovMarketMaker
from strategies.whale_tracking.whale_tracker import WhaleTracker
from risk.risk_engine import RiskEngine

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

        # Module 4: EMS
        self.auth = None
        self.ems = ExecutionEngine(dry_run=config.dry_run)

        # Module 6: Risk Engine
        self.risk = RiskEngine(config.risk, self.data_feed, self.ems, self.oms)

        # Strategies (initialized later based on CLI args)
        self.market_maker = None
        self.whale_tracker = None

        # Track selected market
        self.market_info = None
        self.token_id = ""
        self.tick_size = "0.01"
        self.neg_risk = False

        # Wire up fill callbacks
        self.ems.on_fill(self._on_fill)

    def _on_fill(self, fill: Fill):
        """Handle fill events — update OMS."""
        self.oms.record_fill(fill)

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
        self.ems = ExecutionEngine(auth=self.auth, dry_run=False)
        self.ems.on_fill(self._on_fill)
        self.risk.ems = self.ems
        log.info("Authenticated and ready for live trading")

    # ---- Market Selection ----

    def select_market_interactive(self, query: str = ""):
        """Interactive market selection."""
        markets = self.data_feed.search_markets(query, limit=15) if query else []
        if not markets:
            # Fetch top markets by default
            import requests
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={
                    "_limit": 15, "active": True, "closed": False,
                    "order": "volume24hr", "ascending": False,
                },
            )
            resp.raise_for_status()
            markets = [self.data_feed._parse_market(m) for m in resp.json()]

        print("\nAvailable markets:\n")
        for i, m in enumerate(markets):
            prices_str = " / ".join(
                f"{o}={p:.3f}" for o, p in zip(m.outcomes, m.prices)
            )
            print(f"  [{i:2d}] {m.question}")
            print(f"       {prices_str}  |  24h vol: ${m.volume_24h:,.0f}")

        idx = int(input("\nSelect market number: "))
        market = markets[idx]

        print("\nOutcomes:")
        for i, (outcome, token_id, price) in enumerate(
            zip(market.outcomes, market.token_ids, market.prices)
        ):
            print(f"  [{i}] {outcome} (price={price:.3f}, token={token_id[:20]}...)")

        tidx = int(input("Select outcome: "))

        self.market_info = market
        self.token_id = market.token_ids[tidx]
        self.tick_size = market.tick_size
        self.neg_risk = market.neg_risk

        log.info(
            "Selected: %s [%s] tick=%s neg_risk=%s",
            market.question, market.outcomes[tidx],
            market.tick_size, market.neg_risk,
        )

    def set_token(self, token_id: str, tick_size: str = "0.01", neg_risk: bool = False):
        self.token_id = token_id
        self.tick_size = tick_size
        self.neg_risk = neg_risk

    # ---- Strategy Setup ----

    def setup_market_making(self):
        """Initialize the Stoikov market maker."""
        self.market_maker = StoikovMarketMaker(
            config=self.config.market_making,
            data_feed=self.data_feed,
            ems=self.ems,
            oms=self.oms,
            token_id=self.token_id,
            tick_size=self.tick_size,
            neg_risk=self.neg_risk,
            end_date=self.market_info.end_date if self.market_info else "",
        )
        log.info("Market Making (Stoikov) strategy initialized")

    def setup_whale_tracking(self):
        """Initialize the whale tracker."""
        self.whale_tracker = WhaleTracker(
            config=self.config.whale_tracking,
            data_feed=self.data_feed,
            ems=self.ems,
            oms=self.oms,
        )
        self.whale_tracker.initialize()
        log.info("Whale Tracking strategy initialized")

    # ---- Heartbeat ----

    def _heartbeat_loop(self):
        while self.running and self.auth:
            try:
                self.auth.post("/heartbeat", {"heartbeat_id": ""})
            except Exception:
                pass
            time.sleep(self.config.heartbeat_interval)

    # ---- Main Loop ----

    def run(self, strategies: list):
        """Main trading loop."""
        self.running = True

        # Setup requested strategies
        if "mm" in strategies and self.token_id:
            self.setup_market_making()
        if "whale" in strategies:
            self.setup_whale_tracking()

        # Start heartbeat for live mode
        if not self.config.dry_run and self.auth:
            threading.Thread(target=self._heartbeat_loop, daemon=True).start()

        log.info("=" * 60)
        log.info("Trading system started")
        log.info("  Strategies: %s", ", ".join(strategies))
        log.info("  Dry run: %s", self.config.dry_run)
        if self.token_id:
            log.info("  Token: %s...", self.token_id[:20])
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

                # Market Making step
                if self.market_maker:
                    if self.risk.check_circuit_breaker(self.token_id):
                        self.market_maker.step()

                # Whale Tracking step
                if self.whale_tracker:
                    signals = self.whale_tracker.step()
                    for sig in signals:
                        # Pre-trade risk check
                        if self.risk.check_position_size(
                            sig.token_id, sig.size * sig.price
                        ):
                            self.whale_tracker.execute_copy_trade(sig)

                # Periodic status
                if iteration % 12 == 0:  # every ~60s at 5s interval
                    status = self.risk.status()
                    summary = self.oms.portfolio_summary()
                    log.info(
                        "STATUS: positions=%d exposure=$%.0f pnl=$%.2f orders=%d",
                        summary["num_positions"],
                        summary["total_notional_usdc"],
                        summary["total_pnl"],
                        status["open_orders"],
                    )

                time.sleep(self.config.market_making.refresh_interval)

        except KeyboardInterrupt:
            log.info("Interrupted by user")
        finally:
            self.shutdown()

    def shutdown(self):
        self.running = False
        log.info("Shutting down...")
        self.ems.cancel_all()
        summary = self.oms.portfolio_summary()
        log.info("Final P&L: $%.2f (realized=$%.2f unrealized=$%.2f)",
                 summary["total_pnl"], summary["realized_pnl"], summary["unrealized_pnl"])
        log.info("System stopped.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Institutional Trading System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Strategies:
  mm      Stoikov market making (requires --token or --search)
  whale   Whale tracking & copy trading (no token needed)
  all     Run all strategies

Examples:
  python main.py --strategy mm --search "bitcoin" --dry-run
  python main.py --strategy whale --dry-run
  python main.py --strategy all --search "election" --dry-run
  python main.py --strategy mm --token TOKEN_ID --spread 0.06
        """,
    )
    parser.add_argument("--strategy", type=str, default="mm",
                        help="Strategy: mm, whale, all")
    parser.add_argument("--search", type=str, default="",
                        help="Search for a market interactively")
    parser.add_argument("--token", type=str, help="Token ID to trade")
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
    parser.add_argument("--verbose", action="store_true",
                        help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Build config
    config = SystemConfig.from_env()
    config.dry_run = args.dry_run
    config.market_making.gamma = args.gamma
    config.market_making.spread_k = args.spread_k
    config.market_making.order_size = args.size
    config.market_making.num_levels = args.levels
    config.market_making.refresh_interval = args.interval
    config.risk.max_position_size_usdc = args.max_position
    config.risk.max_drawdown_pct = args.max_drawdown

    # Parse strategies
    if args.strategy == "all":
        strategies = ["mm", "whale"]
    else:
        strategies = [s.strip() for s in args.strategy.split(",")]

    # Initialize system
    system = TradingSystem(config)

    # Handle Ctrl+C
    signal.signal(signal.SIGINT, lambda *_: setattr(system, 'running', False))

    # Market selection for strategies that need it
    needs_token = any(s in strategies for s in ["mm"])
    if args.token:
        system.set_token(args.token)
    elif needs_token:
        system.select_market_interactive(args.search)

    # Connect for live trading
    if not args.dry_run:
        system.connect()

    # Run
    system.run(strategies)


if __name__ == "__main__":
    main()
