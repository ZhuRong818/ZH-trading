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
class RiskConfig:
    # Portfolio level
    max_total_exposure_usdc: float = 100_000
    max_drawdown_pct: float = 20.0

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
    heartbeat_interval: float = 5.0

    risk: RiskConfig = field(default_factory=RiskConfig)
    market_making: MarketMakingConfig = field(default_factory=MarketMakingConfig)
    whale_tracking: WhaleTrackingConfig = field(default_factory=WhaleTrackingConfig)

    @classmethod
    def from_env(cls):
        return cls(
            private_key=os.environ.get("POLYMARKET_PRIVATE_KEY", ""),
            funder=os.environ.get("POLYMARKET_FUNDER", ""),
            sig_type=int(os.environ.get("POLYMARKET_SIG_TYPE", "1")),
        )
