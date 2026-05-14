"""
External price feeds — Binance ticker for BTC, ETH, SOL, XRP, etc.
Used by RollingProvider and Momentum strategy.
"""

import requests

BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"


def get_btc_price() -> float:
    resp = requests.get(BINANCE_TICKER, params={"symbol": "BTCUSDT"}, timeout=5)
    return float(resp.json()["price"])


def get_eth_price() -> float:
    resp = requests.get(BINANCE_TICKER, params={"symbol": "ETHUSDT"}, timeout=5)
    return float(resp.json()["price"])


def get_sol_price() -> float:
    resp = requests.get(BINANCE_TICKER, params={"symbol": "SOLUSDT"}, timeout=5)
    return float(resp.json()["price"])


def get_xrp_price() -> float:
    resp = requests.get(BINANCE_TICKER, params={"symbol": "XRPUSDT"}, timeout=5)
    return float(resp.json()["price"])
