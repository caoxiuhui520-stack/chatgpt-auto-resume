"""Time helpers. All internal timestamps are timezone-aware UTC."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import utcnow

__all__ = [
    "utcnow",
    "now_utc",
    "parse_iso",
    "iso",
    "format_local",
    "humanize_delta",
    "seconds_between",
]


def now_utc() -> datetime:
    return utcnow()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def format_local(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def humanize_delta(target: datetime | None, reference: datetime | None = None) -> str:
    """'2h 14m' style countdown, or 'now'/'past'."""
    if target is None:
        return "unknown"
    ref = reference or utcnow()
    delta = (target - ref).total_seconds()
    if delta <= 0:
        return "now"
    minutes, seconds = divmod(int(delta), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def seconds_between(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (a - b).total_seconds()


def clamp_seconds(value: float, minimum: float = 1.0, maximum: float = 3600.0) -> float:
    return max(minimum, min(maximum, value))


def add_seconds(value: datetime, seconds: float) -> datetime:
    return value + timedelta(seconds=seconds)
