"""Bybit USDT Linear Perpetual adapter via ccxt.

Maps ccxt's normalized data format to our abstract
`ExchangeAdapter` interface. Pattern mirrors `binance.py`:
ccxt-backed market data + stream + account, paper mode
without api keys, live mode with signed REST.

Bybit-specific notes:
  - v5 API is unified: spot, linear, inverse, options all
    under the same base URL (https://api.bybit.com).
  - Default category for USDT perps is `linear`.
  - Some endpoints have a 5-category param: spot, linear,
    inverse, option, index. We pass `category=linear` to
    the futures endpoints.
  - Pagination: list_positions, list_open_orders etc. return
    a `nextPageCursor` — we don't currently paginate because
    paper trading doesn't need that volume.

DNS / network access:
  Same Indonesia-workaround as Binance — DNS-over-HTTPS via
  Cloudflare 1.1.1.1 or Google 8.8.8.8. Configured via the
  `doh` field in config.
"""

from __future__ import annotations

import asyncio
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
    StreamAdapter,
    StreamEvent,
    SymbolInfo,
    Ticker,
    TransientError,
    VenueKind,
)

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────
# DNS-over-HTTPS helper (mirrors binance)
# ─────────────────────────────────────────────────────────────────────


def _build_aiohttp_connector_doh(doh: str) -> dict[str, Any]:
    if doh not in ("cloudflare", "google"):
        return {}
    nameservers = {
        "cloudflare": ["1.1.1.1", "1.0.0.1"],
        "google": ["8.8.8.8", "8.8.4.4"],
    }[doh]
    def factory(loop=None):
        try:
            from aiohttp.resolver import AsyncResolver
        except ImportError:
            return None
        return AsyncResolver(nameservers=nameservers, loop=loop)
    return {"connector_factory": factory}


# ─────────────────────────────────────────────────────────────────────
# Market data
# ─────────────────────────────────────────────────────────────────────


class BybitMarketData(MarketDataAdapter):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._category = cfg.get("category", "linear")  # 'linear' | 'inverse' | 'spot'
        self._doh = cfg.get("doh", "system")
        self._client: Any = None

    @property
    def venue(self) -> VenueKind:
        return VenueKind.BYBIT

    async def connect(self) -> None:
        import ccxt.async_support as ccxt_async
        opts: dict[str, Any] = {
            "defaultType": "swap" if self._category == "linear" else "spot",
            "defaultCategory": self._category,
            "enableRateLimit": True,
        }
        if self._doh != "system":
            d = _build_aiohttp_connector_doh(self._doh)
            factory = d.get("connector_factory")
            if factory is not None:
                try:
                    resolver = factory()
                    if resolver is not None:
                        opts["aiohttp_trust_env"] = False
                        opts.setdefault("connector_args", {})
                        opts["connector_args"]["resolver"] = resolver
                except RuntimeError as exc:
                    logger.warning(
                        "DoH resolver init failed; falling back to system",
                        error=str(exc),
                    )
        self._client = ccxt_async.bybit(opts)
        logger.info("Bybit market data: connected", doh=self._doh, category=self._category)

    async def close(self) -> None:
        if self._client is not None:
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
            if active_only and not info.get("active"):
                continue
            try:
                base = info["base"]
                quote = info["quote"]
            except KeyError:
                continue
            out.append(SymbolInfo(
                symbol=sym,
                base=base, quote=quote,
                venue=VenueKind.BYBIT,
                price_decimals=int(info.get("precision", {}).get("price", 8) or 8),
                size_decimals=int(info.get("precision", {}).get("amount", 4) or 4),
                min_size=float(info.get("limits", {}).get("amount", {}).get("min", 0) or 0),
                min_notional=float(info.get("limits", {}).get("cost", {}).get("min", 0) or 0),
                active=bool(info.get("active", True)),
                raw=info,
            ))
        return out

    async def get_candles(
        self, symbol: str, timeframe: str,
        start: datetime, end: datetime,
        limit: int | None = None,
    ) -> list[NormalizedCandle]:
        if self._client is None:
            raise ExchangeError("not connected")
        since_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        out: list[NormalizedCandle] = []
        cursor = since_ms
        # Bybit returns 200 per page max
        cap = min(200, limit or 200)
        while cursor < end_ms:
            try:
                raw = await self._client.fetch_ohlcv(
                    symbol, timeframe=timeframe, since=cursor, limit=cap,
                    params={"category": self._category},
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
                break
        return out

    async def get_orderbook(self, symbol: str, depth: int = 20) -> NormalizedOrderbook:
        if self._client is None:
            raise ExchangeError("not connected")
        try:
            ob = await self._client.fetch_order_book(symbol, limit=depth,
                params={"category": self._category})
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
            t = await self._client.fetch_ticker(symbol,
                params={"category": self._category})
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


class BybitStream(StreamAdapter):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._category = cfg.get("category", "linear")
        self._client: Any = None
        self._tasks: list[asyncio.Task] = []

    @property
    def venue(self) -> VenueKind:
        return VenueKind.BYBIT

    async def connect(self) -> None:
        import ccxt.async_support as ccxt_async
        self._client = ccxt_async.bybit({
            "defaultType": "swap" if self._category == "linear" else "spot",
            "defaultCategory": self._category,
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

    async def _spawn(self, kind: str, coro_factory, on_event):
        async def loop():
            while True:
                try:
                    async for data in coro_factory():
                        if not data:
                            continue
                        for sym, payload in data.items() if isinstance(data, dict) else [(None, data)]:
                            ev = StreamEvent(
                                kind=kind, symbol=sym or "",
                                ts=datetime.now(timezone.utc), data=payload,
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

    async def subscribe_orderbook(self, symbols, on_event):
        client = self._client
        async def factory():
            while True:
                yield await client.watch_order_book_for_symbols(
                    symbols, params={"category": self._category}
                )
        await self._spawn("orderbook", factory, on_event)

    async def subscribe_trades(self, symbols, on_event):
        client = self._client
        async def factory():
            while True:
                yield await client.watch_trades_for_symbols(
                    symbols, params={"category": self._category}
                )
        await self._spawn("trades", factory, on_event)

    async def subscribe_candles(self, symbols, timeframe, on_event):
        client = self._client
        async def factory():
            while True:
                yield await client.watch_ohlcv_for_symbols(
                    symbols, timeframe=timeframe,
                    params={"category": self._category},
                )
        await self._spawn("candles", factory, on_event)


# ─────────────────────────────────────────────────────────────────────
# Account
# ─────────────────────────────────────────────────────────────────────


class BybitAccount(AccountAdapter):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._api_key = cfg.get("api_key")
        self._api_secret = cfg.get("api_secret")
        self._category = cfg.get("category", "linear")
        self._client: Any = None

    @property
    def venue(self) -> VenueKind:
        return VenueKind.BYBIT

    async def connect(self) -> None:
        import ccxt.async_support as ccxt_async
        opts: dict[str, Any] = {
            "defaultType": "swap" if self._category == "linear" else "spot",
            "defaultCategory": self._category,
            "enableRateLimit": True,
        }
        if self._api_key and self._api_secret:
            opts["apiKey"] = self._api_key
            opts["secret"] = self._api_secret
            logger.info("Bybit account: live mode (api keys present)")
        else:
            logger.warning("Bybit account: paper mode (no api keys)")
        self._client = ccxt_async.bybit(opts)

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
            bal = await self._client.fetch_balance(params={"category": self._category})
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
            try:
                t = await self._client.fetch_ticker(
                    order.symbol, params={"category": self._category}
                )
            except Exception as exc:
                return OrderResult(
                    success=False,
                    error=f"ticker fetch failed for {order.symbol}: {exc}",
                )
            price = float(t.get("last") or 0)
            if price <= 0:
                return OrderResult(
                    success=False, error=f"no last price for {order.symbol}",
                )
            fee_rate = 0.00055  # Bybit taker 5.5 bps
            fee = price * order.size * fee_rate
            return OrderResult(
                success=True,
                order_id=order.client_order_id or f"bybit-paper-{order.symbol}",
                fill_price=price,
                filled_size=order.size,
                fees_paid=fee,
                fee_rate_bps=fee_rate * 10_000,
            )
        try:
            res = await self._client.create_order(
                symbol=order.symbol,
                type=order.order_type,
                side=order.side,
                amount=order.size,
                price=order.limit_price,
                params={
                    "category": self._category,
                    **({"clientOrderId": order.client_order_id} if order.client_order_id else {}),
                },
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
            await self._client.cancel_order(order_id, params={"category": self._category})
            return True
        except Exception:
            return False

    async def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        if self._client is None or self._is_paper():
            return []
        try:
            orders = await self._client.fetch_open_orders(
                symbol, params={"category": self._category}
            )
        except Exception:
            return []
        return orders


# ─────────────────────────────────────────────────────────────────────
# Composite
# ─────────────────────────────────────────────────────────────────────


class BybitAdapter(ExchangeAdapter):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._market_data = BybitMarketData(cfg)
        self._stream = BybitStream(cfg)
        self._account = BybitAccount(cfg)
        self._connected = False

    @property
    def venue(self) -> VenueKind:
        return VenueKind.BYBIT

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
        logger.info("Bybit adapter connected", category=self._market_data._category)

    async def close(self) -> None:
        await self._account.close()
        await self._stream.close()
        await self._market_data.close()
        self._connected = False
