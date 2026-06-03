import time

import pytest

from data_pipeline import price_feeds


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(
            {
                "url": url,
                "params": params,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return FakeResponse(self.payload)


@pytest.fixture(autouse=True)
def reset_price_feed_state(monkeypatch):
    monkeypatch.setenv("KVRUN_BEARER_TOKEN", "test-token")
    price_feeds._CACHE.clear()
    yield
    price_feeds._CACHE.clear()


def test_findata_symbol_maps_binance_usdt_to_usd():
    assert price_feeds.findata_symbol("BTCUSDT") == "BTCUSD"
    assert price_feeds.findata_symbol("eth") == "ETHUSD"


def test_get_asset_price_uses_findata_quotes(monkeypatch):
    session = FakeSession(
        [{"symbol": "BTCUSD", "price": 67023.61, "source": "tier_a:binance", "stale": False}]
    )
    monkeypatch.setattr(price_feeds, "_SESSION", session)

    assert price_feeds.get_asset_price("BTCUSDT") == 67023.61
    assert session.calls[0]["url"].endswith("/quotes")
    assert session.calls[0]["params"] == {"symbols": "BTCUSD"}
    assert session.calls[0]["headers"] == {"Authorization": "Bearer test-token"}


def test_stale_quote_does_not_overwrite_cache(monkeypatch):
    price_feeds._CACHE["BTCUSD"] = {"price": 67000.0, "fetched_at": time.time() - 10}
    session = FakeSession([{"symbol": "BTCUSD", "price": 1.0, "stale": True}])
    monkeypatch.setattr(price_feeds, "_SESSION", session)

    with pytest.raises(ValueError, match="stale price"):
        price_feeds.get_asset_price("btc")

    assert price_feeds._CACHE["BTCUSD"]["price"] == 67000.0


def test_missing_env_token_raises(monkeypatch):
    monkeypatch.delenv("KVRUN_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(price_feeds.Path, "cwd", lambda: price_feeds.Path("__missing_test_dir__"))

    with pytest.raises(RuntimeError, match="KVRUN_BEARER_TOKEN"):
        price_feeds.get_asset_price("btc")
