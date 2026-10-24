"""Exit check and expiry P&L for one scanner alert.

The exit thresholds are fixed: half the credit by the halfway point of the
hold, or 80% at any time. A scorecard grades a play once, when it expires.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

# Hundredths of a percent. 5000 is 50.00%, 8000 is 80.00%.
_HALF_BPS = 5000
_ANYTIME_BPS = 8000


def profit_fraction(credit: float | None, mid: float | None) -> float | None:
    """(C − M) / C. None when the credit cannot support a fraction."""
    if credit is None or mid is None or credit <= 0:
        return None
    return (float(credit) - float(mid)) / float(credit)


def _bps(profit: float) -> int:
    """Nearest hundredth of a percent, so an 80% profit still clears 80%."""
    return int(round(float(profit) * 10000))


def exit_due(profit: float, now: datetime, opened: datetime, expiry: datetime) -> bool:
    """True when a held short should get its one exit alert.

    80% of the credit fires at any time. 50% fires on or before the halfway
    point from open to expiry, and not after.
    """
    bps = _bps(profit)
    if bps >= _ANYTIME_BPS:
        return True
    half = opened + (expiry - opened) / 2
    return bps >= _HALF_BPS and now <= half


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return None


def expiry_at(expiration: date, opened: datetime) -> datetime:
    """Expiry at the open's clock time, so a 30-day hold splits on day 15."""
    opened = as_utc(opened)
    return datetime.combine(expiration, opened.timetz())


def settlement(kind: str, strike: float, spot: float) -> float:
    """Per-share option value at expiry."""
    strike = float(strike)
    spot = float(spot)
    if kind == "CSP":
        return max(0.0, strike - spot)
    if kind == "CC":
        return max(0.0, spot - strike)
    raise ValueError(f"no settlement for kind {kind!r}")


def play_pnl(*, kind: str, strike: float, spot: float, credit: float, contracts: int) -> float:
    """Dollars for a short held to expiry: (credit − intrinsic) × 100 × contracts."""
    intrinsic = settlement(kind, strike, spot)
    return round((float(credit) - intrinsic) * 100 * int(contracts), 2)
