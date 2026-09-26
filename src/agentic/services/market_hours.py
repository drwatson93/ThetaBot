"""Best-effort US equity-options market-hours check (regular session).

``is_market_hours`` is used to pick a polling cadence (faster when open, slower when
closed). Falls back to "open" if timezone data is unavailable so we never under-poll.

``is_order_window`` is the trading gate for new entries, exits, stop-loss orders, and
the tax-reserve equity buy. Both honor weekends, NYSE full-day holidays, and 1:00 PM
ET early closes.
"""
from __future__ import annotations

from datetime import datetime, time, timezone

from .nyse_calendar import session_close


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


def _session_end(et: datetime) -> time | None:
    return session_close(et.date())


def is_market_hours(now: datetime | None = None) -> bool:
    et = _et(now)
    if et is None:
        return True
    end = _session_end(et)
    if end is None:
        return False
    return time(9, 30) <= et.time() <= end


def is_order_window(now: datetime | None = None, *, start: str = "10:00") -> bool:
    """True during the regular session at/after ``start`` (America/New_York).

    New entries, exits, and stop-loss orders use this — not the 09:30 open — so the
    wide opening-bell spread is skipped. Default start is 10:00. Closed on weekends,
    NYSE full-day holidays, and after 1:00 PM ET on early-close days.
    """
    et = _et(now)
    if et is None:
        return True
    end = _session_end(et)
    if end is None:
        return False
    return parse_hhmm(start) <= et.time() <= end
