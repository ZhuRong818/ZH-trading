"""
Backtest Runner — CLI for running backtests.

Usage:
    python -m backtest.run --strategy momentum --days 7
    python -m backtest.run --strategy momentum_legacy --days 7
    python -m backtest.run --strategy oracle --days 30
    python -m backtest.run --strategy momentum,oracle --days 14
    python -m backtest.run --strategy oracle --days 7 --move-bps 4 --staleness 0.15
"""

import argparse
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.data_loader import BinanceDataLoader
from backtest.simulator import BacktestSimulator
from config import SystemConfig


def main():
    parser = argparse.ArgumentParser(description="Backtest BTC 5-minute strategies")
    defaults = SystemConfig()

    parser.add_argument("--strategy", type=str, default="momentum,oracle",
                        help="Strategy: momentum, momentum_legacy, oracle, or comma-separated mix")
    parser.add_argument("--days", type=int, default=7,
                        help="Days of historical data (default: 7)")
    parser.add_argument("--bankroll", type=float, default=10_000,
                        help="Starting bankroll (default: 10000)")
    parser.add_argument("--interval", type=str, default="1m",
                        help="Kline interval: 1m, 5m (default: 1m)")

    # Strategy params
    parser.add_argument("--min-edge", type=float, default=0.03,
                        help="Momentum min edge (default: 0.03)")
    parser.add_argument("--move-bps", type=float, default=2.0,
                        help="Oracle move threshold bps (default: 2.0)")
    parser.add_argument("--staleness", type=float, default=0.01,
                        help="Oracle staleness threshold (default: 0.01)")
    parser.add_argument("--max-notional", type=float, default=500,
                        help="Max notional per trade (default: 500)")
    parser.add_argument("--max-price", type=float, default=defaults.btc5m_max_price,
                        help=f"Max entry price (default: {defaults.btc5m_max_price})")
    parser.add_argument("--min-price", type=float, default=defaults.btc5m_min_price,
                        help=f"Min entry price (default: {defaults.btc5m_min_price})")
    parser.add_argument("--down-edge-boost", type=float, default=defaults.btc5m_down_edge_boost,
                        help=f"Extra DOWN min edge (default: {defaults.btc5m_down_edge_boost})")
    parser.add_argument("--fair-cap", type=float, default=0.80,
                        help="Fair probability cap for V2 momentum (default: 0.80)")
    parser.add_argument("--confirmations", type=int, default=2,
                        help="Consecutive same-side confirmations for V2 momentum (default: 2)")
    parser.add_argument("--disable-trending", action=argparse.BooleanOptionalAction, default=True,
                        help="Filter trending regime unless edge/price are exceptional (default: enabled)")

    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # Load data
    print(f"Loading {args.days} days of {args.interval} BTC data from Binance...")
    loader = BinanceDataLoader()
    klines = loader.load_klines("BTCUSDT", args.interval, days=args.days)
    windows = loader.klines_to_windows(klines)
    print(f"Created {len(windows)} 5-minute windows")

    # Setup simulator
    sim = BacktestSimulator(bankroll=args.bankroll)

    strategies = [s.strip() for s in args.strategy.split(",")]
    for name in strategies:
        if name == "momentum":
            sim.add_strategy(
                "momentum",
                min_edge=args.min_edge,
                max_price=args.max_price,
                min_price=args.min_price,
                down_edge_boost=args.down_edge_boost,
                fair_cap=args.fair_cap,
                confirmations_required=args.confirmations,
                disable_trending=args.disable_trending,
                max_notional=args.max_notional,
            )
        elif name == "momentum_legacy":
            sim.add_strategy(
                "momentum_legacy",
                min_edge=args.min_edge,
                max_price=args.max_price,
                min_price=args.min_price,
                max_notional=args.max_notional,
            )
        elif name == "oracle":
            sim.add_strategy("oracle",
                             move_threshold_bps=args.move_bps,
                             staleness_threshold=args.staleness,
                             max_price=args.max_price,
                             min_price=args.min_price,
                             max_notional=args.max_notional)
        else:
            print(f"Unknown strategy: {name}")
            sys.exit(1)

    # Run
    print(f"Running backtest with {len(strategies)} strategy(ies)...")
    sim.run(windows)
    sim.print_report()


if __name__ == "__main__":
    main()
