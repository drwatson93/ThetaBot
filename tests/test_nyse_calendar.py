"""NYSE holiday calendar and the order / tax-reserve windows that use it."""
from datetime import date, datetime, time

import pytest
from zoneinfo import ZoneInfo

from agentic.config import Settings, TaxReserveConfig
from agentic.services.market_hours import is_market_hours, is_order_window
from agentic.services.nyse_calendar import (
    is_nyse_early_close,
    is_nyse_full_holiday,
    nyse_full_holidays,
    observed,
    session_close,
    western_easter,
)
from agentic.services.tax_reserve import TaxReserveLoop
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.tax_reserve import TaxReserveStore
from agentic.services.killswitch import KillSwitch

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


def _reserve_loop(tmp_path, name, **tax_kw):
    class _Journal:
        def realized_since(self, _since):
            return 500.0, 1

    db = Database(tmp_path / name)
    audit = AuditStore(db)
    settings = Settings(
        mode="paper",
        trading_start="10:00",
        tax_reserve=TaxReserveConfig(enabled=True, dry_run=True, **tax_kw),
    )
    return TaxReserveLoop(
        settings, None, None, _Journal(), TaxReserveStore(db), audit,
        KillSwitch(db, audit),
    )


@pytest.mark.asyncio
async def test_tax_reserve_waits_for_trading_start(tmp_path):
    loop = _reserve_loop(tmp_path, "start.db", weekday=4, hour=9, minute=0)
    # Sweep time has passed, but 09:45 is still before trading_start.
    assert await loop.run_once(now=_et(date(2026, 9, 11), 9, 45)) is None
    assert loop.store.last() is None


@pytest.mark.asyncio
async def test_tax_reserve_closed_on_holiday_and_after_early_close(tmp_path):
    thanks = _reserve_loop(tmp_path, "holiday.db", weekday=3, hour=11, minute=0)
    assert await thanks.run_once(now=_et(date(2026, 11, 26), 11, 30)) is None
    assert thanks.store.last() is None

    early = _reserve_loop(tmp_path, "early.db", weekday=4, hour=12, minute=0)
    assert await early.run_once(now=_et(date(2026, 11, 27), 13, 30)) is None
    assert early.store.last() is None
