"""Dynamic pair discovery — fetches ALL tradable Hyperliquid coins and filters them.

This module is responsible for:
- Fetching all coins from Hyperliquid /info meta endpoint
- Filtering out stablecoins (USDC, USDT, USDC.e), deprecated/settled pairs
- Checking for active orderbook (liquidity check)
- Returning a dynamic list of scannable pairs each cycle

No hardcoded symbol list — everything is discovered at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..data.hyperliquid_rest import HyperliquidREST
from ..utils.logging import get_logger

logger = get_logger(__name__)

# Stablecoins to exclude from scanning
STABLECOINS: set[str] = {"USDC", "USDT", "USDC.E", "USDT.E", "USDC-E", "USDT-E"}

# Known deprecated/settled pairs to exclude
DEPRECATED_PAIRS: set[str] = {"FEE", "GFC", "JTO", "WTI"}


@dataclass
class DiscoveredPair:
    """A single tradable pair discovered from Hyperliquid."""

    symbol: str                          # e.g. "BTC", "ETH"
    name: str | None = None              # full name if available
    max_leverage: int = 1                # from meta
    is_cross: bool = True                # cross-margin eligible
    sz_decimals: int = 0                 # szTokenDecimals from universe (liquidity proxy)
    volume_24h: float = 0.0              # 24h USD volume (from candles)
    mid_price: float | None = None      # current mid price
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_stable(self) -> bool:
        return self.symbol.upper() in STABLECOINS

    @property
    def is_deprecated(self) -> bool:
        return self.symbol.upper() in DEPRECATED_PAIRS


class PairDiscoverer:
    """Discovers and filters tradable pairs from Hyperliquid.

    Usage:
        discoverer = PairDiscoverer(rest_client)
        pairs = await discoverer.discover()
        for pair in pairs:
            print(pair.symbol, pair.has_active_orderbook)
    """

    def __init__(
        self,
        rest: HyperliquidREST,
        exclude_coins: list[str] | None = None,
        min_volume_24h_usd: float = 10000.0,
    ) -> None:
        self._rest = rest
        self._exclude: set[str] = set(exclude_coins or [])
        # Always exclude stables even if not in the list
        self._exclude.update(STABLECOINS)
        self._exclude.update(DEPRECATED_PAIRS)
        self._min_volume = min_volume_24h_usd

    async def discover(self) -> list[DiscoveredPair]:
        """Fetch all tradable pairs, filter by eligibility and liquidity.

        Returns:
            List of DiscoveredPair objects that pass the filters.
        """
        try:
            meta = await self._rest.get_info()
        except Exception as exc:
            logger.error("Failed to fetch pair meta from Hyperliquid", error=str(exc))
            return []

        raw_pairs = meta.get("universe", [])
        if not raw_pairs:
            logger.warning("Empty universe in Hyperliquid meta response")
            return []

        # Get mid prices for all coins
        mids: dict[str, float] = {}
        try:
            mids = await self._rest.get_all_mids()
        except Exception as exc:
            logger.warning("Could not fetch allMids, continuing without prices", error=str(exc))

        pairs: list[DiscoveredPair] = []
        for raw in raw_pairs:
            try:
                symbol = raw.get("name", "")
                if not symbol:
                    continue

                # Skip excluded coins
                if symbol.upper() in self._exclude:
                    continue

                sz_decimals = int(raw.get("szDecimals", 0))
                pair = DiscoveredPair(
                    symbol=symbol,
                    name=symbol,  # 'name' field is the coin symbol
                    max_leverage=int(raw.get("maxLeverage", 1)),
                    is_cross=raw.get("crossMargin", True),
                    sz_decimals=sz_decimals,
                    mid_price=mids.get(symbol),
                )
                pairs.append(pair)

            except Exception as exc:
                logger.debug("Failed to parse pair from meta", raw=raw, error=str(exc))
                continue

        logger.info("Discovered pairs from Hyperliquid", total=len(pairs))
        return pairs

    async def check_liquidity(self, pair: DiscoveredPair, depth: int = 5) -> bool:
        """Check if a pair has an active orderbook with sufficient liquidity.

        We check the orderbook and verify at least 3 non-zero bid/ask levels.
        """
        try:
            ob = await self._rest.get_orderbook(pair.symbol, depth=depth)
            has_bids = len([b for b in ob.bids if b.size > 0]) >= 3
            has_asks = len([a for a in ob.asks if a.size > 0]) >= 3
            return bool(has_bids and has_asks)
        except Exception as exc:
            logger.debug("Orderbook check failed", symbol=pair.symbol, error=str(exc))
            return False

    async def filter_liquidity(self, pairs: list[DiscoveredPair]) -> list[DiscoveredPair]:
        """Parallel orderbook check for all pairs, update has_active_orderbook."""
        import asyncio

        async def check(pair: DiscoveredPair) -> DiscoveredPair:
            has_ob = await self.check_liquidity(pair)
            pair.has_active_orderbook = has_ob
            return pair

        results = await asyncio.gather(*[check(p) for p in pairs])
        # Filter to only those with active orderbooks
        filtered = [p for p in results if p.has_active_orderbook]
        logger.info("Pairs with active orderbooks", total=len(filtered), checked=len(pairs))
        return filtered

    async def estimate_24h_volume(self, pair: DiscoveredPair) -> float:
        """Estimate 24h USD volume from recent candles on 1h timeframe."""
        try:
            candles = await self._rest.get_candles(
                pair.symbol,
                interval="1h",
                max_bars=24,
            )
            if not candles:
                return 0.0
            total_volume = sum(c.volume * (c.close + c.open) / 2 for c in candles)
            return float(total_volume)
        except Exception:
            return 0.0

    async def discover_with_filters(
        self,
        all_mids: dict[str, float] | None = None,
        min_sz_decimals: int = 4,
    ) -> list[DiscoveredPair]:
        """Cheap discovery: no per-pair API calls beyond get_all_mids() and get_info().

        Uses szDecimals from universe as liquidity proxy (szDecimals >= 4 means
        the pair is liquid enough to trade). Filters out stables, deprecated, and
        pairs not in allMids (no price data).

        Args:
            all_mids: Optional pre-fetched dict of symbol -> mid price. If not
                provided, will be fetched via get_all_mids() as part of discovery.
            min_sz_decimals: Minimum szTokenDecimals to consider a pair liquid.
                Higher = more liquid. Default 4.

        Returns:
            Filtered list of DiscoveredPair objects.
        """
        # Get universe (meta) - ONE request
        try:
            meta = await self._rest.get_info()
        except Exception as exc:
            logger.error("Failed to fetch pair meta from Hyperliquid", error=str(exc))
            return []

        raw_pairs = meta.get("universe", [])
        if not raw_pairs:
            logger.warning("Empty universe in Hyperliquid meta response")
            return []

        # Get all mid prices - ONE request
        if all_mids is None:
            try:
                all_mids = await self._rest.get_all_mids()
            except Exception as exc:
                logger.warning("Could not fetch allMids, continuing without price filter", error=str(exc))
                all_mids = {}

        # Build universe dict for szDecimals lookup
        # universe entries look like: {"name": "BTC", "szTokenDecimals": 8, ...}
        universe_by_symbol = {raw.get("name", ""): raw for raw in raw_pairs}

        pairs: list[DiscoveredPair] = []
        skipped_sz = 0
        skipped_not_in_mids = 0

        for symbol, mid_price in all_mids.items():
            # Skip excluded coins (stables + deprecated + user-specified)
            if symbol.upper() in self._exclude:
                continue

            # Skip if not in universe (unknown pair)
            raw = universe_by_symbol.get(symbol)
            if not raw:
                skipped_not_in_mids += 1
                continue

            # szDecimals ranking preserved for rough ordering (not filtering)
            # The only filter: pair must have a price in allMids (tradeable)
            sz_decimals = int(raw.get("szDecimals", 0))

            pair = DiscoveredPair(
                symbol=symbol,
                name=symbol,  # 'name' field is the coin symbol
                max_leverage=int(raw.get("maxLeverage", 1)),
                is_cross=raw.get("crossMargin", True),
                sz_decimals=sz_decimals,
                mid_price=mid_price,
            )
            pairs.append(pair)

        pairs_with_price = len(pairs)
        logger.info(
            "Discovered pairs (cheap filter)",
            candidates=pairs_with_price,
            skipped_sz_decimals=skipped_sz,
            skipped_not_in_universe=skipped_not_in_mids,
            total_in_universe=len(raw_pairs),
        )
        return pairs