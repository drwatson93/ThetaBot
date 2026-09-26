"""NYSE full-day holidays and 1:00 PM ET early closes.

Rule-based (no holiday-library dependency). Covers the regular-session calendar the
order window and tax-reserve equity buy use: New Year's (observed), MLK, Presidents,
Good Friday, Memorial, Juneteenth (observed), Independence (observed), Labor,
Thanksgiving, Christmas (observed), plus the usual 1pm early closes.
"""
from __future__ import annotations

from datetime import date, time, timedelta
from functools import lru_cache


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """``weekday`` is Monday=0. ``n`` is 1-based."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def observed(d: date) -> date:
    """Saturday holidays observe Friday; Sunday holidays observe Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def western_easter(year: int) -> date:
    """Anonymous Gregorian algorithm (Western/Catholic Easter)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month = (h + el - 7 * m + 114) // 31
    day = ((h + el - 7 * m + 114) % 31) + 1
    return date(year, month, day)


@lru_cache(maxsize=32)
def nyse_full_holidays(year: int) -> frozenset[date]:
    """Full-session closures that fall in ``year`` (including a Dec 31 observed New Year)."""
    easter = western_easter(year)
    holidays = {
        observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        easter - timedelta(days=2),
        _last_weekday(year, 5, 0),
        observed(date(year, 6, 19)),
        observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        observed(date(year, 12, 25)),
    }
    next_new_year = observed(date(year + 1, 1, 1))
    if next_new_year.year == year:
        holidays.add(next_new_year)
    return frozenset(d for d in holidays if d.year == year)


@lru_cache(maxsize=32)
def nyse_early_close_dates(year: int) -> frozenset[date]:
    """Weekdays the NYSE regular session ends at 1:00 PM ET."""
    holidays = nyse_full_holidays(year)
    out: set[date] = set()

    day_after_thanksgiving = _nth_weekday(year, 11, 3, 4) + timedelta(days=1)
    if day_after_thanksgiving.weekday() < 5 and day_after_thanksgiving not in holidays:
        out.add(day_after_thanksgiving)

    christmas_eve = date(year, 12, 24)
    if christmas_eve.weekday() < 5 and christmas_eve not in holidays:
        out.add(christmas_eve)

    july_3 = date(year, 7, 3)
    # Early close only when Independence Day itself is a weekday (Tue–Fri), so July 3
    # is a regular session day rather than the observed full holiday.
    if july_3.weekday() < 5 and july_3 not in holidays and date(year, 7, 4).weekday() < 5:
        out.add(july_3)

    return frozenset(out)


def is_nyse_full_holiday(d: date) -> bool:
    return d in nyse_full_holidays(d.year)


def is_nyse_early_close(d: date) -> bool:
    return d in nyse_early_close_dates(d.year)


def session_close(d: date) -> time | None:
    """Regular-session close clock, or None when the floor is closed all day."""
    if d.weekday() >= 5 or is_nyse_full_holiday(d):
        return None
    if is_nyse_early_close(d):
        return time(13, 0)
    return time(16, 0)
