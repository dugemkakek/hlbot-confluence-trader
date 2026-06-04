"""Async PostgreSQL storage layer for market data using asyncpg.

Handles:
- Connection pooling
- OHLCV candle storage (with hypertable support)
- Trade tape storage
- Orderbook snapshots
- Efficient time-series queries
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg

from ..utils.logging import get_logger
from ..utils.config import get_config
from .models import NormalizedCandle, NormalizedOrderbook, NormalizedTrade

logger = get_logger(__name__)


class Database:
    """Async PostgreSQL database layer using asyncpg.

    Manages connection pooling and provides typed methods for
    market data storage and retrieval.
    """

    SCHEMA_SQL = """
    -- OHLCV candles with composite primary key
    CREATE TABLE IF NOT EXISTS ohlcv (
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        timestamp TIMESTAMPTZ NOT NULL,
        open NUMERIC NOT NULL,
        high NUMERIC NOT NULL,
        low NUMERIC NOT NULL,
        close NUMERIC NOT NULL,
        volume NUMERIC NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (symbol, timeframe, timestamp)
    );

    -- Indexes for time-series queries
    CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_time
        ON ohlcv (symbol, timeframe, timestamp DESC);

    -- Trades table
    CREATE TABLE IF NOT EXISTS trades (
        id SERIAL PRIMARY KEY,
        symbol TEXT NOT NULL,
        timestamp TIMESTAMPTZ NOT NULL,
        price NUMERIC NOT NULL,
        size NUMERIC NOT NULL,
        side TEXT NOT NULL,
        trade_id TEXT UNIQUE,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_trades_symbol_time
        ON trades (symbol, timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_trades_trade_id ON trades (trade_id);

    -- Orders table (paper trading)
    CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        size NUMERIC NOT NULL,
        price NUMERIC,
        order_type TEXT NOT NULL,
        status TEXT NOT NULL,
        filled_size NUMERIC DEFAULT 0,
        avg_fill_price NUMERIC,
        slippage_bps NUMERIC DEFAULT 0,
        fee_bps NUMERIC DEFAULT 0,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders (symbol);
    CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);

    -- Trade journal
    CREATE TABLE IF NOT EXISTS trade_journal (
        id SERIAL PRIMARY KEY,
        order_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        size NUMERIC NOT NULL,
        entry_price NUMERIC NOT NULL,
        exit_price NUMERIC,
        pnl NUMERIC,
        pnl_pct NUMERIC,
        fees NUMERIC DEFAULT 0,
        strategy_name TEXT,
        signal_reason JSONB,
        regime TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        closed_at TIMESTAMPTZ
    );

    CREATE INDEX IF NOT EXISTS idx_journal_symbol ON trade_journal (symbol);
    CREATE INDEX IF NOT EXISTS idx_journal_created ON trade_journal (created_at DESC);

    -- Performance metrics
    CREATE TABLE IF NOT EXISTS performance_log (
        id SERIAL PRIMARY KEY,
        timestamp TIMESTAMPTZ DEFAULT NOW(),
        total_equity NUMERIC NOT NULL,
        realized_pnl NUMERIC DEFAULT 0,
        unrealized_pnl NUMERIC DEFAULT 0,
        exposure NUMERIC DEFAULT 0,
        open_positions INTEGER DEFAULT 0,
        regime TEXT,
        metadata JSONB
    );
    """

    def __init__(self) -> None:
        """Initialize database (call connect() to establish pool)."""
        cfg = get_config()
        self.cfg = cfg.database
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """Create asyncpg connection pool."""
        logger.info("Connecting to PostgreSQL", host=self.cfg.host, db=self.cfg.name)
        self._pool = await asyncpg.create_pool(
            host=self.cfg.host,
            port=self.cfg.port,
            database=self.cfg.name,
            user=self.cfg.user,
            password=self.cfg.password,
            min_size=2,
            max_size=self.cfg.pool_size,
            command_timeout=self.cfg.pool_timeout,
        )
        logger.info("PostgreSQL pool created")
        await self.initialize_schema()

    async def close(self) -> None:
        """Close connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None
        logger.info("PostgreSQL pool closed")

    async def initialize_schema(self) -> None:
        """Run schema creation SQL."""
        if not self._pool:
            raise RuntimeError("Database not connected")
        async with self._pool.acquire() as conn:
            await conn.execute(self.SCHEMA_SQL)
        logger.info("Database schema initialized")

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        """Acquire a connection from the pool."""
        if not self._pool:
            raise RuntimeError("Database not connected")
        async with self._pool.acquire() as conn:
            yield conn

    # ---- OHLCV Methods ----

    async def upsert_candle(self, candle: NormalizedCandle) -> None:
        """Insert or update a single OHLCV candle."""
        sql = """
        INSERT INTO ohlcv (symbol, timeframe, timestamp, open, high, low, close, volume)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (symbol, timeframe, timestamp)
        DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume
        """
        async with self.acquire() as conn:
            await conn.execute(
                sql,
                candle.symbol,
                candle.timeframe.value,
                candle.timestamp,
                candle.open,
                candle.high,
                candle.low,
                candle.close,
                candle.volume,
            )

    async def upsert_candles_batch(self, candles: list[NormalizedCandle]) -> int:
        """Batch upsert OHLCV candles.

        Returns:
            Number of candles inserted/updated.
        """
        if not candles:
            return 0
        sql = """
        INSERT INTO ohlcv (symbol, timeframe, timestamp, open, high, low, close, volume)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (symbol, timeframe, timestamp)
        DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume
        """
        values = [
            (
                c.symbol,
                c.timeframe.value,
                c.timestamp,
                c.open,
                c.high,
                c.low,
                c.close,
                c.volume,
            )
            for c in candles
        ]
        async with self.acquire() as conn:
            await conn.executemany(sql, values)
        return len(candles)

    async def get_candles(
        self,
        symbol: str,
        timeframe: str,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Get OHLCV candles from database.

        Returns:
            List of candle dicts ordered oldest → newest.
        """
        sql = """
        SELECT symbol, timeframe, timestamp, open, high, low, close, volume
        FROM ohlcv
        WHERE symbol = $1 AND timeframe = $2
        """
        params: list[Any] = [symbol, timeframe]

        if start_time:
            sql += " AND timestamp >= $" + str(len(params) + 1)
            params.append(start_time)
        if end_time:
            sql += " AND timestamp <= $" + str(len(params) + 1)
            params.append(end_time)

        sql += " ORDER BY timestamp ASC LIMIT $" + str(len(params) + 1)
        params.append(limit)

        async with self.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]

    # ---- Trade Methods ----

    async def insert_trade(self, trade: NormalizedTrade) -> int | None:
        """Insert a single trade (ignore duplicates by trade_id)."""
        sql = """
        INSERT INTO trades (symbol, timestamp, price, size, side, trade_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (trade_id) DO NOTHING
        RETURNING id
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                sql,
                trade.symbol,
                trade.timestamp,
                trade.price,
                trade.size,
                trade.side.value,
                trade.trade_id,
            )
        return row["id"] if row else None

    async def insert_trades_batch(self, trades: list[NormalizedTrade]) -> int:
        """Batch insert trades.

        Returns:
            Number of trades inserted.
        """
        if not trades:
            return 0
        sql = """
        INSERT INTO trades (symbol, timestamp, price, size, side, trade_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (trade_id) DO NOTHING
        """
        values = [
            (t.symbol, t.timestamp, t.price, t.size, t.side.value, t.trade_id)
            for t in trades
        ]
        async with self.acquire() as conn:
            await conn.executemany(sql, values)
        return len(trades)

    # ---- Order Journal Methods ----

    async def log_order(self, order_id: str, symbol: str, side: str,
                        size: float, entry_price: float, order_type: str,
                        strategy_name: str | None = None,
                        signal_reason: dict | None = None,
                        regime: str | None = None) -> None:
        """Log a new order to the trade journal."""
        sql = """
        INSERT INTO orders (order_id, symbol, side, size, price, order_type, status)
        VALUES ($1, $2, $3, $4, $5, $6, 'PENDING')
        """
        async with self.acquire() as conn:
            await conn.execute(sql, order_id, symbol, side, size, entry_price, order_type)

    async def update_order_status(self, order_id: str, status: str,
                                  filled_size: float = 0,
                                  avg_fill_price: float | None = None) -> None:
        """Update order status (for fill/cancel events)."""
        sql = """
        UPDATE orders
        SET status = $2, filled_size = $3, avg_fill_price = $4, updated_at = NOW()
        WHERE order_id = $1
        """
        async with self.acquire() as conn:
            await conn.execute(sql, order_id, status, filled_size, avg_fill_price)

    async def close_trade(self, order_id: str, exit_price: float,
                          pnl: float, pnl_pct: float, fees: float) -> None:
        """Close a trade in the journal."""
        sql = """
        UPDATE trade_journal
        SET exit_price = $2, pnl = $3, pnl_pct = $4, fees = $5, closed_at = NOW()
        WHERE order_id = $1
        """
        async with self.acquire() as conn:
            await conn.execute(sql, order_id, exit_price, pnl, pnl_pct, fees)

    # ---- Performance Methods ----

    async def log_performance(self, total_equity: float, realized_pnl: float = 0,
                              unrealized_pnl: float = 0, exposure: float = 0,
                              open_positions: int = 0,
                              regime: str | None = None,
                              metadata: dict | None = None) -> None:
        """Log portfolio performance snapshot."""
        sql = """
        INSERT INTO performance_log
            (total_equity, realized_pnl, unrealized_pnl, exposure, open_positions, regime, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """
        async with self.acquire() as conn:
            await conn.execute(
                sql, total_equity, realized_pnl, unrealized_pnl, exposure,
                open_positions, regime, metadata
            )
