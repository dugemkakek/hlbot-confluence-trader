"""In-memory paper exchange adapter.

Used for unit tests and as a placeholder for venues that
aren't yet implemented. Every method works against local
state, no network calls.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any, Callable

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


class PaperMarketData(MarketDataAdapter):
    """Empty market data. Override with synthetic candles for tests."""

    @property
    def venue(self) -> VenueKind:
        return VenueKind.PAPER

    async def connect(self) -> None: pass
    async def close(self) -> None: pass

    async def list_symbols(self, active_only: bool = True) -> list[SymbolInfo]:
        return []

    async def get_candles(self, symbol, timeframe, start, end, limit=None) -> list:
        return []

    async def get_orderbook(self, symbol, depth=20) -> Any:
        return None

    async def get_ticker(self, symbol) -> Ticker:
        raise KeyError(symbol)


class PaperStream(StreamAdapter):
    @property
    def venue(self) -> VenueKind:
        return VenueKind.PAPER

    async def connect(self) -> None: pass
    async def close(self) -> None: pass
    async def subscribe_orderbook(self, symbols, on_event) -> None: pass
    async def subscribe_trades(self, symbols, on_event) -> None: pass
    async def subscribe_candles(self, symbols, timeframe, on_event) -> None: pass


class PaperAccount(AccountAdapter):
    """Tracks USD balance, supports market orders at last-tick prices."""

    def __init__(self) -> None:
        self._balances: dict[str, Balance] = {"USD": Balance("USD", free=10_000.0, locked=0.0)}
        self._orders: dict[str, dict] = {}
        self._prices: dict[str, float] = {}

    @property
    def venue(self) -> VenueKind:
        return VenueKind.PAPER

    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    def set_balance(self, asset: str, free: float, locked: float = 0.0) -> None:
        self._balances[asset] = Balance(asset=asset, free=free, locked=locked)

    async def get_balances(self) -> list[Balance]:
        return list(self._balances.values())

    async def place_order(self, order: OrderRequest) -> OrderResult:
        price = self._prices.get(order.symbol)
        if price is None:
            return OrderResult(success=False, error=f"no price for {order.symbol}")
        fee_rate = 0.00035  # 3.5 bps taker
        # Longs cost cash; shorts add cash (mirrors the backtest engine).
        if order.side == "buy":
            cost = price * order.size
            self._balances["USD"].free -= cost
            fill_price = price
        else:
            # Short sale
            self._balances["USD"].free += price * order.size
            fill_price = price
        fees = price * order.size * fee_rate
        self._balances["USD"].free -= fees
        order_id = order.client_order_id or f"paper-{uuid.uuid4().hex[:8]}"
        result = OrderResult(
            success=True,
            order_id=order_id,
            fill_price=fill_price,
            filled_size=order.size,
            fees_paid=fees,
            fee_rate_bps=fee_rate * 10_000,
        )
        self._orders[order_id] = {
            "order_id": order_id,
            "symbol": order.symbol,
            "side": order.side,
            "size": order.size,
            "fill_price": fill_price,
            "fees": fees,
            "ts": datetime.utcnow().isoformat(),
        }
        return result

    async def cancel_order(self, order_id: str) -> bool:
        return self._orders.pop(order_id, None) is not None

    async def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        return list(self._orders.values())


class PaperAdapter(ExchangeAdapter):
    """Composite paper adapter. Useful for tests."""

    def __init__(self) -> None:
        self._market = PaperMarketData()
        self._stream = PaperStream()
        self._account = PaperAccount()
        self._connected = False

    @property
    def venue(self) -> VenueKind:
        return VenueKind.PAPER

    @property
    def market_data(self):
        return self._market

    @property
    def stream(self):
        return self._stream

    @property
    def account(self):
        return self._account

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False
