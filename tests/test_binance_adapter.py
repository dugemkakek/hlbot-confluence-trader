"""Tests for the Binance USDT-M Futures adapter (Phase 4).

Verifies:
  - Factory builds BinanceAdapter for venue="binance"
  - Default market type is usdt-m-future
  - Paper mode when no api keys, live mode when keys present
  - DoH config (cloudflare/google/system) propagates to the ccxt client
  - Paper place_order fills at the last ticker price
"""

from __future__ import annotations

import asyncio

import pytest

from src.exchange.base import (
    ExchangeError,
    OrderRequest,
    VenueKind,
)
from src.exchange.binance import (
    BinanceAdapter,
    BinanceMarketData,
    BinanceStream,
    BinanceAccount,
    _build_aiohttp_connector_doh,
)
from src.exchange.factory import build_exchange_adapter


# ─────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────


def test_factory_builds_binance():
    a = build_exchange_adapter({"venue": "binance"})
    assert a.venue == VenueKind.BINANCE
    assert isinstance(a, BinanceAdapter)


def test_factory_default_market_type():
    a = build_exchange_adapter({"venue": "binance"})
    assert a.market_data._market_type == "usdt-m-future"
    assert a.account._market_type == "usdt-m-future"


def test_factory_custom_market_type():
    a = build_exchange_adapter({"venue": "binance", "market_type": "spot"})
    assert a.market_data._market_type == "spot"


def test_factory_spot_or_coin_m_passthrough():
    a = build_exchange_adapter({"venue": "binance", "market_type": "coin-m-future"})
    assert a.market_data._market_type == "coin-m-future"


# ─────────────────────────────────────────────────────────────────────
# Mode (paper vs live)
# ─────────────────────────────────────────────────────────────────────


def test_paper_mode_without_keys():
    a = build_exchange_adapter({"venue": "binance"})
    assert a.account._is_paper() is True
    assert a.account._api_key is None


def test_live_mode_with_keys():
    a = build_exchange_adapter({
        "venue": "binance",
        "api_key": "test_key",
        "api_secret": "test_secret",
    })
    assert a.account._is_paper() is False
    assert a.account._api_key == "test_key"
    assert a.account._api_secret == "test_secret"


# ─────────────────────────────────────────────────────────────────────
# DoH (DNS-over-HTTPS) — Indonesia workaround
# ─────────────────────────────────────────────────────────────────────


def test_doh_cloudflare_propagates():
    a = build_exchange_adapter({"venue": "binance", "doh": "cloudflare"})
    assert a.market_data._doh == "cloudflare"


def test_doh_google_propagates():
    a = build_exchange_adapter({"venue": "binance", "doh": "google"})
    assert a.market_data._doh == "google"


def test_doh_system_default():
    a = build_exchange_adapter({"venue": "binance"})
    assert a.market_data._doh == "system"


def test_doh_config_helper_cloudflare():
    cfg = _build_aiohttp_connector_doh("cloudflare")
    assert "connector_factory" in cfg
    # The factory must be callable (deferred resolver construction)
    assert callable(cfg["connector_factory"])


def test_doh_config_helper_google():
    cfg = _build_aiohttp_connector_doh("google")
    assert "connector_factory" in cfg
    assert callable(cfg["connector_factory"])


def test_doh_config_helper_system_returns_empty():
    """`doh: system` means use OS resolver; no override needed."""
    cfg = _build_aiohttp_connector_doh("system")
    assert cfg == {}


def test_doh_config_helper_unknown_returns_empty():
    """Unknown provider → empty config (fall back to system)."""
    cfg = _build_aiohttp_connector_doh("quad9")
    assert cfg == {}


# ─────────────────────────────────────────────────────────────────────
# Paper place_order — fills at last ticker price
# ─────────────────────────────────────────────────────────────────────


def test_paper_place_order_uses_last_ticker():
    async def run():
        a = BinanceAdapter({"market_type": "usdt-m-future"})
        await a.connect()
        # In paper mode (no api_key)
        assert a.account._is_paper()

        # Stub the ccxt client's fetch_ticker to return a known price
        class FakeClient:
            async def fetch_ticker(self, symbol):
                return {"last": 50000.0, "bid": 49999.0, "ask": 50001.0}
        a.account._client = FakeClient()

        result = await a.account.place_order(
            OrderRequest(symbol="BTCUSDT", side="buy", size=0.1, order_type="market")
        )
        assert result.success
        assert result.fill_price == 50000.0
        assert result.filled_size == 0.1
        # 50000 * 0.1 * 0.00035 = 1.75 fee
        assert result.fees_paid == pytest.approx(1.75, abs=0.01)
        await a.close()
    asyncio.run(run())


def test_paper_place_order_short():
    async def run():
        a = BinanceAdapter({"market_type": "usdt-m-future"})
        await a.connect()

        class FakeClient:
            async def fetch_ticker(self, symbol):
                return {"last": 50000.0, "bid": 49999.0, "ask": 50001.0}
        a.account._client = FakeClient()

        result = await a.account.place_order(
            OrderRequest(symbol="BTCUSDT", side="sell", size=0.1, order_type="market")
        )
        assert result.success
        # Fee applies on both long and short entries
        assert result.fees_paid == pytest.approx(1.75, abs=0.01)
        await a.close()
    asyncio.run(run())


def test_paper_place_order_no_ticker_fails():
    async def run():
        a = BinanceAdapter({"market_type": "usdt-m-future"})
        await a.connect()

        class FakeClient:
            async def fetch_ticker(self, symbol):
                return {"last": 0}  # no price
        a.account._client = FakeClient()

        result = await a.account.place_order(
            OrderRequest(symbol="BTCUSDT", side="buy", size=0.1, order_type="market")
        )
        assert not result.success
        assert "no last price" in result.error
        await a.close()
    asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────
# Connect / close lifecycle
# ─────────────────────────────────────────────────────────────────────


def test_binance_connects_and_closes():
    async def run():
        a = BinanceAdapter({"market_type": "usdt-m-future"})
        await a.connect()
        assert a.market_data._client is not None
        assert a.stream._client is not None
        assert a.account._client is not None
        await a.close()
        # Clients should be cleared
        assert a.market_data._client is None
        assert a.stream._client is None
        assert a.account._client is None
    asyncio.run(run())


def test_binance_double_close_safe():
    async def run():
        a = BinanceAdapter({"market_type": "usdt-m-future"})
        await a.connect()
        await a.close()
        await a.close()  # should not raise
    asyncio.run(run())
