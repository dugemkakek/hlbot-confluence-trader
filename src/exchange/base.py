"""Abstract base classes for the exchange adapter framework.

The bot's orchestrator, executor, signals, and backtest harness
all depend on these interfaces — not on a specific venue. To
add a new venue (or to swap paper trading for a different
sandbox), implement `ExchangeAdapter` and register it in
`factory.py`.

Design notes:
  - Sync + async split. Market data methods are async because
    the bot already runs in an asyncio loop. Order placement is
    async for the same reason. Streaming is a callback-based
    abstraction so adapters can use their preferred transport
    (websockets, polling, etc.) without us caring.
  - Errors as exceptions. Adapters raise their own exception
    types but the base defines `ExchangeError` for callers
    that want to catch any.
  - All monetary values are in quote currency (USD for perp
    futures against USDT/USDC). Sizes are in base units.
  - Timestamps are tz-aware UTC datetimes. Adapters convert
    from venue-specific representations.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, AsyncIterator, Callable, Optional


# ─────────────────────────────────────────────────────────────────────
# Common types
# ─────────────────────────────────────────────────────────────────────


class VenueKind(str, Enum):
    """Supported venue types."""

    HYPERLIQUID = "hyperliquid"
    BINANCE = "binance"
    BYBIT = "bybit"
    GATE = "gate"
    OKX = "okx"
    PAPER = "paper"  # in-memory, for tests


class ExchangeError(Exception):
    """Base class for all exchange errors."""


class TransientError(ExchangeError):
    """Retryable error (rate limit, network blip, exchange downtime)."""


class PermanentError(ExchangeError):
    """Non-retryable error (bad symbol, auth failure, etc.)."""


@dataclass
class SymbolInfo:
    """Static metadata about a tradable symbol on a venue."""

    symbol: str
    base: str
    quote: str
    venue: VenueKind
    price_decimals: int = 8
    size_decimals: int = 4
    min_size: float = 0.0
    min_notional: float = 0.0
    active: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Ticker:
    """Latest price/size snapshot for a symbol."""

    symbol: str
    bid: float
    ask: float
    last: float
    ts: datetime
    raw: dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────
# Market data
# ─────────────────────────────────────────────────────────────────────


class MarketDataAdapter(abc.ABC):
    """Async market-data interface.

    All methods are async; the bot's main loop is already async.
    Concrete adapters translate between their venue's transport
    (httpx, aiohttp, websockets, etc.) and this interface.
    """

    @property
    @abc.abstractmethod
    def venue(self) -> VenueKind:
        """Which venue this adapter is for."""

    @abc.abstractmethod
    async def connect(self) -> None:
        """Open connections, warm caches, etc. Idempotent."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Tear down connections."""

    @abc.abstractmethod
    async def list_symbols(self, active_only: bool = True) -> list[SymbolInfo]:
        """Discover all tradable symbols on this venue."""

    @abc.abstractmethod
    async def get_candles(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> list[Any]:
        """Fetch OHLCV candles in [start, end].

        Returns NormalizedCandle objects. Implementations should
        handle pagination internally for venues that cap response
        sizes (Hyperliquid ~500 per call, Binance/Bybit 1000).
        """

    @abc.abstractmethod
    async def get_orderbook(
        self,
        symbol: str,
        depth: int = 20,
    ) -> Any:
        """Fetch a snapshot of the orderbook at `depth` levels per side."""

    @abc.abstractmethod
    async def get_ticker(self, symbol: str) -> Ticker:
        """Latest bid/ask/last for `symbol`."""


# ─────────────────────────────────────────────────────────────────────
# Streaming
# ─────────────────────────────────────────────────────────────────────


@dataclass
class StreamEvent:
    """A single update from a streaming subscription."""

    kind: str             # 'orderbook' | 'trade' | 'candle' | 'fill'
    symbol: str
    ts: datetime
    data: dict[str, Any] = field(default_factory=dict)


class StreamAdapter(abc.ABC):
    """Streaming interface for live data.

    Streaming is callback-based so adapters can pick their
    preferred transport. The bot subscribes once per venue, the
    adapter multiplexes the events, and the bot's handler
    routes by `kind` + `symbol`.
    """

    @property
    @abc.abstractmethod
    def venue(self) -> VenueKind:
        ...

    @abc.abstractmethod
    async def connect(self) -> None:
        ...

    @abc.abstractmethod
    async def close(self) -> None:
        ...

    @abc.abstractmethod
    async def subscribe_orderbook(
        self,
        symbols: list[str],
        on_event: Callable[[StreamEvent], None],
    ) -> None:
        """Subscribe to orderbook updates for `symbols`."""

    @abc.abstractmethod
    async def subscribe_trades(
        self,
        symbols: list[str],
        on_event: Callable[[StreamEvent], None],
    ) -> None:
        """Subscribe to public trade tape for `symbols`."""

    @abc.abstractmethod
    async def subscribe_candles(
        self,
        symbols: list[str],
        timeframe: str,
        on_event: Callable[[StreamEvent], None],
    ) -> None:
        """Subscribe to real-time candle updates for `symbols`."""


# ─────────────────────────────────────────────────────────────────────
# Account + order placement (used by paper executor + future live)
# ─────────────────────────────────────────────────────────────────────


@dataclass
class Balance:
    """Account balance for one asset."""

    asset: str
    free: float
    locked: float

    @property
    def total(self) -> float:
        return self.free + self.locked


@dataclass
class OrderRequest:
    """Specification for a new order."""

    symbol: str
    side: str             # "buy" or "sell"
    size: float           # base units
    order_type: str = "market"  # "market" | "limit"
    limit_price: float | None = None
    client_order_id: str | None = None
    reduce_only: bool = False


@dataclass
class OrderResult:
    """Result of an order placement."""

    success: bool
    order_id: str | None = None
    fill_price: float | None = None
    filled_size: float = 0.0
    fees_paid: float = 0.0
    fee_rate_bps: float = 0.0
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class AccountAdapter(abc.ABC):
    """Async account + order placement.

    Paper trading uses this with simulated fills. Real trading
    would route through ccxt or a venue's signed REST endpoint.
    """

    @property
    @abc.abstractmethod
    def venue(self) -> VenueKind:
        ...

    @abc.abstractmethod
    async def get_balances(self) -> list[Balance]:
        ...

    @abc.abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResult:
        """Submit an order. Returns OrderResult with fill details if
        the order filled (market orders do; limit orders may not)."""

    @abc.abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        ...

    @abc.abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        ...


# ─────────────────────────────────────────────────────────────────────
# Composite adapter
# ─────────────────────────────────────────────────────────────────────


class ExchangeAdapter(abc.ABC):
    """A single venue combining market data, streaming, and account.

    The bot typically only uses `market_data` and `stream`
    in production. `account` is included for live trading and
    for the paper executor (which uses AccountAdapter directly
    to simulate fills).
    """

    @property
    @abc.abstractmethod
    def venue(self) -> VenueKind:
        ...

    @property
    @abc.abstractmethod
    def market_data(self) -> MarketDataAdapter:
        ...

    @property
    @abc.abstractmethod
    def stream(self) -> StreamAdapter:
        ...

    @property
    @abc.abstractmethod
    def account(self) -> AccountAdapter:
        ...

    @abc.abstractmethod
    async def connect(self) -> None:
        ...

    @abc.abstractmethod
    async def close(self) -> None:
        ...
