"""Data capture layer — extended SQLite storage for live data.

Builds on the existing `audit_log` schema (which captures
decisions) by adding:
  - `ohlcv` — every candle the bot has seen, with `source`
    distinguishing 'live' (REST fetch by the bot) from
    'historical' (backtest harness fetch).
  - `orderbook_snapshots` — periodic top-of-book + depth
    captures. Used for slippage model calibration.
  - `performance_snapshots` — per-cycle portfolio state.
    Used for live-vs-backtest equity comparison.

Schema is written in SQL that runs unchanged on both SQLite
and Postgres. The Postgres migration is a config flip (set
`database.host` in config/base.yaml and start the bot) — no
schema rewrite needed.

Why SQLite first?
  - Zero infra setup. Works on every Windows/Mac/Linux dev box.
  - The audit_log already lives there. Add the new tables to
    the same file.
  - WAL journaling = durable on power loss, ~50µs per row.
  - The Postgres `Database` class in `storage.py` is the
    eventual primary, but a Postgres dependency blocks every
    new contributor until they have it installed. SQLite
    unblocks data capture today.

Capture is best-effort. Failures are logged but never raise —
the trading loop must not be blocked by a logging failure.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..utils.config import get_config
from ..utils.logging import get_logger

logger = get_logger(__name__)


SCHEMA_SQL = """
-- OHLCV candles. Composite uniqueness on (symbol, tf, ts) so
-- re-fetches upsert cleanly.
CREATE TABLE IF NOT EXISTS ohlcv (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'live',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(symbol, timeframe, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_lookup
    ON ohlcv (symbol, timeframe, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_ohlcv_source
    ON ohlcv (source, timestamp DESC);

-- Orderbook snapshots: top-of-book + 5% depth, taken every N
-- cycles per symbol. Lets us calibrate the slippage model
-- against real spreads.
CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    best_bid REAL,
    best_ask REAL,
    bid_size REAL,
    ask_size REAL,
    spread_bps REAL,
    depth_5pct_bid REAL,
    depth_5pct_ask REAL,
    mid_price REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ob_lookup
    ON orderbook_snapshots (symbol, timestamp DESC);

-- Performance snapshots: per-cycle portfolio state. Lets us
-- draw the live equity curve alongside backtest curves.
CREATE TABLE IF NOT EXISTS performance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    total_equity REAL NOT NULL,
    cash REAL NOT NULL,
    exposure REAL NOT NULL,
    unrealized_pnl REAL,
    realized_pnl REAL,
    num_positions INTEGER NOT NULL,
    num_trades_today INTEGER,
    cycle_ms REAL,
    regime TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_perf_lookup
    ON performance_snapshots (timestamp DESC);

-- Signal registry: every signal computed (sma_cross, rsi, etc.)
-- with its metadata. Lets us analyze which signals are most
-- predictive AFTER trades are made.
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    name TEXT NOT NULL,
    direction TEXT NOT NULL,
    confidence REAL NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_signals_lookup
    ON signals (symbol, name, timestamp DESC);
"""


class DataCapture:
    """Live data capture writer (SQLite).

    Captures OHLCV candles, orderbook snapshots, performance
    snapshots, and signals. Thread-safe (uses an internal lock
    for writes). One writer per process; reads concurrent.

    Schema is identical to the Postgres version in
    `storage.py` so the migration path is a connection-string
    flip, not a schema rewrite.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        cfg = get_config()
        if db_path is None:
            # Reuse the audit.db path so we have one DB to ship.
            db_path = (
                getattr(getattr(cfg, "audit", None), "db_path", None)
                or "data/audit.db"
            )
        self.db_path = Path(db_path)
        if not self.db_path.is_absolute():
            project_root = Path(__file__).resolve().parent.parent.parent
            self.db_path = project_root / self.db_path

        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._init_failed = False
        self._init_db()

    def _init_db(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                timeout=10.0,
                isolation_level=None,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            # Apply the audit_log schema (idempotent) so audit_log
            # table exists alongside our new tables.
            self._conn.executescript(SCHEMA_SQL)
            logger.info("Data capture initialised", db_path=str(self.db_path))
        except Exception as exc:
            logger.error(
                "Data capture init failed — capture will be disabled",
                db_path=str(self.db_path),
                error=str(exc),
            )
            self._init_failed = True
            self._conn = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        if self._init_failed or self._conn is None:
            raise RuntimeError("DataCapture not initialised")
        cur = self._conn.cursor()
        try:
            yield cur
        finally:
            pass

    # ──── Write API ───────────────────────────────────────────────────────

    def capture_candle(
        self,
        symbol: str,
        timeframe: str,
        timestamp: datetime,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        source: str = "live",
    ) -> bool:
        """Upsert one OHLCV candle. Best-effort, never raises."""
        if self._init_failed or self._conn is None:
            return False
        ts_iso = _to_iso(timestamp)
        sql = """
        INSERT INTO ohlcv (symbol, timeframe, timestamp, open, high, low, close, volume, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (symbol, timeframe, timestamp) DO UPDATE SET
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            volume = excluded.volume,
            source = excluded.source
        """
        try:
            with self._lock:
                with self._cursor() as cur:
                    cur.execute(sql, (
                        symbol.upper(), timeframe, ts_iso,
                        open_, high, low, close, volume, source,
                    ))
            return True
        except Exception as exc:
            logger.debug("capture_candle failed", symbol=symbol, error=str(exc))
            return False

    def capture_candles_batch(
        self,
        candles: list[dict[str, Any]],
        source: str = "live",
    ) -> int:
        """Batch upsert. Each dict: {symbol, timeframe, timestamp,
        open, high, low, close, volume}. Returns count written."""
        if self._init_failed or self._conn is None or not candles:
            return 0
        rows = []
        for c in candles:
            rows.append((
                c["symbol"].upper(),
                c["timeframe"],
                _to_iso(c["timestamp"]),
                c["open"], c["high"], c["low"], c["close"], c["volume"],
                source,
            ))
        sql = """
        INSERT INTO ohlcv (symbol, timeframe, timestamp, open, high, low, close, volume, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (symbol, timeframe, timestamp) DO UPDATE SET
            open = excluded.open, high = excluded.high,
            low = excluded.low, close = excluded.close,
            volume = excluded.volume, source = excluded.source
        """
        try:
            with self._lock:
                with self._cursor() as cur:
                    cur.executemany(sql, rows)
            return len(rows)
        except Exception as exc:
            logger.debug("capture_candles_batch failed", error=str(exc))
            return 0

    def capture_orderbook(
        self,
        symbol: str,
        timestamp: datetime,
        best_bid: float | None,
        best_ask: float | None,
        bid_size: float | None = None,
        ask_size: float | None = None,
        depth_5pct_bid: float | None = None,
        depth_5pct_ask: float | None = None,
    ) -> bool:
        """Capture one top-of-book + depth snapshot. Best-effort."""
        if self._init_failed or self._conn is None:
            return False
        spread_bps = None
        mid = None
        if best_bid is not None and best_ask is not None and best_bid > 0:
            mid = (best_bid + best_ask) / 2
            spread_bps = (best_ask - best_bid) / mid * 10_000
        sql = """
        INSERT INTO orderbook_snapshots
            (timestamp, symbol, best_bid, best_ask, bid_size, ask_size,
             spread_bps, depth_5pct_bid, depth_5pct_ask, mid_price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        try:
            with self._lock:
                with self._cursor() as cur:
                    cur.execute(sql, (
                        _to_iso(timestamp), symbol.upper(),
                        best_bid, best_ask, bid_size, ask_size,
                        spread_bps, depth_5pct_bid, depth_5pct_ask, mid,
                    ))
            return True
        except Exception as exc:
            logger.debug("capture_orderbook failed", symbol=symbol, error=str(exc))
            return False

    def capture_performance(
        self,
        timestamp: datetime,
        total_equity: float,
        cash: float,
        exposure: float,
        unrealized_pnl: float | None = None,
        realized_pnl: float | None = None,
        num_positions: int = 0,
        num_trades_today: int | None = None,
        cycle_ms: float | None = None,
        regime: str | None = None,
    ) -> bool:
        """Capture per-cycle portfolio state. Best-effort."""
        if self._init_failed or self._conn is None:
            return False
        sql = """
        INSERT INTO performance_snapshots
            (timestamp, total_equity, cash, exposure, unrealized_pnl,
             realized_pnl, num_positions, num_trades_today, cycle_ms, regime)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        try:
            with self._lock:
                with self._cursor() as cur:
                    cur.execute(sql, (
                        _to_iso(timestamp),
                        total_equity, cash, exposure,
                        unrealized_pnl, realized_pnl,
                        num_positions, num_trades_today,
                        cycle_ms, regime,
                    ))
            return True
        except Exception as exc:
            logger.debug("capture_performance failed", error=str(exc))
            return False

    def capture_signal(
        self,
        timestamp: datetime,
        symbol: str,
        timeframe: str,
        name: str,
        direction: str,
        confidence: float,
        metadata: dict | None = None,
    ) -> bool:
        """Capture one signal computation. Best-effort."""
        if self._init_failed or self._conn is None:
            return False
        sql = """
        INSERT INTO signals
            (timestamp, symbol, timeframe, name, direction, confidence, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        try:
            with self._lock:
                with self._cursor() as cur:
                    cur.execute(sql, (
                        _to_iso(timestamp), symbol.upper(), timeframe,
                        name, direction, confidence,
                        json.dumps(metadata) if metadata else None,
                    ))
            return True
        except Exception as exc:
            logger.debug("capture_signal failed", symbol=symbol, error=str(exc))
            return False

    # ──── Read API (for backtest harness) ────────────────────────────────

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Read OHLCV candles from captured data, ordered oldest→newest.

        Returns dicts with keys: symbol, timeframe, timestamp,
        open, high, low, close, volume, source.
        """
        if self._init_failed or self._conn is None:
            return []
        where = ["symbol = ?", "timeframe = ?"]
        params: list[Any] = [symbol.upper(), timeframe]
        if start is not None:
            where.append("timestamp >= ?")
            params.append(_to_iso(start))
        if end is not None:
            where.append("timestamp <= ?")
            params.append(_to_iso(end))
        sql = f"""
        SELECT symbol, timeframe, timestamp, open, high, low, close, volume, source
        FROM ohlcv
        WHERE {' AND '.join(where)}
        ORDER BY timestamp ASC
        """
        try:
            with self._cursor() as cur:
                rows = cur.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.debug("get_candles failed", error=str(exc))
            return []

    def get_performance_curve(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Read performance snapshots ordered oldest→newest."""
        if self._init_failed or self._conn is None:
            return []
        where = ["1=1"]
        params: list[Any] = []
        if start is not None:
            where.append("timestamp >= ?")
            params.append(_to_iso(start))
        if end is not None:
            where.append("timestamp <= ?")
            params.append(_to_iso(end))
        sql = f"""
        SELECT timestamp, total_equity, cash, exposure, unrealized_pnl,
               realized_pnl, num_positions, num_trades_today, cycle_ms, regime
        FROM performance_snapshots
        WHERE {' AND '.join(where)}
        ORDER BY timestamp ASC
        """
        try:
            with self._cursor() as cur:
                rows = cur.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.debug("get_performance_curve failed", error=str(exc))
            return []

    def get_signals_summary(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, Any]:
        """Persistent signals summary.

        Returns the same shape as `SignalRegistry.summary()` but
        reads from SQLite, so the count survives bot restarts.

        Keys:
          - total_signals: COUNT(*) of all rows (within the window if
            start/end given)
          - symbols: distinct symbols that emitted any signal
          - by_key: count grouped by `"{symbol}:{timeframe}"`
          - first_seen / last_seen: window bounds on the SIGNAL
            timestamp (the candle's close time). Note: also exposed
            via `last_captured` for the write time (more useful for
            "is the bot still writing?" checks).
        """
        if self._init_failed or self._conn is None:
            return {
                "total_signals": 0, "symbols": [], "by_key": {},
                "first_seen": None, "last_seen": None,
                "last_captured": None, "first_captured": None,
            }
        where = ["1=1"]
        params: list[Any] = []
        if start is not None:
            where.append("timestamp >= ?")
            params.append(_to_iso(start))
        if end is not None:
            where.append("timestamp <= ?")
            params.append(_to_iso(end))
        where_sql = f"WHERE {' AND '.join(where)}"
        try:
            with self._cursor() as cur:
                total = cur.execute(
                    f"SELECT COUNT(*) FROM signals {where_sql}", params
                ).fetchone()[0]
                syms = [
                    r[0] for r in cur.execute(
                        f"SELECT DISTINCT symbol FROM signals {where_sql} "
                        f"ORDER BY symbol", params
                    ).fetchall()
                ]
                by_key = {
                    r[0]: r[1] for r in cur.execute(
                        f"SELECT symbol || ':' || timeframe AS k, COUNT(*) "
                        f"FROM signals {where_sql} "
                        f"GROUP BY k ORDER BY k", params
                    ).fetchall()
                }
                bounds = cur.execute(
                    f"SELECT MIN(timestamp), MAX(timestamp) FROM signals {where_sql}",
                    params,
                ).fetchone()
                # created_at is the WRITE time (default datetime('now')),
                # which is what you want to confirm the bot is still
                # persisting live.
                write_bounds = cur.execute(
                    f"SELECT MIN(created_at), MAX(created_at) FROM signals {where_sql}",
                    params,
                ).fetchone()
            return {
                "total_signals": int(total),
                "symbols": syms,
                "by_key": by_key,
                "first_seen": bounds[0] if bounds and bounds[0] is not None else None,
                "last_seen": bounds[1] if bounds and bounds[1] is not None else None,
                "first_captured": write_bounds[0] if write_bounds and write_bounds[0] is not None else None,
                "last_captured": write_bounds[1] if write_bounds and write_bounds[1] is not None else None,
            }
        except Exception as exc:
            logger.debug("get_signals_summary failed", error=str(exc))
            return {
                "total_signals": 0, "symbols": [], "by_key": {},
                "first_seen": None, "last_seen": None,
                "last_captured": None, "first_captured": None,
            }

    def stats(self) -> dict[str, int]:
        """Row counts for monitoring."""
        if self._init_failed or self._conn is None:
            return {}
        out: dict[str, int] = {}
        for table in ("ohlcv", "orderbook_snapshots", "performance_snapshots", "signals"):
            try:
                with self._cursor() as cur:
                    n = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                out[table] = int(n)
            except Exception:
                out[table] = -1
        return out


# ─────────────────────────────────────────────────────────────────────
# Singleton + helpers
# ─────────────────────────────────────────────────────────────────────


_capture_singleton: DataCapture | None = None
_capture_lock = threading.Lock()


def get_data_capture() -> DataCapture:
    """Return the process-wide DataCapture, creating on first call."""
    global _capture_singleton
    if _capture_singleton is not None:
        return _capture_singleton
    with _capture_lock:
        if _capture_singleton is None:
            _capture_singleton = DataCapture()
    return _capture_singleton


def reset_data_capture_for_tests() -> None:
    """Drop the singleton. Tests only."""
    global _capture_singleton
    with _capture_lock:
        if _capture_singleton is not None:
            try:
                _capture_singleton.close()
            except Exception:
                pass
        _capture_singleton = None


def _to_iso(ts: datetime) -> str:
    """Normalize a datetime to ISO 8601 UTC string."""
    if isinstance(ts, str):
        return ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()
