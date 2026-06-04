"""AuditLogger — synchronous SQLite writer for decision audit rows.

Why sync?
    The audit log is the most important thing to never lose. A crashed
    process with a half-written asyncpg trade journal is recoverable
    from the upstream decision data; a missing audit row for the cycle
    that exposed a bug is not. We use synchronous SQLite writes with
    WAL journaling: ~50µs per row, durable on power loss.

Schema:
    audit_log table — one row per decision (BUY/SELL/NO_TRADE) per cycle.
    Wide column shape with JSON blobs for subsystem_scores and metadata.

Lifecycle:
    AuditLogger is a process-wide singleton, lazily created.
    `get_audit_logger()` is the only public entry point.

Failure modes:
    - DB directory unwritable: log a warning, return None from log(),
      do not raise. Audit must never crash the decision engine.
    - DB lock contention: 3 retries with exponential backoff.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..utils.config import get_config
from ..utils.logging import get_logger
from .models import AuditEntry, AuditEntryInput, SubsystemScoreRow

logger = get_logger(__name__)

DEFAULT_DB_PATH = "data/audit.db"
DEFAULT_RETENTION_DAYS = 90

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT,
    decision TEXT NOT NULL,
    reason TEXT,
    reason_code TEXT,
    regime TEXT,
    regime_confidence REAL,
    final_score REAL,
    confirming_count INTEGER,
    required_confirmations INTEGER,
    confluence_score REAL,
    structure_score REAL,
    pullback_score REAL,
    momentum_score REAL,
    volume_score REAL,
    confidence REAL,
    direction TEXT,
    is_actionable INTEGER,
    order_id TEXT,
    entry_price REAL,
    size REAL,
    stop_loss REAL,
    take_profit REAL,
    source TEXT NOT NULL DEFAULT 'decision_engine',
    subsystem_scores_json TEXT,
    metadata_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_symbol_time
    ON audit_log (symbol, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_decision_time
    ON audit_log (decision, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_reason_code
    ON audit_log (reason_code, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp
    ON audit_log (timestamp DESC);
"""


class AuditLogger:
    """SQLite-backed decision audit log writer.

    Thread-safe (uses an internal lock for writes). One writer per
    process; reads can happen concurrently via short transactions.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        cfg = get_config()
        if db_path is None:
            db_path = getattr(getattr(cfg, "audit", None), "db_path", None) or DEFAULT_DB_PATH

        self.db_path = Path(db_path)
        if not self.db_path.is_absolute():
            # Resolve relative to project root (HLBot/), not cwd
            project_root = Path(__file__).resolve().parent.parent.parent
            self.db_path = project_root / self.db_path

        # Single connection, shared across threads. SQLite + check_same_thread
        # is fine for our access pattern (serialized writes, read-only API).
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._init_failed = False
        # Defer mkdir + connection open to `_init_db` so failures here
        # set `_init_failed` cleanly and the public API stays no-throw.
        self._init_db()

        # Retention days for periodic pruning (used by `prune_old_entries`)
        self.retention_days: int = int(
            getattr(getattr(cfg, "audit", None), "retention_days", None) or DEFAULT_RETENTION_DAYS
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Open the DB and apply schema. Failures are non-fatal — audit
        is best-effort and must not block the trading loop."""
        try:
            # Ensure the parent directory exists, but don't fail on a
            # pre-existing file at the parent path (e.g. a misuse of
            # AuditLogger with a bad config). We surface the failure
            # through `_init_failed` instead of raising.
            try:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            except (FileExistsError, PermissionError, OSError) as exc:
                logger.error(
                    "Audit log dir creation failed — audit will be disabled",
                    db_path=str(self.db_path),
                    error=str(exc),
                )
                self._init_failed = True
                self._conn = None
                return

            self._conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                timeout=10.0,
                isolation_level=None,  # autocommit; we manage txns explicitly
            )
            # Row factory so we can use named column access in `_row_to_entry`.
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA_SQL)
            logger.info("Audit log initialised", db_path=str(self.db_path))
        except Exception as exc:
            logger.error(
                "Audit log init failed — audit will be disabled",
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
        """Yield a cursor, opening the connection if needed."""
        if self._init_failed or self._conn is None:
            raise RuntimeError("AuditLogger not initialised")
        # Reconnect if the connection was closed (e.g. process restart)
        try:
            cur = self._conn.cursor()
            yield cur
        finally:
            # Connection is reused; don't close here. Caller is responsible.
            pass

    # ── Write API ─────────────────────────────────────────────────────────────

    def log(self, entry: AuditEntryInput) -> int | None:
        """Write one audit row. Returns the row id, or None on failure.

        Never raises — audit is best-effort. Callers should not gate
        trading decisions on this.
        """
        if self._init_failed or self._conn is None:
            return None

        payload = _entry_to_row(entry)
        sql = """
        INSERT INTO audit_log (
            timestamp, symbol, timeframe, decision, reason, reason_code,
            regime, regime_confidence, final_score, confirming_count,
            required_confirmations, confluence_score, structure_score,
            pullback_score, momentum_score, volume_score, confidence,
            direction, is_actionable, order_id, entry_price, size,
            stop_loss, take_profit, source, subsystem_scores_json, metadata_json
        ) VALUES (
            :timestamp, :symbol, :timeframe, :decision, :reason, :reason_code,
            :regime, :regime_confidence, :final_score, :confirming_count,
            :required_confirmations, :confluence_score, :structure_score,
            :pullback_score, :momentum_score, :volume_score, :confidence,
            :direction, :is_actionable, :order_id, :entry_price, :size,
            :stop_loss, :take_profit, :source, :subsystem_scores_json, :metadata_json
        )
        """

        # Retry on transient lock errors
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                with self._lock:
                    with self._cursor() as cur:
                        cur.execute(sql, payload)
                        row_id = cur.lastrowid
                if entry.decision == "NO_TRADE" and entry.reason_code:
                    logger.debug(
                        "Audit row written (NO_TRADE)",
                        row_id=row_id,
                        symbol=entry.symbol,
                        reason_code=entry.reason_code,
                    )
                else:
                    logger.debug(
                        "Audit row written",
                        row_id=row_id,
                        symbol=entry.symbol,
                        decision=entry.decision,
                    )
                return int(row_id) if row_id is not None else None
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    time.sleep(0.05 * (2 ** attempt))
                    continue
                break
            except Exception as exc:
                last_exc = exc
                break

        logger.error(
            "Audit log write failed",
            symbol=entry.symbol,
            decision=entry.decision,
            error=str(last_exc) if last_exc else "unknown",
        )
        return None

    # ── Read API ──────────────────────────────────────────────────────────────

    def get_entries(
        self,
        symbol: str | None = None,
        decision: str | None = None,
        reason_code: str | None = None,
        limit: int = 50,
    ) -> list[AuditEntry]:
        """Read recent audit entries with optional filters.

        Args:
            symbol: Filter by trading pair (case-insensitive).
            decision: Filter by "BUY" | "SELL" | "NO_TRADE".
            reason_code: Filter by NoTradeReason enum value.
            limit: Max rows to return (clamped to [1, 1000]).

        Returns:
            List of AuditEntry, newest first.
        """
        if self._init_failed or self._conn is None:
            return []
        limit = max(1, min(int(limit), 1000))

        where: list[str] = []
        params: list[Any] = []
        if symbol:
            where.append("UPPER(symbol) = ?")
            params.append(symbol.upper())
        if decision:
            where.append("decision = ?")
            params.append(decision.upper())
        if reason_code:
            where.append("reason_code = ?")
            params.append(reason_code)

        sql = """
        SELECT id, timestamp, symbol, timeframe, decision, reason, reason_code,
               regime, regime_confidence, final_score, confirming_count,
               required_confirmations, confluence_score, structure_score,
               pullback_score, momentum_score, volume_score, confidence,
               direction, is_actionable, order_id, entry_price, size,
               stop_loss, take_profit, source, subsystem_scores_json, metadata_json
        FROM audit_log
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(limit)

        try:
            with self._lock:
                with self._cursor() as cur:
                    rows = cur.execute(sql, params).fetchall()
        except Exception as exc:
            logger.error("Audit log read failed", error=str(exc))
            return []

        return [_row_to_entry(r) for r in rows]

    def count_by_reason_code(
        self,
        since: datetime | None = None,
        symbol: str | None = None,
    ) -> dict[str, int]:
        """Return counts grouped by reason_code, useful for dashboards.

        Args:
            since: Only count rows at or after this timestamp.
            symbol: Optional symbol filter.

        Returns:
            dict mapping reason_code -> count.
        """
        if self._init_failed or self._conn is None:
            return {}

        where = ["decision = 'NO_TRADE'", "reason_code IS NOT NULL"]
        params: list[Any] = []
        if since:
            where.append("timestamp >= ?")
            params.append(since.isoformat())
        if symbol:
            where.append("UPPER(symbol) = ?")
            params.append(symbol.upper())

        sql = f"""
        SELECT reason_code, COUNT(*) AS n
        FROM audit_log
        WHERE {' AND '.join(where)}
        GROUP BY reason_code
        ORDER BY n DESC
        """
        try:
            with self._lock:
                with self._cursor() as cur:
                    rows = cur.execute(sql, params).fetchall()
        except Exception as exc:
            logger.error("Audit log count failed", error=str(exc))
            return {}
        return {r[0]: int(r[1]) for r in rows}

    def prune_old_entries(self, days: int | None = None) -> int:
        """Delete rows older than `days`. Returns the number deleted.

        Safe to call from a cron job; not invoked automatically.
        """
        if self._init_failed or self._conn is None:
            return 0
        days = int(days or self.retention_days)
        cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()

        try:
            with self._lock:
                with self._cursor() as cur:
                    cur.execute("DELETE FROM audit_log WHERE timestamp < ?", (cutoff_iso,))
                    deleted = cur.rowcount
            if deleted:
                logger.info("Pruned old audit rows", deleted=deleted, cutoff=cutoff_iso)
            return int(deleted)
        except Exception as exc:
            logger.error("Audit prune failed", error=str(exc))
            return 0


# ─────────────────────────────────────────────────────────────────────────────
# Singleton accessor
# ─────────────────────────────────────────────────────────────────────────────

_logger_singleton: AuditLogger | None = None
_singleton_lock = threading.Lock()


def get_audit_logger() -> AuditLogger:
    """Return the process-wide AuditLogger, creating it on first call."""
    global _logger_singleton
    if _logger_singleton is not None:
        return _logger_singleton
    with _singleton_lock:
        if _logger_singleton is None:
            _logger_singleton = AuditLogger()
    return _logger_singleton


def reset_audit_logger_for_tests() -> None:
    """Drop the cached singleton so the next call rebuilds it. Tests only."""
    global _logger_singleton
    with _singleton_lock:
        if _logger_singleton is not None:
            try:
                _logger_singleton.close()
            except Exception:
                pass
        _logger_singleton = None


# ─────────────────────────────────────────────────────────────────────────────
# Row <-> model conversion
# ─────────────────────────────────────────────────────────────────────────────


def _entry_to_row(entry: AuditEntryInput) -> dict[str, Any]:
    """Flatten an AuditEntryInput into a dict for SQL insertion."""
    timestamp = entry.metadata.pop("timestamp", None) if entry.metadata else None
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    elif isinstance(timestamp, str):
        timestamp = timestamp
    elif isinstance(timestamp, datetime):
        timestamp = (
            timestamp.astimezone(timezone.utc).isoformat()
            if timestamp.tzinfo
            else timestamp.replace(tzinfo=timezone.utc).isoformat()
        )

    subsystem_dicts = [s.model_dump() for s in entry.subsystem_scores]
    metadata = dict(entry.metadata)

    return {
        "timestamp": timestamp if isinstance(timestamp, str) else timestamp.isoformat(),
        "symbol": entry.symbol.upper(),
        "timeframe": entry.timeframe,
        "decision": entry.decision,
        "reason": entry.reason or "",
        "reason_code": entry.reason_code,
        "regime": entry.regime,
        "regime_confidence": entry.regime_confidence,
        "final_score": entry.final_score,
        "confirming_count": entry.confirming_count,
        "required_confirmations": entry.required_confirmations,
        "confluence_score": entry.confluence_score,
        "structure_score": entry.structure_score,
        "pullback_score": entry.pullback_score,
        "momentum_score": entry.momentum_score,
        "volume_score": entry.volume_score,
        "confidence": entry.confidence,
        "direction": entry.direction,
        "is_actionable": 1 if entry.is_actionable else 0 if entry.is_actionable is not None else None,
        "order_id": entry.order_id,
        "entry_price": entry.entry_price,
        "size": entry.size,
        "stop_loss": entry.stop_loss,
        "take_profit": entry.take_profit,
        "source": entry.source,
        "subsystem_scores_json": json.dumps(subsystem_dicts) if subsystem_dicts else None,
        "metadata_json": json.dumps(metadata) if metadata else None,
    }


def _row_to_entry(row: sqlite3.Row) -> AuditEntry:
    """Inverse of `_entry_to_row`.

    `row` is a `sqlite3.Row` (named column access). All field reads use
    the column name; integer indexing is a fallback only.
    """
    has_named = hasattr(row, "keys")

    def col(idx: int, key: str) -> Any:
        if has_named:
            # If the key exists in the row, use it (even if value is None).
            # Falling back to idx would misalign columns when NULLs are present.
            try:
                return row[key]
            except (KeyError, IndexError):
                pass
        try:
            return row[idx]
        except (KeyError, IndexError):
            return None

    raw_ts = col(0, "timestamp")
    if not isinstance(raw_ts, str):
        timestamp = datetime.now(timezone.utc).isoformat()
    else:
        timestamp = raw_ts

    raw_subs = col(26, "subsystem_scores_json")
    subs: list[SubsystemScoreRow] = []
    if raw_subs:
        try:
            subs = [SubsystemScoreRow(**s) for s in json.loads(raw_subs)]
        except Exception:
            subs = []

    raw_meta = col(27, "metadata_json")
    meta: dict[str, Any] = {}
    if raw_meta:
        try:
            meta = json.loads(raw_meta)
        except Exception:
            meta = {}

    is_act_raw = col(19, "is_actionable")
    if is_act_raw is None:
        is_actionable: bool | None = None
    else:
        is_actionable = bool(int(is_act_raw))

    # Numeric coercions — SQLite returns REAL for any number, which trips
    # strict pydantic int validators. Coerce explicitly where the schema
    # expects an int.
    def as_int(v: Any) -> int | None:
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def as_float(v: Any) -> float | None:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return AuditEntry(
        id=int(col(0, "id") or 0),
        timestamp=timestamp,  # type: ignore[arg-type]
        symbol=str(col(1, "symbol") or ""),
        timeframe=col(2, "timeframe"),
        decision=str(col(3, "decision") or "NO_TRADE"),  # type: ignore[arg-type]
        reason=str(col(4, "reason") or ""),
        reason_code=col(5, "reason_code"),
        regime=col(6, "regime"),
        regime_confidence=as_float(col(7, "regime_confidence")),
        final_score=as_float(col(8, "final_score")),
        confirming_count=as_int(col(9, "confirming_count")),
        required_confirmations=as_int(col(10, "required_confirmations")),
        confluence_score=as_float(col(11, "confluence_score")),
        structure_score=as_float(col(12, "structure_score")),
        pullback_score=as_float(col(13, "pullback_score")),
        momentum_score=as_float(col(14, "momentum_score")),
        volume_score=as_float(col(15, "volume_score")),
        confidence=as_float(col(16, "confidence")),
        direction=col(17, "direction"),
        is_actionable=is_actionable,
        order_id=col(18, "order_id"),
        entry_price=as_float(col(20, "entry_price")),
        size=as_float(col(21, "size")),
        stop_loss=as_float(col(22, "stop_loss")),
        take_profit=as_float(col(23, "take_profit")),
        source=str(col(24, "source") or "decision_engine"),
        subsystem_scores=subs,
        metadata=meta,
    )
