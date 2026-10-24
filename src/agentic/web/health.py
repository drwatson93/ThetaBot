"""Read-only /health snapshot.

Reports the order lock, whether the scanner loop has written a scan_runs row
inside two scan intervals, and the age of the newest quote that scan used.
Market hours and a stale feed stay fields on a 200. The body has no balances,
secrets, or paper/live mode.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..brokers.robinhood_mcp import ORDERS_HARD_DISABLED, REAL_ORDERS_LOCK_LABEL
from ..domain.models import utcnow

# Keys copied from the paper-practice snapshot. Anything else (balances, tokens) stays out.
_PRACTICE_KEYS = ("execution_broker", "market_data", "robinhood_connected", "practice")


def scanner_window_seconds(interval_seconds: int) -> int:
    """How old the last scan_runs row can be and still count as the loop being up.

    One interval is the wait between cycles. Two intervals covers one slow cycle.
    """
    return max(int(interval_seconds or 0), 1) * 2


def feed_age_seconds(scanner: Any, now: datetime | None = None) -> float | None:
    """Age in seconds of the newest quote the scanner used, or None if it has not used one."""
    quotes = getattr(scanner, "last_quotes", None) or {}
    if not quotes:
        return None
    now = now or utcnow()
    ages: list[float] = []
    for quote in quotes.values():
        as_of = getattr(quote, "as_of", None)
        if not isinstance(as_of, datetime):
            continue
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        ages.append((now - as_of).total_seconds())
    if not ages:
        return None
    return round(min(ages), 1)


def scanner_status(deps: Any, now: datetime | None = None) -> dict[str, Any]:
    """Whether the scanner loop looks up, from the latest scan_runs row."""
    interval = int(getattr(getattr(deps.settings, "entry", None), "scan_interval_seconds", 0) or 0)
    window = scanner_window_seconds(interval)
    store = getattr(getattr(deps, "scanner", None), "alerts", None)
    last = store.latest_run() if store is not None else None
    empty = {
        "up": False,
        "last_scan_at": None,
        "last_status": None,
        "age_seconds": None,
        "window_seconds": window,
    }
    if not last:
        return empty
    raw = last.get("scanned_at")
    try:
        scanned_at = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return empty
    if scanned_at.tzinfo is None:
        scanned_at = scanned_at.replace(tzinfo=timezone.utc)
    now = now or utcnow()
    age = (now - scanned_at).total_seconds()
    return {
        "up": age <= window,
        "last_scan_at": raw,
        "last_status": last.get("status"),
        "age_seconds": round(age, 1),
        "window_seconds": window,
    }


def build_health(deps: Any, now: datetime | None = None) -> dict[str, Any]:
    """The /health body. Always meant to be served with HTTP 200 when the app is up."""
    body: dict[str, Any] = {
        "status": "ok",
        "paused": bool(deps.killswitch.is_paused()),
        "orders_hard_disabled": ORDERS_HARD_DISABLED,
        "scanner": scanner_status(deps, now),
        "feed_age_seconds": feed_age_seconds(getattr(deps, "scanner", None), now),
    }
    if ORDERS_HARD_DISABLED:
        body["real_orders"] = "HARD DISABLED in code"
        body["real_orders_lock"] = REAL_ORDERS_LOCK_LABEL
    practice = getattr(deps, "practice", None) or {}
    for key in _PRACTICE_KEYS:
        if key in practice:
            body[key] = practice[key]
    return body
