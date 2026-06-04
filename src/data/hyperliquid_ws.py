"""Async WebSocket client for Hyperliquid market data.

Handles:
- Connection management with auto-reconnect (exponential backoff)
- Subscription management (trades, candles, orderbook, fills)
- Message normalization to Pydantic models
- Async event emission to registered callbacks
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

import websockets
from websockets.client import WebSocketClientProtocol

from ..utils.logging import get_logger
from ..utils.config import get_config
from .models import (
    NormalizedCandle,
    NormalizedOrderbook,
    NormalizedTrade,
    OrderbookLevel,
    Side,
    TimeFrame,
)
from ..utils.datetime_utils import ms_to_dt

logger = get_logger(__name__)


@dataclass
class WSSubscription:
    """Active WebSocket subscription."""

    name: str
    symbol: str
    channel: str
    subscription_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class WSMessage:
    """Parsed WebSocket message."""

    channel: str
    data: dict[str, Any]
    raw: dict[str, Any]


# Type for message handlers
MessageHandler = Callable[[WSMessage], Awaitable[None]]


class HyperliquidWebSocket:
    """Async WebSocket client for Hyperliquid.

    Connects to the Hyperliquid WebSocket API and normalizes incoming
    market data events. Handles reconnection with exponential backoff.

    Example:
        async with HyperliquidWebSocket() as ws:
            await ws.subscribe("trades", "BTC")
            async for msg in ws.messages():
                process(msg)
    """

    MAX_RECONNECT_DELAY = 60.0
    INITIAL_RECONNECT_DELAY = 1.0

    def __init__(
        self,
        ws_url: str | None = None,
        reconnect: bool = True,
        max_reconnect_delay: float = MAX_RECONNECT_DELAY,
    ) -> None:
        """Initialize the WebSocket client.

        Args:
            ws_url: WebSocket URL. Defaults to config hyperliquid.ws_url.
            reconnect: Whether to auto-reconnect on disconnect.
            max_reconnect_delay: Maximum reconnect delay in seconds.
        """
        cfg = get_config()
        self.ws_url = ws_url or cfg.hyperliquid.ws_url
        self.reconnect = reconnect
        self.max_reconnect_delay = max_reconnect_delay

        self._ws: WebSocketClientProtocol | None = None
        self._reader_task: asyncio.Task | None = None
        self._handlers: dict[str, list[MessageHandler]] = {}
        self._subscriptions: dict[str, WSSubscription] = {}
        self._running = False
        self._reconnect_delay = self.INITIAL_RECONNECT_DELAY
        self._closing = False
        self._msg_queue: asyncio.Queue[WSMessage | None] = field(
            default_factory=asyncio.Queue
        )

    async def __aenter__(self) -> "HyperliquidWebSocket":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit."""
        await self.close()

    async def connect(self) -> None:
        """Establish WebSocket connection and start reader loop."""
        logger.info("Connecting to Hyperliquid WebSocket", url=self.ws_url)
        self._ws = await websockets.connect(self.ws_url, ping_interval=20)
        self._running = True
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._reconnect_delay = self.INITIAL_RECONNECT_DELAY
        logger.info("WebSocket connected")

    async def close(self) -> None:
        """Close WebSocket connection gracefully."""
        self._closing = True
        self._running = False
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("WebSocket closed")

    async def _reader_loop(self) -> None:
        """Read messages from WebSocket and dispatch to handlers."""
        try:
            while self._running and self._ws:
                try:
                    raw = await self._ws.recv()
                    msg = self._parse_message(raw)
                    if msg:
                        await self._dispatch(msg)
                except websockets.exceptions.ConnectionClosed as e:
                    if self._closing:
                        break
                    logger.warning("WebSocket disconnected", reason=str(e))
                    await self._maybe_reconnect()
                    break
                except Exception as e:
                    logger.error("Error reading WebSocket message", error=str(e))
        except asyncio.CancelledError:
            pass

    async def _maybe_reconnect(self) -> None:
        """Attempt reconnection with exponential backoff."""
        if not self.reconnect or self._closing:
            return
        logger.info(
            "Attempting reconnect",
            delay=self._reconnect_delay,
        )
        await asyncio.sleep(self._reconnect_delay)
        try:
            await self.connect()
            # Resubscribe to all active subscriptions
            for sub in self._subscriptions.values():
                await self._send_subscribe(sub.channel, sub.symbol)
            self._reconnect_delay = self.INITIAL_RECONNECT_DELAY
        except Exception as e:
            self._reconnect_delay = min(
                self._reconnect_delay * 2, self.max_reconnect_delay
            )
            logger.error(
                "Reconnect failed",
                error=str(e),
                next_delay=self._reconnect_delay,
            )
            await self._maybe_reconnect()

    def _parse_message(self, raw: str | bytes) -> WSMessage | None:
        """Parse raw WebSocket JSON message."""
        try:
            data = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
            # Hyperliquid sends subscription confirmations and data on same channel
            if "channel" in data and "data" in data:
                return WSMessage(channel=data["channel"], data=data["data"], raw=data)
            elif "type" in data:
                # Some message types don't have channel field
                channel = data.get("channel", data.get("type", "unknown"))
                return WSMessage(channel=channel, data=data, raw=data)
            return None
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to parse WebSocket message", error=str(e))
            return None

    async def _dispatch(self, msg: WSMessage) -> None:
        """Dispatch message to registered handlers."""
        handlers = self._handlers.get(msg.channel, [])
        for handler in handlers:
            try:
                await handler(msg)
            except Exception as e:
                logger.error(
                    "Handler error",
                    channel=msg.channel,
                    error=str(e),
                )

    async def _send_subscribe(self, channel: str, symbol: str) -> None:
        """Send subscription request to Hyperliquid WebSocket."""
        if not self._ws:
            return
        payload = {
            "method": "subscribe",
            "subscription": {"type": channel, "coin": symbol},
        }
        await self._ws.send(json.dumps(payload))
        logger.info("Subscribed", channel=channel, symbol=symbol)

    async def subscribe(
        self,
        channel: str,
        symbol: str,
        handler: MessageHandler | None = None,
    ) -> str:
        """Subscribe to a market data channel.

        Args:
            channel: Channel name (trades, candles, l2_book, fills).
            symbol: Trading pair symbol (e.g. "BTC").
            handler: Optional async handler for messages on this channel.

        Returns:
            Subscription ID.
        """
        sub = WSSubscription(name=f"{channel}:{symbol}", symbol=symbol, channel=channel)
        self._subscriptions[sub.subscription_id] = sub

        if handler:
            self._handlers.setdefault(channel, []).append(handler)

        await self._send_subscribe(channel, symbol)
        return sub.subscription_id

    async def unsubscribe(self, subscription_id: str) -> None:
        """Unsubscribe by subscription ID."""
        sub = self._subscriptions.pop(subscription_id, None)
        if sub:
            channel = sub.channel
            if self._handlers.get(channel):
                # Remove last handler for simplicity (ideally track by sub)
                self._handlers[channel] = self._handlers[channel][:-1]

    async def subscribe_trades(self, symbol: str) -> str:
        """Subscribe to trade tape for a symbol."""
        return await self.subscribe("trades", symbol)

    async def subscribe_candles(self, symbol: str, timeframe: str = "1m") -> str:
        """Subscribe to OHLCV candles for a symbol."""
        return await self.subscribe("candles", symbol)

    async def subscribe_orderbook(self, symbol: str) -> str:
        """Subscribe to L2 orderbook for a symbol."""
        return await self.subscribe("l2Book", symbol)

    async def subscribe_fills(self, symbol: str) -> str:
        """Subscribe to user fill events (for live trading, not paper)."""
        return await self.subscribe("fills", symbol)

    # ---- Normalization methods ----

    def normalize_trade(self, data: dict[str, Any], symbol: str) -> NormalizedTrade | None:
        """Normalize Hyperliquid trade data to internal format."""
        try:
            # Hyperliquid trade format: [time_ms, coin, side, size, price, hash]
            time_ms = data.get("T", 0)
            side_str = data.get("side", "")
            side = Side.BUY if side_str.lower() == "buy" else Side.SELL

            return NormalizedTrade(
                symbol=symbol,
                timestamp=ms_to_dt(time_ms),
                price=float(data["px"]),
                size=float(data["sz"]),
                side=side,
                trade_id=str(data.get("hash", "")),
                raw=data,
            )
        except (KeyError, ValueError) as e:
            logger.warning("Failed to normalize trade", error=str(e), data=data)
            return None

    def normalize_candle(
        self, data: dict[str, Any], symbol: str, timeframe: str
    ) -> NormalizedCandle | None:
        """Normalize Hyperliquid candle data to internal format."""
        try:
            # Hyperliquid candle format: {t, T, s, i, o, h, l, v, x, n}
            # t=start time, T=end time, s=symbol, i=interval, o=open, h=high, l=low, v=volume, x=closed, n=num_trades
            tf = TimeFrame(timeframe)
            return NormalizedCandle(
                symbol=symbol,
                timeframe=tf,
                timestamp=ms_to_dt(data["t"]),
                open=float(data["o"]),
                high=float(data["h"]),
                low=float(data["l"]),
                close=float(data["c"]),
                volume=float(data["v"]),
                raw=data,
            )
        except (KeyError, ValueError) as e:
            logger.warning("Failed to normalize candle", error=str(e), data=data)
            return None

    def normalize_orderbook(
        self, data: dict[str, Any], symbol: str
    ) -> NormalizedOrderbook | None:
        """Normalize Hyperliquid L2 orderbook data to internal format."""
        try:
            raw_levels = data.get("levels", [])
            bids = [
                OrderbookLevel(price=float(l["px"]), size=float(l["n"]))
                for l in raw_levels[0]
            ]
            asks = [
                OrderbookLevel(price=float(l["px"]), size=float(l["n"]))
                for l in raw_levels[1]
            ]
            return NormalizedOrderbook(
                symbol=symbol,
                bids=bids,
                asks=asks,
                raw=data,
            )
        except (KeyError, IndexError) as e:
            logger.warning("Failed to normalize orderbook", error=str(e), data=data)
            return None
