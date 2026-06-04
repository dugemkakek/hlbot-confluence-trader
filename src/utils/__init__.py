"""Datetime and timestamp utilities."""

from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware in UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ms_to_dt(ms: int) -> datetime:
    """Convert millisecond Unix timestamp to UTC datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    """Convert UTC datetime to millisecond Unix timestamp."""
    return int(to_utc(dt).timestamp() * 1000)
