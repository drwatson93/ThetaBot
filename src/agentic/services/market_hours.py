"""Best-effort US equity-options market-hours check (regular session).

Used only to pick a polling cadence (faster when open, slower when closed). Not a
trading gate. Falls back to "open" if timezone data is unavailable so we never under-poll.
Does not account for market holidays.
"""
from __future__ import annotations

from datetime import datetime, time, timezone


def _et(now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001 — missing tzdata on some Windows installs
        return None


def parse_hhmm(value: str) -> time:
    """Parse ``HH:MM`` (default 10:00) into a clock time."""
    raw = (value or "10:00").strip()
    parts = raw.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        return time(max(0, min(hour, 23)), max(0, min(minute, 59)))
    except (TypeError, ValueError):
        return time(10, 0)


def is_market_hours(now: datetime | None = None) -> bool:
    et = _et(now)
    if et is None:
        return True
    if et.weekday() >= 5:  # Sat/Sun
        return False
    return time(9, 30) <= et.time() <= time(16, 0)


def is_order_window(now: datetime | None = None, *, start: str = "10:00") -> bool:
    """True during the regular session at/after ``start`` (America/New_York).

    New entries, exits, and stop-loss orders use this — not the 09:30 open — so the
    wide opening-bell spread is skipped. Default start is 10:00.
    """
    et = _et(now)
    if et is None:
        return True
    if et.weekday() >= 5:
        return False
    return parse_hhmm(start) <= et.time() <= time(16, 0)
