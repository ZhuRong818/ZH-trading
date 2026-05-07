"""
Module 4 — Execution Management System (EMS) & Smart Order Routing

Receives signals from strategy modules, applies SOR logic,
manages order types (GTC/FOK/FAK), and enforces rate limits.
"""

import hashlib
import hmac
import json
import logging
import os
import time
from base64 import b64decode, b64encode
from typing import Dict, List, Optional

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data

from config import CLOB_BASE, CHAIN_ID, CTF_EXCHANGE, NEG_RISK_CTF_EXCHANGE
from oms.position_manager import Fill

log = logging.getLogger(__name__)


class ClobAuth:
    """EIP-712 + HMAC authentication for the CLOB API."""

    def __init__(self, private_key: str, chain_id: int = 137,
                 sig_type: int = 1, funder: str = ""):
        if private_key and not private_key.startswith("0x"):
            private_key = "0x" + private_key
        self.account = Account.from_key(private_key) if private_key else None
        self.address = self.account.address if self.account else ""
        self.chain_id = chain_id
        self.sig_type = sig_type
        self.funder = funder or self.address
        self.api_key = ""
        self.api_secret = ""
        self.api_passphrase = ""
        self.session = requests.Session()

    def _l1_headers(self) -> dict:
        timestamp = str(int(time.time()))
        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                ],
                "ClobAuth": [
                    {"name": "address", "type": "address"},
                    {"name": "timestamp", "type": "string"},
                    {"name": "nonce", "type": "uint256"},
                ],
            },
            "primaryType": "ClobAuth",
            "domain": {"name": "ClobAuthDomain", "version": "1", "chainId": self.chain_id},
            "message": {"address": self.address, "timestamp": timestamp, "nonce": 0},
        }
        signed = self.account.sign_message(encode_typed_data(full_message=typed_data))
        return {
            "POLY_ADDRESS": self.address,
            "POLY_SIGNATURE": signed.signature.hex(),
            "POLY_TIMESTAMP": timestamp,
            "POLY_NONCE": "0",
        }

    def _l2_headers(self, method: str, path: str, body: str = "") -> dict:
        timestamp = str(int(time.time()))
        msg = timestamp + method.upper() + path + body
        secret_bytes = b64decode(self.api_secret)
        sig = hmac.new(secret_bytes, msg.encode(), hashlib.sha256).digest()
        return {
            "POLY_ADDRESS": self.address,
            "POLY_SIGNATURE": b64encode(sig).decode(),
            "POLY_TIMESTAMP": timestamp,
            "POLY_API_KEY": self.api_key,
            "POLY_PASSPHRASE": self.api_passphrase,
        }

    def derive_api_creds(self):
        headers = self._l1_headers()
        resp = self.session.get(f"{CLOB_BASE}/auth/derive-api-key", headers=headers)
        if resp.status_code != 200:
            resp = self.session.post(f"{CLOB_BASE}/auth/api-key", headers=headers)
        resp.raise_for_status()
        data = resp.json()
        self.api_key = data["apiKey"]
        self.api_secret = data["secret"]
        self.api_passphrase = data["passphrase"]
        log.info("API credentials derived for %s", self.address[:10] + "...")

    def post(self, path: str, json_data: dict = None) -> dict:
        body = json.dumps(json_data) if json_data else ""
        headers = self._l2_headers("POST", path, body)
        headers["Content-Type"] = "application/json"
        resp = self.session.post(CLOB_BASE + path, headers=headers, data=body)
        resp.raise_for_status()
        return resp.json()

    def delete(self, path: str, json_data: dict = None) -> dict:
        body = json.dumps(json_data) if json_data else ""
        headers = self._l2_headers("DELETE", path, body)
        headers["Content-Type"] = "application/json"
        resp = self.session.delete(CLOB_BASE + path, headers=headers, data=body)
        resp.raise_for_status()
        return resp.json()

    def sign_order(self, order: dict) -> str:
        exchange = NEG_RISK_CTF_EXCHANGE if order.get("neg_risk") else CTF_EXCHANGE
        message = {
            "salt": int(order["salt"]),
            "maker": self.funder,
            "signer": self.address,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": int(order["token_id"]),
            "makerAmount": int(order["maker_amount"]),
            "takerAmount": int(order["taker_amount"]),
            "expiration": int(order.get("expiration", 0)),
            "nonce": int(order["nonce"]),
            "feeRateBps": int(order.get("fee_rate_bps", 0)),
            "side": 0 if order["side"] == "BUY" else 1,
            "signatureType": self.sig_type,
        }
        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Order": [
                    {"name": "salt", "type": "uint256"},
                    {"name": "maker", "type": "address"},
                    {"name": "signer", "type": "address"},
                    {"name": "taker", "type": "address"},
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "makerAmount", "type": "uint256"},
                    {"name": "takerAmount", "type": "uint256"},
                    {"name": "expiration", "type": "uint256"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "feeRateBps", "type": "uint256"},
                    {"name": "side", "type": "uint8"},
                    {"name": "signatureType", "type": "uint8"},
                ],
            },
            "primaryType": "Order",
            "domain": {
                "name": "Polymarket CTF Exchange", "version": "1",
                "chainId": self.chain_id, "verifyingContract": exchange,
            },
            "message": message,
        }
        signed = self.account.sign_message(encode_typed_data(full_message=typed_data))
        return signed.signature.hex()


# ---------------------------------------------------------------------------
# Rate Limit Manager
# ---------------------------------------------------------------------------

class RateLimitManager:
    """Only requote if price moved enough to justify the API call."""

    def __init__(self, min_delta: float = 0.003, max_per_second: int = 50):
        self.min_delta = min_delta
        self.max_per_second = max_per_second
        self._last_prices: Dict[str, float] = {}
        self._call_times: List[float] = []

    def should_requote(self, token_id: str, new_price: float) -> bool:
        old = self._last_prices.get(token_id)
        if old is None:
            self._last_prices[token_id] = new_price
            return True
        if abs(new_price - old) >= self.min_delta:
            self._last_prices[token_id] = new_price
            return True
        return False

    def check_rate(self) -> bool:
        now = time.time()
        self._call_times = [t for t in self._call_times if now - t < 1.0]
        if len(self._call_times) >= self.max_per_second:
            return False
        self._call_times.append(now)
        return True


# ---------------------------------------------------------------------------
# Execution Management System
# ---------------------------------------------------------------------------

class ExecutionEngine:
    """
    Central EMS — all strategy modules submit orders through here.
    Handles order building, signing, rate limiting, and fill tracking.
    Uses DryRunSimulator for realistic paper trading fills.
    """

    def __init__(self, auth: Optional[ClobAuth] = None, dry_run: bool = True,
                 data_feed=None, capital_allocator=None):
        self.auth = auth
        self.dry_run = dry_run
        self.data = data_feed
        self.capital_allocator = capital_allocator
        self.rate_limiter = RateLimitManager()
        self.open_order_ids: List[str] = []
        self._fill_callbacks = []
        self._simulator = None

        if dry_run and data_feed:
            from ems.dry_run_sim import DryRunSimulator
            self._simulator = DryRunSimulator(data_feed)

    def on_fill(self, callback):
        """Register a callback for fill events."""
        self._fill_callbacks.append(callback)

    def _fire_fill(self, fill: Fill):
        """Notify all fill callbacks."""
        for cb in self._fill_callbacks:
            cb(fill)

    def check_pending_dry_run(self):
        """Process pending GTC orders in dry-run simulator. Call each loop iteration."""
        if self._simulator:
            for fill in self._simulator.check_pending():
                self._fire_fill(fill)

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        tick_size: str = "0.01",
        neg_risk: bool = False,
        order_type: str = "GTC",
        source: str = "",
    ) -> Optional[str]:
        """Place an order. Returns order ID or None."""
        tick = float(tick_size)
        price = round(round(price / tick) * tick, 4)
        price = max(tick, min(1.0 - tick, price))

        if not self.rate_limiter.check_rate():
            log.warning("Rate limited, skipping order")
            return None

        # Capital allocation check
        if self.capital_allocator:
            cost = size * price
            approved = self.capital_allocator.request_capital(source, token_id, cost)
            if approved <= 0:
                log.info("Capital rejected for %s %s $%.0f", source, side, cost)
                return None
            # Reduce size if capital was reduced
            if approved < cost:
                size = approved / price if price > 0 else 0

        # Depth check
        if self.data:
            book = self.data.get_book(token_id)
            if book and not book.has_sufficient_depth(side, size):
                log.warning("Insufficient depth: %s %s %.1f", side, token_id[:16], size)
                return None

        if self.dry_run:
            if self._simulator:
                # Realistic simulation — VWAP fill, probabilistic
                fill = self._simulator.simulate_fill(
                    token_id, side, price, size, order_type, source,
                )
                if fill:
                    log.info("[SIM] %s %s %.1f @ %.4f [%s]",
                             source or "EMS", fill.side, fill.size, fill.price, order_type)
                    self._fire_fill(fill)
                    return fill.order_id
                else:
                    log.debug("[SIM] %s %s %.1f @ %.4f — pending/rejected [%s]",
                              source or "EMS", side, size, price, order_type)
                    return None
            else:
                # Fallback: instant fill (no data feed available)
                log.info("[DRY] %s %s %.1f @ %.4f [%s]", source or "EMS", side, size, price, order_type)
                fill = Fill(
                    token_id=token_id, side=side, size=size, price=price,
                    timestamp=time.time(), source=source,
                )
                self._fire_fill(fill)
                return f"dry_{side}_{price}_{time.time()}"

        try:
            scale = 10 ** 6
            if side == "BUY":
                maker_amount = int(size * price * scale)
                taker_amount = int(size * scale)
            else:
                maker_amount = int(size * scale)
                taker_amount = int(size * price * scale)

            salt = int.from_bytes(os.urandom(16), "big")
            order = {
                "salt": str(salt), "token_id": token_id,
                "maker_amount": str(maker_amount), "taker_amount": str(taker_amount),
                "side": side, "expiration": "0", "nonce": "0",
                "fee_rate_bps": "0", "neg_risk": neg_risk,
            }
            signature = self.auth.sign_order(order)

            payload = {
                "order": {
                    "salt": salt, "maker": self.auth.funder,
                    "signer": self.auth.address,
                    "taker": "0x0000000000000000000000000000000000000000",
                    "tokenId": token_id,
                    "makerAmount": str(maker_amount), "takerAmount": str(taker_amount),
                    "expiration": "0", "nonce": "0", "feeRateBps": "0",
                    "side": side, "signatureType": self.auth.sig_type,
                    "signature": signature,
                },
                "owner": self.auth.funder,
                "orderType": order_type,
            }
            resp = self.auth.post("/order", payload)

            if resp.get("success"):
                oid = resp["orderID"]
                self.open_order_ids.append(oid)
                log.info("PLACED %s %s %.1f @ %.4f id=%s [%s]",
                         source, side, size, price, oid[:16], order_type)
                return oid
            else:
                log.warning("Rejected: %s", resp.get("errorMsg", resp))
                return None
        except Exception as e:
            log.error("Order failed: %s", e)
            return None

    def cancel_all(self):
        if not self.open_order_ids:
            return
        if self.dry_run:
            log.info("[DRY] Cancel %d orders", len(self.open_order_ids))
        else:
            try:
                self.auth.delete("/cancel-all")
            except Exception as e:
                log.error("Cancel failed: %s", e)
        self.open_order_ids.clear()

    def cancel_order(self, order_id: str):
        if self.dry_run:
            log.info("[DRY] Cancel %s", order_id[:16])
        else:
            try:
                self.auth.delete("/order", {"orderID": order_id})
            except Exception as e:
                log.error("Cancel %s failed: %s", order_id[:16], e)
        if order_id in self.open_order_ids:
            self.open_order_ids.remove(order_id)


# ---------------------------------------------------------------------------
# Smart Order Router (Synthetic Equality)
# ---------------------------------------------------------------------------

class SyntheticEqualitySOR:
    """
    For every desired YES acquisition, evaluate:
    1. Direct:    Buy YES from ask side
    2. Synthetic: Buy NO from ask side (cheaper) -> will profit if YES wins
    Routes to the cheaper path.
    """

    def __init__(self, data_feed, ems: ExecutionEngine):
        self.data = data_feed
        self.ems = ems

    def route_buy(
        self,
        yes_token: str,
        no_token: str,
        size: float,
        tick_size: str = "0.01",
        neg_risk: bool = False,
        source: str = "SOR",
    ) -> Optional[str]:
        """Route a YES buy through the cheapest path."""
        yes_book = self.data.get_book(yes_token)
        no_book = self.data.get_book(no_token)

        if not yes_book or not no_book:
            # Fallback to direct
            if yes_book and yes_book.best_ask:
                return self.ems.place_order(
                    yes_token, "BUY", yes_book.best_ask, size,
                    tick_size=tick_size, neg_risk=neg_risk, source=source,
                )
            return None

        direct_cost = yes_book.best_ask if yes_book.best_ask else 1.0
        # Buying NO at no_ask means spending no_ask per share.
        # If YES wins, NO is worthless. If NO wins, we get $1.
        # Synthetic YES = 1 - no_ask (effective YES price via NO)
        synthetic_yes_price = 1.0 - no_book.best_ask if no_book.best_ask else 1.0

        if synthetic_yes_price < direct_cost - 0.005:
            # Synthetic path is cheaper — sell NO instead of buying YES
            log.info("SOR: synthetic path cheaper (%.4f vs %.4f direct)", synthetic_yes_price, direct_cost)
            return self.ems.place_order(
                no_token, "SELL", no_book.best_bid, size,
                tick_size=tick_size, neg_risk=neg_risk, source=source + "_synth",
            )
        else:
            return self.ems.place_order(
                yes_token, "BUY", direct_cost, size,
                tick_size=tick_size, neg_risk=neg_risk, source=source,
            )
