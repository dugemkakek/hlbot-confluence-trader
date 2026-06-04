"""Tests for the exchange adapter framework.

Verifies the abstract interface, the Hyperliquid implementation,
the paper implementation, and the factory's venue registry.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.exchange.base import (
    AccountAdapter,
    Balance,
    ExchangeAdapter,
    ExchangeError,
    MarketDataAdapter,
    OrderRequest,
    OrderResult,
    PermanentError,
    StreamAdapter,
    SymbolInfo,
    Ticker,
    TransientError,
    VenueKind,
)
from src.exchange.factory import build_exchange_adapter
from src.exchange.paper import PaperAdapter, PaperAccount


# ─────────────────────────────────────────────────────────────────────
# Interface contract
# ─────────────────────────────────────────────────────────────────────


def test_venue_kind_values():
    """VenueKind enum covers the venues we'll add."""
    assert VenueKind.HYPERLIQUID.value == "hyperliquid"
    assert VenueKind.BINANCE.value == "binance"
    assert VenueKind.BYBIT.value == "bybit"
    assert VenueKind.GATE.value == "gate"
    assert VenueKind.PAPER.value == "paper"


def test_abstract_classes_cannot_be_instantiated():
    """ABC enforcement: can't instantiate the abstract base."""
    with pytest.raises(TypeError):
        MarketDataAdapter()  # type: ignore[abstract]
    with pytest.raises(TypeError):
        StreamAdapter()  # type: ignore[abstract]
    with pytest.raises(TypeError):
        AccountAdapter()  # type: ignore[abstract]
    with pytest.raises(TypeError):
        ExchangeAdapter()  # type: ignore[abstract]


# ─────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────


def test_factory_hyperliquid():
    a = build_exchange_adapter({"venue": "hyperliquid"})
    assert a.venue == VenueKind.HYPERLIQUID
    assert isinstance(a, ExchangeAdapter)


def test_factory_paper():
    a = build_exchange_adapter({"venue": "paper"})
    assert a.venue == VenueKind.PAPER
    assert isinstance(a.account, PaperAccount)


def test_factory_default_uses_loaded_config():
    """When no config is passed, the factory reads from the loaded
    AppConfig. As of 2026-06-04 dev.yaml defaults venue=binance, so
    the factory default follows that. Pass an explicit `config={}` to
    force the 'hyperliquid' fallback (see test_factory_hyperliquid)."""
    from src.utils.config import get_config
    cfg = get_config()
    expected_venue = getattr(cfg, "exchange", None)
    if expected_venue is not None:
        expected = expected_venue.venue
    else:
        expected = "hyperliquid"
    a = build_exchange_adapter()
    assert a.venue.value == expected


def test_factory_unknown_venue_raises():
    with pytest.raises(ExchangeError) as exc_info:
        build_exchange_adapter({"venue": "kraken"})
    assert "kraken" in str(exc_info.value)
    assert "Available" in str(exc_info.value)


def test_factory_binance_now_implemented():
    """Binance is now implemented. The factory builds it cleanly."""
    a = build_exchange_adapter({"venue": "binance"})
    assert a.venue == VenueKind.BINANCE
    from src.exchange.binance import BinanceAdapter
    assert isinstance(a, BinanceAdapter)


def test_factory_bybit_now_implemented():
    """Bybit is now implemented."""
    a = build_exchange_adapter({"venue": "bybit"})
    assert a.venue == VenueKind.BYBIT
    from src.exchange.bybit import BybitAdapter
    assert isinstance(a, BybitAdapter)


def test_factory_gate_now_implemented():
    """Gate.io adapter is now wired in (replaces the old stub).

    See tests/test_gate_adapter.py for full coverage.
    """
    from src.exchange.gate import GateAdapter
    a = build_exchange_adapter({"venue": "gate"})
    assert a.venue == VenueKind.GATE
    assert isinstance(a, GateAdapter)


# ─────────────────────────────────────────────────────────────────────
# Paper adapter — full lifecycle
# ─────────────────────────────────────────────────────────────────────


def test_paper_long_updates_balance():
    """Going LONG should deduct notional + fee from cash."""
    async def run():
        paper = PaperAdapter()
        await paper.connect()
        paper.account.set_price("BTC", 50000.0)
        paper.account.set_balance("USD", 10000.0)
        result = await paper.account.place_order(
            OrderRequest(symbol="BTC", side="buy", size=0.1, order_type="market")
        )
        assert result.success
        assert result.fill_price == 50000.0
        assert result.filled_size == 0.1
        # 10000 - (50000 * 0.1) - (50000 * 0.1 * 0.00035 fee) = 4998.25
        balances = await paper.account.get_balances()
        assert balances[0].free == pytest.approx(4998.25, abs=0.01)
        await paper.close()
    asyncio.run(run())


def test_paper_short_credits_cash():
    """Going SHORT should add sale proceeds minus fee to cash."""
    async def run():
        paper = PaperAdapter()
        await paper.connect()
        paper.account.set_price("BTC", 50000.0)
        paper.account.set_balance("USD", 10000.0)
        result = await paper.account.place_order(
            OrderRequest(symbol="BTC", side="sell", size=0.1, order_type="market")
        )
        assert result.success
        # Short: 10000 + (50000 * 0.1) - fee = 10000 + 5000 - 1.75 = 14998.25
        balances = await paper.account.get_balances()
        assert balances[0].free == pytest.approx(14998.25, abs=0.01)
        await paper.close()
    asyncio.run(run())


def test_paper_open_orders_tracking():
    async def run():
        paper = PaperAdapter()
        await paper.connect()
        paper.account.set_price("BTC", 50000.0)
        paper.account.set_balance("USD", 10000.0)
        await paper.account.place_order(
            OrderRequest(
                symbol="BTC", side="buy", size=0.1,
                order_type="market", client_order_id="my-order-1",
            )
        )
        open_orders = await paper.account.get_open_orders()
        assert len(open_orders) == 1
        assert open_orders[0]["order_id"] == "my-order-1"
        # Cancel
        ok = await paper.account.cancel_order("my-order-1")
        assert ok
        assert await paper.account.get_open_orders() == []
        await paper.close()
    asyncio.run(run())


def test_paper_unknown_symbol_rejects():
    async def run():
        paper = PaperAdapter()
        await paper.connect()
        paper.account.set_balance("USD", 10000.0)
        result = await paper.account.place_order(
            OrderRequest(symbol="UNKNOWN", side="buy", size=0.1, order_type="market")
        )
        assert not result.success
        assert "no price" in result.error
        await paper.close()
    asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────
# Balance.total
# ─────────────────────────────────────────────────────────────────────


def test_balance_total():
    b = Balance("USD", free=750.0, locked=250.0)
    assert b.total == 1000.0


# ─────────────────────────────────────────────────────────────────────
# Hyperliquid adapter
# ─────────────────────────────────────────────────────────────────────


def test_hyperliquid_venue_is_correct():
    a = build_exchange_adapter({"venue": "hyperliquid"})
    assert a.venue == VenueKind.HYPERLIQUID
    # Sub-adapter types
    from src.exchange.hyperliquid import (
        HyperliquidMarketData,
        HyperliquidStream,
        HyperliquidAccountStub,
    )
    assert isinstance(a.market_data, HyperliquidMarketData)
    assert isinstance(a.stream, HyperliquidStream)
    assert isinstance(a.account, HyperliquidAccountStub)


# ─────────────────────────────────────────────────────────────────────
# Error types
# ─────────────────────────────────────────────────────────────────────


def test_error_hierarchy():
    assert issubclass(TransientError, ExchangeError)
    assert issubclass(PermanentError, ExchangeError)
    with pytest.raises(TransientError):
        raise TransientError("rate limited")
    with pytest.raises(PermanentError):
        raise PermanentError("invalid symbol")
