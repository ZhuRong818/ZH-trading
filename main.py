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
from risk.risk_engine import RiskEngine
from analytics.trade_log import TradeLog
from analytics.performance import PerformanceTracker
from analytics.tuner import ParameterTuner
from analytics.post_session import PostSessionAnalyzer
from analytics.learner import Learner
from data_pipeline.market_provider import RollingProvider
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
        self.risk = RiskEngine(
            config.risk,
            self.data_feed,
            self.ems,
            self.oms,
            initial_capital=config.capital.total_capital_usdc,
        )

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

        # Strategies and Runners (V2)
        self.static_runner = None
        self.rolling_runner = None
        self.rolling_runners = []
        
        # For legacy compatibility or general use
        self.mm_markets: list[dict] = []

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

    # ---- Strategy Setup (V2) ----

    def setup_v2_strategies(self, strategies: list[str]):
        """Initialize static runner and requested V2 strategies."""
        from strategies.v2.runner import UnifiedRunnerV2
        from strategies.v2.mm import StoikovMM
        from strategies.v2.whale import WhaleCopy
        from strategies.v2.meanrev import MeanReversion
        from data_pipeline.market_provider import StaticProvider

        static_provider = StaticProvider(self.data_feed, self.mm_markets)
        self.static_runner = UnifiedRunnerV2(static_provider, self.pipeline, self.ems, oms=self.oms)

        if "mm" in strategies and self.mm_markets:
            mm = StoikovMM(self.config.market_making, self.data_feed)
            self.static_runner.add(mm)
            self.post_analyzer.register_strategy(mm, "v2_stoikov_mm")
            log.info("Market Making initialized on %d market(s)", len(self.mm_markets))

        if "whale" in strategies:
            whale = WhaleCopy(self.config.whale_tracking)
            self.static_runner.add(whale)
            self.post_analyzer.register_strategy(whale, "v2_whale")
            log.info("Whale Tracker initialized")

        if "meanrev" in strategies and self.mm_markets:
            meanrev = MeanReversion(bankroll=self.config.risk.max_total_exposure_usdc)
            self.static_runner.add(meanrev)
            self.post_analyzer.register_strategy(meanrev, "v2_meanrev")
            log.info("Mean Reversion initialized")

        log.info("Static Runner mapped %d strategies", len(self.static_runner.strategies))

    def setup_rolling(self, strategies: list[str], asset: str = "btc", interval: str = "5m"):
        """Initialize rolling runner and requested V2 strategies for continuous markets."""
        from strategies.v2.runner import UnifiedRunnerV2
        from data_pipeline.market_provider import RollingProvider
        from data_pipeline.price_feeds import get_btc_price, get_eth_price, get_sol_price, get_xrp_price
        from strategies.v2.momentum import Momentum
        from strategies.v2.mm import StoikovMM
        from strategies.v2.meanrev import MeanReversion
        from strategies.v2.last_seconds_snipe import LastSecondsSnipe
        from strategies.v2.portfolio import PortfolioRegimeStrategy
        from strategies.v2.rl_shadow import RLShadowStrategy

        price_feeds = {
            "btc": get_btc_price,
            "eth": get_eth_price,
            "sol": get_sol_price,
            "xrp": get_xrp_price,
        }
        price_feed = price_feeds.get(asset, get_btc_price)

        provider = RollingProvider(self.data_feed, asset=asset, interval=interval, price_feed=price_feed)
        runner = UnifiedRunnerV2(provider, self.pipeline, self.ems, oms=self.oms)
        self.rolling_runner = self.rolling_runner or runner
        self.rolling_runners.append(runner)

        default_budget = self.config.capital.strategy_budgets.get("btc5m", 0.10)
        budget_key = {
            "btc": "btc5m",
            "eth": "eth5m",
            "sol": "sol5m",
            "xrp": "xrp5m",
        }.get(asset, "btc5m")
        budget_frac = self.config.capital.strategy_budgets.get(budget_key, default_budget)
        actual_bankroll = self.config.capital.total_capital_usdc * budget_frac

        snipe_params_by_asset = {
            "btc": {
                "min_seconds_remaining": self.config.btc5m_snipe_min_seconds,
                "min_distance_usd": self.config.btc5m_snipe_min_distance_usd,
                "soft_min_distance_usd": self.config.btc5m_snipe_soft_min_distance_usd,
                "min_market_odds": self.config.btc5m_snipe_min_market_odds,
                "max_market_odds": self.config.btc5m_snipe_max_market_odds,
                "soft_min_market_odds": self.config.btc5m_snipe_soft_min_market_odds,
                "soft_max_market_odds": self.config.btc5m_snipe_soft_max_market_odds,
                "max_notional_usdc": self.config.btc5m_snipe_max_notional_usdc,
            },
            "eth": {
                "min_seconds_remaining": self.config.eth5m_snipe_min_seconds,
                "min_distance_usd": self.config.eth5m_snipe_min_distance_usd,
                "soft_min_distance_usd": self.config.eth5m_snipe_soft_min_distance_usd,
                "min_market_odds": self.config.eth5m_snipe_min_market_odds,
                "max_market_odds": self.config.eth5m_snipe_max_market_odds,
                "soft_min_market_odds": self.config.eth5m_snipe_soft_min_market_odds,
                "soft_max_market_odds": self.config.eth5m_snipe_soft_max_market_odds,
                "max_notional_usdc": self.config.eth5m_snipe_max_notional_usdc,
            },
            "sol": {
                "min_seconds_remaining": self.config.sol5m_snipe_min_seconds,
                "min_distance_usd": self.config.sol5m_snipe_min_distance_usd,
                "soft_min_distance_usd": self.config.sol5m_snipe_soft_min_distance_usd,
                "min_market_odds": self.config.sol5m_snipe_min_market_odds,
                "max_market_odds": self.config.sol5m_snipe_max_market_odds,
                "soft_min_market_odds": self.config.sol5m_snipe_soft_min_market_odds,
                "soft_max_market_odds": self.config.sol5m_snipe_soft_max_market_odds,
                "max_notional_usdc": self.config.sol5m_snipe_max_notional_usdc,
            },
            "xrp": {
                "min_seconds_remaining": self.config.xrp5m_snipe_min_seconds,
                "min_distance_usd": self.config.xrp5m_snipe_min_distance_usd,
                "soft_min_distance_usd": self.config.xrp5m_snipe_soft_min_distance_usd,
                "min_market_odds": self.config.xrp5m_snipe_min_market_odds,
                "max_market_odds": self.config.xrp5m_snipe_max_market_odds,
                "soft_min_market_odds": self.config.xrp5m_snipe_soft_min_market_odds,
                "soft_max_market_odds": self.config.xrp5m_snipe_soft_max_market_odds,
                "max_notional_usdc": self.config.xrp5m_snipe_max_notional_usdc,
            },
        }
        p = snipe_params_by_asset.get(asset, snipe_params_by_asset["btc"])
        
        if "btc5m" in strategies or "momentum" in strategies:
            mom = Momentum(
                bankroll=actual_bankroll,
                min_edge=self.config.btc5m_min_edge,
                max_price=self.config.btc5m_max_price,
                min_price=self.config.btc5m_min_price,
                min_entry_age=self.config.btc5m_min_entry_age,
                entry_deadline=self.config.btc5m_entry_deadline,
                min_abs_z=self.config.btc5m_min_abs_z,
                down_min_abs_z=self.config.btc5m_down_min_abs_z,
                min_mom_vol_ratio=self.config.btc5m_min_mom_vol_ratio,
                fair_cap=self.config.btc5m_fair_cap,
                confirmations_required=self.config.btc5m_confirmations_required,
                max_vwap_slippage=self.config.btc5m_max_vwap_slippage,
                down_edge_boost=self.config.btc5m_down_edge_boost,
            )
            runner.add(mom)
            self.post_analyzer.register_strategy(mom, "v2_momentum")

        if "mm" in strategies:
            mm = StoikovMM(self.config.market_making, self.data_feed)
            runner.add(mm)
            self.post_analyzer.register_strategy(mm, "v2_rolling_mm")

        if "meanrev" in strategies:
            mr = MeanReversion(bankroll=self.config.risk.max_total_exposure_usdc)
            runner.add(mr)
            self.post_analyzer.register_strategy(mr, "v2_rolling_meanrev")

        if "snipe" in strategies:
            snipe = LastSecondsSnipe(
                asset=asset,
                max_seconds_remaining=self.config.btc5m_snipe_max_seconds,
                min_seconds_remaining=p["min_seconds_remaining"],
                min_distance_usd=p["min_distance_usd"],
                min_distance_bps=self.config.btc5m_snipe_min_distance_bps,
                min_market_odds=p["min_market_odds"],
                max_market_odds=p["max_market_odds"],
                min_edge=self.config.btc5m_snipe_min_edge,
                min_fair=self.config.btc5m_snipe_min_fair,
                soft_max_seconds_remaining=self.config.btc5m_snipe_soft_max_seconds,
                soft_min_distance_usd=p["soft_min_distance_usd"],
                soft_min_distance_bps=self.config.btc5m_snipe_soft_min_distance_bps,
                soft_min_market_odds=p["soft_min_market_odds"],
                soft_max_market_odds=p["soft_max_market_odds"],
                soft_min_edge=self.config.btc5m_snipe_soft_min_edge,
                soft_min_fair=self.config.btc5m_snipe_soft_min_fair,
                kelly_frac=self.config.btc5m_snipe_kelly_frac,
                max_bet_pct=self.config.btc5m_snipe_max_bet_pct,
                bankroll=actual_bankroll,
                max_notional_usdc=p["max_notional_usdc"],
                max_vwap_slippage=self.config.btc5m_snipe_max_vwap_slippage,
                cooldown=self.config.btc5m_snipe_cooldown,
            )
            runner.add(snipe)
            self.post_analyzer.register_strategy(snipe, "v2_btc5m_snipe")

        if "oracle" in strategies:
            from strategies.v2.oracle_frontrun import OracleFrontrun
            oracle = OracleFrontrun(
                asset=asset,
                bankroll=actual_bankroll if "btc5m" in strategies or "momentum" in strategies else 10_000,
            )
            runner.add(oracle)
            self.post_analyzer.register_strategy(oracle, "v2_oracle_frontrun")

        if "portfolio" in strategies:
            snipe_params = {
                "max_seconds_remaining": self.config.btc5m_snipe_max_seconds,
                "min_seconds_remaining": p["min_seconds_remaining"],
                "min_distance_usd": p["min_distance_usd"],
                "min_distance_bps": self.config.btc5m_snipe_min_distance_bps,
                "min_market_odds": p["min_market_odds"],
                "max_market_odds": p["max_market_odds"],
                "min_edge": self.config.btc5m_snipe_min_edge,
                "min_fair": self.config.btc5m_snipe_min_fair,
                "soft_max_seconds_remaining": self.config.btc5m_snipe_soft_max_seconds,
                "soft_min_distance_usd": p["soft_min_distance_usd"],
                "soft_min_distance_bps": self.config.btc5m_snipe_soft_min_distance_bps,
                "soft_min_market_odds": p["soft_min_market_odds"],
                "soft_max_market_odds": p["soft_max_market_odds"],
                "soft_min_edge": self.config.btc5m_snipe_soft_min_edge,
                "soft_min_fair": self.config.btc5m_snipe_soft_min_fair,
                "kelly_frac": self.config.btc5m_snipe_kelly_frac,
                "max_bet_pct": self.config.btc5m_snipe_max_bet_pct,
                "max_notional_usdc": p["max_notional_usdc"],
                "max_vwap_slippage": self.config.btc5m_snipe_max_vwap_slippage,
                "cooldown": self.config.btc5m_snipe_cooldown,
            }
            portfolio = PortfolioRegimeStrategy(
                asset=asset,
                leader="btc",
                bankroll=actual_bankroll,
                include_leadlag=(asset != "btc"),
                momentum_params={
                    "min_edge": self.config.btc5m_min_edge,
                    "max_price": self.config.btc5m_max_price,
                    "min_price": self.config.btc5m_min_price,
                    "min_entry_age": self.config.btc5m_min_entry_age,
                    "entry_deadline": self.config.btc5m_entry_deadline,
                    "min_abs_z": self.config.btc5m_min_abs_z,
                    "down_min_abs_z": self.config.btc5m_down_min_abs_z,
                    "min_mom_vol_ratio": self.config.btc5m_min_mom_vol_ratio,
                    "fair_cap": self.config.btc5m_fair_cap,
                    "confirmations_required": self.config.btc5m_confirmations_required,
                    "max_vwap_slippage": self.config.btc5m_max_vwap_slippage,
                    "down_edge_boost": self.config.btc5m_down_edge_boost,
                },
                snipe_params=snipe_params,
            )
            runner.add(portfolio)
            self.post_analyzer.register_strategy(portfolio, "v2_portfolio")

        if "rl_shadow" in strategies:
            rl_shadow = RLShadowStrategy(
                asset=asset,
                bankroll=actual_bankroll,
                model_path=getattr(self.config, "_rl_model", "reports/rl_model.json"),
                min_price=getattr(self.config, "_rl_min_price", 0.20),
                max_price=getattr(self.config, "_rl_max_price", 0.95),
                log_every=getattr(self.config, "_rl_log_every", 1),
            )
            runner.add(rl_shadow)
            self.post_analyzer.register_strategy(rl_shadow, "v2_rl_shadow")

        log.info("Rolling runner initialized on %s %s with %d strategies", asset.upper(), interval, len(runner.strategies))

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

    # ---- Strategy Steps (V2) ----

    def _step_static(self):
        """Run all static market strategies."""
        if self.static_runner:
            self.static_runner.step()

    def _step_rolling(self):
        """Run unified rolling runner."""
        runners = self.rolling_runners or ([self.rolling_runner] if self.rolling_runner else [])
        for runner in runners:
            runner.step()

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

        # Automatically add rolling strategy wrapper if only "btc5m" is specified
        if "btc5m" in strategies and "rolling" not in strategies:
            strategies.append("rolling")
        if "portfolio" in strategies and "rolling" not in strategies:
            strategies.append("rolling")
        if "rl_shadow" in strategies and "rolling" not in strategies:
            strategies.append("rolling")

        asset = getattr(self.config, '_rolling_asset', 'btc')
        assets = getattr(self.config, '_rolling_assets', [asset])

        # Setup V2 Rolling Strategies (auto-discovers 5m tokens)
        if "rolling" in strategies:
            roll_strats = [s for s in strategies if s in ("mm", "meanrev", "btc5m", "momentum", "oracle", "snipe", "portfolio", "rl_shadow")]
            if not roll_strats:
                roll_strats = ["momentum", "oracle"]
            if "portfolio" in roll_strats:
                roll_strats = [s for s in roll_strats if s not in ("btc5m", "momentum", "oracle", "snipe")]
            for rolling_asset in assets:
                self.setup_rolling(roll_strats, asset=rolling_asset)

        # Setup V2 Static Strategies (only if NOT using rolling for these)
        # Avoids double-setup when running rolling,meanrev
        rolling_handles = set(roll_strats) if "rolling" in strategies else set()
        static_strats = [s for s in strategies if s in ("mm", "whale", "meanrev") and s not in rolling_handles]
        if static_strats:
            self.setup_v2_strategies(static_strats)

        # Start heartbeat for live mode
        if not self.config.dry_run and self.auth:
            threading.Thread(target=self._heartbeat_loop, daemon=True).start()

        active_strats = []
        if self.static_runner:
            s_stat = self.static_runner.status()
            active_strats.append(f"static({','.join(s_stat['strategies'])})")
        if self.rolling_runner:
            rolling_labels = []
            for runner in self.rolling_runners or [self.rolling_runner]:
                r_stat = runner.status()
                asset_name = getattr(runner.provider, "asset", "?").upper()
                rolling_labels.append(f"{asset_name}:{','.join(r_stat['strategies'])}")
            active_strats.append(f"rolling({';'.join(rolling_labels)})")

        log.info("=" * 60)
        log.info("Trading system started (V2 Engine)")
        log.info("  Strategies: %s", " | ".join(active_strats) or "none")
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

                # Collect market/strategy snapshots for post-session analysis
                self.post_analyzer.collect_snapshots()

                # Strategy steps (each wrapped so one failure doesn't crash the loop)
                for step_fn in [
                    self._step_static,
                    self._step_rolling,
                ]:
                    try:
                        step_fn()
                    except Exception as e:
                        log.warning("Strategy step %s failed: %s", step_fn.__name__, e)

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

        # Close all open positions at current market price
        self._close_all_positions()

        # Run post-session analysis (replaces the old simple report)
        self.post_analyzer.run_analysis()

    def _close_all_positions(self):
        """Sell all open positions at current mid price on shutdown."""
        open_positions = self.oms.get_all_open()
        if not open_positions:
            return

        log.info("Closing %d open position(s) at market...", len(open_positions))
        for pos in open_positions:
            if pos.size <= 0:
                continue

            # Get current price
            book = self.data_feed.get_book(pos.token_id)
            if book and book.best_bid:
                sell_price = book.best_bid
            elif book and book.mid:
                sell_price = book.mid
            else:
                sell_price = pos.cur_price if pos.cur_price > 0 else pos.avg_price

            # Emit a sell fill directly (bypass simulator — we're shutting down)
            from oms.position_manager import Fill
            import time
            fill = Fill(
                token_id=pos.token_id,
                side="SELL",
                size=pos.size,
                price=sell_price,
                timestamp=time.time(),
                source="shutdown_close",
            )
            self.ems._fire_fill(fill)

            pnl = (sell_price - pos.avg_price) * pos.size
            log.info(
                "CLOSED: %s %.1f @ %.4f (entry=%.4f) pnl=$%.2f",
                pos.token_id[:16], pos.size, sell_price, pos.avg_price, pnl,
            )

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
  meanrev  Mean reversion — buy dips, sell rips in contested markets
  btc5m    BTC 5-minute rolling markets — momentum (auto)
  rolling  Run strategies on 5-minute rolling markets (momentum + oracle by default)
    oracle   Oracle front-run — exploit Binance-Polymarket price lag
        snipe    Last-seconds snipe on rolling 5m markets (high-odds endgame)
        portfolio Regime portfolio wrapper for momentum/oracle/leadlag/snipe
        rl_shadow Tabular RL shadow policy logger (no orders)
  all      Run all strategies

5-minute rolling market (all strategies on BTC 5m):
    python main.py --strategy rolling --rolling-asset btc --dry-run --no-learn
    python main.py --strategy rolling --rolling-asset eth --dry-run --no-learn

Examples:
  python main.py --strategy mm --search "bitcoin" --dry-run
  python main.py --strategy mm,meanrev --token TOKEN --dry-run
  python main.py --strategy whale --dry-run
  python main.py --strategy rolling --dry-run --no-learn
  python main.py --strategy rolling,oracle --dry-run --no-learn
  python main.py --strategy all --search "election" --dry-run
        """,
    )
    parser.add_argument("--strategy", type=str, default="mm",
                        help="Strategy: mm, whale, meanrev, btc5m, rolling, oracle, snipe, portfolio, rl_shadow, all (comma-separated)")
    parser.add_argument("--search", type=str, default="",
                        help="Search for market(s) interactively")
    parser.add_argument("--token", type=str,
                        help="Token ID(s) to trade (comma-separated for multi-market)")
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
    parser.add_argument("--rolling-asset", type=str, default="btc",
                        help="Asset for rolling 5m markets: btc, eth, sol, xrp (default: btc)")
    parser.add_argument("--rolling-assets", type=str, default="",
                        help="Comma-separated rolling assets, e.g. btc,eth,sol,xrp")
    parser.add_argument("--rl-model", type=str, default="reports/rl_model.json",
                        help="Path to tabular RL model JSON (default: reports/rl_model.json)")
    parser.add_argument("--rl-min-price", type=float, default=0.20,
                        help="RL shadow hard min entry price (default: 0.20)")
    parser.add_argument("--rl-max-price", type=float, default=0.95,
                        help="RL shadow hard max entry price (default: 0.95)")
    parser.add_argument("--rl-log-every", type=int, default=1,
                        help="Log every N RL shadow ticks (default: 1)")
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
    config._rolling_asset = args.rolling_asset.lower()
    config._rolling_assets = (
        [a.strip().lower() for a in args.rolling_assets.split(",") if a.strip()]
        if args.rolling_assets else [config._rolling_asset]
    )
    config._rl_model = args.rl_model
    config._rl_min_price = args.rl_min_price
    config._rl_max_price = args.rl_max_price
    config._rl_log_every = args.rl_log_every
    config.market_making.gamma = args.gamma
    config.market_making.spread_k = args.spread_k
    config.market_making.order_size = args.size
    config.market_making.num_levels = args.levels
    config.market_making.refresh_interval = args.interval
    config.risk.max_position_size_usdc = args.max_position
    config.risk.max_drawdown_pct = args.max_drawdown

    # Parse strategies
    if args.strategy == "all":
        strategies = ["mm", "whale", "meanrev", "btc5m", "rolling"]
    else:
        strategies = [s.strip() for s in args.strategy.split(",")]

    # Initialize system
    system = TradingSystem(config)
    system.reconcile_interval = args.reconcile_interval

    # Handle Ctrl+C
    signal.signal(signal.SIGINT, lambda *_: setattr(system, 'running', False))

    # Market selection for strategies that need tokens
    # Skip if only rolling strategies — rolling auto-discovers its own tokens
    needs_market = any(s in strategies for s in ["mm", "meanrev"]) and "rolling" not in strategies
    if needs_market:
        if args.token:
            system.set_tokens(args.token)
        else:
            multi = input("Multi-market mode? (y/N): ").strip().lower() == "y" if not args.search else False
            system.select_markets_interactive(args.search, multi=multi)

    # Connect for live trading
    if not args.dry_run:
        system.connect()

    # Run
    system.run(strategies)


if __name__ == "__main__":
    main()
