"""Taken, stale, exit, and scorecard. Synthetic tickers and made-up prices only.

No live broker is constructed. A write method on the book fails the test.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agentic.config import Settings
from agentic.domain.enums import Direction, OptionType, PositionStatus, Strategy
from agentic.domain.models import EquityHolding, Position, utcnow
from agentic.marketdata.base import MarketDataProvider
from agentic.marketdata.quote import OptionContractQuote
from agentic.notify.trade_alerts import TradeAlerts
from agentic.services.alert_score import exit_due, play_pnl, profit_fraction
from agentic.services.killswitch import KillSwitch
from agentic.services.scanner import OpportunityScanner
from agentic.store.alerts import AlertStore
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.entry_decisions import EntryDecisionStore

HOOK = "https://alerts.example.test/hook/scan"
HEADER_NAME = "X-Test-Key"
HEADER_VALUE = "header-value-not-a-real-secret"


def _contract(underlying, option_type, strike, dte, delta, bid, ask, *, as_of=None):
    exp = utcnow().date() + timedelta(days=dte)
    side = "P" if option_type == "put" else "C"
    occ = f"{underlying}{exp:%y%m%d}{side}{int(round(strike * 1000)):08d}"
    quote = OptionContractQuote(
        occ_symbol=occ, underlying=underlying, option_id=None,
        option_type=option_type, strike=float(strike), expiration=exp,
        bid=bid, ask=ask, mark=round((bid + ask) / 2, 4),
        delta=delta, iv=0.35, open_interest=400, volume=80,
    )
    if as_of is not None:
        quote.as_of = as_of
    return quote


class _Book:
    """Read-only account. A write method records the call and raises."""

    def __init__(self, buying_power, account_value, holdings=None, positions=None, *,
                 connected=True):
        self.writes: list[str] = []
        self.reads: list[str] = []
        self._bp = float(buying_power)
        self._av = float(account_value)
        self._holdings = list(holdings or [])
        self.positions = list(positions or [])
        if connected is not None:
            self._connected = connected

    async def get_buying_power(self) -> float:
        self.reads.append("buying_power")
        return self._bp

    async def get_account_value(self) -> float:
        self.reads.append("account_value")
        return self._av

    async def get_open_positions(self):
        self.reads.append("positions")
        return list(self.positions)

    async def get_equity_positions(self):
        self.reads.append("equity")
        return list(self._holdings)

    def _refuse(self, name: str):
        self.writes.append(name)
        raise AssertionError(f"scan tried to place an order via {name}")

    async def submit_open_order(self, order):
        self._refuse("submit_open_order")

    async def submit_close_order(self, order):
        self._refuse("submit_close_order")

    async def submit_equity_order(self, **kwargs):
        self._refuse("submit_equity_order")

    async def cancel_order(self, order):
        self._refuse("cancel_order")


class _Chain(MarketDataProvider):
    def __init__(self, chains, price=100.0):
        self._chains = chains
        self._price = price

    async def get_quote(self, position):
        return None

    async def get_chain(self, underlying: str):
        return list(self._chains.get(underlying, []))

    async def get_underlying_bars(self, underlying: str, lookback_days: int = 260):
        return []

    async def get_underlying_price(self, underlying: str):
        return self._price


class _NoOrders:
    async def execute_open(self, decision, quote):
        raise AssertionError("scan tried to execute an order")


def _scanner(tmp_path, book, chains, watchlist, *, price=100.0, trade_alerts=None):
    db = Database(tmp_path / "alerts.db")
    audit = AuditStore(db)
    settings = Settings(
        mode="paper", broker="paper", market_data="paper",
        entry={"enabled": True, "watchlist": watchlist},
    )
    scanner = OpportunityScanner(
        settings, book, _Chain(chains, price=price), EntryDecisionStore(db), _NoOrders(),
        audit, KillSwitch(db, audit), alerts=AlertStore(db), trade_alerts=trade_alerts,
    )
    return scanner


def _short(occ, underlying, option_type, strike, expiration, quantity, *,
            average, credit=9.99, opened_at=None, direction=Direction.SHORT):
    kind = OptionType.PUT if option_type == "put" else OptionType.CALL
    strategy = Strategy.CASH_SECURED_PUT if kind is OptionType.PUT else Strategy.COVERED_CALL
    return Position(
        occ_symbol=occ, underlying=underlying, option_type=kind, strategy=strategy,
        direction=direction, quantity=quantity, strike=float(strike), expiration=expiration,
        credit_received=credit, open_avg_price=average, opened_at=opened_at,
        status=PositionStatus.OPEN,
    )


def _csp_book(positions=None):
    return _Book(buying_power=10_000, account_value=200_000, positions=positions)


@pytest.fixture()
def _open(monkeypatch):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)


def _put(underlying="AAA"):
    return _contract(underlying, "put", 40, 35, -0.25, 1.40, 1.50)


def test_exit_boundaries():
    """49.9% vs 50% at the halfway mark, the day-10 / day-20 examples, and 80%."""
    t0 = datetime(2026, 3, 1, 15, 0, tzinfo=timezone.utc)
    expiry = t0 + timedelta(days=30)
    half = t0 + (expiry - t0) / 2
    day10 = t0 + timedelta(days=10)
    day20 = t0 + timedelta(days=20)
    day25 = t0 + timedelta(days=25)

    assert exit_due(0.499, half, t0, expiry) is False
    assert exit_due(0.50, half, t0, expiry) is True
    assert exit_due(0.50, half + timedelta(seconds=1), t0, expiry) is False
    assert exit_due(0.52, day10, t0, expiry) is True
    assert exit_due(0.52, day20, t0, expiry) is False
    assert exit_due(0.81, day25, t0, expiry) is True
    assert exit_due(0.799, day25, t0, expiry) is False
    # 0.2 is not exact in binary. 80% of a 1.00 credit still clears the boundary.
    assert exit_due(profit_fraction(1.0, 0.2), day25, t0, expiry) is True


def test_scorecard_pnl_uses_intrinsic_at_expiry():
    assert play_pnl(kind="CSP", strike=40, spot=100, credit=1.10, contracts=2) == 220.0
    assert play_pnl(kind="CSP", strike=40, spot=36, credit=1.10, contracts=2) == -580.0
    assert play_pnl(kind="CC", strike=30, spot=20, credit=0.80, contracts=1) == 80.0
    assert play_pnl(kind="CC", strike=30, spot=35, credit=0.80, contracts=1) == -420.0


@pytest.mark.asyncio
async def test_match_on_occ_and_side_marks_taken(tmp_path, _open):
    put = _put()
    book = _csp_book()
    scanner = _scanner(tmp_path, book, {"AAA": [put]}, ["AAA"])
    assert await scanner.run_once() == 1
    alert = scanner.alerts.all()[0]
    assert alert["status"] == "new" and alert["contracts"] == 2
    assert len(scanner.alerts.scorecards()) == 1

    book.positions = [_short(
        alert["occ_symbol"], "AAA", "put", 40, put.expiration, alert["contracts"],
        average=1.10,
    )]
    before = book.reads.count("positions")
    assert await scanner.run_once() == 0
    # The match uses the positions this scan already read.
    assert book.reads.count("positions") == before + 1
    row = scanner.alerts.all()[0]
    assert row["status"] == "taken"
    assert row["fill_price"] == pytest.approx(1.10)
    assert row["taken_at"] and row["late"] == 0 and row["partial"] == 0
    assert scanner.alerts.scorecards()[0]["taken"] == 1
    assert book.writes == []


@pytest.mark.asyncio
async def test_covered_call_match_is_taken(tmp_path, _open):
    call = _contract("AAA", "call", 25, 35, 0.25, 1.00, 1.10)
    book = _Book(
        buying_power=50_000, account_value=50_000,
        holdings=[EquityHolding("AAA", 100, 20.0)],
    )
    scanner = _scanner(tmp_path, book, {"AAA": [call]}, [], price=30.0)
    assert await scanner.run_once() == 1
    alert = scanner.alerts.all()[0]
    assert alert["kind"] == "CC" and alert["contracts"] == 1
    book.positions = [_short(
        alert["occ_symbol"], "AAA", "call", 25, call.expiration, 1, average=0.90,
    )]
    assert await scanner.run_once() == 0
    row = scanner.alerts.all()[0]
    assert row["status"] == "taken" and row["fill_price"] == pytest.approx(0.90)
    assert row["partial"] == 0 and row["late"] == 0


@pytest.mark.asyncio
async def test_long_same_occ_goes_stale(tmp_path, _open):
    put = _put()
    book = _csp_book()
    scanner = _scanner(tmp_path, book, {"AAA": [put]}, ["AAA"])
    assert await scanner.run_once() == 1
    alert = scanner.alerts.all()[0]
    book.positions = [_short(
        alert["occ_symbol"], "AAA", "put", 40, put.expiration, 2, average=1.10,
        direction=Direction.LONG,
    )]
    assert await scanner.run_once() == 0
    row = scanner.alerts.all()[0]
    assert row["status"] == "stale" and row["stale_at"] and row["taken_at"] is None


@pytest.mark.asyncio
async def test_no_match_on_the_next_scan_is_stale(tmp_path, _open):
    put = _put()
    book = _csp_book()
    scanner = _scanner(tmp_path, book, {"AAA": [put]}, ["AAA"])
    assert await scanner.run_once() == 1
    assert scanner.alerts.all()[0]["status"] == "new"

    assert await scanner.run_once() == 0
    row = scanner.alerts.all()[0]
    assert row["status"] == "stale" and row["stale_at"]
    assert row["taken_at"] is None and row["late"] == 0
    assert len(scanner.alerts.scorecards()) == 1


@pytest.mark.asyncio
async def test_stale_match_before_expiry_is_late(tmp_path, _open):
    put = _put()
    book = _csp_book()
    scanner = _scanner(tmp_path, book, {"AAA": [put]}, ["AAA"])
    assert await scanner.run_once() == 1
    assert await scanner.run_once() == 0
    assert scanner.alerts.all()[0]["status"] == "stale"

    alert = scanner.alerts.all()[0]
    book.positions = [_short(
        alert["occ_symbol"], "AAA", "put", 40, put.expiration, alert["contracts"],
        average=1.25,
    )]
    assert await scanner.run_once() == 0
    row = scanner.alerts.all()[0]
    assert row["status"] == "taken" and row["late"] == 1
    assert row["fill_price"] == pytest.approx(1.25) and row["taken_at"]
    assert row["partial"] == 0


@pytest.mark.asyncio
async def test_partial_quantity_is_flagged(tmp_path, _open):
    put = _put()
    book = _csp_book()
    scanner = _scanner(tmp_path, book, {"AAA": [put]}, ["AAA"])
    assert await scanner.run_once() == 1
    alert = scanner.alerts.all()[0]
    assert alert["contracts"] == 2
    book.positions = [_short(
        alert["occ_symbol"], "AAA", "put", 40, put.expiration, 1, average=1.10,
    )]
    assert await scanner.run_once() == 0
    row = scanner.alerts.all()[0]
    assert row["status"] == "taken" and row["partial"] == 1
    assert row["contracts"] == 2
    assert len(scanner.alerts.all()) == 1


@pytest.mark.asyncio
async def test_after_expiry_a_match_is_expired(tmp_path, _open, monkeypatch):
    put = _put()
    book = _csp_book()
    scanner = _scanner(tmp_path, book, {"AAA": [put]}, ["AAA"])
    assert await scanner.run_once() == 1
    assert await scanner.run_once() == 0
    assert scanner.alerts.all()[0]["status"] == "stale"

    later = datetime.combine(
        put.expiration + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc,
    ) + timedelta(hours=16)
    put.as_of = later
    monkeypatch.setattr("agentic.services.scanner.utcnow", lambda: later)
    alert = scanner.alerts.all()[0]
    book.positions = [_short(
        alert["occ_symbol"], "AAA", "put", 40, put.expiration, alert["contracts"],
        average=1.10,
    )]
    assert await scanner.run_once() == 0
    row = scanner.alerts.all()[0]
    assert row["status"] == "expired"
    assert row["taken_at"] is None and row["fill_price"] is None


@pytest.mark.asyncio
async def test_stale_quote_does_not_reconcile(tmp_path, _open):
    put = _put()
    book = _csp_book()
    scanner = _scanner(tmp_path, book, {"AAA": [put]}, ["AAA"])
    assert await scanner.run_once() == 1
    alert = scanner.alerts.all()[0]
    book.positions = [_short(
        alert["occ_symbol"], "AAA", "put", 40, put.expiration, 2, average=1.10,
    )]
    put.as_of = utcnow() - timedelta(seconds=120)
    assert await scanner.run_once() == 0
    assert scanner.alerts.all()[0]["status"] == "new"
    run = scanner.alerts.runs()[-1]
    assert run["status"] == "skipped" and run["reason"] == "stale quote"
    assert run["alerts_sent"] == 0

    put.as_of = utcnow()
    assert await scanner.run_once() == 0
    row = scanner.alerts.all()[0]
    assert row["status"] == "taken" and row["late"] == 0


@pytest.mark.asyncio
async def test_broker_down_does_not_reconcile(tmp_path, _open):
    put = _put()
    book = _csp_book()
    scanner = _scanner(tmp_path, book, {"AAA": [put]}, ["AAA"])
    assert await scanner.run_once() == 1
    book._connected = False
    reads = len(book.reads)
    assert await scanner.run_once() == 0
    assert scanner.alerts.all()[0]["status"] == "new"
    assert book.reads[reads:] == []
    run = scanner.alerts.runs()[-1]
    assert run["status"] == "skipped" and run["reason"] == "broker degraded"


@pytest.mark.asyncio
async def test_empty_chain_does_not_reconcile(tmp_path, _open):
    put = _put()
    chains = {"AAA": [put]}
    book = _csp_book()
    scanner = _scanner(tmp_path, book, chains, ["AAA"])
    assert await scanner.run_once() == 1
    alert = scanner.alerts.all()[0]
    book.positions = [_short(
        alert["occ_symbol"], "AAA", "put", 40, put.expiration, 2, average=1.10,
    )]
    chains["AAA"] = []
    assert await scanner.run_once() == 0
    assert scanner.alerts.all()[0]["status"] == "new"
    run = scanner.alerts.runs()[-1]
    assert run["status"] == "skipped" and run["reason"] == "empty chain"


def _held_put(occ, expiration, *, opened_at, average=1.0, quantity=1):
    return _short(
        occ, "AAA", "put", 40, expiration, quantity, average=average, credit=average,
        opened_at=opened_at,
    )


def _exit_quote(t0, *, dte_days, bid, ask):
    exp = (t0 + timedelta(days=dte_days)).date()
    occ = f"AAA{exp:%y%m%d}P{int(round(40 * 1000)):08d}"
    quote = OptionContractQuote(
        occ_symbol=occ, underlying="AAA", option_id=None, option_type="put",
        strike=40.0, expiration=exp, bid=bid, ask=ask,
        mark=round((bid + ask) / 2, 4), delta=-0.25, iv=0.35,
        open_interest=400, volume=80,
    )
    return quote


@pytest.mark.asyncio
async def test_exit_fires_once_on_day_10_of_30(tmp_path, _open, monkeypatch):
    t0 = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)
    now = t0 + timedelta(days=10)
    quote = _exit_quote(t0, dte_days=30, bid=0.47, ask=0.49)  # mid 0.48 → 52% of 1.00
    quote.as_of = now
    monkeypatch.setattr("agentic.services.scanner.utcnow", lambda: now)
    sent: list[dict] = []
    monkeypatch.setenv("TRADE_ALERT_URL", HOOK)
    monkeypatch.setenv("TRADE_ALERT_HEADER_NAME", HEADER_NAME)
    monkeypatch.setenv("TRADE_ALERT_HEADER_VALUE", HEADER_VALUE)
    monkeypatch.setattr(
        "agentic.notify.trade_alerts._http_post", lambda url, payload, timeout=3.0: sent.append(payload),
    )
    db = Database(tmp_path / "exit.db")
    audit = AuditStore(db)
    transport = TradeAlerts(db, audit, Settings(mode="paper"), background=False)
    book = _Book(
        buying_power=50_000, account_value=50_000,
        positions=[_held_put(quote.occ_symbol, quote.expiration, opened_at=t0, average=1.0)],
    )
    scanner = OpportunityScanner(
        Settings(mode="paper", broker="paper", market_data="paper",
                 entry={"enabled": True, "watchlist": []}),
        book, _Chain({"AAA": [quote]}), EntryDecisionStore(db), _NoOrders(),
        audit, KillSwitch(db, audit), alerts=AlertStore(db), trade_alerts=transport,
    )

    assert await scanner.run_once() == 1
    alerts = scanner.alerts.all()
    assert len(alerts) == 1
    row = alerts[0]
    assert row["kind"] == "exit"
    assert row["dedup_key"] == f"exit:AAA:{quote.occ_symbol}"
    assert row["delivery_status"] == "sent" and row["status"] == "new"
    assert len(scanner.alerts.scorecards()) == 1
    card = scanner.alerts.scorecards()[0]
    assert card["alert_id"] == row["id"]
    assert card["strike"] == 40.0 and card["contracts"] == 1
    assert card["mid"] == pytest.approx(0.48) and card["taken"] == 0
    assert card["pnl"] is None
    assert len(sent) == 1
    body = json.dumps(sent[0])
    assert sent[0]["event"] == "scan_alert" and sent[0]["kind"] == "exit"
    assert sent[0]["action"] == "BTC" and sent[0]["right"] == "P"
    for banned in ("buying_power", "account_value", "balance", "cash", HEADER_VALUE, HOOK):
        assert banned not in body
    assert book.writes == []

    assert await scanner.run_once() == 0
    assert len(scanner.alerts.all()) == 1
    assert len(sent) == 1
    assert scanner.alerts.all()[0]["status"] == "new"
    assert len(scanner.alerts.scorecards()) == 1


@pytest.mark.asyncio
async def test_unsent_exit_retries_after_the_rule_stops(tmp_path, _open, monkeypatch):
    """A failed exit post is retried later, even when the 50% window has closed."""
    t0 = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)
    now = t0 + timedelta(days=10)
    quote = _exit_quote(t0, dte_days=30, bid=0.47, ask=0.49)
    quote.as_of = now
    clock = {"now": now}
    monkeypatch.setattr("agentic.services.scanner.utcnow", lambda: clock["now"])
    monkeypatch.setenv("TRADE_ALERT_URL", HOOK)
    attempts: list[int] = []

    def _post(url, payload, timeout=3.0):
        attempts.append(1)
        if len(attempts) <= 2:
            raise OSError("webhook down")

    monkeypatch.setattr("agentic.notify.trade_alerts._http_post", _post)
    db = Database(tmp_path / "retry.db")
    audit = AuditStore(db)
    transport = TradeAlerts(db, audit, Settings(mode="paper"), background=False)
    book = _Book(
        buying_power=50_000, account_value=50_000,
        positions=[_held_put(quote.occ_symbol, quote.expiration, opened_at=t0, average=1.0)],
    )
    scanner = OpportunityScanner(
        Settings(mode="paper", broker="paper", market_data="paper",
                 entry={"enabled": True, "watchlist": []}),
        book, _Chain({"AAA": [quote]}), EntryDecisionStore(db), _NoOrders(),
        audit, KillSwitch(db, audit), alerts=AlertStore(db), trade_alerts=transport,
    )
    assert await scanner.run_once() == 1
    assert scanner.alerts.all()[0]["delivery_status"] == "failed"
    assert scanner.alerts.runs()[-1]["alerts_sent"] == 0

    clock["now"] = t0 + timedelta(days=20)
    quote.as_of = clock["now"]
    assert await scanner.run_once() == 0
    assert len(scanner.alerts.all()) == 1
    assert scanner.alerts.all()[0]["kind"] == "exit"
    assert scanner.alerts.all()[0]["delivery_status"] == "sent"
    assert scanner.alerts.runs()[-1]["alerts_sent"] == 1
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_exit_half_time_boundary_through_the_scan(tmp_path, _open, monkeypatch):
    t0 = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)
    half = t0 + timedelta(days=15)
    monkeypatch.setattr("agentic.services.scanner.utcnow", lambda: half)

    under = _exit_quote(t0, dte_days=30, bid=50.1, ask=50.1)  # 49.9% of a 100 credit
    under.as_of = half
    book = _Book(
        buying_power=50_000, account_value=50_000,
        positions=[_held_put(under.occ_symbol, under.expiration, opened_at=t0, average=100.0)],
    )
    quiet = _scanner(tmp_path, book, {"AAA": [under]}, [])
    assert await quiet.run_once() == 0
    assert quiet.alerts.all() == []

    even = _exit_quote(t0, dte_days=30, bid=50.0, ask=50.0)  # 50% of a 100 credit
    even.as_of = half
    book.positions = [_held_put(even.occ_symbol, even.expiration, opened_at=t0, average=100.0)]
    fired = _scanner(tmp_path / "half", book, {"AAA": [even]}, [])
    assert await fired.run_once() == 1
    assert fired.alerts.all()[0]["kind"] == "exit"


@pytest.mark.asyncio
async def test_exit_52_percent_on_day_20_does_not_fire(tmp_path, _open, monkeypatch):
    t0 = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)
    now = t0 + timedelta(days=20)
    quote = _exit_quote(t0, dte_days=30, bid=0.47, ask=0.49)
    quote.as_of = now
    monkeypatch.setattr("agentic.services.scanner.utcnow", lambda: now)
    book = _Book(
        buying_power=50_000, account_value=50_000,
        positions=[_held_put(quote.occ_symbol, quote.expiration, opened_at=t0, average=1.0)],
    )
    scanner = _scanner(tmp_path, book, {"AAA": [quote]}, [])
    assert await scanner.run_once() == 0
    assert scanner.alerts.all() == []
    assert scanner.alerts.runs()[-1]["status"] == "ok"


@pytest.mark.asyncio
async def test_missing_quote_or_open_time_does_not_crash(tmp_path, _open, monkeypatch):
    t0 = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)
    now = t0 + timedelta(days=10)
    monkeypatch.setattr("agentic.services.scanner.utcnow", lambda: now)
    quote = _exit_quote(t0, dte_days=30, bid=0.10, ask=0.10)  # would be an 80%+ exit
    quote.as_of = now
    held = _held_put(quote.occ_symbol, quote.expiration, opened_at=t0, average=1.0)

    no_quote = _Book(buying_power=50_000, account_value=50_000, positions=[held])
    scanner = _scanner(tmp_path, no_quote, {"AAA": []}, [])
    assert await scanner.run_once() == 0
    assert scanner.alerts.all() == []
    run = scanner.alerts.runs()[-1]
    assert run["status"] == "ok" and "missing quote" in run["reason"]
    assert no_quote.writes == []

    no_open = _Book(
        buying_power=50_000, account_value=50_000,
        positions=[_held_put(quote.occ_symbol, quote.expiration, opened_at=None, average=1.0)],
    )
    blind = _scanner(tmp_path / "open", no_open, {"AAA": [quote]}, [])
    assert await blind.run_once() == 0
    assert blind.alerts.all() == []
    assert "missing open time" in blind.alerts.runs()[-1]["reason"]
    assert no_open.writes == []


@pytest.mark.asyncio
async def test_exit_uses_fill_price_when_the_alert_was_taken(tmp_path, _open, monkeypatch):
    """Past halfway, 85% of the fill fires and 70% of the broker average does not."""
    t0 = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)
    now = t0 + timedelta(days=20)
    monkeypatch.setattr("agentic.services.scanner.utcnow", lambda: now)
    quote = _exit_quote(t0, dte_days=30, bid=0.30, ask=0.30)
    quote.as_of = now
    db = Database(tmp_path / "fill.db")
    alerts = AlertStore(db)
    exp = quote.expiration.isoformat()
    alerts.insert_if_new({
        "dedup_key": f"CSP:AAA:{quote.occ_symbol}",
        "kind": "CSP", "underlying": "AAA", "occ_symbol": quote.occ_symbol,
        "strike": 40.0, "expiration": exp, "contracts": 1, "premium": 1.50,
        "bid": 1.40, "ask": 1.60, "created_at": t0.isoformat(),
    })
    row = alerts.get_by_dedup(f"CSP:AAA:{quote.occ_symbol}")
    alerts.mark_taken(
        row["id"], fill_price=2.0, taken_at=t0.isoformat(), late=False, partial=False,
    )
    book = _Book(
        buying_power=50_000, account_value=50_000,
        positions=[_held_put(
            quote.occ_symbol, quote.expiration, opened_at=None, average=1.0,
        )],
    )
    audit = AuditStore(db)
    scanner = OpportunityScanner(
        Settings(mode="paper", broker="paper", market_data="paper",
                 entry={"enabled": True, "watchlist": []}),
        book, _Chain({"AAA": [quote]}), EntryDecisionStore(db), _NoOrders(),
        audit, KillSwitch(db, audit), alerts=alerts,
    )
    assert await scanner.run_once() == 1
    kinds = [a["kind"] for a in scanner.alerts.all()]
    assert kinds.count("exit") == 1
    assert len(scanner.alerts.scorecards()) == 2


@pytest.mark.asyncio
async def test_one_scorecard_row_and_taken_versus_hypothetical(tmp_path, _open, monkeypatch):
    aaa = _put("AAA")
    bbb = _put("BBB")
    book = _Book(buying_power=500_000, account_value=500_000)
    scanner = _scanner(tmp_path, book, {"AAA": [aaa], "BBB": [bbb]}, ["AAA", "BBB"], price=100.0)
    assert await scanner.run_once() == 2
    by_name = {a["underlying"]: a for a in scanner.alerts.all()}
    assert set(by_name) == {"AAA", "BBB"}
    book.positions = [_short(
        by_name["AAA"]["occ_symbol"], "AAA", "put", 40, aaa.expiration,
        by_name["AAA"]["contracts"], average=1.10,
    )]
    assert await scanner.run_once() == 0
    assert scanner.alerts.get_by_dedup(by_name["AAA"]["dedup_key"])["status"] == "taken"
    assert scanner.alerts.get_by_dedup(by_name["BBB"]["dedup_key"])["status"] == "stale"

    later = datetime.combine(
        aaa.expiration + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc,
    ) + timedelta(hours=16)
    aaa.as_of = later
    bbb.as_of = later
    monkeypatch.setattr("agentic.services.scanner.utcnow", lambda: later)
    assert await scanner.run_once() == 0

    alerts = {a["underlying"]: a for a in scanner.alerts.all()}
    by_id = {c["alert_id"]: c for c in scanner.alerts.scorecards()}
    assert len(by_id) == len(alerts) == 2
    taken = alerts["AAA"]
    skipped = alerts["BBB"]
    taken_card = by_id[taken["id"]]
    skipped_card = by_id[skipped["id"]]
    assert taken["status"] == "taken" and skipped["status"] == "expired"
    assert taken_card["strike"] == taken["strike"]
    assert taken_card["expiration"] == taken["expiration"]
    assert taken_card["contracts"] == taken["contracts"]
    assert taken_card["mid"] == pytest.approx(taken["premium"])
    assert taken_card["taken"] == 1 and taken_card["result"] == "realized"
    assert skipped_card["taken"] == 0 and skipped_card["result"] == "hypothetical"
    assert taken_card["pnl"] == play_pnl(
        kind="CSP", strike=taken["strike"], spot=100.0, credit=taken["fill_price"],
        contracts=taken["contracts"],
    )
    assert skipped_card["pnl"] == play_pnl(
        kind="CSP", strike=skipped["strike"], spot=100.0, credit=skipped["premium"],
        contracts=skipped["contracts"],
    )
    assert taken_card["pnl"] != skipped_card["pnl"]

    scanner.market_data._price = 1.0
    graded = taken_card["graded_at"]
    assert await scanner.run_once() == 0
    again = {c["alert_id"]: c for c in scanner.alerts.scorecards()}
    assert again[taken["id"]]["pnl"] == taken_card["pnl"]
    assert again[skipped["id"]]["pnl"] == skipped_card["pnl"]
    assert again[taken["id"]]["graded_at"] == graded
    assert len(scanner.alerts.scorecards()) == 2

    reopened = Database(tmp_path / "alerts.db")
    assert len(AlertStore(reopened).scorecards()) == 2
