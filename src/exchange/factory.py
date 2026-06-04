"""Build an ExchangeAdapter from config.

Central place for venue selection. To add a new venue:
1. Implement the `ExchangeAdapter` interface in a new module
   (see `hyperliquid.py` for the reference).
2. Register the venue here.
3. Add a config section in `config/base.yaml`.
"""

from __future__ import annotations

from typing import Any

from ..utils.logging import get_logger
from .base import (
    ExchangeAdapter,
    ExchangeError,
    VenueKind,
)

logger = get_logger(__name__)


def build_exchange_adapter(config: dict[str, Any] | None = None) -> ExchangeAdapter:
    """Build the configured venue's adapter.

    The passed `config` dict is passed to the venue's adapter
    constructor. The `venue` key selects the venue; other keys
    (api_key, market_type, doh, ...) are venue-specific.

    If `config` is None, the function falls back to the global
    `app.exchange` config (read via `get_config()`).

    Raises:
        ExchangeError: if the requested venue isn't registered
            or the adapter can't be constructed.
    """
    if config is None:
        try:
            from ..utils.config import get_config
            app_cfg = get_config()
            exch = getattr(app_cfg, "exchange", None)
            if exch is not None and hasattr(exch, "model_dump"):
                config = exch.model_dump()
        except Exception:
            config = {}

    if config is None:
        config = {}
    venue_str = config.get("venue", "hyperliquid")
    try:
        venue = VenueKind(venue_str)
    except ValueError as exc:
        raise ExchangeError(
            f"Unknown venue '{venue_str}'. "
            f"Available: {[v.value for v in VenueKind]}"
        ) from exc

    builder = _BUILDERS.get(venue)
    if builder is None:
        raise ExchangeError(
            f"No adapter implemented for venue '{venue.value}'. "
            f"Available: {[v.value for v in _BUILDERS]}"
        )
    # Pass the config to the venue-specific builder. We extract
    # everything except `venue` so the adapter gets the rest.
    builder_kwargs = {k: v for k, v in config.items() if k != "venue"}
    adapter = builder(builder_kwargs)
    logger.info("Built exchange adapter", venue=venue.value)
    return adapter


# Registry of venue -> factory. Each entry is a zero-arg callable
# returning an ExchangeAdapter for that venue.
_BUILDERS: dict[VenueKind, callable] = {}


def _register(venue: VenueKind) -> callable:
    """Decorator to register a builder for a venue."""
    def deco(factory_fn):
        _BUILDERS[venue] = factory_fn
        return factory_fn
    return deco


# ─────────────────────────────────────────────────────────────────────
# Concrete builders
# ─────────────────────────────────────────────────────────────────────


@_register(VenueKind.HYPERLIQUID)
def _build_hyperliquid(config: dict[str, Any] | None = None) -> ExchangeAdapter:
    from .hyperliquid import HyperliquidAdapter
    return HyperliquidAdapter()


@_register(VenueKind.PAPER)
def _build_paper(config: dict[str, Any] | None = None) -> ExchangeAdapter:
    """In-memory paper adapter. Useful for unit tests."""
    from .paper import PaperAdapter
    return PaperAdapter()


@_register(VenueKind.BINANCE)
def _build_binance(config: dict[str, Any] | None = None) -> ExchangeAdapter:
    from .binance import BinanceAdapter
    return BinanceAdapter(config or {})


@_register(VenueKind.BYBIT)
def _build_bybit(config: dict[str, Any] | None = None) -> ExchangeAdapter:
    from .bybit import BybitAdapter
    return BybitAdapter(config or {})


@_register(VenueKind.GATE)
def _build_gate(config: dict[str, Any] | None = None) -> ExchangeAdapter:
    from .gate import GateAdapter
    return GateAdapter(config or {})


@_register(VenueKind.OKX)
def _build_okx(config: dict[str, Any] | None = None) -> ExchangeAdapter:
    from .okx import OKXAdapter
    return OKXAdapter(config or {})
