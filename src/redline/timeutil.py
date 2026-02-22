"""Time utilities."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


def utc_floor_day(dt: datetime | None = None) -> datetime:
    """Floor datetime to midnight UTC (start of current day)."""
    if dt is None:
        dt = utcnow()
    return dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
