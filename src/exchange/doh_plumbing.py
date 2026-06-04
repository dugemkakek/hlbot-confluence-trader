"""Shared DoH plumbing for CEX adapters.

Both BinanceMarketData and OKXMarketData (and others) need
the same DoH-aware aiohttp connector builder. Kept here
to avoid duplication.
"""

from __future__ import annotations

from typing import Any

from ..utils.logging import get_logger

logger = get_logger(__name__)


def build_aiohttp_connector_doh(doh: str = "cloudflare") -> dict[str, Any]:
    """Build aiohttp connector kwargs that use a DoH resolver.

    Returns a `connector_factory` callable that the ccxt
    BinanceMarketData/OKXMarketData/etc. consume in `connect()`.
    We can't construct the AsyncResolver here directly because
    it needs a running event loop; the factory defers that.
    """
    if doh not in ("cloudflare", "google"):
        return {}

    nameservers = {
        "cloudflare": ["1.1.1.1", "1.0.0.1"],
        "google": ["8.8.8.8", "8.8.4.4"],
    }[doh]

    def _factory(loop=None):
        try:
            from aiohttp.resolver import AsyncResolver
        except ImportError:
            return None
        return AsyncResolver(nameservers=nameservers, loop=loop)

    return {"connector_factory": _factory}
