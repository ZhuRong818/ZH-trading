"""
Polymarket Trading Bot

A configurable trading bot for Polymarket prediction markets.
Supports market-making and signal-based strategies.

Uses direct REST API + EIP-712 signing (no py-clob-client dependency).

Setup:
    1. pip install requests eth-account
    2. Copy .env.example to .env and fill in your credentials
    3. python polymarket_bot.py --help

Environment Variables:
    POLYMARKET_PRIVATE_KEY  - Your Polygon wallet private key
    POLYMARKET_FUNDER       - Your funder/proxy wallet address (if using Polymarket.com account)
    POLYMARKET_SIG_TYPE     - Signature type: 0=EOA, 1=POLY_PROXY (default: 1)
"""

import argparse
import hashlib
import hmac
import json
import logging
import math
import os
import signal
import sys
import threading
import time
from base64 import b64decode, b64encode
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CHAIN_ID = 137

# CTF Exchange addresses on Polygon
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

BUY = "BUY"
SELL = "SELL"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("polymarket_bot")


class Strategy(str, Enum):
    MARKET_MAKE = "market_make"
    SIGNAL = "signal"


@dataclass
class BotConfig:
    # Market
    token_id: str = ""
    condition_id: str = ""
    outcome: str = ""
    neg_risk: bool = False
    tick_size: str = "0.01"

    # Strategy
    strategy: Strategy = Strategy.MARKET_MAKE

    # Market-making params
    spread: float = 0.04
    order_size: float = 10.0
    num_levels: int = 3
    level_spacing: float = 0.01
    refresh_interval: float = 5.0

    # Signal params
    signal_threshold: float = 0.05
    signal_size: float = 20.0

    # Risk
    max_position: float = 200.0
    max_loss_usd: float = 50.0
    cooldown_after_fill: float = 1.0

    # Runtime
    dry_run: bool = False
    heartbeat_interval: float = 5.0


# ---------------------------------------------------------------------------
# CLOB Auth Client (direct REST, no SDK needed)
# ---------------------------------------------------------------------------

class ClobAuth:
    """Handles Polymarket CLOB authentication and order signing."""

    def __init__(self, private_key: str, chain_id: int = 137,
                 sig_type: int = 1, funder: str = ""):
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        self.account = Account.from_key(private_key)
        self.address = self.account.address
        self.chain_id = chain_id
        self.sig_type = sig_type
        self.funder = funder or self.address
        self.api_key = ""
        self.api_secret = ""
        self.api_passphrase = ""
        self.session = requests.Session()

    def _l1_headers(self) -> dict:
        """Generate L1 auth headers (EIP-712 signature)."""
        timestamp = str(int(time.time()))
        nonce = "0"

        # Sign the CLOB auth message
        domain = {
            "name": "ClobAuthDomain",
            "version": "1",
            "chainId": self.chain_id,
        }
        message = {
            "address": self.address,
            "timestamp": timestamp,
            "nonce": int(nonce),
        }
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
            "domain": domain,
            "message": message,
        }
        signed = self.account.sign_message(encode_typed_data(full_message=typed_data))

        return {
            "POLY_ADDRESS": self.address,
            "POLY_SIGNATURE": signed.signature.hex(),
            "POLY_TIMESTAMP": timestamp,
            "POLY_NONCE": nonce,
        }

    def _l2_headers(self, method: str, path: str, body: str = "") -> dict:
        """Generate L2 auth headers (HMAC)."""
        timestamp = str(int(time.time()))
        msg = timestamp + method.upper() + path + body
        secret_bytes = b64decode(self.api_secret)
        sig = hmac.new(secret_bytes, msg.encode(), hashlib.sha256).digest()
        sig_b64 = b64encode(sig).decode()

        return {
            "POLY_ADDRESS": self.address,
            "POLY_SIGNATURE": sig_b64,
            "POLY_TIMESTAMP": timestamp,
            "POLY_API_KEY": self.api_key,
            "POLY_PASSPHRASE": self.api_passphrase,
        }

    def derive_api_creds(self):
        """Create or derive API credentials."""
        headers = self._l1_headers()
        resp = self.session.get(f"{CLOB_BASE}/auth/derive-api-key", headers=headers)
        if resp.status_code != 200:
            # Try creating new creds
            resp = self.session.post(f"{CLOB_BASE}/auth/api-key", headers=headers)
        resp.raise_for_status()
        data = resp.json()
        self.api_key = data["apiKey"]
        self.api_secret = data["secret"]
        self.api_passphrase = data["passphrase"]
        log.info("API credentials derived for %s", self.address[:10] + "...")

    def get(self, path: str, params: dict = None) -> dict:
        """Authenticated GET request."""
        url = CLOB_BASE + path
        headers = self._l2_headers("GET", path)
        resp = self.session.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, json_data: dict = None) -> dict:
        """Authenticated POST request."""
        url = CLOB_BASE + path
        body = json.dumps(json_data) if json_data else ""
        headers = self._l2_headers("POST", path, body)
        headers["Content-Type"] = "application/json"
        resp = self.session.post(url, headers=headers, data=body)
        resp.raise_for_status()
        return resp.json()

    def delete(self, path: str, json_data: dict = None) -> dict:
        """Authenticated DELETE request."""
        url = CLOB_BASE + path
        body = json.dumps(json_data) if json_data else ""
        headers = self._l2_headers("DELETE", path, body)
        headers["Content-Type"] = "application/json"
        resp = self.session.delete(url, headers=headers, data=body)
        resp.raise_for_status()
        return resp.json()

    def sign_order(self, order: dict) -> str:
        """Sign an order using EIP-712."""
        exchange = NEG_RISK_CTF_EXCHANGE if order.get("neg_risk") else CTF_EXCHANGE

        domain = {
            "name": "Polymarket CTF Exchange",
            "version": "1",
            "chainId": self.chain_id,
            "verifyingContract": exchange,
        }
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
            "side": 0 if order["side"] == BUY else 1,
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
            "domain": domain,
            "message": message,
        }
        signed = self.account.sign_message(encode_typed_data(full_message=typed_data))
        return signed.signature.hex()


# ---------------------------------------------------------------------------
# Market helpers
# ---------------------------------------------------------------------------

def fetch_markets(query: str = "", limit: int = 20) -> list:
    params = {"_limit": limit, "active": True, "closed": False}
    if query:
        params["slug"] = query
    resp = requests.get(f"{GAMMA_BASE}/markets", params=params)
    resp.raise_for_status()
    markets = resp.json()
    if query and not markets:
        resp = requests.get(
            f"{GAMMA_BASE}/markets",
            params={"_limit": 100, "active": True, "closed": False},
        )
        resp.raise_for_status()
        q = query.lower()
        markets = [m for m in resp.json() if q in m.get("question", "").lower()][:limit]
    return markets


def parse_tokens(market: dict) -> list:
    outcomes = json.loads(market.get("outcomes", "[]")) if isinstance(market.get("outcomes"), str) else (market.get("outcomes") or [])
    clob_ids = json.loads(market.get("clobTokenIds", "[]")) if isinstance(market.get("clobTokenIds"), str) else (market.get("clobTokenIds") or [])
    prices = json.loads(market.get("outcomePrices", "[]")) if isinstance(market.get("outcomePrices"), str) else (market.get("outcomePrices") or [])
    tokens = []
    for i in range(len(outcomes)):
        tokens.append({
            "outcome": outcomes[i] if i < len(outcomes) else "?",
            "token_id": clob_ids[i] if i < len(clob_ids) else None,
            "price": float(prices[i]) if i < len(prices) else None,
        })
    return tokens


def get_order_book(token_id: str) -> dict:
    resp = requests.get(f"{CLOB_BASE}/book", params={"token_id": token_id})
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class PolymarketBot:
    def __init__(self, config: BotConfig):
        self.config = config
        self.auth: Optional[ClobAuth] = None
        self.running = False
        self.position = 0.0
        self.pnl = 0.0
        self.avg_entry = 0.0
        self.open_order_ids = []
        self._heartbeat_id = ""
        self._heartbeat_thread = None

    # ---- Setup ----

    def connect(self):
        """Initialize auth and derive API credentials."""
        private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
        if not private_key:
            raise RuntimeError("Set POLYMARKET_PRIVATE_KEY env var")

        funder = os.environ.get("POLYMARKET_FUNDER", "")
        sig_type = int(os.environ.get("POLYMARKET_SIG_TYPE", "1"))

        self.auth = ClobAuth(
            private_key=private_key,
            chain_id=CHAIN_ID,
            sig_type=sig_type,
            funder=funder,
        )
        log.info("Deriving API credentials...")
        self.auth.derive_api_creds()
        log.info("Authenticated successfully.")

    def select_market_interactive(self, query: str = ""):
        """Let user pick a market interactively."""
        markets = fetch_markets(query, limit=15)
        if not markets:
            log.error("No markets found.")
            sys.exit(1)

        print("\nAvailable markets:\n")
        for i, m in enumerate(markets):
            tokens = parse_tokens(m)
            prices_str = " / ".join(f"{t['outcome']}={t['price']}" for t in tokens)
            vol = m.get("volume24hr", 0)
            print(f"  [{i}] {m['question']}")
            print(f"      {prices_str}  |  24h vol: {vol:.0f}")

        idx = int(input("\nSelect market number: "))
        market = markets[idx]

        tokens = parse_tokens(market)
        print("\nOutcomes:")
        for i, t in enumerate(tokens):
            print(f"  [{i}] {t['outcome']} (price={t['price']}, token_id={t['token_id']})")

        tidx = int(input("Select outcome: "))
        token = tokens[tidx]

        self.config.token_id = token["token_id"]
        self.config.condition_id = market.get("conditionId", "")
        self.config.outcome = token["outcome"]
        self.config.neg_risk = market.get("negRisk", False)
        tick = market.get("orderPriceMinTickSize", "0.01")
        self.config.tick_size = str(tick)

        log.info(
            "Selected: %s [%s] tick=%s neg_risk=%s",
            market["question"], token["outcome"],
            self.config.tick_size, self.config.neg_risk,
        )

    # ---- Heartbeat ----

    def _heartbeat_loop(self):
        while self.running:
            try:
                resp = self.auth.post("/heartbeat", {"heartbeat_id": self._heartbeat_id})
                self._heartbeat_id = resp.get("heartbeat_id", self._heartbeat_id)
            except Exception as e:
                log.warning("Heartbeat failed: %s", e)
            time.sleep(self.config.heartbeat_interval)

    def _start_heartbeat(self):
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        log.info("Heartbeat thread started (interval=%.1fs)", self.config.heartbeat_interval)

    # ---- Order Management ----

    def cancel_all_orders(self):
        """Cancel all open orders."""
        if not self.open_order_ids:
            return
        try:
            if self.config.dry_run:
                log.info("[DRY RUN] Would cancel %d orders", len(self.open_order_ids))
            else:
                self.auth.delete("/cancel-all")
            self.open_order_ids.clear()
            log.info("All orders cancelled.")
        except Exception as e:
            log.error("Cancel failed: %s", e)

    def _build_order(self, side: str, price: float, size: float) -> dict:
        """Build an order payload with proper amounts."""
        tick = float(self.config.tick_size)
        price = round(round(price / tick) * tick, 4)
        price = max(tick, min(1.0 - tick, price))

        # Convert to raw amounts (6 decimals for USDC)
        # For BUY: maker_amount = size * price (USDC you pay), taker_amount = size (shares you get)
        # For SELL: maker_amount = size (shares you give), taker_amount = size * price (USDC you get)
        decimals = 6
        scale = 10 ** decimals

        if side == BUY:
            maker_amount = int(size * price * scale)
            taker_amount = int(size * scale)
        else:
            maker_amount = int(size * scale)
            taker_amount = int(size * price * scale)

        salt = int.from_bytes(os.urandom(16), "big")
        nonce = 0

        return {
            "salt": str(salt),
            "token_id": self.config.token_id,
            "maker_amount": str(maker_amount),
            "taker_amount": str(taker_amount),
            "side": side,
            "expiration": "0",
            "nonce": str(nonce),
            "fee_rate_bps": "0",
            "neg_risk": self.config.neg_risk,
            "price": price,
            "size": size,
        }

    def place_order(self, side: str, price: float, size: float) -> Optional[str]:
        """Place a single limit order. Returns order ID or None."""
        tick = float(self.config.tick_size)
        price = round(round(price / tick) * tick, 4)
        price = max(tick, min(1.0 - tick, price))

        if self.config.dry_run:
            log.info("[DRY RUN] %s %.1f @ %.4f", side, size, price)
            return "dry_%s_%.4f" % (side, price)

        try:
            order = self._build_order(side, price, size)
            signature = self.auth.sign_order(order)

            payload = {
                "order": {
                    "salt": int(order["salt"]),
                    "maker": self.auth.funder,
                    "signer": self.auth.address,
                    "taker": "0x0000000000000000000000000000000000000000",
                    "tokenId": order["token_id"],
                    "makerAmount": order["maker_amount"],
                    "takerAmount": order["taker_amount"],
                    "expiration": "0",
                    "nonce": "0",
                    "feeRateBps": "0",
                    "side": side,
                    "signatureType": self.auth.sig_type,
                    "signature": signature,
                },
                "owner": self.auth.funder,
                "orderType": "GTC",
            }

            resp = self.auth.post("/order", payload)

            if resp.get("success"):
                oid = resp["orderID"]
                self.open_order_ids.append(oid)
                log.info("PLACED %s %.1f @ %.4f  id=%s", side, size, price, oid[:16])
                return oid
            else:
                log.warning("Order rejected: %s", resp.get("errorMsg", resp))
                return None
        except Exception as e:
            log.error("Order failed: %s", e)
            return None

    def sync_open_orders(self):
        """Refresh the list of open orders from the exchange."""
        try:
            orders = self.auth.get("/data/orders", {
                "asset_id": self.config.token_id,
                "state": "LIVE",
            })
            self.open_order_ids = [o["id"] for o in orders]
        except Exception as e:
            log.warning("Failed to sync orders: %s", e)

    # ---- Strategies ----

    def _get_mid_price(self) -> Optional[float]:
        """Get the current mid price from the order book."""
        try:
            book = get_order_book(self.config.token_id)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if bids and asks:
                best_bid = float(bids[0]["price"])
                best_ask = float(asks[0]["price"])
                return (best_bid + best_ask) / 2
            elif bids:
                return float(bids[0]["price"])
            elif asks:
                return float(asks[0]["price"])
            return None
        except Exception as e:
            log.warning("Failed to get mid price: %s", e)
            return None

    def _get_book_summary(self) -> Optional[dict]:
        """Get order book with best bid/ask and mid."""
        try:
            book = get_order_book(self.config.token_id)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
            mid = None
            if best_bid and best_ask:
                mid = (best_bid + best_ask) / 2
            spread = (best_ask - best_bid) if (best_bid and best_ask) else None
            return {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "mid": mid,
                "spread": spread,
                "bid_depth": sum(float(b["size"]) for b in bids[:5]),
                "ask_depth": sum(float(a["size"]) for a in asks[:5]),
            }
        except Exception as e:
            log.warning("Failed to get book: %s", e)
            return None

    def run_market_make_step(self):
        """One iteration of the market-making strategy."""
        summary = self._get_book_summary()
        if not summary or summary["mid"] is None:
            log.warning("No mid price available, skipping.")
            return

        mid = summary["mid"]

        # Cancel stale orders
        self.cancel_all_orders()

        half_spread = self.config.spread / 2
        cfg = self.config

        # Skew quotes based on position (lean away from risk)
        position_skew = 0.0
        if cfg.max_position > 0:
            position_skew = (self.position / cfg.max_position) * half_spread * 0.5

        for level in range(cfg.num_levels):
            offset = half_spread + level * cfg.level_spacing

            bid_price = mid - offset - position_skew
            ask_price = mid + offset - position_skew

            # Reduce size at wider levels
            size = cfg.order_size * (1.0 - 0.2 * level)

            # Position limits
            if self.position + size <= cfg.max_position:
                self.place_order(BUY, bid_price, size)
            if self.position - size >= -cfg.max_position:
                self.place_order(SELL, ask_price, size)

        log.info(
            "Quotes refreshed: mid=%.4f spread=%.4f pos=%.1f pnl=%.2f",
            mid, summary["spread"] or 0, self.position, self.pnl,
        )

    def run_signal_step(self):
        """One iteration of the signal-based strategy.

        Override `compute_fair_value` to plug in your own signal.
        """
        mid = self._get_mid_price()
        if mid is None:
            return

        fair = self.compute_fair_value()
        if fair is None:
            return

        edge = fair - mid
        cfg = self.config

        if abs(edge) < cfg.signal_threshold:
            log.info("No edge: fair=%.4f mid=%.4f edge=%.4f", fair, mid, edge)
            return

        self.cancel_all_orders()

        if edge > 0 and self.position + cfg.signal_size <= cfg.max_position:
            self.place_order(BUY, mid + 0.005, cfg.signal_size)
            log.info("SIGNAL BUY: fair=%.4f mid=%.4f edge=%.4f", fair, mid, edge)
        elif edge < 0 and self.position - cfg.signal_size >= -cfg.max_position:
            self.place_order(SELL, mid - 0.005, cfg.signal_size)
            log.info("SIGNAL SELL: fair=%.4f mid=%.4f edge=%.4f", fair, mid, edge)

    def compute_fair_value(self) -> Optional[float]:
        """Compute fair value for the signal strategy.

        Override this method with your own model / signal source.
        Returns a probability between 0 and 1, or None if no signal.
        """
        return self._get_mid_price()

    # ---- Risk ----

    def check_risk_limits(self) -> bool:
        """Check if risk limits are breached. Returns True if OK."""
        mid = self._get_mid_price()
        if mid is not None and self.position != 0:
            unrealized = self.position * (mid - self.avg_entry) if self.avg_entry > 0 else 0
            if unrealized < -self.config.max_loss_usd:
                log.warning(
                    "RISK LIMIT: unrealized loss $%.2f exceeds max $%.2f",
                    unrealized, self.config.max_loss_usd,
                )
                return False
        return True

    # ---- Main Loop ----

    def run(self):
        """Main bot loop."""
        self.running = True
        log.info("Starting bot: strategy=%s dry_run=%s", self.config.strategy.value, self.config.dry_run)
        log.info("Token: %s... [%s]", self.config.token_id[:20], self.config.outcome)

        if not self.config.dry_run:
            self._start_heartbeat()

        try:
            while self.running:
                if not self.check_risk_limits():
                    log.warning("Risk limit breached - cancelling all orders and stopping.")
                    self.cancel_all_orders()
                    break

                if self.config.strategy == Strategy.MARKET_MAKE:
                    self.run_market_make_step()
                elif self.config.strategy == Strategy.SIGNAL:
                    self.run_signal_step()

                time.sleep(self.config.refresh_interval)

        except KeyboardInterrupt:
            log.info("Interrupted by user.")
        finally:
            self.shutdown()

    def shutdown(self):
        """Clean shutdown: cancel all orders."""
        self.running = False
        log.info("Shutting down...")
        if not self.config.dry_run and self.auth:
            self.cancel_all_orders()
        log.info("Bot stopped.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run market making on a market you search for
  python polymarket_bot.py --search "bitcoin" --strategy market_make --dry-run

  # Market make with custom spread and size
  python polymarket_bot.py --token TOKEN_ID --strategy market_make --spread 0.06 --size 15

  # Signal-based trading
  python polymarket_bot.py --search "election" --strategy signal --dry-run
        """,
    )
    parser.add_argument("--search", type=str, default="", help="Search for a market interactively")
    parser.add_argument("--token", type=str, help="Token ID to trade")
    parser.add_argument("--condition-id", type=str, default="", help="Market condition ID")
    parser.add_argument("--outcome", type=str, default="YES", help="Outcome: YES or NO")
    parser.add_argument("--strategy", type=str, default="market_make",
                        choices=["market_make", "signal"], help="Trading strategy")
    parser.add_argument("--spread", type=float, default=0.04, help="Spread for market making (default: 0.04)")
    parser.add_argument("--size", type=float, default=10.0, help="Order size in shares (default: 10)")
    parser.add_argument("--levels", type=int, default=3, help="Number of price levels per side (default: 3)")
    parser.add_argument("--max-position", type=float, default=200.0, help="Max position in shares (default: 200)")
    parser.add_argument("--max-loss", type=float, default=50.0, help="Max unrealized loss in USD (default: 50)")
    parser.add_argument("--interval", type=float, default=5.0, help="Refresh interval in seconds (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without placing real orders")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = BotConfig(
        strategy=Strategy(args.strategy),
        spread=args.spread,
        order_size=args.size,
        num_levels=args.levels,
        max_position=args.max_position,
        max_loss_usd=args.max_loss,
        refresh_interval=args.interval,
        dry_run=args.dry_run,
    )

    bot = PolymarketBot(config)

    # Handle Ctrl+C gracefully
    signal.signal(signal.SIGINT, lambda *_: setattr(bot, 'running', False))

    if args.token:
        config.token_id = args.token
        config.condition_id = args.condition_id
        config.outcome = args.outcome
    else:
        bot.select_market_interactive(args.search)

    if not config.dry_run:
        bot.connect()
    else:
        log.info("DRY RUN mode - no real orders will be placed.")

    bot.run()


if __name__ == "__main__":
    main()
