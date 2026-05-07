"""
Unified Strategy Runner — runs any strategy on any market type.

Bridges the gap between:
  - Long-dated strategies (MM, meanrev, fade) that expect static tokens
  - Rolling 5m markets where tokens change every 5 minutes

Instead of modifying every strategy, this runner:
  1. Gets MarketContexts from a provider (Static or Rolling)
  2. For Rolling markets, re-initializes strategies each window
  3. Calls strategy.step() each cycle
  4. Handles cleanup when windows roll

Usage:
    provider = RollingProvider(data_feed, "btc", "5m", binance_price)
    runner = UnifiedRunner(provider, ems, oms, config)
    runner.add_strategy("mm")
    runner.add_strategy("meanrev")
    runner.step()  # call each iteration
"""

import logging
import math
import time
from typing import Dict, List, Optional

import requests

from config import SystemConfig, MarketMakingConfig
from data_pipeline.market_data import MarketDataFeed
from data_pipeline.market_provider import MarketProvider, MarketContext, RollingProvider, StaticProvider
from ems.execution import ExecutionEngine
from oms.position_manager import PositionManager
from strategies.market_making.stoikov_model import StoikovMarketMaker
from strategies.mean_reversion.mean_reversion import MeanReversionStrategy, MeanReversionConfig
from strategies.resolution_fade.resolution_fade import ResolutionFadeStrategy, ResolutionFadeConfig
from strategies.kelly import kelly_size
from pipeline.signal import TradingSignal
from pipeline.engine import PipelineEngine

log = logging.getLogger(__name__)

BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"


def get_btc_price() -> float:
    resp = requests.get(BINANCE_TICKER, params={"symbol": "BTCUSDT"}, timeout=5)
    return float(resp.json()["price"])


def get_eth_price() -> float:
    resp = requests.get(BINANCE_TICKER, params={"symbol": "ETHUSDT"}, timeout=5)
    return float(resp.json()["price"])


class UnifiedRunner:
    """
    Runs multiple strategies on markets from any provider.
    Handles window rotation for rolling markets.
    """

    def __init__(
        self,
        provider: MarketProvider,
        ems: ExecutionEngine,
        oms: PositionManager,
        config: SystemConfig,
        pipeline: Optional[PipelineEngine] = None,
    ):
        self.provider = provider
        self.ems = ems
        self.oms = oms
        self.config = config
        self.pipeline = pipeline

        self._strategy_names: List[str] = []
        self._mm_instances: Dict[str, StoikovMarketMaker] = {}
        self._meanrev: Optional[MeanReversionStrategy] = None
        self._fade: Optional[ResolutionFadeStrategy] = None
        self._last_window_key: str = ""
        self._is_rolling = isinstance(provider, RollingProvider)

    def add_strategy(self, name: str):
        """Register a strategy to run. One of: mm, meanrev, fade."""
        if name not in self._strategy_names:
            self._strategy_names.append(name)

    def step(self):
        """One iteration: refresh markets, run all strategies."""
        # Refresh market data
        self.provider.refresh()
        contexts = self.provider.all_contexts()

        if not contexts:
            return

        # For rolling markets: check if window changed
        if self._is_rolling:
            window_key = contexts[0].condition_id if contexts else ""
            if window_key != self._last_window_key:
                self._on_window_roll(contexts)
                self._last_window_key = window_key

            # Skip if too little time left
            remaining = contexts[0].seconds_remaining if contexts else 0
            if remaining < 30:
                return

        # Run each registered strategy
        for name in self._strategy_names:
            try:
                if name == "mm":
                    self._step_mm(contexts)
                elif name == "meanrev":
                    self._step_meanrev(contexts)
                elif name == "fade":
                    self._step_fade(contexts)
            except Exception as e:
                log.warning("UnifiedRunner: %s step failed: %s", name, e)

    def _on_window_roll(self, contexts: List[MarketContext]):
        """Re-initialize strategies for the new window."""
        log.info("UnifiedRunner: window rolled to %s",
                 contexts[0].condition_id if contexts else "?")

        # Cancel any existing orders
        self.ems.cancel_all()

        # Rebuild strategy instances for new tokens
        self._mm_instances.clear()
        self._meanrev = None
        self._fade = None

        for ctx in contexts:
            # MM: create one per token
            if "mm" in self._strategy_names:
                mm = StoikovMarketMaker(
                    config=self.config.market_making,
                    data_feed=self.provider.data,
                    ems=self.ems,
                    oms=self.oms,
                    token_id=ctx.token_id,
                    tick_size=ctx.tick_size,
                    neg_risk=ctx.neg_risk,
                    gamma_price=ctx.mid_price or ctx.external_price * 0.5,
                )
                self._mm_instances[ctx.token_id] = mm

        # Mean reversion: uses all token IDs
        if "meanrev" in self._strategy_names:
            token_ids = [c.token_id for c in contexts]
            self._meanrev = MeanReversionStrategy(
                config=MeanReversionConfig(
                    bankroll=self.config.risk.max_total_exposure_usdc,
                    lookback_window=15,       # shorter for 5m markets
                    entry_threshold=0.02,      # tighter for fast markets
                    min_observations=5,        # less history needed
                    cooldown_seconds=30,       # shorter cooldown
                ),
                data_feed=self.provider.data,
                ems=self.ems,
                oms=self.oms,
                token_ids=token_ids,
            )

        # Fade: uses market context list
        if "fade" in self._strategy_names:
            fade_markets = [{
                "token_id": c.token_id,
                "end_date": "",  # rolling markets compute remaining differently
                "tick_size": c.tick_size,
                "neg_risk": c.neg_risk,
                "question": c.question,
            } for c in contexts]
            self._fade = ResolutionFadeStrategy(
                config=ResolutionFadeConfig(
                    bankroll=self.config.risk.max_total_exposure_usdc,
                    certainty_min_days_left=0,           # no minimum for 5m
                    convergence_max_days=999,             # always check
                    convergence_min_price=0.80,           # 80%+ certainty
                    last_minute_hours=0.05,               # final 3 minutes
                    kelly_fraction=0.15,
                ),
                data_feed=self.provider.data,
                ems=self.ems,
                oms=self.oms,
                markets=fade_markets,
            )

    def _step_mm(self, contexts: List[MarketContext]):
        """Run MM on all tokens."""
        for ctx in contexts:
            if not ctx.is_valid:
                continue
            mm = self._mm_instances.get(ctx.token_id)
            if mm:
                mm.step()

    def _step_meanrev(self, contexts: List[MarketContext]):
        """Run mean reversion."""
        if self._meanrev:
            self._meanrev.step()

    def _step_fade(self, contexts: List[MarketContext]):
        """Run resolution fade with live remaining time."""
        if not self._fade:
            return

        # For rolling markets, inject the real seconds_remaining
        # into the fade strategy's market configs
        for ctx in contexts:
            for mkt in self._fade.markets:
                if mkt["token_id"] == ctx.token_id:
                    # Convert seconds to days for the fade strategy
                    days = ctx.seconds_remaining / 86400
                    # Override end_date calculation by setting a near date
                    from datetime import datetime, timezone, timedelta
                    mkt["end_date"] = (
                        datetime.now(timezone.utc) + timedelta(seconds=ctx.seconds_remaining)
                    ).isoformat()

        self._fade.step()

    def status(self) -> dict:
        return {
            "provider_type": "rolling" if self._is_rolling else "static",
            "active_contexts": len(self.provider.all_contexts()),
            "strategies": self._strategy_names,
            "mm_instances": len(self._mm_instances),
            "window": self._last_window_key[:30] if self._last_window_key else "none",
        }
