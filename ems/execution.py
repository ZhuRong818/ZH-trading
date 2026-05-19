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
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data

from config import CLOB_BASE, CHAIN_ID, CTF_EXCHANGE, NEG_RISK_CTF_EXCHANGE
from oms.position_manager import Fill

log = logging.getLogger(__name__)


@dataclass
class LiveOrderState:
    order_id: str
    token_id: str
    side: str
    price: float
    size: float
    source: str = ""
    edge: float = 0.0
    fair_value: float = 0.0
    direction: str = ""
    order_type: str = "GTC"
    filled_size: float = 0.0
    last_status: str = ""


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
        self.sdk_client = None

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
        env_key = os.environ.get("POLYMARKET_API_KEY") or os.environ.get("CLOB_API_KEY")
        env_secret = os.environ.get("POLYMARKET_API_SECRET") or os.environ.get("CLOB_API_SECRET")
        env_passphrase = (
            os.environ.get("POLYMARKET_API_PASSPHRASE")
            or os.environ.get("POLYMARKET_API_PASS_PHRASE")
            or os.environ.get("CLOB_API_PASSPHRASE")
            or os.environ.get("CLOB_API_PASS_PHRASE")
        )
        if env_key and env_secret and env_passphrase:
            self.api_key = env_key
            self.api_secret = env_secret
            self.api_passphrase = env_passphrase
            log.info("API credentials loaded from environment for %s", self.address[:10] + "...")
            return

        try:
            from py_clob_client_v2 import ClobClient, SignatureTypeV2

            signature_type = self._sdk_signature_type(SignatureTypeV2)
            client = ClobClient(
                host=CLOB_BASE,
                chain_id=self.chain_id,
                key=self.account.key.hex(),
                signature_type=signature_type,
                funder=self.funder,
            )
            creds = client.create_or_derive_api_key()
            self.api_key = creds.api_key
            self.api_secret = creds.api_secret
            self.api_passphrase = creds.api_passphrase
            client.set_api_creds(creds)
            self.sdk_client = client
            log.info("API credentials derived with py-clob-client-v2 for %s", self.address[:10] + "...")
            return
        except Exception as sdk_exc:
            log.warning("py-clob-client-v2 API credential derivation failed: %s", sdk_exc)

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

    def get(self, path: str, params: dict = None) -> dict:
        headers = self._l2_headers("GET", path, "")
        resp = self.session.get(CLOB_BASE + path, headers=headers, params=params or {})
        resp.raise_for_status()
        return resp.json()

    def delete(self, path: str, json_data: dict = None) -> dict:
        body = json.dumps(json_data) if json_data else ""
        headers = self._l2_headers("DELETE", path, body)
        headers["Content-Type"] = "application/json"
        resp = self.session.delete(CLOB_BASE + path, headers=headers, data=body)
        resp.raise_for_status()
        return resp.json()

    def get_balance_allowance(self, asset_type: str = "COLLATERAL", token_id: str = "") -> dict:
        if self.sdk_client is not None:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams, SignatureTypeV2

            sdk_asset_type = getattr(AssetType, asset_type, asset_type)
            params = {
                "asset_type": sdk_asset_type,
                "signature_type": self._sdk_signature_type(SignatureTypeV2),
            }
            if token_id:
                params["token_id"] = token_id
            data = self.sdk_client.get_balance_allowance(BalanceAllowanceParams(**params))
            return data if isinstance(data, dict) else getattr(data, "__dict__", {})

        params = {"asset_type": asset_type}
        if token_id:
            params["token_id"] = token_id
        return self.get("/balance-allowance", params=params)

    def update_balance_allowance(self, asset_type: str = "COLLATERAL", token_id: str = "") -> dict:
        if self.sdk_client is not None:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams, SignatureTypeV2

            sdk_asset_type = getattr(AssetType, asset_type, asset_type)
            params = {
                "asset_type": sdk_asset_type,
                "signature_type": self._sdk_signature_type(SignatureTypeV2),
            }
            if token_id:
                params["token_id"] = token_id
            return self.sdk_client.update_balance_allowance(BalanceAllowanceParams(**params))

        params = {"asset_type": asset_type, "signature_type": self.sig_type}
        if token_id:
            params["token_id"] = token_id
        return self.get("/balance-allowance/update", params=params)

    def get_open_orders(self) -> list:
        if self.sdk_client is not None:
            return self.sdk_client.get_open_orders()
        data = self.get("/orders")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("orders") or data.get("data") or []
        return []

    def _sdk_signature_type(self, signature_enum):
        names_by_type = {
            0: ("EOA",),
            1: ("POLY_PROXY",),
            2: ("POLY_GNOSIS_SAFE",),
            3: ("POLY_1271",),
        }
        for name in names_by_type.get(self.sig_type, ()):
            if hasattr(signature_enum, name):
                return getattr(signature_enum, name)
        return self.sig_type

    def get_order(self, order_id: str) -> dict:
        if self.sdk_client is not None:
            data = self.sdk_client.get_order(order_id)
            return data if isinstance(data, dict) else getattr(data, "__dict__", {})
        return self.get(f"/order/{order_id}")

    def get_trades(self) -> list:
        if self.sdk_client is not None:
            return self.sdk_client.get_trades()
        data = self.get("/trades")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("trades") or data.get("data") or []
        return []

    def cancel_all_orders(self) -> dict:
        if self.sdk_client is not None:
            data = self.sdk_client.cancel_all()
            return data if isinstance(data, dict) else getattr(data, "__dict__", {})
        return self.delete("/cancel-all")

    def cancel_order(self, order_id: str) -> dict:
        if self.sdk_client is not None:
            from py_clob_client_v2 import OrderPayload

            data = self.sdk_client.cancel_order(OrderPayload(orderID=order_id))
            return data if isinstance(data, dict) else getattr(data, "__dict__", {})
        return self.delete("/order", {"orderID": order_id})

    def post_heartbeat(self, heartbeat_id: str = "") -> dict:
        if self.sdk_client is not None:
            data = self.sdk_client.post_heartbeat(heartbeat_id)
            return data if isinstance(data, dict) else getattr(data, "__dict__", {})
        return self.post("/heartbeat", {"heartbeat_id": heartbeat_id})

    def ensure_sdk_client(self):
        """
        Optional v2 SDK path for deposit-wallet users (signatureType=3).
        The repo can run without the package installed; live connect fails with
        a clear message if this mode is requested but the SDK is unavailable.
        """
        if self.sdk_client is not None:
            return self.sdk_client
        try:
            from py_clob_client_v2 import ApiCreds, ClobClient, SignatureTypeV2
        except Exception as exc:
            raise RuntimeError(
                "Live trading requires the official py-clob-client-v2 package. "
                "Install it with: python -m pip install py-clob-client-v2"
            ) from exc

        creds = ApiCreds(
            api_key=self.api_key,
            api_secret=self.api_secret,
            api_passphrase=self.api_passphrase,
        )
        signature_type = self._sdk_signature_type(SignatureTypeV2)
        client = ClobClient(
            host=CLOB_BASE,
            chain_id=self.chain_id,
            key=self.account.key.hex(),
            creds=creds,
            signature_type=signature_type,
            funder=self.funder,
        )
        self.sdk_client = client
        return client

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
        self.live_orders: Dict[str, LiveOrderState] = {}
        self._fill_callbacks = []
        self._simulator = None
        self._fills_by_order_id: Dict[str, Fill] = {}
        self.max_adverse_slippage = 0.02
        self.live_max_order_usdc: float = 0.0
        self.live_force_order_type: str = ""
        self.live_poll_interval: float = 2.0
        self._last_live_poll = 0.0

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

    def _apply_live_order_cap(
        self, side: str, price: float, size: float, source: str = "",
    ) -> float:
        """Clamp live order size to the configured USDC cap."""
        cap = float(self.live_max_order_usdc or 0.0)
        if self.dry_run or side != "BUY" or cap <= 0 or price <= 0 or size <= 0:
            return size

        notional = size * price
        if notional <= cap:
            return size

        resized = cap / price
        log.info(
            "Live order capped by EMS: %s %s %.4f -> %.4f @ %.4f "
            "notional=$%.2f -> $%.2f",
            source or "EMS", side, size, resized, price, notional, cap,
        )
        return resized

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
        edge: float = 0.0,
        fair_value: float = 0.0,
        direction: str = "",
    ) -> Optional[str]:
        """Place an order with automatic size decomposition.

        If the book does not have enough depth for the full size:
          - FOK: split into smaller FOK chunks, each sized to fit book depth.
          - GTC/FAK: reduce order size to the fillable portion.
        Returns the first order ID on success, or None.
        """
        tick = float(tick_size)
        price = round(round(price / tick) * tick, 4)
        price = max(tick, min(1.0 - tick, price))

        if not self.dry_run:
            if self.live_force_order_type:
                order_type = self.live_force_order_type
            size = self._apply_live_order_cap(side, price, size, source)
            if size <= 0:
                return None

        if not self.rate_limiter.check_rate():
            log.warning("Rate limited, skipping order")
            return None

        # Capital allocation is handled by the pipeline's CapitalGate.
        # Do NOT check here — it would double-book the same capital.

        # ── Adaptive depth handling & order decomposition ──────────────
        if self.data:
            book = self.data.get_book(token_id)
            if book:
                vwap, fillable = book.vwap_price(side, size)
                if fillable < 1:
                    log.warning("Insufficient liquidity: %s %s %.1f", side, token_id[:16], size)
                    return None
                if vwap is not None:
                    adverse_slippage = (vwap - price) if side == "BUY" else (price - vwap)
                    if adverse_slippage > self.max_adverse_slippage:
                        log.warning(
                            "Slippage rejected: %s %s %.1f @ %.4f vwap=%.4f slip=%.4f > %.4f",
                            source, side, size, price, vwap, adverse_slippage,
                            self.max_adverse_slippage,
                        )
                        return None
                    if side == "BUY" and vwap > 0:
                        max_cost = size * price
                        resized = max_cost / vwap
                        if resized < size:
                            log.info(
                                "VWAP-resized %s %s: %.1f -> %.1f @ %.4f to keep cost=$%.0f",
                                source, side, size, resized, vwap, max_cost,
                            )
                            size = resized
                if fillable < size:
                    if order_type == "FOK":
                        return self._place_fok_chunks(
                            token_id, side, price, size,
                            tick_size, neg_risk, source, fillable,
                        )
                    # GTC / FAK: reduce to what the book can serve
                    log.info("Depth-limited %s %s: %.1f → %.1f (depth=%.1f)",
                             source, side, size, fillable, fillable)
                    size = fillable

        if not self.dry_run:
            size = self._apply_live_order_cap(side, price, size, source)
            if size <= 0:
                return None

        # ── Execute single order ──────────────────────────────────────
        return self._execute_single_order(
            token_id, side, price, size,
            tick_size, neg_risk, order_type, source,
            edge=edge, fair_value=fair_value, direction=direction,
        )

    def _place_fok_chunks(
        self, token_id: str, side: str, price: float, total_size: float,
        tick_size: str, neg_risk: bool, source: str, chunk_size: float,
    ) -> Optional[str]:
        """Split a large FOK order into smaller FOK chunks that fit book depth."""
        oids: List[str] = []
        remaining = total_size
        while remaining > 0:
            chunk = min(remaining, chunk_size)
            # Re-check depth for this chunk (depth may have changed)
            if self.data:
                book = self.data.get_book(token_id)
                if book:
                    _, avail = book.vwap_price(side, chunk)
                    if avail < chunk:
                        chunk = max(avail, 1.0)
            if chunk < 1:
                log.warning("FOK chunk: no liquidity for remaining %.1f shares", remaining)
                break

            oid = self._execute_single_order(
                token_id, side, price, chunk,
                tick_size, neg_risk, "FOK", source,
            )
            if oid:
                oids.append(oid)
                remaining -= chunk
            else:
                log.warning("FOK chunk failed at %.1f shares (%.1f remaining)", chunk, remaining)
                break

        if oids:
            log.info("Placed %d FOK chunks (%.1f / %.1f shares) for %s %s",
                     len(oids), total_size - remaining, total_size, source, side)
            return oids[0]
        return None

    def _execute_single_order(
        self, token_id: str, side: str, price: float, size: float,
        tick_size: str, neg_risk: bool, order_type: str, source: str,
        edge: float = 0.0, fair_value: float = 0.0, direction: str = "",
    ) -> Optional[str]:
        """Execute a single order — no capital/depth checks, just place it."""
        if self.dry_run:
            if self._simulator:
                fill = self._simulator.simulate_fill(
                    token_id, side, price, size, order_type, source,
                    edge=edge, fair_value=fair_value, direction=direction,
                )
                if fill:
                    log.info("[SIM] %s %s %.1f @ %.4f [%s]",
                             source or "EMS", fill.side, fill.size, fill.price, order_type)
                    self._fills_by_order_id[fill.order_id] = fill
                    self._fire_fill(fill)
                    return fill.order_id
                else:
                    log.debug("[SIM] %s %s %.1f @ %.4f — pending/rejected [%s]",
                              source or "EMS", side, size, price, order_type)
                    return None
            else:
                # Fallback: instant fill (no simulator)
                log.info("[DRY] %s %s %.1f @ %.4f [%s]",
                         source or "EMS", side, size, price, order_type)
                fill = Fill(
                    token_id=token_id, side=side, size=size, price=price,
                    timestamp=time.time(), source=source,
                    edge=edge, fair_value=fair_value, direction=direction,
                )
                if fill.order_id:
                    self._fills_by_order_id[fill.order_id] = fill
                self._fire_fill(fill)
                return f"dry_{side}_{price}_{time.time()}"

        # ── Live order via CLOB API ───────────────────────────────────
        try:
            if not os.environ.get("POLYMARKET_ALLOW_LEGACY_RAW_ORDERS"):
                return self._execute_single_order_sdk(
                    token_id, side, price, size,
                    tick_size, neg_risk, order_type, source,
                    edge=edge, fair_value=fair_value, direction=direction,
                )

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
                self._track_live_order(
                    oid, token_id, side, price, size, source,
                    edge=edge, fair_value=fair_value,
                    direction=direction, order_type=order_type,
                    status=str(resp.get("status", "")),
                )
                log.info("PLACED %s %s %.1f @ %.4f id=%s [%s]",
                         source, side, size, price, oid[:16], order_type)
                return oid
            else:
                log.warning("Rejected: %s", resp.get("errorMsg", resp))
                return None
        except Exception as e:
            log.error("Order failed: %s", e)
            return None

    def _execute_single_order_sdk(
        self, token_id: str, side: str, price: float, size: float,
        tick_size: str, neg_risk: bool, order_type: str, source: str,
        edge: float = 0.0, fair_value: float = 0.0, direction: str = "",
    ) -> Optional[str]:
        """Place a live order through the optional Polymarket v2 SDK."""
        try:
            client = self.auth.ensure_sdk_client()
            from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side

            sdk_side = Side.BUY if side == "BUY" else Side.SELL
            sdk_order_type = getattr(OrderType, order_type, order_type)
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=sdk_side,
            )
            resp = client.create_and_post_order(
                order_args=order_args,
                options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
                order_type=sdk_order_type,
            )
            data = resp if isinstance(resp, dict) else getattr(resp, "__dict__", {})
            oid = (
                data.get("orderID")
                or data.get("order_id")
                or data.get("id")
                or data.get("hash")
            )
            if not oid:
                log.warning("SDK order rejected/no id: %s", data or resp)
                return None
            self.open_order_ids.append(oid)
            self._track_live_order(
                oid, token_id, side, price, size, source,
                edge=edge, fair_value=fair_value,
                direction=direction, order_type=order_type,
                status=str(data.get("status", "")),
            )
            log.info("PLACED %s %s %.1f @ %.4f id=%s [%s]",
                     source, side, size, price, oid[:16], order_type)
            return oid
        except Exception as e:
            log.error("SDK order failed: %s", e)
            return None

    def _track_live_order(
        self,
        order_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        source: str,
        edge: float = 0.0,
        fair_value: float = 0.0,
        direction: str = "",
        order_type: str = "GTC",
        status: str = "",
    ) -> None:
        self.live_orders[order_id] = LiveOrderState(
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            source=source,
            edge=edge,
            fair_value=fair_value,
            direction=direction,
            order_type=order_type,
            last_status=status,
        )

    @staticmethod
    def _float_field(data: dict, *names: str, default: float = 0.0) -> float:
        for name in names:
            if name in data and data[name] not in (None, ""):
                try:
                    return float(data[name])
                except (TypeError, ValueError):
                    continue
        return default

    def check_live_fills(self, force: bool = False):
        """
        Poll accepted live orders and emit fills only when CLOB reports matched
        size. This keeps live OMS/PnL separate from mere order acceptance.
        """
        if self.dry_run or not self.auth or not self.live_orders:
            return
        now = time.time()
        if not force and now - self._last_live_poll < self.live_poll_interval:
            return
        self._last_live_poll = now

        terminal = {"cancelled", "canceled", "expired", "rejected", "failed"}
        remove_ids = []

        for order_id, state in list(self.live_orders.items()):
            try:
                order = self.auth.get_order(order_id)
            except Exception as e:
                log.debug("Live fill poll failed for %s: %s", order_id[:16], e)
                continue

            status = str(
                order.get("status")
                or order.get("orderStatus")
                or order.get("state")
                or ""
            ).lower()
            state.last_status = status
            matched = self._float_field(
                order,
                "size_matched", "sizeMatched", "matched_size",
                "filled_size", "filledSize", "matched",
            )
            if matched <= 0:
                original = self._float_field(order, "original_size", "originalSize", "size")
                remaining = self._float_field(order, "remaining_size", "remainingSize")
                if original > 0 and remaining >= 0:
                    matched = max(0.0, original - remaining)

            if matched > state.filled_size + 1e-9:
                delta = min(matched - state.filled_size, state.size - state.filled_size)
                fill_price = self._float_field(order, "avg_price", "avgPrice", "price", default=state.price)
                fill = Fill(
                    token_id=state.token_id,
                    side=state.side,
                    size=delta,
                    price=fill_price,
                    timestamp=time.time(),
                    order_id=order_id,
                    source=state.source,
                    edge=state.edge,
                    fair_value=state.fair_value,
                    direction=state.direction,
                )
                state.filled_size += delta
                self._fills_by_order_id[order_id] = fill
                self._fire_fill(fill)
                log.info(
                    "LIVE FILL %s %s %.1f @ %.4f id=%s status=%s",
                    state.source or "EMS", state.side, delta, fill_price,
                    order_id[:16], status or "?",
                )

            if status in terminal or state.filled_size >= state.size - 1e-9:
                remove_ids.append(order_id)

        for order_id in remove_ids:
            self.live_orders.pop(order_id, None)
            if order_id in self.open_order_ids:
                self.open_order_ids.remove(order_id)

    def cancel_all(self):
        if not self.open_order_ids:
            return
        if self.dry_run:
            log.info("[DRY] Cancel %d orders", len(self.open_order_ids))
        else:
            try:
                self.auth.cancel_all_orders()
            except Exception as e:
                log.error("Cancel failed: %s", e)
        self.live_orders.clear()
        self.open_order_ids.clear()

    def cancel_pending_for_source(self, source: str):
        """Cancel pending dry-run orders for a specific strategy."""
        if self._simulator:
            self._simulator.cancel_pending_for_source(source)

    def cancel_pending_for_token(self, token_id: str):
        """Cancel pending dry-run orders for a specific token."""
        if self._simulator:
            self._simulator.cancel_pending_for_token(token_id)

    def cancel_order(self, order_id: str):
        if self.dry_run:
            log.info("[DRY] Cancel %s", order_id[:16])
        else:
            try:
                self.auth.cancel_order(order_id)
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
