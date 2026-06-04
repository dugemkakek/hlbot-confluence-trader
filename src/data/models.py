"""Pydantic models for normalized market data.

All internal data structures use these models for type safety
and validation across the trading stack.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Side(str, Enum):
    """Trade side."""

    BUY = "BUY"
    SELL = "SELL"


class OrderSide(str, Enum):
    """Order side."""

    LONG = "LONG"
    SHORT = "SHORT"


class TimeFrame(str, Enum):
    """Standardized timeframes (lowercase to match Hyperliquid API)."""

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


class OrderbookLevel(BaseModel):
    """Single price level in orderbook."""

    price: float
    size: float


class OrderbookSnapshot(BaseModel):
    """Full orderbook snapshot at a point in time."""

    symbol: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    bids: list[OrderbookLevel] = Field(default_factory=list)
    asks: list[OrderbookLevel] = Field(default_factory=list)

    @property
    def best_bid(self) -> float | None:
        """Best bid price."""
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        """Best ask price."""
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        """Bid-ask spread."""
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None


class NormalizedOrderbook(BaseModel):
    """Orderbook data from Hyperliquid WebSocket (used internally by WS client)."""

    symbol: str
    bids: list[OrderbookLevel] = Field(default_factory=list)
    asks: list[OrderbookLevel] = Field(default_factory=list)
    raw: dict[str, Any] | None = None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None


class Trade(BaseModel):
    """Raw Hyperliquid trade event."""

    raw: dict[str, Any]


class NormalizedTrade(BaseModel):
    """Standardized trade format used internally."""

    symbol: str
    timestamp: datetime
    price: float
    size: float
    side: Side
    trade_id: str
    raw: dict[str, Any] | None = None


class Candle(BaseModel):
    """Raw Hyperliquid OHLCV candle."""

    raw: dict[str, Any]


class NormalizedCandle(BaseModel):
    """Standardized OHLCV candle used internally."""

    symbol: str
    timeframe: TimeFrame
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    raw: dict[str, Any] | None = None

    @property
    def is_bullish(self) -> bool:
        """Candle close > open."""
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        """Candle close < open."""
        return self.close < self.open

    @property
    def body_size(self) -> float:
        """Absolute candle body size."""
        return abs(self.close - self.open)

    @property
    def range_size(self) -> float:
        """Full candle range (high - low)."""
        return self.high - self.low


class Signal(BaseModel):
    """Trading signal output by a signal module."""

    name: str
    symbol: str
    timeframe: TimeFrame
    direction: Side  # BUY or SELL
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    def __repr__(self) -> str:
        return (
            f"Signal(name={self.name!r}, symbol={self.symbol!r}, "
            f"direction={self.direction.value}, confidence={self.confidence:.2f})"
        )


class OrderType(str, Enum):
    """Order type."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    TAKE_PROFIT = "TAKE_PROFIT"


class OrderStatus(str, Enum):
    """Order status."""

    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class SimulatedOrder(BaseModel):
    """Paper trading order record."""

    order_id: str
    symbol: str
    side: OrderSide
    size: float
    price: float | None
    order_type: OrderType
    status: OrderStatus = OrderStatus.PENDING
    filled_size: float = 0.0
    avg_fill_price: float | None = None
    slippage_bps: float = 0.0
    fee_bps: float = 0.0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Position(BaseModel):
    """Open position record."""

    symbol: str
    side: OrderSide
    size: float
    entry_price: float
    current_price: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    exposure: float
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PortfolioSummary(BaseModel):
    """Portfolio-level summary."""

    total_equity: float
    cash_balance: float
    unrealized_pnl: float
    realized_pnl: float
    total_pnl: float
    margin_used: float
    exposure: float
    exposure_pct: float
    positions: list[Position] = Field(default_factory=list)


class Regime(str, Enum):
    """Market regime classification.

    16 named regimes decomposed along three axes (trend / vol / liquidity).
    See `signals/regime_detector.py` for the full taxonomy and trading presets.
    The legacy 7-value enum (TREND_UP, TREND_DOWN, RANGE_BOUND, HIGH_VOL,
    LOW_VOL, LOW_LIQUIDITY, UNKNOWN) is preserved for backward compatibility
    with older callers / logs.
    """

    # ── Legacy regimes (kept for backward compatibility) ─────────────────────
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE_BOUND = "RANGE_BOUND"
    HIGH_VOL = "HIGH_VOL"
    LOW_VOL = "LOW_VOL"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"

    # ── Trend × Volatility regimes (NORMAL/DEEP liquidity assumed) ───────────
    STRONG_TREND_STABLE_VOL = "STRONG_TREND_STABLE_VOL"
    STRONG_TREND_EXPANDING_VOL = "STRONG_TREND_EXPANDING_VOL"
    WEAK_TREND_STABLE_VOL = "WEAK_TREND_STABLE_VOL"
    WEAK_TREND_CONTRACTING_VOL = "WEAK_TREND_CONTRACTING_VOL"
    RANGING_STABLE_VOL = "RANGING_STABLE_VOL"
    RANGING_LOW_VOL = "RANGING_LOW_VOL"
    RANGING_HIGH_VOL = "RANGING_HIGH_VOL"
    CHOPPY_CONTRACTING_VOL = "CHOPPY_CONTRACTING_VOL"
    CHOPPY_EXPANDING_VOL = "CHOPPY_EXPANDING_VOL"

    # ── Transitional / setup regimes ────────────────────────────────────────
    BREAKOUT_ATTEMPT = "BREAKOUT_ATTEMPT"
    REVERSAL_SETUP = "REVERSAL_SETUP"

    # ── Safety / hazard regimes (always override trend × vol) ───────────────
    VOL_SPIKE = "VOL_SPIKE"
    LIQUIDITY_CRISIS = "LIQUIDITY_CRISIS"
    VOLUME_ANOMALY = "VOLUME_ANOMALY"
    MARKET_DISTORTION = "MARKET_DISTORTION"

    UNKNOWN = "UNKNOWN"


class Decision(BaseModel):
    """Decision engine output."""

    action: str = Field(description="BUY, SELL, or HOLD")
    symbol: str
    size: float = 0.0
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    confidence: float = 0.0
    regime: Regime = Regime.UNKNOWN
    signals: list[Signal] = Field(default_factory=list)
    reason: str = ""
    timestamp: datetime = Field(default_factory=datetime.utcnow)
