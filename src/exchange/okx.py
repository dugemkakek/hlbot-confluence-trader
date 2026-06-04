"""OKX exchange adapter — ccxt-backed, paper-mode when no API keys.

Public market data and (in live mode) authenticated trading on
OKX's USDT-margined swap (perpetual) and spot markets.

Paper mode
----------
When `api_key` / `api_secret` / `passphrase` are absent, the
adapter engages paper mode automatically. `place_order` returns
a synthetic fill at the last ticker price; `get_balances` returns
a mock USDT balance. No real orders are placed.

Symbol mapping
--------------
OKX symbols use a hyphenated format ("BTC-USDT" for spot,
"BTC-USDT-SWAP" for linear perps). PaperExecutor's mapping
helper translates "BTC" -> "BTC-USDT" by default.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .base import (
    AccountAdapter,
    Balance,
    ExchangeAdapter,
    ExchangeError,
    MarketDataAdapter,
    OrderRequest,
    OrderResult,
    StreamAdapter,
    SymbolInfo,
    Ticker,
    VenueKind,
)
from .doh_plumbing import build_aiohttp_connector_doh
from ..utils.logging import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _okx_interval_to_ms(interval: str) -> int | None:
    """Convert a candle interval string to milliseconds for OKX."""
    s = interval.strip().lower()
    if s.endswith("m"):
        return int(s[:-1]) * 60 * 1000
    if s.endswith("h"):
        return int(s[:-1]) * 60 * 60 * 1000
    if s.endswith("d"):
        return int(s[:-1]) * 24 * 60 * 60 * 1000
    if s.endswith("w"):
        return int(s[:-1]) * 7 * 24 * 60 * 60 * 1000
    return None


# ─────────────────────────────────────────────────────────────────────
# Market data
# ─────────────────────────────────────────────────────────────────────


class OKXMarketData(MarketDataAdapter):
    """ccxt-backed OKX market data, behind MarketDataAdapter."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._market_type = cfg.get("market_type", "usdt-m-future")
        self._doh = cfg.get("doh", "system")
        self._client: Any = None

    @property
    def venue(self) -> VenueKind:
        return VenueKind.OKX

    async def connect(self) -> None:
        import ccxt.async_support as ccxt_async

        # OKX uses 'swap' for linear perpetuals, 'spot' for spot.
        opts: dict[str, Any] = {
            "defaultType": (
                "swap" if self._market_type == "usdt-m-future"
                else "spot" if self._market_type == "spot"
                else "swap"
            ),
            "enableRateLimit": True,
        }
        if self._doh != "system":
            factory = build_aiohttp_connector_doh(self._doh)
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
        self._client = ccxt_async.okx(opts)
        logger.info(
            "OKX market data: connected",
            doh=self._doh, market_type=self._market_type,
        )

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
            raise ExchangeError(f"OKX load_markets failed: {exc}") from exc
        out: list[SymbolInfo] = []
        for sym, m in markets.items():
            if active_only and not m.get("active", True):
                continue
            out.append(SymbolInfo(
                symbol=sym,
                base=m.get("base", ""),
                quote=m.get("quote", ""),
                venue=VenueKind.OKX,
                active=m.get("active", True),
                min_size=m.get("limits", {}).get("amount", {}).get("min"),
                tick_size=m.get("limits", {}).get("price", {}).get("min"),
            ))
        return out

    async def get_candles(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch OHLCV candles for `symbol` in [start, end].

        OKX paginates at 100 candles/call. We loop until we
        cover the requested range or hit the per-call cap.
        """
        if self._client is None:
            raise ExchangeError("not connected")
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        tf_ms = _okx_interval_to_ms(timeframe)
        if tf_ms is None:
            raise ExchangeError(f"OKX unsupported timeframe: {timeframe}")
        page = min(limit or 100, 100)
        all_rows: list[list[float]] = []
        cursor = start_ms
        while cursor < end_ms:
            try:
                batch = await self._client.fetch_ohlcv(
                    symbol, timeframe=timeframe,
                    since=cursor, limit=page,
                )
            except Exception as exc:
                raise ExchangeError(f"OKX fetch_ohlcv failed: {exc}") from exc
            if not batch:
                break
            all_rows.extend(batch)
            # Advance cursor past the last candle's close
            last_ts = int(batch[-1][0])
            if last_ts + tf_ms <= cursor:
                break  # no progress
            cursor = last_ts + tf_ms
        return [
            {
                "timestamp": datetime.fromtimestamp(int(r[0]) / 1000, tz=timezone.utc),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
            }
            for r in all_rows
        ]

    async def get_ticker(self, symbol: str) -> Ticker:
        if self._client is None:
            raise ExchangeError("not connected")
        t = await self._client.fetch_ticker(symbol)
        return Ticker(
            symbol=symbol,
            bid=float(t.get("bid") or 0),
            ask=float(t.get("ask") or 0),
            last=float(t.get("last") or 0),
            volume_24h=float(t.get("quoteVolume") or 0),
            timestamp=int(t.get("timestamp") or 0),
        )

    async def get_orderbook(self, symbol: str, depth: int = 20) -> dict[str, Any]:
        """Return bids/asks as list of [price, size] pairs.

        OKX's native orderbook has separate bids/asks arrays. We
        keep the same shape BinanceAdapter uses so the calling
        code stays uniform.
        """
        if self._client is None:
            raise ExchangeError("not connected")
        ob = await self._client.fetch_order_book(symbol, limit=depth)
        return {
            "bids": [[float(p), float(s)] for p, s in ob.get("bids", [])[:depth]],
            "asks": [[float(p), float(s)] for p, s in ob.get("asks", [])[:depth]],
            "timestamp": int(ob.get("timestamp") or 0),
        }


# ─────────────────────────────────────────────────────────────────────
# Stream (placeholder — paper mode does not need a real WS)
# ─────────────────────────────────────────────────────────────────────


class OKXStream(StreamAdapter):
    @property
    def venue(self) -> VenueKind:
        return VenueKind.OKX

    async def connect(self) -> None:
        # OKX WebSocket lives at wss://ws.okx.com:8443/ws/v5/public.
        # We don't open a connection in paper mode; the polling
        # loop in PaperExecutor is enough for our 1h/4h strategy.
        logger.info("OKX stream: paper mode (no live WS)")

    async def close(self) -> None:
        return

    async def subscribe_orderbook(
        self,
        symbols: list[str],
        on_event: Any,
    ) -> None:
        # No-op in paper mode.
        return

    async def subscribe_trades(
        self,
        symbols: list[str],
        on_event: Any,
    ) -> None:
        # No-op in paper mode.
        return

    async def subscribe_candles(
        self,
        symbols: list[str],
        timeframe: str,
        on_event: Any,
    ) -> None:
        # No-op in paper mode.
        return


# ─────────────────────────────────────────────────────────────────────
# Account
# ─────────────────────────────────────────────────────────────────────


class OKXAccount(AccountAdapter):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._api_key: str | None = cfg.get("api_key")
        self._api_secret: str | None = cfg.get("api_secret")
        self._passphrase: str | None = cfg.get("passphrase")
        self._client: Any = None

    @property
    def venue(self) -> VenueKind:
        return VenueKind.OKX

    def _is_paper(self) -> bool:
        return not (self._api_key and self._api_secret and self._passphrase)

    async def connect(self) -> None:
        if self._is_paper():
            logger.info("OKX account: paper mode (no api keys)")
            return
        import ccxt.async_support as ccxt_async
        self._client = ccxt_async.okx({
            "apiKey": self._api_key,
            "secret": self._api_secret,
            "password": self._passphrase,
            "enableRateLimit": True,
        })
        logger.info("OKX account: live mode")

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    async def get_balances(self) -> list[Balance]:
        if self._client is None:
            raise ExchangeError("not connected")
        if self._is_paper():
            return [Balance("USDT", free=10_000.0, locked=0.0)]
        try:
            bals = await self._client.fetch_balance()
        except Exception as exc:
            raise ExchangeError(f"OKX fetch_balance failed: {exc}") from exc
        out: list[Balance] = []
        for asset, info in bals.get("total", {}).items():
            free = float(bals.get("free", {}).get(asset, 0) or 0)
            locked = float(bals.get("used", {}).get(asset, 0) or 0)
            if free or locked:
                out.append(Balance(asset, free=free, locked=locked))
        return out

    async def place_order(self, order: OrderRequest) -> OrderResult:
        if self._is_paper():
            # Paper: fill at last ticker price
            try:
                if self._client is None:
                    import ccxt.async_support as ccxt_async
                    self._client = ccxt_async.okx({"enableRateLimit": True})
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
            fee_rate = 0.00035  # OKX taker fee ~3.5 bps
            fee = price * order.size * fee_rate
            return OrderResult(
                success=True,
                order_id=order.client_order_id or f"okx-paper-{order.symbol}",
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
                params={"clOrdId": order.client_order_id} if order.client_order_id else {},
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


class OKXAdapter(ExchangeAdapter):
    """Composite OKX adapter (market data + stream + account)."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._market_data = OKXMarketData(self._config)
        self._stream = OKXStream()
        self._account = OKXAccount(self._config)
        self._connected = False

    @property
    def venue(self) -> VenueKind:
        return VenueKind.OKX

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
        logger.info("OKX adapter connected", market_type=self._market_data._market_type)

    async def close(self) -> None:
        await self._account.close()
        await self._stream.close()
        await self._market_data.close()
        self._connected = False
