"""
Backtest Simulator - replays historical BTC 5-minute windows.

The default ``momentum`` path is aligned with ``strategies.v2.momentum``:
it uses the same probability cap, price band, edge gates, DOWN penalty,
time gates, and confirmation requirement. ``momentum_legacy`` keeps the
older skills-based implementation for baseline comparisons.
"""

import logging
import math
import random
from dataclasses import dataclass, field
from typing import List, Optional

from skills.edge import EdgeSkill
from skills.fair_value import MomentumFairValueSkill, OracleFairValueSkill
from skills.price_features import PriceFeatureSkill
from skills.risk_gate import RiskGateConfig, RiskGateSkill
from skills.sizing import PositionSizerSkill, SizingConfig
from skills.types import RollingMarket

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
    outcome: str
    pnl: float = 0.0
    won: bool = False
    entry_age_seconds: float = 0.0
    z_score: float = 0.0
    market_price: float = 0.0
    fee: float = 0.0
    slippage: float = 0.0
    regime: str = "contested"


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


class LegacyStrategyRunner:
    """Runs the older skills-based strategies for baseline comparison."""

    def __init__(
        self,
        name: str,
        features: PriceFeatureSkill,
        fair_value_skill,
        edge_skill: EdgeSkill,
        risk_gate: RiskGateSkill,
        risk_config: RiskGateConfig,
        sizer: PositionSizerSkill,
        sizing_config: SizingConfig,
        move_threshold_bps: float = 0.0,
        staleness_threshold: float = 0.0,
        max_notional: float = 500.0,
    ):
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
        if len(prices) < 25:
            return None

        pf = self.features.compute(prices)
        if pf is None:
            return None

        if self.move_threshold_bps > 0 and abs(pf.move_bps) < self.move_threshold_bps:
            return None

        fair = self.fair_value.estimate(pf, market)
        if fair is None:
            return None

        edge = self.edge_skill.best_edge(fair, market)
        if self.staleness_threshold > 0 and edge.edge < self.staleness_threshold:
            return None

        if not self.risk_gate.passes(edge, market, self.risk_config):
            return None

        size_usdc = self.sizer.size(edge, self.sizing_config)
        if size_usdc <= 0:
            return None

        return {
            "direction": edge.direction,
            "entry_price": edge.market_price,
            "market_price": edge.market_price,
            "size_usdc": min(size_usdc, self.max_notional),
            "edge": edge.edge,
            "fair": edge.fair,
            "z_score": 0.0,
            "entry_age_seconds": 0.0,
            "regime": BacktestSimulator.classify_regime(edge.market_price),
        }


class V2MomentumBacktestRunner:
    """Offline equivalent of strategies.v2.momentum.Momentum."""

    name = "momentum"

    def __init__(
        self,
        bankroll: float,
        min_edge: float = 0.14,
        kelly_frac: float = 0.20,
        max_bet_pct: float = 0.025,
        max_notional: float = 500.0,
        max_price: float = 0.55,
        min_price: float = 0.40,
        momentum_window: int = 20,
        min_mom_vol_ratio: float = 0.8,
        min_entry_age: float = 20.0,
        entry_deadline: float = 60.0,
        min_abs_z: float = 0.15,
        min_distance_bps: float = 2.0,
        down_edge_boost: float = 0.08,
        down_min_abs_z: float = 0.35,
        fair_cap: float = 0.80,
        confirmations_required: int = 2,
        disable_trending: bool = True,
    ):
        self.bankroll = bankroll
        self.min_edge = min_edge
        self.kelly_frac = kelly_frac
        self.max_bet_pct = max_bet_pct
        self.max_notional = max_notional
        self.max_price = max_price
        self.min_price = min_price
        self.momentum_window = momentum_window
        self.min_mom_vol_ratio = min_mom_vol_ratio
        self.min_entry_age = min_entry_age
        self.entry_deadline = entry_deadline
        self.min_abs_z = min_abs_z
        self.min_distance_bps = min_distance_bps
        self.down_edge_boost = down_edge_boost
        self.down_min_abs_z = down_min_abs_z
        self.fair_cap = fair_cap
        self.confirmations_required = max(1, confirmations_required)
        self.disable_trending = disable_trending

    def evaluate_window(
        self,
        window_idx: int,
        window: dict,
        history_before_window: List[float],
        simulator: "BacktestSimulator",
    ) -> Optional[dict]:
        prices = window["prices"]
        if len(prices) < 2:
            return None

        pending_key = None
        pending_count = 0
        total_seconds = 300.0
        sample_interval = total_seconds / max(len(prices), 1)

        for entry_idx in range(1, len(prices)):
            entry_age = entry_idx * sample_interval
            seconds_remaining = max(total_seconds - entry_age, 0.0)
            if entry_age < self.min_entry_age or seconds_remaining < self.entry_deadline:
                continue

            eval_prices = history_before_window + prices[: entry_idx + 1]
            decision = self._evaluate_candidate(
                eval_prices=eval_prices,
                strike=window["strike"],
                seconds_remaining=seconds_remaining,
                entry_age=entry_age,
                window_idx=window_idx,
                simulator=simulator,
            )

            if decision is None:
                pending_key = None
                pending_count = 0
                continue

            key = (decision["direction"], decision["token_id"])
            if key == pending_key:
                pending_count += 1
            else:
                pending_key = key
                pending_count = 1

            if pending_count >= self.confirmations_required:
                decision["confirmations"] = pending_count
                return decision

        return None

    def _evaluate_candidate(
        self,
        eval_prices: List[float],
        strike: float,
        seconds_remaining: float,
        entry_age: float,
        window_idx: int,
        simulator: "BacktestSimulator",
    ) -> Optional[dict]:
        if len(eval_prices) < self.momentum_window + 1:
            return None

        current = eval_prices[-1]
        if current <= 0 or strike <= 0:
            return None

        recent = eval_prices[-self.momentum_window :]
        returns = [
            (recent[i] - recent[i - 1]) / recent[i - 1]
            for i in range(1, len(recent))
            if recent[i - 1] > 0
        ]
        vol = max(self._std(returns), 0.00002)
        momentum = (eval_prices[-1] - eval_prices[-self.momentum_window]) / eval_prices[-self.momentum_window]
        mom_vol_ratio = abs(momentum) / vol if vol > 0 else 0.0

        sample_interval = 60.0  # backtests generally use 1m candles
        horizon_ticks = max(seconds_remaining / sample_interval, 1.0)
        horizon_sigma = current * vol * math.sqrt(horizon_ticks)
        distance = current - strike
        z_score = distance / horizon_sigma if horizon_sigma > 0 else 0.0
        distance_bps = abs(distance) / current * 10_000

        if (
            mom_vol_ratio < self.min_mom_vol_ratio
            or abs(z_score) < self.min_abs_z
            or distance_bps < self.min_distance_bps
        ):
            return None

        mom_z = max(-2.0, min(2.0, momentum / vol if vol > 0 else 0.0))
        adjusted_z = z_score + 0.15 * mom_z
        fair_prob_up = 0.5 * (1.0 + math.erf(adjusted_z / math.sqrt(2.0)))
        fair_prob_up = max(1.0 - self.fair_cap, min(self.fair_cap, fair_prob_up))

        up_ask, down_ask = simulator.simulate_asks(strike, current, window_idx, entry_age)
        if fair_prob_up > 0.52:
            direction = "UP"
            fair = fair_prob_up
            market_price = up_ask
            token_id = f"up_{window_idx}"
        elif fair_prob_up < 0.48:
            direction = "DOWN"
            fair = 1.0 - fair_prob_up
            market_price = down_ask
            token_id = f"down_{window_idx}"
        else:
            return None

        if direction == "DOWN" and abs(z_score) < self.down_min_abs_z:
            return None
        if market_price > self.max_price or market_price < self.min_price:
            return None

        required_edge = self.min_edge + (self.down_edge_boost if direction == "DOWN" else 0.0)
        if market_price >= 0.50:
            required_edge = max(required_edge, 0.18)

        edge = fair - market_price
        regime = simulator.classify_regime(market_price)
        if self.disable_trending and regime == "trending" and not (edge >= 0.20 and market_price <= 0.52):
            return None
        if edge < required_edge:
            return None

        vwap = simulator.estimate_vwap(market_price)
        if vwap > self.max_price or vwap < self.min_price:
            return None
        edge = fair - vwap
        if edge < required_edge:
            return None

        # Binary Kelly. This mirrors the live strategy's sizing intent while
        # keeping the backtest independent from live network-bound state.
        full_kelly = (fair - vwap) / (1.0 - vwap) if vwap < 1.0 else 0.0
        size_usdc = self.bankroll * min(max(full_kelly * self.kelly_frac, 0.0), self.max_bet_pct)
        size_usdc = min(size_usdc, self.max_notional)
        if size_usdc < 5:
            return None

        return {
            "direction": direction,
            "token_id": token_id,
            "entry_price": vwap,
            "market_price": market_price,
            "size_usdc": size_usdc,
            "edge": edge,
            "fair": fair,
            "z_score": z_score,
            "entry_age_seconds": entry_age,
            "regime": regime,
        }

    @staticmethod
    def _std(xs: List[float]) -> float:
        if len(xs) < 2:
            return 0.0
        mean = sum(xs) / len(xs)
        return (sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


class BacktestSimulator:
    """
    Replays historical BTC 5-minute windows through strategies.
    """

    def __init__(
        self,
        bankroll: float = 10_000,
        fee_rate: float = 0.07,
        slippage_bps: float = 200,
        spread_bps: float = 500,
        seed: int = 42,
    ):
        self.bankroll = bankroll
        self.fee_rate_constant = fee_rate
        self.slippage_rate = slippage_bps / 10_000
        self.spread_rate = spread_bps / 10_000
        self.rng = random.Random(seed)
        self.strategies: List[object] = []
        self.result = BacktestResult()

    def add_strategy(self, name: str, **kwargs):
        features = PriceFeatureSkill(lookback_ticks=5, momentum_ticks=20)
        edge_skill = EdgeSkill()
        sizer = PositionSizerSkill()

        if name == "momentum":
            runner = V2MomentumBacktestRunner(
                bankroll=self.bankroll,
                min_edge=kwargs.get("min_edge", 0.14),
                kelly_frac=kwargs.get("kelly_frac", 0.20),
                max_bet_pct=kwargs.get("max_bet_pct", 0.025),
                max_notional=kwargs.get("max_notional", 500),
                max_price=kwargs.get("max_price", 0.55),
                min_price=kwargs.get("min_price", 0.40),
                down_edge_boost=kwargs.get("down_edge_boost", 0.08),
                fair_cap=kwargs.get("fair_cap", 0.80),
                confirmations_required=kwargs.get("confirmations_required", 2),
                disable_trending=kwargs.get("disable_trending", True),
            )
        elif name == "momentum_legacy":
            runner = LegacyStrategyRunner(
                name="momentum_legacy",
                features=features,
                fair_value_skill=MomentumFairValueSkill(),
                edge_skill=edge_skill,
                risk_gate=RiskGateSkill(),
                risk_config=RiskGateConfig(
                    min_edge=kwargs.get("min_edge", 0.03),
                    max_price=kwargs.get("max_price", 0.65),
                    min_price=kwargs.get("min_price", 0.05),
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
            runner = LegacyStrategyRunner(
                name="oracle",
                features=features,
                fair_value_skill=OracleFairValueSkill(scale=50.0, max_shift=0.35),  # aligned with v2: bps/50
                edge_skill=edge_skill,
                risk_gate=RiskGateSkill(),
                risk_config=RiskGateConfig(
                    min_edge=kwargs.get("staleness_threshold", 0.15),   # v2 default
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
                move_threshold_bps=kwargs.get("move_threshold_bps", 6.0),    # v2 default
                staleness_threshold=kwargs.get("staleness_threshold", 0.15), # v2 default
                max_notional=kwargs.get("max_notional", 500),
            )
        else:
            raise ValueError(f"Unknown strategy: {name}")

        self.strategies.append(runner)
        log.info("Backtest: added strategy '%s'", name)

    def run(self, windows: List[dict]) -> BacktestResult:
        self.result = BacktestResult()
        equity = self.bankroll
        peak = self.bankroll
        price_history: List[float] = []

        for i, window in enumerate(windows):
            history_before_window = list(price_history)

            for strategy in self.strategies:
                if isinstance(strategy, V2MomentumBacktestRunner):
                    decision = strategy.evaluate_window(i, window, history_before_window, self)
                else:
                    decision = self._evaluate_legacy(strategy, i, window, history_before_window)

                if decision is None:
                    continue

                trade = self._settle_decision(i, strategy.name, decision, window["outcome"])
                self.result.trades.append(trade)

                equity += trade.pnl
                peak = max(peak, equity)
                self.result.max_drawdown = max(self.result.max_drawdown, peak - equity)
                if trade.won:
                    self.result.wins += 1
                else:
                    self.result.losses += 1

            price_history.extend(window["prices"])
            if len(price_history) > 500:
                price_history = price_history[-300:]
            self.result.equity_curve.append(equity)

        self.result.total_pnl = equity - self.bankroll
        self.result.peak_equity = peak
        log.info("Backtest complete: %d windows, %d trades", len(windows), len(self.result.trades))
        return self.result

    def _evaluate_legacy(
        self,
        strategy: LegacyStrategyRunner,
        window_idx: int,
        window: dict,
        history_before_window: List[float],
    ) -> Optional[dict]:
        prices = window["prices"]
        entry_idx = max(1, len(prices) // 3)
        if entry_idx >= len(prices):
            return None
        current = prices[entry_idx]
        up_ask, down_ask = self.simulate_asks(window["strike"], current, window_idx, entry_idx * 60.0)
        seconds_remaining = 300.0 * (1.0 - entry_idx / max(len(prices), 1))
        market = RollingMarket(
            strike_price=window["strike"],
            seconds_remaining=seconds_remaining,
            up_price=up_ask,
            down_price=down_ask,
            up_token_id=f"up_{window_idx}",
            down_token_id=f"down_{window_idx}",
        )
        eval_prices = history_before_window + prices[: entry_idx + 1]
        return strategy.evaluate(eval_prices, market)

    def _settle_decision(
        self,
        window_idx: int,
        strategy_name: str,
        decision: dict,
        outcome: str,
    ) -> BacktestTrade:
        signal_price = decision["entry_price"]
        slippage = signal_price * self.slippage_rate * self.rng.uniform(0.5, 1.5)
        fill_price = min(0.95, signal_price + slippage)
        size_usdc = decision["size_usdc"]

        shares = size_usdc / fill_price if fill_price > 0 else 0.0
        fee_per_share = self.fee_rate_constant * fill_price * (1.0 - fill_price)
        fees = shares * fee_per_share

        if decision["direction"] == outcome:
            pnl = shares * (1.0 - fill_price) - fees
            won = pnl > 0
        else:
            pnl = -(size_usdc + fees)
            won = False

        return BacktestTrade(
            window_idx=window_idx,
            strategy=strategy_name,
            direction=decision["direction"],
            entry_price=fill_price,
            size_usdc=size_usdc,
            edge=decision["edge"],
            fair=decision["fair"],
            outcome=outcome,
            pnl=pnl,
            won=won,
            entry_age_seconds=decision.get("entry_age_seconds", 0.0),
            z_score=decision.get("z_score", 0.0),
            market_price=decision.get("market_price", signal_price),
            fee=fees,
            slippage=slippage,
            regime=decision.get("regime", self.classify_regime(decision.get("market_price", signal_price))),
        )

    def simulate_asks(self, strike: float, current: float, window_idx: int, entry_age: float) -> tuple[float, float]:
        distance_pct = (current - strike) / strike if strike > 0 else 0.0
        # Stable per candidate so confirmation checks are not just noise flips.
        rng = random.Random((window_idx + 1) * 1_000_003 + int(entry_age))
        noise = rng.gauss(0, 0.05)
        raw_prob = 0.50 + distance_pct * 30
        simulated_up_price = max(0.10, min(0.90, raw_prob + noise))
        simulated_down_price = 1.0 - simulated_up_price
        half_spread = self.spread_rate / 2
        return min(0.95, simulated_up_price + half_spread), min(0.95, simulated_down_price + half_spread)

    def estimate_vwap(self, ask_price: float) -> float:
        return min(0.95, ask_price * (1.0 + self.slippage_rate * 0.5))

    @staticmethod
    def classify_regime(price: float) -> str:
        if price <= 0.15 or price >= 0.85:
            return "tail"
        if 0.35 <= price <= 0.65:
            return "contested"
        return "trending"

    def print_report(self):
        r = self.result
        trades = r.trades
        total = r.wins + r.losses

        print()
        print("=" * 60)
        print("BACKTEST REPORT")
        print("=" * 60)
        print(f"  Windows processed: {len(r.equity_curve)}")
        print(f"  Total trades:      {total}")
        print(f"  Trade rate:        {total / max(len(r.equity_curve), 1) * 100:.1f}%")
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
            req_wr = abs(avg_loss) / (avg_win + abs(avg_loss)) * 100 if avg_win > 0 else 100
            print(f"  Avg win:           ${avg_win:.2f}")
            print(f"  Avg loss:          ${avg_loss:.2f}")
            print(f"  Required WR:       {req_wr:.1f}%")

        self._print_breakdown("Per-Strategy", trades, lambda t: t.strategy)
        self._print_breakdown("Direction", trades, lambda t: t.direction)
        self._print_breakdown("Regime", trades, lambda t: t.regime)
        self._print_breakdown("Market price bucket", trades, lambda t: self._price_bucket(t.market_price))
        self._print_breakdown("Fill price bucket", trades, lambda t: self._price_bucket(t.entry_price))
        self._print_breakdown("Edge bucket", trades, lambda t: self._edge_bucket(t.edge))
        self._print_breakdown("Entry age bucket", trades, lambda t: self._age_bucket(t.entry_age_seconds))
        print("=" * 60)

    def _print_breakdown(self, title: str, trades: List[BacktestTrade], key_fn):
        buckets = {}
        for t in trades:
            buckets.setdefault(key_fn(t), []).append(t)
        if not buckets:
            return
        print()
        print(f"  {title}:")
        for key in sorted(buckets):
            group = buckets[key]
            wins = sum(1 for t in group if t.won)
            pnl = sum(t.pnl for t in group)
            wr = wins / len(group) * 100
            print(f"    {str(key):15s}: {len(group):4d} trades, {wr:5.1f}% WR, ${pnl:9.2f}")

    @staticmethod
    def _price_bucket(price: float) -> str:
        if price < 0.40:
            return "<0.40"
        if price < 0.45:
            return "0.40-0.45"
        if price < 0.50:
            return "0.45-0.50"
        if price <= 0.55:
            return "0.50-0.55"
        return ">0.55"

    @staticmethod
    def _edge_bucket(edge: float) -> str:
        if edge < 0.14:
            return "<0.14"
        if edge < 0.18:
            return "0.14-0.18"
        if edge < 0.25:
            return "0.18-0.25"
        return ">=0.25"

    @staticmethod
    def _age_bucket(age: float) -> str:
        if age < 60:
            return "<60s"
        if age < 120:
            return "60-120s"
        if age < 180:
            return "120-180s"
        return ">=180s"
