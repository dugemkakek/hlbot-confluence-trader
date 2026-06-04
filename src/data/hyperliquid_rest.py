"""Async REST client for Hyperliquid API.

Handles:
- REST API calls for historical OHLCV, trades, orderbook, and info
- Rate limiting (10 req/s)
- Automatic retry with backoff
- Response validation
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .models import NormalizedOrderbook, OrderbookLevel

import aiohttp

from ..utils.logging import get_logger
from ..utils.config import get_config
from .models import NormalizedCandle, NormalizedOrderbook, NormalizedTrade, TimeFrame
from ..utils.datetime_utils import ms_to_dt

logger = get_logger(__name__)


class RateLimiter:
    """Token bucket rate limiter for API calls."""

    def __init__(self, rate: float, burst: int = 1) -> None:
        """Initialize rate limiter.

        Args:
            rate: Calls per second.
            burst: Maximum burst size.
        """
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Acquire a token, waiting if necessary."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last_update = now
            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate
                await asyncio.sleep(wait)
                self.tokens = 0.0
            else:
                self.tokens -= 1


class HyperliquidREST:
    """Async REST client for Hyperliquid exchange API.

    Endpoints:
        POST /info - General exchange info
        POST /candles - Historical OHLCV data
        POST /trades - Historical trades
        POST /orderbook - L2 orderbook snapshot
        POST /trade_sz - Position and margin info
    """

    RATE_LIMIT = 10  # req/s
    MAX_RETRIES = 3
    BASE_URL = "https://api.hyperliquid.xyz"

    def __init__(self, base_url: str | None = None) -> None:
        """Initialize REST client.

        Args:
            base_url: Override base URL (defaults to config).
        """
        cfg = get_config()
        self.base_url = base_url or cfg.hyperliquid.rest_url
        self._session: aiohttp.ClientSession | None = None
        self._rate_limiter = RateLimiter(rate=self.RATE_LIMIT)

    async def __aenter__(self) -> "HyperliquidREST":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit."""
        await self.close()

    async def connect(self) -> None:
        """Create aiohttp session (lazy, idempotent)."""
        if self._session:
            return
        self._session = aiohttp.ClientSession(
            base_url=self.base_url,
            timeout=aiohttp.ClientTimeout(total=10),
        )
        logger.info("REST client connected", base_url=self.base_url)

    async def close(self) -> None:
        """Close aiohttp session."""
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("REST client closed")

    async def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Make a POST request with rate limiting and retry."""
        await self._rate_limiter.acquire()

        if not self._session:
            raise RuntimeError("REST client not connected. Call connect() first.")

        last_error: Exception | None = None
        for attempt in range(self.MAX_RETRIES):
            url = self._session.url / endpoint if hasattr(self._session, 'url') else f"{self.base_url}{endpoint}"
            logger.debug("HL REST request", endpoint=endpoint, url=str(url), payload=payload)
            try:
                async with self._session.post(endpoint, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if "code" in data and data["code"] != 0:
                            logger.warning(
                                "API error response",
                                code=data["code"],
                                endpoint=endpoint,
                            )
                        return data
                    elif resp.status == 429:
                        wait = 2 ** attempt
                        logger.warning("Rate limited", attempt=attempt, wait=wait)
                        await asyncio.sleep(wait)
                        continue
                    else:
                        text = await resp.text()
                        raise aiohttp.ClientResponseError(
                            resp.request_info,
                            resp.history,
                            status=resp.status,
                            message=text,
                        )
            except aiohttp.ClientError as e:
                last_error = e
                logger.warning(
                    "Request failed, retrying",
                    endpoint=endpoint,
                    attempt=attempt,
                    error=str(e),
                )
                await asyncio.sleep(2 ** attempt)

        raise RuntimeError(
            f"Failed after {self.MAX_RETRIES} retries: {last_error}"
        ) from last_error

    # ---- API Methods ----

    async def get_info(self) -> dict[str, Any]:
        """Get general exchange info (symbols, perpetuals, etc.)."""
        return await self._post("/info", {"type": "meta"})

    async def get_candles(
        self,
        symbol: str,
        interval: str = "1m",
        start_time: int | None = None,
        end_time: int | None = None,
        max_bars: int = 500,
    ) -> list[NormalizedCandle]:
        """Get historical OHLCV candles.

        Args:
            symbol: Trading pair (e.g. "BTC").
            interval: Timeframe (1m, 5m, 15m, 1h, 4h, 1d).
            start_time: Start time in milliseconds.
            end_time: End time in milliseconds.
            max_bars: Maximum number of bars to return.

        Returns:
            List of NormalizedCandle objects.
        """
        import time as time_module

        now_ms = int(time_module.time() * 1000)
        req: dict[str, Any] = {
            "coin": symbol,
            "interval": interval,
        }
        if start_time:
            req["startTime"] = start_time
        else:
            # Default to ~30 days back if no start time
            req["startTime"] = now_ms - (30 * 24 * 60 * 60 * 1000)

        if end_time:
            req["endTime"] = end_time
        else:
            req["endTime"] = now_ms

        payload: dict[str, Any] = {
            "type": "candleSnapshot",
            "req": req,
        }

        # candleSnapshot returns a raw list directly, not {data: [...]}
        raw_candles: list[dict[str, Any]] = await self._post("/info", payload)
        if not isinstance(raw_candles, list):
            logger.warning("Unexpected candle response type", type=type(raw_candles))
            return []
        candles = []
        for raw in reversed(raw_candles[-max_bars:]):
            candle = NormalizedCandle(
                symbol=symbol,
                timeframe=TimeFrame(interval),
                timestamp=ms_to_dt(raw["t"]),
                open=float(raw["o"]),
                high=float(raw["h"]),
                low=float(raw["l"]),
                close=float(raw["c"]),
                volume=float(raw["v"]),
                raw=raw,
            )
            candles.append(candle)
        return candles

    async def get_trades(
        self,
        symbol: str,
        start_time: int | None = None,
        end_time: int | None = None,
        max_trades: int = 500,
    ) -> list[NormalizedTrade]:
        """Get historical trades.

        Args:
            symbol: Trading pair.
            start_time: Start time in ms.
            end_time: End time in ms.
            max_trades: Max trades to return.

        Returns:
            List of NormalizedTrade objects.
        """
        payload: dict[str, Any] = {
            "type": "trades",
            "coin": symbol,
        }
        if start_time:
            payload["startTime"] = start_time
        if end_time:
            payload["endTime"] = end_time

        data = await self._post("/info", payload)
        raw_trades = data.get("data", [])
        trades = []
        for raw in raw_trades[-max_trades:]:
            side = "BUY" if raw.get("side", "").lower() == "buy" else "SELL"
            trade = NormalizedTrade(
                symbol=symbol,
                timestamp=ms_to_dt(raw["t"]),
                price=float(raw["px"]),
                size=float(raw["sz"]),
                side=side,
                trade_id=str(raw.get("hash", "")),
                raw=raw,
            )
            trades.append(trade)
        return trades

    async def get_orderbook(self, symbol: str, depth: int = 10) -> NormalizedOrderbook:
        """Get L2 orderbook snapshot.

        Args:
            symbol: Trading pair.
            depth: Number of price levels per side.

        Returns:
            NormalizedOrderbook.
        """
        payload = {
            "type": "l2Book",
            "coin": symbol,
            "depth": depth,
        }
        data = await self._post("/info", payload)
        # Hyperliquid L2 response: {"coin": "BTC", "time": N, "levels": [[[bid_entries], [ask_entries]]]}
        raw = data if isinstance(data, dict) else {}
        raw_levels: list[list[list[Any]]] = raw.get("levels", [])
        bid_entries = raw_levels[0] if len(raw_levels) > 0 else []
        ask_entries = raw_levels[1] if len(raw_levels) > 1 else []

        bids = [
            OrderbookLevel(price=float(l["px"]), size=float(l["sz"]))
            for l in bid_entries[:depth]
        ]
        asks = [
            OrderbookLevel(price=float(l["px"]), size=float(l["sz"]))
            for l in ask_entries[:depth]
        ]
        return NormalizedOrderbook(
            symbol=symbol,
            bids=bids,
            asks=asks,
            raw=raw,
        )

    async def get_all_mids(self) -> dict[str, float]:
        """Get all mid prices (used for mark price).
        
        Response: {"BTC": "73722.0", "ETH": "2480.5", ...} — direct dict, no wrapper.
        """
        data = await self._post("/info", {"type": "allMids"})
        mids: dict[str, float] = {}
        if not isinstance(data, dict):
            return mids
        for k, v in data.items():
            # Skip synthetic/index keys like "#1000", "@1", etc.
            if k.startswith("#") or k.startswith("@"):
                continue
            try:
                mids[k] = float(v)
            except ValueError:
                pass
        return mids
