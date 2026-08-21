"""
Time handling (§0.5).

Every timestamp stored by this application is UTC, ISO-8601, millisecond
precision. Two helpers, used everywhere, so no module invents its own format.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Timezone-aware UTC now, truncated to milliseconds."""
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def iso(value: datetime | None) -> str | None:
    """
    Render a datetime as ISO-8601 UTC with exactly three fractional digits.

    A naive datetime is assumed to be UTC -- the database columns are all
    ``TIMESTAMP WITH TIME ZONE``, but a driver may still hand back a naive
    value depending on configuration, and guessing local time here would
    silently shift every stored measurement.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def duration_ms(start: datetime, end: datetime) -> int:
    """Whole milliseconds between two instants, never negative."""
    delta = (end - start).total_seconds() * 1000.0
    return max(0, int(round(delta)))
