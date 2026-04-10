"""UTC-focused time helpers used across persistence and scheduling."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


def to_iso8601(value: Optional[datetime]) -> Optional[str]:
    """Serialize a timestamp for SQLite storage."""

    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def from_iso8601(value: Optional[str]) -> Optional[datetime]:
    """Parse a timestamp previously written by :func:`to_iso8601`."""

    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
