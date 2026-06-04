"""Datetime utilities for timestamp conversions."""

from datetime import datetime, timezone


def ms_to_dt(ms: int) -> datetime:
    """Convert millisecond Unix timestamp to UTC datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    """Convert UTC datetime to millisecond Unix timestamp."""
    return int(dt.timestamp() * 1000)