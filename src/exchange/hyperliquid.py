"""Hyperliquid reference implementation of ExchangeAdapter.

Wraps the existing `HyperliquidREST` and `HyperliquidWebSocket`
classes so the bot's higher layers (orchestrator, signals,
executor) can depend on the abstract interface rather than
Hyperliquid specifics.

This is the only adapter that goes live today. The ccxt-based
CEX adapters (`binance.py`, `bybit.py`, `gate.py`) are stubs
for the multi-venue work.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from ..data.hyperliquid_rest import HyperliquidREST
from ..data.hyperliquid_ws import HyperliquidWebSocket, WSMessage
from ..data.models import (
    NormalizedCandle,
    NormalizedOrderbook,
    OrderbookLevel,
    TimeFrame,
)
from ..utils.logging import get_logger
from .base import (
    AccountAdapter,
    Balance,
    ExchangeAdapter,
    MarketDataAdapter,
    OrderRequest,
    OrderResult,
    StreamAdapter,
    StreamEvent,
    SymbolInfo,
    Ticker,
    VenueKind,
)

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Market data
# ─────────────────────────────────────────────────────────────────────


class HyperliquidMarketData(MarketDataAdapter):
    """Hyperliquid REST market data, behind the MarketDataAdapter
    interface."""

    def __init__(self) -> None:
        self._rest = HyperliquidREST()

    @property
    def venue(self) -> VenueKind:
        return VenueKind.HYPERLIQUID

    async def connect(self) -> None:
        await self._rest.connect()

    async def close(self) -> None:
        await self._rest.close()

    async def list_symbols(self, active_only: bool = True) -> list[SymbolInfo]:
        # Reuse the existing pair discovery path
        mids = await self._rest.get_all_mids()
        # We don't have a single "list all perps" endpoint, so
        # we mirror the data layer's PairDiscoverer output.
        # For now, return a SymbolInfo for each symbol we have
        # a mid price for, with conservative defaults.
        # (Production-ready adapter would call /info metaAndAssetCtxs.)
        out: list[SymbolInfo] = []
        for sym in mids:
            out.append(SymbolInfo(
                symbol=sym, base=sym, quote="USD",
                venue=VenueKind.HYPERLIQUID,
                price_decimals=5, size_decimals=4,
                min_size=0.0, min_notional=10.0,
                active=True, raw={"mid": mids[sym]},
            ))
        if active_only:
            out = [s for s in out if s.active]
        return out

    async def get_candles(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> list[NormalizedCandle]:
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        return await self._rest.get_candles(
            symbol=symbol,
            interval=timeframe,
            start_time=start_ms,
            end_time=end_ms,
            max_bars=limit or 1500,
        )

    async def get_orderbook(self, symbol: str, depth: int = 20) -> NormalizedOrderbook:
        raw = await self._rest.get_orderbook(symbol, depth=depth)
        # The REST client's NormalizedOrderbook already has the
        # right shape; we just re-emit.
        return raw

    async def get_ticker(self, symbol: str) -> Ticker:
        mids = await self._rest.get_all_mids()
        if symbol not in mids:
            raise KeyError(f"symbol not found on Hyperliquid: {symbol}")
        last = float(mids[symbol])
        # Hyperliquid's mid is a mark; real bid/ask come from the
        # orderbook. For the adapter interface, fall back to mid
        # for both sides if the orderbook isn't cached.
        ob = await self.get_orderbook(symbol, depth=1)
        bid = ob.bids[0].price if ob.bids else last
        ask = ob.asks[0].price if ob.asks else last
        return Ticker(
            symbol=symbol, bid=bid, ask=ask, last=last,
            ts=datetime.utcnow(),
        )


# ─────────────────────────────────────────────────────────────────────
# Streaming
# ─────────────────────────────────────────────────────────────────────


class HyperliquidStream(StreamAdapter):
    """Hyperliquid WebSocket, behind the StreamAdapter interface.

    Adapts the existing `HyperliquidWebSocket` (which uses an
    internal handler-dispatch model) to the callback-based
    StreamAdapter interface.
    """

    def __init__(self) -> None:
        self._ws = HyperliquidWebSocket()
        self._callbacks: dict[str, list[Callable[[StreamEvent], None]]] = {
            "orderbook": [], "trades": [], "candles": [],
        }

    @property
    def venue(self) -> VenueKind:
        return VenueKind.HYPERLIQUID

    async def connect(self) -> None:
        await self._ws.connect()

    async def close(self) -> None:
        await self._ws.close()

    async def _subscribe(
        self,
        kind: str,
        symbols: list[str],
        on_event: Callable[[StreamEvent], None],
    ) -> None:
        self._callbacks[kind].append(on_event)
        if kind == "orderbook":
            for sym in symbols:
                await self._ws.subscribe_orderbook(sym)
        elif kind == "candles":
            # Hyperliquid candles come per (symbol, interval).
            # We default to 1m since the interface doesn't expose
            # an interval parameter; the bot's rest layer handles
            # higher TFs.
            for sym in symbols:
                await self._ws.subscribe_candles(sym, "1m")
        # trades: Hyperliquid doesn't have a separate public-trade
        # WS feed in our existing client; skip.

    async def subscribe_orderbook(
        self, symbols: list[str], on_event: Callable[[StreamEvent], None]
    ) -> None:
        await self._subscribe("orderbook", symbols, on_event)

    async def subscribe_trades(
        self, symbols: list[str], on_event: Callable[[StreamEvent], None]
    ) -> None:
        # Not yet implemented for Hyperliquid; stubs for the
        # interface. Could be added by enabling the trades WS
        # in HyperliquidWebSocket.
        logger.debug("subscribe_trades not implemented for Hyperliquid yet")

    async def subscribe_candles(
        self, symbols: list[str], timeframe: str,
        on_event: Callable[[StreamEvent], None],
    ) -> None:
        self._callbacks["candles"].append(on_event)
        for sym in symbols:
            await self._ws.subscribe_candles(sym, timeframe)

    def dispatch(self, kind: str, msg: WSMessage) -> None:
        """Bridge from HyperliquidWebSocket's message dispatch to
        StreamEvent callbacks. Called by the orchestrator after
        wiring up the WS."""
        if not self._callbacks[kind]:
            return
        ev = StreamEvent(
            kind=kind,
            symbol=msg.raw.get("data", {}).get("coin", ""),
            ts=msg.ts if hasattr(msg, "ts") else datetime.utcnow(),
            data=msg.raw.get("data", {}),
        )
        for cb in self._callbacks[kind]:
            cb(ev)


# ─────────────────────────────────────────────────────────────────────
# Account (paper only — Hyperliquid has no auth in this repo)
# ─────────────────────────────────────────────────────────────────────


class HyperliquidAccountStub(AccountAdapter):
    """Stub adapter for Hyperliquid account ops.

    The existing bot is paper-only and doesn't sign orders
    with Hyperliquid. This stub returns zeros for balances
    and rejects order placement. A real implementation would
    use Hyperliquid's signed REST endpoint to place orders.
    """

    @property
    def venue(self) -> VenueKind:
        return VenueKind.HYPERLIQUID

    async def get_balances(self) -> list[Balance]:
        return [Balance(asset="USDC", free=0.0, locked=0.0)]

    async def place_order(self, order: OrderRequest) -> OrderResult:
        return OrderResult(
            success=False,
            error="Hyperlive order placement not implemented; use paper executor",
        )

    async def cancel_order(self, order_id: str) -> bool:
        return False

    async def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        return []


# ─────────────────────────────────────────────────────────────────────
# Composite
# ─────────────────────────────────────────────────────────────────────


class HyperliquidAdapter(ExchangeAdapter):
    """Concrete ExchangeAdapter for Hyperliquid."""

    def __init__(self) -> None:
        self._market_data = HyperliquidMarketData()
        self._stream = HyperliquidStream()
        self._account = HyperliquidAccountStub()
        self._connected = False

    @property
    def venue(self) -> VenueKind:
        return VenueKind.HYPERLIQUID

    @property
    def market_data(self) -> MarketDataAdapter:
        return self._market_data

    @property
    def stream(self) -> StreamAdapter:
        return self._stream

    @property
    def account(self) -> AccountAdapter:
        return self._account

    async def connect(self) -> None:
        if self._connected:
            return
        await self._market_data.connect()
        await self._stream.connect()
        self._connected = True
        logger.info("Hyperliquid adapter connected")

    async def close(self) -> None:
        await self._stream.close()
        await self._market_data.close()
        self._connected = False
