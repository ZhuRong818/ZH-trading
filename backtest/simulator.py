"""
Backtest Simulator — replays historical data through strategies.

Simulates the full lifecycle:
  1. Feed price history to strategies tick by tick
  2. Strategies emit signals via skills
  3. Simulator fills at the signal price (no book depth simulation)
  4. Settlement at window end using actual historical outcome
  5. Track PnL, win rate, drawdown

No network calls — everything runs from loaded data.

Usage:
    from backtest.data_loader import BinanceDataLoader
    from backtest.simulator import BacktestSimulator

    loader = BinanceDataLoader()
    klines = loader.load_klines("BTCUSDT", "1m", days=7)
    windows = loader.klines_to_windows(klines)

    sim = BacktestSimulator(bankroll=10000)
    sim.add_strategy("momentum")
    sim.add_strategy("oracle")
    results = sim.run(windows)
    sim.print_report()
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from skills.price_features import PriceFeatureSkill
from skills.fair_value import MomentumFairValueSkill, OracleFairValueSkill
from skills.edge import EdgeSkill
from skills.risk_gate import RiskGateSkill, RiskGateConfig
from skills.sizing import PositionSizerSkill, SizingConfig
from skills.types import RollingMarket, PriceFeatures

log = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    window_idx: int
    strategy: str
    direction: str
    entry_price: float
    size_usdc: float
    edge: float
    fair: float
    outcome: str        # actual: "UP" or "DOWN"
    pnl: float = 0.0
    won: bool = False


@dataclass
class BacktestResult:
    trades: List[BacktestTrade] = field(default_factory=list)
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    max_drawdown: float = 0.0
    peak_equity: float = 0.0
    equity_curve: List[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total * 100 if total > 0 else 0

    @property
    def sharpe(self) -> float:
        if len(self.trades) < 2:
            return 0.0
        pnls = [t.pnl for t in self.trades]
        avg = sum(pnls) / len(pnls)
        var = sum((p - avg) ** 2 for p in pnls) / len(pnls)
        std = math.sqrt(var) if var > 0 else 1e-10
        return (avg / std) * math.sqrt(252)

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.pnl < 0))
        return gross_profit / gross_loss if gross_loss > 0 else float("inf")


class StrategyRunner:
    """Runs a single strategy on one window's price data."""

    def __init__(self, name: str, features: PriceFeatureSkill,
                 fair_value_skill, edge_skill: EdgeSkill,
                 risk_gate: RiskGateSkill, risk_config: RiskGateConfig,
                 sizer: PositionSizerSkill, sizing_config: SizingConfig,
                 move_threshold_bps: float = 0.0,
                 staleness_threshold: float = 0.0,
                 max_notional: float = 500.0):
        self.name = name
        self.features = features
        self.fair_value = fair_value_skill
        self.edge_skill = edge_skill
        self.risk_gate = risk_gate
        self.risk_config = risk_config
        self.sizer = sizer
        self.sizing_config = sizing_config
        self.move_threshold_bps = move_threshold_bps
        self.staleness_threshold = staleness_threshold
        self.max_notional = max_notional

    def evaluate(self, prices: List[float], market: RollingMarket) -> Optional[dict]:
        """
        Run the strategy on a window's price data.
        Returns trade decision dict or None.
        """
        # Need enough price history
        if len(prices) < 25:
            return None

        pf = self.features.compute(prices)
        if pf is None:
            return None

        # Oracle-specific: move filter
        if self.move_threshold_bps > 0:
            if abs(pf.move_bps) < self.move_threshold_bps:
                return None

        fair = self.fair_value.estimate(pf, market)
        if fair is None:
            return None

        edge = self.edge_skill.best_edge(fair, market)

        # Oracle-specific: staleness check
        if self.staleness_threshold > 0:
            if edge.edge < self.staleness_threshold:
                return None

        if not self.risk_gate.passes(edge, market, self.risk_config):
            return None

        size_usdc = self.sizer.size(edge, self.sizing_config)
        if size_usdc <= 0:
            return None

        size_usdc = min(size_usdc, self.max_notional)

        return {
            "direction": edge.direction,
            "entry_price": edge.market_price,
            "size_usdc": size_usdc,
            "edge": edge.edge,
            "fair": edge.fair,
        }


class BacktestSimulator:
    """
    Replays historical BTC 5-minute windows through strategies.
    """

    def __init__(self, bankroll: float = 10_000):
        self.bankroll = bankroll
        self.strategies: List[StrategyRunner] = []
        self.result = BacktestResult()

    def add_strategy(self, name: str, **kwargs):
        """Add a strategy by name."""
        features = PriceFeatureSkill(lookback_ticks=5, momentum_ticks=20)
        edge_skill = EdgeSkill()
        sizer = PositionSizerSkill()

        if name == "momentum":
            runner = StrategyRunner(
                name="momentum",
                features=features,
                fair_value_skill=MomentumFairValueSkill(),
                edge_skill=edge_skill,
                risk_gate=RiskGateSkill(),
                risk_config=RiskGateConfig(
                    min_edge=kwargs.get("min_edge", 0.03),
                    max_price=kwargs.get("max_price", 0.65),
                    min_seconds_remaining=30,
                ),
                sizer=sizer,
                sizing_config=SizingConfig(
                    bankroll=self.bankroll,
                    kelly_fraction=kwargs.get("kelly_frac", 0.20),
                    max_bet_pct=kwargs.get("max_bet_pct", 0.05),
                ),
                max_notional=kwargs.get("max_notional", 500),
            )
        elif name == "oracle":
            runner = StrategyRunner(
                name="oracle",
                features=features,
                fair_value_skill=OracleFairValueSkill(),
                edge_skill=edge_skill,
                risk_gate=RiskGateSkill(),
                risk_config=RiskGateConfig(
                    min_edge=kwargs.get("staleness_threshold", 0.01),
                    max_price=kwargs.get("max_price", 0.55),
                    min_price=kwargs.get("min_price", 0.20),
                    min_seconds_remaining=60,
                ),
                sizer=sizer,
                sizing_config=SizingConfig(
                    bankroll=self.bankroll,
                    kelly_fraction=kwargs.get("kelly_frac", 0.25),
                    max_bet_pct=kwargs.get("max_bet_pct", 0.05),
                ),
                move_threshold_bps=kwargs.get("move_threshold_bps", 2.0),
                staleness_threshold=kwargs.get("staleness_threshold", 0.01),
                max_notional=kwargs.get("max_notional", 500),
            )
        else:
            raise ValueError(f"Unknown strategy: {name}")

        self.strategies.append(runner)
        log.info("Backtest: added strategy '%s'", name)

    def run(self, windows: List[dict]) -> BacktestResult:
        """
        Run all strategies across all windows.

        Each window:
        1. Build price history from prior windows
        2. Simulate market prices (up_price = 0.50 at start, shift based on price movement)
        3. Run each strategy
        4. Settle using actual outcome
        """
        self.result = BacktestResult()
        equity = self.bankroll
        peak = self.bankroll
        price_history = []

        for i, window in enumerate(windows):
            # Accumulate price history
            price_history.extend(window["prices"])
            if len(price_history) > 500:
                price_history = price_history[-300:]

            strike = window["strike"]
            outcome = window["outcome"]
            end_price = window["end_price"]

            # Simulate market prices at mid-window
            # Use actual price movement to estimate what Polymarket odds would be
            if len(window["prices"]) > 2:
                mid_window_price = window["prices"][len(window["prices"]) // 2]
            else:
                mid_window_price = strike

            distance_pct = (mid_window_price - strike) / strike if strike > 0 else 0
            # Simple model: map distance to probability
            simulated_up_price = max(0.10, min(0.90, 0.50 + distance_pct * 50))
            simulated_down_price = 1.0 - simulated_up_price

            market = RollingMarket(
                strike_price=strike,
                seconds_remaining=150,  # simulate entry at mid-window
                up_price=simulated_up_price,
                down_price=simulated_down_price,
                up_token_id=f"up_{i}",
                down_token_id=f"down_{i}",
            )

            # Run each strategy
            for strategy in self.strategies:
                decision = strategy.evaluate(price_history, market)
                if decision is None:
                    continue

                # Settlement
                direction = decision["direction"]
                entry_price = decision["entry_price"]
                size_usdc = decision["size_usdc"]

                if direction == outcome:
                    payout = 1.0
                    pnl = size_usdc * (payout / entry_price - 1) if entry_price > 0 else 0
                    won = True
                else:
                    pnl = -size_usdc
                    won = False

                trade = BacktestTrade(
                    window_idx=i,
                    strategy=strategy.name,
                    direction=direction,
                    entry_price=entry_price,
                    size_usdc=size_usdc,
                    edge=decision["edge"],
                    fair=decision["fair"],
                    outcome=outcome,
                    pnl=pnl,
                    won=won,
                )
                self.result.trades.append(trade)

                equity += pnl
                if equity > peak:
                    peak = equity
                dd = peak - equity
                if dd > self.result.max_drawdown:
                    self.result.max_drawdown = dd

                if won:
                    self.result.wins += 1
                else:
                    self.result.losses += 1

            self.result.equity_curve.append(equity)

        self.result.total_pnl = equity - self.bankroll
        self.result.peak_equity = peak

        log.info("Backtest complete: %d windows, %d trades", len(windows), len(self.result.trades))
        return self.result

    def print_report(self):
        """Print backtest results."""
        r = self.result
        trades = r.trades
        total = r.wins + r.losses

        print()
        print("=" * 60)
        print("BACKTEST REPORT")
        print("=" * 60)
        print(f"  Windows processed: {len(r.equity_curve)}")
        print(f"  Total trades:      {total}")
        print(f"  Wins:              {r.wins}")
        print(f"  Losses:            {r.losses}")
        print(f"  Win rate:          {r.win_rate:.1f}%")
        print(f"  Total PnL:         ${r.total_pnl:.2f}")
        print(f"  Max drawdown:      ${r.max_drawdown:.2f}")
        print(f"  Sharpe ratio:      {r.sharpe:.2f}")
        print(f"  Profit factor:     {r.profit_factor:.2f}")

        if trades:
            avg_win = sum(t.pnl for t in trades if t.won) / max(r.wins, 1)
            avg_loss = sum(t.pnl for t in trades if not t.won) / max(r.losses, 1)
            print(f"  Avg win:           ${avg_win:.2f}")
            print(f"  Avg loss:          ${avg_loss:.2f}")

        # Per strategy breakdown
        strat_names = set(t.strategy for t in trades)
        if len(strat_names) > 1:
            print()
            print("  Per-Strategy:")
            for name in sorted(strat_names):
                st = [t for t in trades if t.strategy == name]
                sw = sum(1 for t in st if t.won)
                sl = len(st) - sw
                spnl = sum(t.pnl for t in st)
                swr = sw / len(st) * 100 if st else 0
                print(f"    {name:15s}: {len(st)} trades, {swr:.0f}% WR, ${spnl:.2f} PnL")

        # Direction breakdown
        up_trades = [t for t in trades if t.direction == "UP"]
        down_trades = [t for t in trades if t.direction == "DOWN"]
        if up_trades or down_trades:
            print()
            up_wr = sum(1 for t in up_trades if t.won) / max(len(up_trades), 1) * 100
            down_wr = sum(1 for t in down_trades if t.won) / max(len(down_trades), 1) * 100
            print(f"  UP trades:   {len(up_trades)}, {up_wr:.0f}% WR")
            print(f"  DOWN trades: {len(down_trades)}, {down_wr:.0f}% WR")

        print("=" * 60)
