"""Tests for the OKX adapter.

Verifies the abstract interface contract, paper mode auto-engages
when no API keys are set, and the factory builds OKX correctly.
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
from src.exchange.okx import (
    OKXAccount,
    OKXAdapter,
    OKXMarketData,
    OKXStream,
)


# ─────────────────────────────────────────────────────────────────────
# Interface contract
# ─────────────────────────────────────────────────────────────────────


def test_venue_kind_okx():
    assert VenueKind.OKX.value == "okx"


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


def test_factory_okx_now_implemented():
    """OKX is now implemented. The factory builds it cleanly."""
    a = build_exchange_adapter({"venue": "okx"})
    assert a.venue == VenueKind.OKX
    assert isinstance(a, OKXAdapter)
    assert isinstance(a.market_data, OKXMarketData)
    assert isinstance(a.stream, OKXStream)
    assert isinstance(a.account, OKXAccount)


# ─────────────────────────────────────────────────────────────────────
# Paper mode
# ─────────────────────────────────────────────────────────────────────


def test_okx_account_paper_mode_engages_without_keys():
    """When api_key/secret/passphrase are absent, paper mode is on."""
    a = OKXAccount({})  # no keys
    assert a._is_paper() is True


def test_okx_account_paper_mode_with_partial_keys():
    """All three of (key, secret, passphrase) must be present for live mode."""
    a = OKXAccount({"api_key": "k", "api_secret": "s"})  # missing passphrase
    assert a._is_paper() is True


def test_okx_account_live_mode_with_all_keys():
    a = OKXAccount({"api_key": "k", "api_secret": "s", "passphrase": "p"})
    assert a._is_paper() is False


# ─────────────────────────────────────────────────────────────────────
# Symbol mapping (matches PaperExecutor's expectations)
# ─────────────────────────────────────────────────────────────────────


def test_okx_symbol_mapper_base_to_swap():
    """Hyperliquid 'BTC' -> OKX 'BTC-USDT' (SWAP)."""
    from src.executor.paper_executor import PaperExecutor
    assert PaperExecutor._okx_symbol("BTC") == "BTC-USDT"
    assert PaperExecutor._okx_symbol("ETH") == "ETH-USDT"
    assert PaperExecutor._okx_symbol("SOL") == "SOL-USDT"


def test_okx_symbol_mapper_already_paired():
    """If already in OKX format, pass through."""
    from src.executor.paper_executor import PaperExecutor
    assert PaperExecutor._okx_symbol("BTC-USDT") == "BTC-USDT"


# ─────────────────────────────────────────────────────────────────────
# Venue kind
# ─────────────────────────────────────────────────────────────────────


def test_okx_venue_is_correct():
    a = build_exchange_adapter({"venue": "okx"})
    assert a.venue == VenueKind.OKX


# ─────────────────────────────────────────────────────────────────────
# Error types
# ─────────────────────────────────────────────────────────────────────


def test_error_hierarchy_includes_okx_context():
    assert issubclass(TransientError, Exception)
    assert issubclass(PermanentError, Exception)
    with pytest.raises(TransientError):
        raise TransientError("rate limited")
    with pytest.raises(PermanentError):
        raise PermanentError("invalid symbol")
