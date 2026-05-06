"""
Module 5d — Whale Tracking & Copy Trading

Monitors Polymarket leaderboard and top traders' positions/trades.
Detects when high-win-rate traders make large conviction moves.
Generates copy-trade signals.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import requests

from config import DATA_API_BASE, GAMMA_BASE, WhaleTrackingConfig
from data_pipeline.market_data import MarketDataFeed
from ems.execution import ExecutionEngine
from oms.position_manager import PositionManager

log = logging.getLogger(__name__)


@dataclass
class WhaleProfile:
    wallet: str
    username: str = ""
    total_volume: float = 0.0
    pnl: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    trust_score: float = 0.0       # composite score
    domains: List[str] = field(default_factory=list)
    last_seen: float = 0.0


@dataclass
class WhaleSignal:
    wallet: str
    username: str
    token_id: str
    condition_id: str
    market_question: str
    outcome: str
    side: str          # BUY or SELL
    size: float        # in shares
    price: float
    win_rate: float
    pnl: float
    timestamp: float


class WhaleRegistry:
    """
    Maintains a scored registry of top traders.
    Built from the Polymarket leaderboard + trade history.
    """

    def __init__(self, config: WhaleTrackingConfig):
        self.config = config
        self.whales: Dict[str, WhaleProfile] = {}
        self.session = requests.Session()

    def refresh_from_leaderboard(self):
        """Pull top traders from the Polymarket leaderboard API."""
        try:
            for period in ["MONTH", "ALL"]:
                resp = self.session.get(
                    f"{DATA_API_BASE}/v1/leaderboard",
                    params={
                        "orderBy": "PNL",
                        "timePeriod": period,
                        "limit": self.config.top_n_traders,
                    },
                )
                resp.raise_for_status()
                entries = resp.json()

                for entry in entries:
                    wallet = entry.get("proxyWallet", "")
                    if not wallet:
                        continue

                    existing = self.whales.get(wallet)
                    if existing:
                        # Update with latest data
                        existing.pnl = max(existing.pnl, float(entry.get("pnl", 0)))
                        existing.total_volume = max(existing.total_volume, float(entry.get("vol", 0)))
                        existing.username = entry.get("userName", existing.username)
                    else:
                        self.whales[wallet] = WhaleProfile(
                            wallet=wallet,
                            username=entry.get("userName", ""),
                            total_volume=float(entry.get("vol", 0)),
                            pnl=float(entry.get("pnl", 0)),
                        )

            log.info("Whale registry refreshed: %d whales tracked", len(self.whales))

        except Exception as e:
            log.error("Leaderboard fetch failed: %s", e)

    def enrich_whale(self, wallet: str):
        """Fetch trade history to compute win rate and trust score."""
        whale = self.whales.get(wallet)
        if not whale:
            return

        try:
            # Get recent trades
            resp = self.session.get(
                f"{DATA_API_BASE}/trades",
                params={"user": wallet, "limit": 200},
            )
            resp.raise_for_status()
            trades = resp.json()

            whale.total_trades = len(trades)

            # Get closed positions for win rate
            resp = self.session.get(
                f"{DATA_API_BASE}/closed-positions",
                params={"user": wallet, "limit": 50},
            )
            resp.raise_for_status()
            closed = resp.json()

            if closed:
                wins = sum(1 for p in closed if float(p.get("realizedPnl", 0)) > 0)
                whale.win_rate = wins / len(closed) if closed else 0

            # Compute trust score
            whale.trust_score = self._compute_trust_score(whale)
            whale.last_seen = time.time()

        except Exception as e:
            log.warning("Failed to enrich whale %s: %s", wallet[:10], e)

    def _compute_trust_score(self, whale: WhaleProfile) -> float:
        """
        Composite trust score from:
        - Win rate (40%)
        - PnL magnitude (30%)
        - Trade count / experience (20%)
        - Recency (10%)
        """
        wr_score = min(whale.win_rate / 0.9, 1.0) * 0.4
        pnl_score = min(max(whale.pnl, 0) / 100_000, 1.0) * 0.3
        exp_score = min(whale.total_trades / 100, 1.0) * 0.2
        recency = 0.1 if whale.last_seen > time.time() - 86400 else 0.0
        return wr_score + pnl_score + exp_score + recency

    def get_trusted_whales(self) -> List[WhaleProfile]:
        """Return whales that meet our minimum criteria."""
        return [
            w for w in self.whales.values()
            if w.trust_score >= 0.5
            and w.total_trades >= self.config.min_trades
        ]


class WhaleTracker:
    """
    Monitors whale positions and generates copy-trade signals.

    Strategy:
    1. Poll leaderboard for top traders
    2. Track their open positions
    3. Detect new position entries (conviction moves)
    4. Generate copy-trade signals with position sizing
    """

    def __init__(
        self,
        config: WhaleTrackingConfig,
        data_feed: MarketDataFeed,
        ems: ExecutionEngine,
        oms: PositionManager,
    ):
        self.config = config
        self.data = data_feed
        self.ems = ems
        self.oms = oms
        self.registry = WhaleRegistry(config)
        self._known_positions: Dict[str, Dict[str, float]] = {}  # wallet -> {token_id: size}
        self._last_refresh = 0.0
        self.signals: List[WhaleSignal] = []

    def initialize(self):
        """First-time setup: build whale registry."""
        log.info("Building whale registry from leaderboard...")
        self.registry.refresh_from_leaderboard()

        # Enrich top whales with trade history
        trusted = sorted(
            self.registry.whales.values(),
            key=lambda w: w.pnl,
            reverse=True,
        )[:10]

        for whale in trusted:
            self.registry.enrich_whale(whale.wallet)
            log.info(
                "  %s (%s): PnL=$%.0f win_rate=%.0f%% trades=%d trust=%.2f",
                whale.username or whale.wallet[:10],
                whale.wallet[:10],
                whale.pnl, whale.win_rate * 100,
                whale.total_trades, whale.trust_score,
            )

        # Snapshot current positions
        for whale in self.registry.get_trusted_whales():
            self._snapshot_positions(whale.wallet)

    def _snapshot_positions(self, wallet: str):
        """Record a whale's current positions for change detection."""
        try:
            resp = requests.get(
                f"{DATA_API_BASE}/positions",
                params={"user": wallet, "sizeThreshold": 1, "limit": 100},
            )
            resp.raise_for_status()
            positions = resp.json()

            self._known_positions[wallet] = {}
            for pos in positions:
                token_id = pos.get("asset", "")
                size = float(pos.get("size", 0))
                if token_id and size > 0:
                    self._known_positions[wallet][token_id] = size

        except Exception as e:
            log.warning("Failed to snapshot positions for %s: %s", wallet[:10], e)

    def step(self) -> List[WhaleSignal]:
        """
        One iteration: check for new whale moves.
        Returns list of new signals.
        """
        now = time.time()

        # Refresh registry every 10 minutes
        if now - self._last_refresh > 600:
            self.registry.refresh_from_leaderboard()
            self._last_refresh = now

        signals = []
        trusted_whales = self.registry.get_trusted_whales()

        for whale in trusted_whales:
            new_signals = self._check_whale_activity(whale)
            signals.extend(new_signals)

        self.signals.extend(signals)
        return signals

    def _check_whale_activity(self, whale: WhaleProfile) -> List[WhaleSignal]:
        """Check if a whale has made new trades since last check."""
        signals = []

        try:
            # Get current positions
            resp = requests.get(
                f"{DATA_API_BASE}/positions",
                params={"user": whale.wallet, "sizeThreshold": 1, "limit": 100},
            )
            resp.raise_for_status()
            current_positions = resp.json()

            old_positions = self._known_positions.get(whale.wallet, {})
            new_positions = {}

            for pos in current_positions:
                token_id = pos.get("asset", "")
                size = float(pos.get("size", 0))
                if not token_id or size <= 0:
                    continue

                new_positions[token_id] = size
                old_size = old_positions.get(token_id, 0)
                delta = size - old_size

                # Detect significant new entries or additions
                if delta > 0 and delta * float(pos.get("curPrice", 0.5)) >= 100:
                    # Whale added to this position
                    cur_price = float(pos.get("curPrice", 0.5))

                    # Skip if market is in tail regime (too close to resolution)
                    if cur_price > 0.90 or cur_price < 0.10:
                        continue

                    signal = WhaleSignal(
                        wallet=whale.wallet,
                        username=whale.username,
                        token_id=token_id,
                        condition_id=pos.get("conditionId", ""),
                        market_question=pos.get("title", ""),
                        outcome=pos.get("outcome", ""),
                        side="BUY",
                        size=delta,
                        price=cur_price,
                        win_rate=whale.win_rate,
                        pnl=whale.pnl,
                        timestamp=time.time(),
                    )
                    signals.append(signal)

                    log.info(
                        "WHALE SIGNAL: %s (%s, WR=%.0f%%) BUY %.0f %s @ %.4f | %s",
                        whale.username or whale.wallet[:10],
                        whale.wallet[:10],
                        whale.win_rate * 100,
                        delta, pos.get("outcome", "?"),
                        cur_price,
                        pos.get("title", "")[:50],
                    )

            # Update snapshot
            self._known_positions[whale.wallet] = new_positions

        except Exception as e:
            log.warning("Failed to check whale %s: %s", whale.wallet[:10], e)

        return signals

    def execute_copy_trade(self, signal: WhaleSignal) -> Optional[str]:
        """
        Execute a copy trade based on a whale signal.
        Applies conservative sizing and position limits.
        """
        cfg = self.config

        # Validate whale quality
        if signal.win_rate < cfg.high_confidence_win_rate:
            log.info("Skip copy: win_rate %.0f%% < threshold %.0f%%",
                     signal.win_rate * 100, cfg.high_confidence_win_rate * 100)
            return None

        # Calculate copy size (fraction of whale's position)
        copy_size = min(
            signal.size * cfg.copy_fraction,
            cfg.max_copy_size_usdc / signal.price if signal.price > 0 else 0,
        )

        if copy_size < 1:
            return None

        # Check existing position
        existing = self.oms.get_position(signal.token_id)
        existing_notional = existing.notional if existing else 0

        if existing_notional + copy_size * signal.price > cfg.max_copy_size_usdc:
            log.info("Skip copy: would exceed max position ($%.0f)",
                     existing_notional + copy_size * signal.price)
            return None

        log.info(
            "COPY TRADE: %s %.1f shares @ %.4f from %s (WR=%.0f%%)",
            signal.side, copy_size, signal.price,
            signal.username or signal.wallet[:10],
            signal.win_rate * 100,
        )

        return self.ems.place_order(
            token_id=signal.token_id,
            side=signal.side,
            price=signal.price,
            size=copy_size,
            source=f"whale_copy_{signal.username or signal.wallet[:8]}",
        )
