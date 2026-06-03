"""
External crypto price feeds from findata.

Used by live/paper rolling strategies for BTC, ETH, SOL, XRP spot prices.
Backtest loaders and settlement-oracle historical data are intentionally kept
separate from this live decision feed.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import requests

FINDATA_BASE_URL = os.environ.get("FINDATA_BASE_URL", "https://kv.run:5000").rstrip("/")
FINDATA_QUOTES_URL = f"{FINDATA_BASE_URL}/quotes"
FINDATA_CACHE_TTL_SECONDS = float(os.environ.get("FINDATA_CACHE_TTL_SECONDS", "0.45"))
FINDATA_MAX_CACHE_AGE_SECONDS = float(os.environ.get("FINDATA_MAX_CACHE_AGE_SECONDS", "2.0"))

_ASSET_TO_FINDATA_SYMBOL = {
    "btc": "BTCUSD",
    "eth": "ETHUSD",
    "sol": "SOLUSD",
    "xrp": "XRPUSD",
}
_SESSION = requests.Session()
_CACHE: dict[str, dict[str, float]] = {}
_LOCK = threading.Lock()


def _load_dotenv_token() -> str:
    token = os.environ.get("KVRUN_BEARER_TOKEN", "").strip()
    if token:
        return token

    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        return ""

    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "KVRUN_BEARER_TOKEN":
                token = value.strip().strip('"').strip("'")
                if token:
                    os.environ["KVRUN_BEARER_TOKEN"] = token
                    return token
    except OSError:
        return ""
    return ""


def findata_symbol(asset_or_symbol: str) -> str:
    value = (asset_or_symbol or "").strip().upper().replace("-", "")
    if not value:
        raise ValueError("asset_or_symbol is required")

    lower = value.lower()
    if lower in _ASSET_TO_FINDATA_SYMBOL:
        return _ASSET_TO_FINDATA_SYMBOL[lower]
    if value.endswith("USDT"):
        return f"{value[:-4]}USD"
    if value.endswith("USD"):
        return value
    return _ASSET_TO_FINDATA_SYMBOL.get(lower, f"{value}USD")


def _headers() -> dict[str, str]:
    token = _load_dotenv_token()
    if not token:
        raise RuntimeError("KVRUN_BEARER_TOKEN is required for findata quotes")
    return {"Authorization": f"Bearer {token}"}


def _extract_quote(payload: Any, symbol: str) -> dict[str, Any]:
    if isinstance(payload, list):
        for item in payload:
            if str(item.get("symbol", "")).upper() == symbol:
                return item
        raise KeyError(f"findata quote missing for {symbol}")
    if isinstance(payload, dict):
        if str(payload.get("symbol", "")).upper() == symbol:
            return payload
        for key in ("quotes", "data", "results"):
            items = payload.get(key)
            if isinstance(items, list):
                return _extract_quote(items, symbol)
    raise ValueError("unexpected findata quote response")


def get_asset_price(asset_or_symbol: str) -> float:
    symbol = findata_symbol(asset_or_symbol)
    now = time.time()

    with _LOCK:
        cached = _CACHE.get(symbol)
        if cached and now - cached["fetched_at"] < FINDATA_CACHE_TTL_SECONDS:
            return cached["price"]

    resp = _SESSION.get(
        FINDATA_QUOTES_URL,
        params={"symbols": symbol},
        headers=_headers(),
        timeout=3,
    )
    resp.raise_for_status()
    quote = _extract_quote(resp.json(), symbol)
    price = float(quote.get("price") or 0.0)
    if price <= 0:
        raise ValueError(f"findata returned invalid price for {symbol}: {price}")
    if quote.get("stale") is True:
        raise ValueError(f"findata returned stale price for {symbol}")

    with _LOCK:
        _CACHE[symbol] = {"price": price, "fetched_at": now}
    return price


def get_asset_price_cached(asset_or_symbol: str) -> float:
    symbol = findata_symbol(asset_or_symbol)
    try:
        return get_asset_price(symbol)
    except Exception:
        with _LOCK:
            cached = _CACHE.get(symbol)
            if cached and time.time() - cached["fetched_at"] <= FINDATA_MAX_CACHE_AGE_SECONDS:
                return cached["price"]
        raise


def get_btc_price() -> float:
    return get_asset_price_cached("btc")


def get_eth_price() -> float:
    return get_asset_price_cached("eth")


def get_sol_price() -> float:
    return get_asset_price_cached("sol")


def get_xrp_price() -> float:
    return get_asset_price_cached("xrp")
