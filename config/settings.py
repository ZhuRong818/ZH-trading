"""
Central configuration — all modules import from here.
Maps to risk_config.yaml from the design doc (Module 6).
"""

import os
from dataclasses import dataclass, field

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"
CHAIN_ID = 137

CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"


@dataclass
class FeeConfig:
    maker_fee_bps: float = 0.0          # Polymarket maker fee (always 0)
    # Taker fee uses parabolic formula: fee = shares × feeRate × p × (1-p)
    # feeRate varies by category:
    crypto_fee_rate: float = 0.07       # crypto markets (max $1.75 per 100 shares at p=0.50)
    politics_fee_rate: float = 0.04     # politics/tech/finance
    sports_fee_rate: float = 0.03       # sports
    geopolitics_fee_rate: float = 0.0   # geopolitics (free)


@dataclass
class CapitalConfig:
    total_capital_usdc: float = 100_000
    reserve_pct: float = 0.20           # keep 20% as reserve
    max_per_market_pct: float = 0.10    # max 10% in any single market
    max_per_strategy_pct: float = 0.40  # max 40% for any one strategy
    strategy_budgets: dict = field(default_factory=lambda: {
        "stoikov_mm": 0.30,
        "whale_copy": 0.15,
        "arb": 0.20,
        "mean_rev": 0.15,
        "fade": 0.10,
        "btc5m": 0.10,
    })


@dataclass
class RiskConfig:
    # Portfolio level
    max_total_exposure_usdc: float = 100_000
    max_drawdown_pct: float = 1.0

    # Per market
    max_position_size_usdc: float = 10_000
    max_concentration_pct: float = 5.0
    stop_loss_pct: float = 15.0

    # Per strategy
    mm_max_inventory_imbalance_usdc: float = 5_000
    arb_max_leg_exposure_usdc: float = 20_000
    sentiment_max_signal_size_usdc: float = 5_000
    whale_max_copy_size_usdc: float = 2_000

    # Circuit breakers
    volatility_pause_threshold: float = 0.15
    min_incentive_size_usdc: float = 50.0


@dataclass
class MarketMakingConfig:
    gamma: float = 0.5           # risk aversion (Stoikov)
    spread_k: float = 1.5       # spread scaling factor
    volatility_window: int = 60  # minutes for rolling sigma
    num_levels: int = 3
    order_size: float = 20.0
    min_price_delta_to_requote: float = 0.003
    refresh_interval: float = 5.0


@dataclass
class WhaleTrackingConfig:
    whale_threshold_usdc: float = 10_000
    high_confidence_win_rate: float = 0.80
    copy_fraction: float = 0.15       # copy 15% of whale's position
    max_copy_size_usdc: float = 2_000
    poll_interval: float = 10.0       # seconds between leaderboard checks
    top_n_traders: int = 20
    require_llm_confirmation: bool = False
    min_trades: int = 20
    min_win_rate: float = 0.70


@dataclass
class SystemConfig:
    private_key: str = ""
    funder: str = ""
    sig_type: int = 1
    dry_run: bool = True
    no_learn: bool = False
    heartbeat_interval: float = 5.0
    btc5m_min_edge: float = 0.16
    btc5m_max_price: float = 0.55
    btc5m_min_price: float = 0.40
    btc5m_min_entry_age: float = 60.0
    btc5m_entry_deadline: float = 180.0
    btc5m_min_abs_z: float = 0.15
    btc5m_down_min_abs_z: float = 0.45
    btc5m_min_mom_vol_ratio: float = 0.8
    btc5m_fair_cap: float = 0.80
    btc5m_confirmations_required: int = 2
    btc5m_max_vwap_slippage: float = 0.015
    btc5m_down_edge_boost: float = 0.10

    btc5m_snipe_max_seconds: float = 30.0
    btc5m_snipe_min_seconds: float = 12.0
    btc5m_snipe_min_distance_usd: float = 25.0
    btc5m_snipe_min_distance_bps: float = 0.0
    btc5m_snipe_min_market_odds: float = 0.98
    btc5m_snipe_min_edge: float = 0.005
    btc5m_snipe_min_fair: float = 0.99
    btc5m_snipe_soft_max_seconds: float = 60.0
    btc5m_snipe_soft_min_distance_usd: float = 50.0
    btc5m_snipe_soft_min_distance_bps: float = 0.0
    btc5m_snipe_soft_min_market_odds: float = 0.90
    btc5m_snipe_soft_min_edge: float = 0.02
    btc5m_snipe_soft_min_fair: float = 0.95
    btc5m_snipe_kelly_frac: float = 0.10
    btc5m_snipe_max_bet_pct: float = 0.01
    btc5m_snipe_max_notional_usdc: float = 250.0
    btc5m_snipe_max_vwap_slippage: float = 0.01
    btc5m_snipe_cooldown: float = 5.0

    eth5m_snipe_min_seconds: float = 10.0
    eth5m_snipe_min_distance_usd: float = 1.0
    eth5m_snipe_soft_min_distance_usd: float = 0.67
    eth5m_snipe_min_market_odds: float = 0.97
    eth5m_snipe_soft_min_market_odds: float = 0.88
    eth5m_snipe_max_notional_usdc: float = 200.0

    sol5m_snipe_min_seconds: float = 8.0
    sol5m_snipe_min_distance_usd: float = 0.5
    sol5m_snipe_soft_min_distance_usd: float = 0.2
    sol5m_snipe_min_market_odds: float = 0.95
    sol5m_snipe_soft_min_market_odds: float = 0.85
    sol5m_snipe_max_notional_usdc: float = 100.0

    xrp5m_snipe_min_seconds: float = 6.0
    xrp5m_snipe_min_distance_usd: float = 0.001
    xrp5m_snipe_soft_min_distance_usd: float = 0.0005
    xrp5m_snipe_min_market_odds: float = 0.95
    xrp5m_snipe_soft_min_market_odds: float = 0.85
    xrp5m_snipe_max_notional_usdc: float = 50.0

    risk: RiskConfig = field(default_factory=RiskConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    capital: CapitalConfig = field(default_factory=CapitalConfig)
    market_making: MarketMakingConfig = field(default_factory=MarketMakingConfig)
    whale_tracking: WhaleTrackingConfig = field(default_factory=WhaleTrackingConfig)

    @classmethod
    def from_env(cls):
        return cls(
            private_key=os.environ.get("POLYMARKET_PRIVATE_KEY", ""),
            funder=os.environ.get("POLYMARKET_FUNDER", ""),
            sig_type=int(os.environ.get("POLYMARKET_SIG_TYPE", "1")),
        )
