"""NYSE holiday calendar and the order window that uses it."""
from datetime import date, datetime, time

from zoneinfo import ZoneInfo

from agentic.services.market_hours import is_market_hours, is_order_window
from agentic.services.nyse_calendar import (
    is_nyse_early_close,
    is_nyse_full_holiday,
    nyse_full_holidays,
    observed,
    session_close,
    western_easter,
)

ET = ZoneInfo("America/New_York")

HOLIDAYS_2026 = {
    date(2026, 1, 1),    # New Year's
    date(2026, 1, 19),   # MLK
    date(2026, 2, 16),   # Presidents
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence (July 4 is Saturday)
    date(2026, 9, 7),    # Labor
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
}

HOLIDAYS_2027 = {
    date(2027, 1, 1),    # New Year's
    date(2027, 1, 18),   # MLK
    date(2027, 2, 15),   # Presidents
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial
    date(2027, 6, 18),   # Juneteenth (June 19 is Saturday)
    date(2027, 7, 5),    # Independence (July 4 is Sunday)
    date(2027, 9, 6),    # Labor
    date(2027, 11, 25),  # Thanksgiving
    date(2027, 12, 24),  # Christmas (December 25 is Saturday)
    date(2027, 12, 31),  # New Year's 2028 observed (January 1 is Saturday)
}


def _et(d: date, h: int, m: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ET)


def test_official_holidays_2026_and_2027():
    assert nyse_full_holidays(2026) == frozenset(HOLIDAYS_2026)
    assert nyse_full_holidays(2027) == frozenset(HOLIDAYS_2027)
    for d in HOLIDAYS_2026 | HOLIDAYS_2027:
        assert is_nyse_full_holiday(d) is True
        assert session_close(d) is None


def test_easter_and_observed_shifts():
    assert western_easter(2026) == date(2026, 4, 5)
    assert western_easter(2027) == date(2027, 3, 28)
    assert observed(date(2026, 7, 4)) == date(2026, 7, 3)   # Saturday -> Friday
    assert observed(date(2027, 7, 4)) == date(2027, 7, 5)   # Sunday -> Monday
    assert observed(date(2027, 6, 19)) == date(2027, 6, 18)
    assert observed(date(2027, 12, 25)) == date(2027, 12, 24)
    # New Year's Saturday observes the prior Friday (previous calendar year).
    assert observed(date(2033, 1, 1)) == date(2032, 12, 31)
    assert date(2032, 12, 31) in nyse_full_holidays(2032)
    # New Year's Sunday observes Monday.
    assert observed(date(2034, 1, 1)) == date(2034, 1, 2)
    assert is_nyse_full_holiday(date(2034, 1, 2)) is True
    assert is_nyse_full_holiday(date(2026, 9, 21)) is False  # a regular Monday


def test_early_close_day_after_thanksgiving_and_christmas_eve():
    black_friday = date(2026, 11, 27)
    christmas_eve = date(2026, 12, 24)
    assert is_nyse_early_close(black_friday) is True
    assert is_nyse_early_close(christmas_eve) is True
    assert session_close(black_friday) == time(13, 0)
    assert session_close(date(2026, 9, 21)) == time(16, 0)
    # 2027 Christmas Eve is the observed full holiday, not an early close.
    assert is_nyse_full_holiday(date(2027, 12, 24)) is True
    assert is_nyse_early_close(date(2027, 12, 24)) is False
    # July 3 is a full holiday in 2026 (observed Independence), not an early close.
    assert is_nyse_full_holiday(date(2026, 7, 3)) is True
    assert is_nyse_early_close(date(2026, 7, 3)) is False
    # 2025 July 4 is a Friday, so July 3 is the 1pm early close.
    assert is_nyse_early_close(date(2025, 7, 3)) is True


def test_order_window_closed_on_holiday_and_after_early_close():
    thanksgiving = date(2026, 11, 26)
    assert is_order_window(_et(thanksgiving, 11, 0), start="10:00") is False
    assert is_market_hours(_et(thanksgiving, 11, 0)) is False

    early = date(2026, 11, 27)
    assert is_order_window(_et(early, 12, 30), start="10:00") is True
    assert is_market_hours(_et(early, 12, 30)) is True
    assert is_order_window(_et(early, 13, 1), start="10:00") is False
    assert is_market_hours(_et(early, 13, 1)) is False

    monday = date(2026, 9, 21)
    assert is_order_window(_et(monday, 9, 30), start="10:00") is False
    assert is_market_hours(_et(monday, 9, 30)) is True
    assert is_order_window(_et(monday, 10, 0), start="10:00") is True
