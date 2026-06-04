"""Tests for the Gate.io adapter.

Verifies the abstract interface, paper mode auto-engages, and
the factory builds Gate.io correctly. Mirrors test_okx_adapter.py.
"""

from __future__ import annotations

import pytest

from src.exchange.base import (
    AccountAdapter,
    ExchangeAdapter,
    MarketDataAdapter,
    PermanentError,
    StreamAdapter,
    TransientError,
    VenueKind,
)
from src.exchange.factory import build_exchange_adapter
from src.exchange.gate import (
    GateAccount,
    GateAdapter,
    GateMarketData,
    GateStream,
)


# ─────────────────────────────────────────────────────────────────────
# Interface contract
# ─────────────────────────────────────────────────────────────────────


def test_venue_kind_gate():
    assert VenueKind.GATE.value == "gate"


def test_abstract_classes_cannot_be_instantiated():
    with pytest.raises(TypeError):
        MarketDataAdapter()  # type: ignore[abstract]
    with pytest.raises(TypeError):
        StreamAdapter()  # type: ignore[abstract]
    with pytest.raises(TypeError):
        AccountAdapter()  # type: ignore[abstract]


# ─────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────


def test_factory_gate_now_implemented():
    """Gate.io is now implemented (replaces the old stub)."""
    a = build_exchange_adapter({"venue": "gate"})
    assert a.venue == VenueKind.GATE
    assert isinstance(a, GateAdapter)
    assert isinstance(a.market_data, GateMarketData)
    assert isinstance(a.stream, GateStream)
    assert isinstance(a.account, GateAccount)


# ─────────────────────────────────────────────────────────────────────
# Paper mode
# ─────────────────────────────────────────────────────────────────────


def test_gate_account_paper_mode_engages_without_keys():
    a = GateAccount({})
    assert a._is_paper() is True


def test_gate_account_paper_mode_with_only_key():
    """Need both api_key and api_secret for live mode."""
    a = GateAccount({"api_key": "k"})
    assert a._is_paper() is True


def test_gate_account_live_mode_with_both_keys():
    a = GateAccount({"api_key": "k", "api_secret": "s"})
    assert a._is_paper() is False


# ─────────────────────────────────────────────────────────────────────
# Symbol mapping
# ─────────────────────────────────────────────────────────────────────


def test_gate_symbol_mapper_base_to_usdt():
    """Hyperliquid 'BTC' -> Gate.io 'BTC_USDT' (underscore)."""
    from src.executor.paper_executor import PaperExecutor
    assert PaperExecutor._gate_symbol("BTC") == "BTC_USDT"
    assert PaperExecutor._gate_symbol("ETH") == "ETH_USDT"
    assert PaperExecutor._gate_symbol("SOL") == "SOL_USDT"


def test_gate_symbol_mapper_already_paired():
    """If already in Gate.io format, pass through."""
    from src.executor.paper_executor import PaperExecutor
    assert PaperExecutor._gate_symbol("BTC_USDT") == "BTC_USDT"


# ─────────────────────────────────────────────────────────────────────
# Error types
# ─────────────────────────────────────────────────────────────────────


def test_error_hierarchy():
    assert issubclass(TransientError, Exception)
    assert issubclass(PermanentError, Exception)
    with pytest.raises(TransientError):
        raise TransientError("rate limited")
    with pytest.raises(PermanentError):
        raise PermanentError("invalid symbol")
