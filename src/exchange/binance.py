"""Binance USDT-M Futures adapter via ccxt.

Maps ccxt's normalized data format to our abstract
`ExchangeAdapter` interface. Supports:
  - Market data: candles, orderbook, ticker, symbol list
  - Streaming: ccxt's built-in WebSocket support
  - Account: place_order via signed REST (real trading)
  - Paper mode: simulates fills against the last ticker price

DNS / network access
--------------------
Binance is geo-blocked in some regions. The user's local
DNS may return a sinkhole or block api.binance.com. This
adapter supports DNS-over-HTTPS (DoH) to bypass local DNS:

  - Default: Cloudflare 1.1.1.1 (https://1.1.1.1/dns-query)
  - Fallback: Google 8.8.8.8 (https://dns.google/dns-query)

Configure via `config/base.yaml`:

  exchange:
    venue: binance
    binance:
      doh: cloudflare   # or 'google', 'system'
      market_type: usdt-m-future  # or 'spot', 'coin-m-future'

`doh: system` falls back to the OS resolver (no override).
The OS resolver is used by default because it's the fastest;
set to `cloudflare` if you see DNS errors in the bot logs.

Auth keys (api_key, api_secret) are only required for
authenticated endpoints (place_order, get_balances,
get_open_orders). Public market data works without them.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

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
    ExchangeError,
    MarketDataAdapter,
    OrderRequest,
    OrderResult,
    PermanentError,
    StreamAdapter,
    StreamEvent,
    SymbolInfo,
    Ticker,
    TransientError,
    VenueKind,
)

logger = get_logger(__name__)


# ccxt timeframe mapping: "1h" → "1h", "4h" → "4h", etc.
# Our internal TimeFrame enum values already match ccxt.
def _ccxt_timeframe(tf: str) -> str:
    return tf


# ─────────────────────────────────────────────────────────────────────
# DNS-over-HTTPS resolver (now in src.exchange.doh_plumbing)
# ─────────────────────────────────────────────────────────────────────

from .doh_plumbing import build_aiohttp_connector_doh as _build_aiohttp_connector_doh  # noqa: F401


# ─────────────────────────────────────────────────────────────────────
# Market data
# ─────────────────────────────────────────────────────────────────────


class BinanceMarketData(MarketDataAdapter):
    """ccxt-backed Binance market data, behind MarketDataAdapter."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._market_type = cfg.get("market_type", "usdt-m-future")
        self._doh = cfg.get("doh", "system")
        self._client: Any = None
        self._owns_client = True

    @property
    def venue(self) -> VenueKind:
        return VenueKind.BINANCE

    async def connect(self) -> None:
        import ccxt.async_support as ccxt_async

        opts: dict[str, Any] = {
            "defaultType": (
                "future" if self._market_type == "usdt-m-future"
                else "spot" if self._market_type == "spot"
                else "delivery" if self._market_type == "coin-m-future"
                else "future"
            ),
            "enableRateLimit": True,
        }
        # Apply DoH if configured. We expose the resolver
        # as a factory that the ccxt async client wraps into
        # aiohttp's TCPConnector. The factory runs inside
        # an event loop, which is what AsyncResolver requires.
        if self._doh != "system":
            d = _build_aiohttp_connector_doh(self._doh)
            factory = d.get("connector_factory")
            if factory is not None:
                # ccxt doesn't expose a direct hook for the
                # connector resolver. The aiohttp_connector
                # approach: pre-build the resolver and inject
                # into aiohttp's default. Fall back to OS DNS
                # if anything fails.
                try:
                    resolver = factory()
                    if resolver is not None:
                        opts["aiohttp_trust_env"] = False
                        # Newer ccxt supports `connector_args` —
                        # if present, we use it. Otherwise we
                        # fall back to the OS resolver.
                        opts.setdefault("connector_args", {})
                        # aiohttp's TCPConnector takes `resolver`
                        opts["connector_args"]["resolver"] = resolver
                except RuntimeError as exc:
                    logger.warning(
                        "DoH resolver init failed; falling back to system",
                        error=str(exc),
                    )

        self._client = ccxt_async.binance(opts)
        if self._doh != "system":
            logger.info(
                "Binance market data: using DoH resolver",
                doh=self._doh,
            )
        else:
            logger.info("Binance market data: using system DNS")

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    async def list_symbols(self, active_only: bool = True) -> list[SymbolInfo]:
        if self._client is None:
            raise ExchangeError("not connected")
        try:
            markets = await self._client.load_markets()
        except Exception as exc:
            raise TransientError(f"load_markets failed: {exc}") from exc

        out: list[SymbolInfo] = []
        for sym, info in markets.items():
            # Filter to USDT-M futures for our default
            if self._market_type == "usdt-m-future" and not info.get("swap"):
                continue
            if self._market_type == "spot" and info.get("swap"):
                continue
            if active_only and not info.get("active"):
                continue
            try:
                base = info["base"]
                quote = info["quote"]
            except KeyError:
                continue
            out.append(SymbolInfo(
                symbol=sym,
                base=base,
                quote=quote,
                venue=VenueKind.BINANCE,
                price_decimals=int(info.get("precision", {}).get("price", 8) or 8),
                size_decimals=int(info.get("precision", {}).get("amount", 4) or 4),
                min_size=float(info.get("limits", {}).get("amount", {}).get("min", 0) or 0),
                min_notional=float(info.get("limits", {}).get("cost", {}).get("min", 0) or 0),
                active=bool(info.get("active", True)),
                raw=info,
            ))
        return out

    async def get_candles(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> list[NormalizedCandle]:
        if self._client is None:
            raise ExchangeError("not connected")
        tf = _ccxt_timeframe(timeframe)
        # ccxt wants millisecond timestamps
        since_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        # Cap to 1000 per call (Binance's hard limit)
        out: list[NormalizedCandle] = []
        cursor = since_ms
        while cursor < end_ms:
            cap = min(1000, limit or 1000)
            try:
                raw = await self._client.fetch_ohlcv(
                    symbol, timeframe=tf, since=cursor, limit=cap,
                )
            except Exception as exc:
                raise TransientError(f"fetch_ohlcv failed: {exc}") from exc
            if not raw:
                break
            for row in raw:
                ts_ms, o, h, l, c, v = row
                if ts_ms > end_ms:
                    break
                out.append(NormalizedCandle(
                    symbol=symbol,
                    timeframe=TimeFrame(timeframe),
                    timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                    open=float(o), high=float(h), low=float(l),
                    close=float(c), volume=float(v),
                ))
            cursor = int(raw[-1][0]) + 1
            if len(raw) < cap:
                break  # exhausted
        return out

    async def get_orderbook(self, symbol: str, depth: int = 20) -> NormalizedOrderbook:
        if self._client is None:
            raise ExchangeError("not connected")
        try:
            ob = await self._client.fetch_order_book(symbol, limit=depth)
        except Exception as exc:
            raise TransientError(f"fetch_order_book failed: {exc}") from exc
        bids = [OrderbookLevel(price=float(b[0]), size=float(b[1])) for b in ob.get("bids", [])]
        asks = [OrderbookLevel(price=float(a[0]), size=float(a[1])) for a in ob.get("asks", [])]
        ts_ms = ob.get("timestamp")
        ts = (
            datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            if ts_ms else datetime.now(timezone.utc)
        )
        return NormalizedOrderbook(
            symbol=symbol, bids=bids, asks=asks, timestamp=ts, raw=ob,
        )

    async def get_ticker(self, symbol: str) -> Ticker:
        if self._client is None:
            raise ExchangeError("not connected")
        try:
            t = await self._client.fetch_ticker(symbol)
        except Exception as exc:
            raise TransientError(f"fetch_ticker failed: {exc}") from exc
        return Ticker(
            symbol=symbol,
            bid=float(t.get("bid") or 0),
            ask=float(t.get("ask") or 0),
            last=float(t.get("last") or 0),
            ts=datetime.fromtimestamp((t.get("timestamp") or 0) / 1000, tz=timezone.utc),
            raw=t,
        )


# ─────────────────────────────────────────────────────────────────────
# Streaming
# ─────────────────────────────────────────────────────────────────────


class BinanceStream(StreamAdapter):
    """ccxt watch_* based streaming.

    ccxt's async API uses long-poll WebSockets under the hood.
    Adapters call `await client.watch_order_book(...)` etc.
    and we route results through the StreamEvent callback.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._market_type = cfg.get("market_type", "usdt-m-future")
        self._client: Any = None
        self._tasks: list[asyncio.Task] = []

    @property
    def venue(self) -> VenueKind:
        return VenueKind.BINANCE

    async def connect(self) -> None:
        import ccxt.async_support as ccxt_async
        self._client = ccxt_async.binance({
            "defaultType": "future" if self._market_type == "usdt-m-future" else "spot",
            "enableRateLimit": True,
        })

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    async def _spawn(
        self, kind: str, coro_factory: Callable, symbols: list[str],
        on_event: Callable[[StreamEvent], None],
    ) -> None:
        async def loop():
            while True:
                try:
                    async for sym_data in coro_factory():
                        if not sym_data:
                            continue
                        # sym_data is a dict {symbol: payload}
                        for sym, payload in sym_data.items():
                            ev = StreamEvent(
                                kind=kind, symbol=sym,
                                ts=datetime.now(timezone.utc),
                                data=payload,
                            )
                            try:
                                on_event(ev)
                            except Exception as exc:
                                logger.debug("stream callback error", error=str(exc))
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.warning("stream loop error", kind=kind, error=str(exc))
                    await asyncio.sleep(1.0)

        t = asyncio.create_task(loop())
        self._tasks.append(t)

    async def subscribe_orderbook(
        self, symbols: list[str], on_event: Callable[[StreamEvent], None]
    ) -> None:
        client = self._client
        async def factory():
            while True:
                yield await client.watch_order_book_for_symbols(symbols)
        await self._spawn("orderbook", factory, symbols, on_event)

    async def subscribe_trades(
        self, symbols: list[str], on_event: Callable[[StreamEvent], None]
    ) -> None:
        client = self._client
        async def factory():
            while True:
                yield await client.watch_trades_for_symbols(symbols)
        await self._spawn("trades", factory, symbols, on_event)

    async def subscribe_candles(
        self, symbols: list[str], timeframe: str,
        on_event: Callable[[StreamEvent], None],
    ) -> None:
        client = self._client
        async def factory():
            while True:
                yield await client.watch_ohlcv_for_symbols(symbols, timeframe=timeframe)
        await self._spawn("candles", factory, symbols, on_event)


# ─────────────────────────────────────────────────────────────────────
# Account
# ─────────────────────────────────────────────────────────────────────


class BinanceAccount(AccountAdapter):
    """ccxt-backed Binance account ops.

    For PAPER trading (no api_key), we simulate fills against
    the last ticker price. For LIVE trading, pass api_key +
    api_secret in the exchange config and ccxt will sign the
    REST calls.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._api_key = cfg.get("api_key")
        self._api_secret = cfg.get("api_secret")
        self._market_type = cfg.get("market_type", "usdt-m-future")
        self._client: Any = None

    @property
    def venue(self) -> VenueKind:
        return VenueKind.BINANCE

    async def connect(self) -> None:
        import ccxt.async_support as ccxt_async
        opts: dict[str, Any] = {
            "defaultType": "future" if self._market_type == "usdt-m-future" else "spot",
            "enableRateLimit": True,
        }
        if self._api_key and self._api_secret:
            opts["apiKey"] = self._api_key
            opts["secret"] = self._api_secret
            logger.info("Binance account: live mode (api keys present)")
        else:
            logger.warning("Binance account: paper mode (no api keys)")
        self._client = ccxt_async.binance(opts)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    def _is_paper(self) -> bool:
        return not (self._api_key and self._api_secret)

    async def get_balances(self) -> list[Balance]:
        if self._client is None:
            raise ExchangeError("not connected")
        if self._is_paper():
            return [Balance("USDT", free=10_000.0, locked=0.0)]
        try:
            bal = await self._client.fetch_balance()
        except Exception as exc:
            raise TransientError(f"fetch_balance failed: {exc}") from exc
        out: list[Balance] = []
        total = bal.get("total") or {}
        free = bal.get("free") or {}
        used = bal.get("used") or {}
        for asset in set(total) | set(free) | set(used):
            out.append(Balance(
                asset=asset,
                free=float(free.get(asset, 0) or 0),
                locked=float(used.get(asset, 0) or 0),
            ))
        return out

    async def place_order(self, order: OrderRequest) -> OrderResult:
        if self._client is None:
            raise ExchangeError("not connected")

        if self._is_paper():
            # Paper: fill at last ticker price
            try:
                t = await self._client.fetch_ticker(order.symbol)
            except Exception as exc:
                return OrderResult(
                    success=False,
                    error=f"ticker fetch failed for {order.symbol}: {exc}",
                )
            price = float(t.get("last") or 0)
            if price <= 0:
                return OrderResult(
                    success=False,
                    error=f"no last price for {order.symbol}",
                )
            fee_rate = 0.00035
            fee = price * order.size * fee_rate
            return OrderResult(
                success=True,
                order_id=order.client_order_id or f"binance-paper-{order.symbol}",
                fill_price=price,
                filled_size=order.size,
                fees_paid=fee,
                fee_rate_bps=fee_rate * 10_000,
            )

        # Live: route to ccxt
        try:
            res = await self._client.create_order(
                symbol=order.symbol,
                type=order.order_type,
                side=order.side,
                amount=order.size,
                price=order.limit_price,
                params={"clientOrderId": order.client_order_id} if order.client_order_id else {},
            )
        except Exception as exc:
            return OrderResult(success=False, error=str(exc))

        return OrderResult(
            success=True,
            order_id=str(res.get("id", "")),
            fill_price=float(res.get("average") or res.get("price") or 0),
            filled_size=float(res.get("filled", order.size)),
            fees_paid=float(res.get("fee", {}).get("cost", 0) or 0),
            raw=res,
        )

    async def cancel_order(self, order_id: str) -> bool:
        if self._client is None or self._is_paper():
            return True
        try:
            await self._client.cancel_order(order_id)
            return True
        except Exception:
            return False

    async def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        if self._client is None:
            return []
        if self._is_paper():
            return []
        try:
            orders = await self._client.fetch_open_orders(symbol)
        except Exception:
            return []
        return orders


# ─────────────────────────────────────────────────────────────────────
# Composite
# ─────────────────────────────────────────────────────────────────────


class BinanceAdapter(ExchangeAdapter):
    """Composite Binance adapter (market data + stream + account)."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._market_data = BinanceMarketData(cfg)
        self._stream = BinanceStream(cfg)
        self._account = BinanceAccount(cfg)
        self._connected = False

    @property
    def venue(self) -> VenueKind:
        return VenueKind.BINANCE

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
        await self._account.connect()
        self._connected = True
        logger.info("Binance adapter connected", market_type=self._market_data._market_type)

    async def close(self) -> None:
        await self._account.close()
        await self._stream.close()
        await self._market_data.close()
        self._connected = False
