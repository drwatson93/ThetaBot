"""Quote freshness is judged per symbol, not at the end of a slow scan.

Synthetic tickers and made-up prices only. No live broker is constructed.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agentic.brokers.robinhood_mcp import RobinhoodMCPBroker
from agentic.config import Settings
from agentic.domain.models import utcnow
from agentic.main import build_market_data
from agentic.marketdata.base import MarketDataProvider
from agentic.marketdata.quote import OptionContractQuote
from agentic.marketdata.robinhood_md import RobinhoodMarketData
from agentic.services.killswitch import KillSwitch
from agentic.services.scanner import OpportunityScanner
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.entry_decisions import EntryDecisionStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app
from agentic.web.health import feed_age_seconds
from tests.fakes import OrderStore

MAX_AGE = 90


def _put(underlying: str, *, as_of=None) -> OptionContractQuote:
    exp = utcnow().date() + timedelta(days=35)
    occ = f"{underlying}{exp:%y%m%d}P{int(40 * 1000):08d}"
    return OptionContractQuote(
        occ_symbol=occ, underlying=underlying, option_id=None, option_type="put",
        strike=40.0, expiration=exp, bid=1.40, ask=1.50, mark=1.45,
        delta=-0.25, iv=0.35, open_interest=400, volume=80,
        as_of=as_of or utcnow(),
    )


class _Book:
    def __init__(self, *, connected=True):
        self.writes: list[str] = []
        self.reads: list[str] = []
        if connected is not None:
            self._connected = connected

    async def get_buying_power(self) -> float:
        self.reads.append("buying_power")
        return 500_000.0

    async def get_account_value(self) -> float:
        self.reads.append("account_value")
        return 500_000.0

    async def get_open_positions(self):
        self.reads.append("positions")
        return []

    async def get_equity_positions(self):
        self.reads.append("equity")
        return []

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
    def __init__(self, chains):
        self._chains = chains

    async def get_quote(self, position):
        return None

    async def get_chain(self, underlying: str):
        return list(self._chains.get(underlying, []))

    async def get_underlying_bars(self, underlying: str, lookback_days: int = 260):
        return []

    async def get_underlying_price(self, underlying: str):
        return 100.0


class _NoOrders:
    async def execute_open(self, decision, quote):
        raise AssertionError("scan tried to execute an order")


def _scanner(tmp_path, book, market_data, watchlist):
    db = Database(tmp_path / "fresh.db")
    audit = AuditStore(db)
    settings = Settings(
        mode="paper", broker="paper", market_data="paper",
        max_quote_age_seconds=MAX_AGE,
        entry={"enabled": True, "watchlist": watchlist},
    )
    return OpportunityScanner(
        settings, book, market_data, EntryDecisionStore(db), _NoOrders(),
        audit, KillSwitch(db, audit),
    )


@pytest.fixture()
def _open(monkeypatch):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)


@pytest.mark.asyncio
async def test_slow_sequential_scan_alerts_when_each_quote_is_fresh(tmp_path, _open, monkeypatch):
    """Each symbol's quote is fresh when that symbol is evaluated.

    The clock jumps 50s inside every chain fetch. After three names the first
    quote is older than max_quote_age, which used to skip the whole scan.
    """
    clock = {"now": datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc)}

    def _now():
        return clock["now"]

    monkeypatch.setattr("agentic.services.scanner.utcnow", _now)

    class _Slow(MarketDataProvider):
        def __init__(self):
            self.order: list[str] = []

        async def get_quote(self, position):
            return None

        async def get_chain(self, underlying: str):
            clock["now"] = clock["now"] + timedelta(seconds=50)
            self.order.append(underlying)
            return [_put(underlying, as_of=clock["now"])]

        async def get_underlying_bars(self, underlying: str, lookback_days: int = 260):
            return []

        async def get_underlying_price(self, underlying: str):
            return 100.0

    book = _Book()
    slow = _Slow()
    names = ["XYZ", "ABC", "LMN"]
    scanner = _scanner(tmp_path, book, slow, names)

    assert await scanner.run_once() == 3
    assert slow.order == names
    assert {a["underlying"] for a in scanner.alerts.all()} == set(names)
    run = scanner.alerts.runs()[-1]
    assert run["status"] == "ok" and run["reason"] is None
    assert not any(s.get("reason") == "stale quote" for s in scanner.last_skips)
    ages = [(clock["now"] - q.as_of).total_seconds() for q in scanner.last_quotes.values()]
    assert max(ages) > MAX_AGE
    assert min(ages) < MAX_AGE
    assert feed_age_seconds(scanner, now=clock["now"]) == min(ages)
    assert book.writes == []


@pytest.mark.asyncio
async def test_one_stale_symbol_is_skipped_and_others_alert(tmp_path, _open):
    """A quote that is already old at evaluation blocks that symbol only."""
    fresh_xyz = _put("XYZ")
    stale_abc = _put("ABC", as_of=utcnow() - timedelta(seconds=200))
    fresh_lmn = _put("LMN")
    book = _Book()
    scanner = _scanner(
        tmp_path, book, _Chain({"XYZ": [fresh_xyz], "ABC": [stale_abc], "LMN": [fresh_lmn]}),
        ["XYZ", "ABC", "LMN"],
    )

    assert await scanner.run_once() == 2
    assert {a["underlying"] for a in scanner.alerts.all()} == {"XYZ", "LMN"}
    run = scanner.alerts.runs()[-1]
    assert run["status"] == "ok" and run["alerts_sent"] == 0
    assert "ABC: stale quote" in (run["reason"] or "")
    assert {"symbol": "ABC", "reason": "stale quote"} in scanner.last_skips
    assert not any(s["symbol"] in {"XYZ", "LMN"} and s["reason"] == "stale quote"
                   for s in scanner.last_skips)

    db = scanner.audit.db
    deps = WebDeps(
        settings=scanner.settings, signals=SignalStore(db), killswitch=scanner.killswitch,
        approval_gate=None, audit=scanner.audit, positions=PositionStore(db),
        orders=OrderStore(db), decisions=DecisionStore(db),
        entry_decisions=scanner.entry_decisions, scanner=scanner,
    )
    body = TestClient(create_app(deps)).get("/api/scan-status").json()
    assert {"symbol": "ABC", "reason": "stale quote"} in body["last_skips"]
    assert book.writes == []


@pytest.mark.asyncio
async def test_all_stale_skips_the_whole_scan(tmp_path, _open):
    old = utcnow() - timedelta(seconds=200)
    chains = {name: [_put(name, as_of=old)] for name in ("XYZ", "ABC")}
    book = _Book()
    scanner = _scanner(tmp_path, book, _Chain(chains), ["XYZ", "ABC"])

    assert await scanner.run_once() == 0
    assert scanner.alerts.all() == []
    run = scanner.alerts.runs()[-1]
    assert run["status"] == "skipped" and run["reason"] == "stale quote"
    assert run["alerts_sent"] == 0
    assert {s["symbol"] for s in scanner.last_skips if s["reason"] == "stale quote"} == {"XYZ", "ABC"}
    assert feed_age_seconds(scanner) > MAX_AGE
    assert book.writes == []


@pytest.mark.asyncio
async def test_broker_down_skips_the_whole_scan(tmp_path, _open):
    book = _Book(connected=False)
    scanner = _scanner(tmp_path, book, _Chain({"XYZ": [_put("XYZ")]}), ["XYZ"])

    assert await scanner.run_once() == 0
    assert scanner.alerts.all() == []
    assert book.reads == [] and book.writes == []
    run = scanner.alerts.runs()[-1]
    assert run["status"] == "skipped" and run["reason"] == "broker degraded"
    assert run["alerts_sent"] == 0


class _Clock:
    def __init__(self, t: float):
        self.t = t

    def time(self) -> float:
        return self.t

    def utcnow(self) -> datetime:
        return datetime.fromtimestamp(self.t, tz=timezone.utc)


class _RhBook:
    """In-memory stand-in for the Robinhood read tools get_chain uses."""

    def __init__(self):
        self.calls: list[str] = []
        self.fail_chains = False
        self.exp = (date.today() + timedelta(days=10)).isoformat()

    async def _call_tool(self, name, args):
        self.calls.append(name)
        if name == "get_equity_quotes":
            return {"data": {"results": [{"quote": {"last_trade_price": "100"}}]}}
        if name == "get_option_chains":
            if self.fail_chains:
                raise RuntimeError("chains down")
            return {"data": {"chains": [{"expiration_dates": [self.exp]}]}}
        if name == "get_option_instruments":
            return {"data": {"instruments": [{"id": "inst-xyz", "strike_price": "90"}]}}
        if name == "get_option_quotes":
            return {"data": {"results": [{"quote": {
                "instrument_id": "inst-xyz",
                "bid_price": "1.20", "ask_price": "1.30",
                "adjusted_mark_price": "1.25", "delta": "-0.25",
            }}]}}
        raise AssertionError(name)

    _iter_records = staticmethod(RobinhoodMCPBroker._iter_records)


@pytest.mark.asyncio
async def test_chain_cache_fallback_keeps_the_real_age(monkeypatch):
    """A cache hit stays inside max quote age. A failed refetch returns the old as_of."""
    clock = _Clock(1_800_000_000.0)
    monkeypatch.setattr("agentic.marketdata.robinhood_md.time.time", clock.time)

    class _DateTime(datetime):
        """utcnow() reads datetime from domain.models at call time."""

        @classmethod
        def now(cls, tz=None):
            return clock.utcnow()

    monkeypatch.setattr("agentic.domain.models.datetime", _DateTime)
    book = _RhBook()
    md = RobinhoodMarketData(book, chain_ttl_seconds=240, max_quote_age_seconds=MAX_AGE)
    assert md._chain_ttl == MAX_AGE

    first = await md.get_chain("XYZ")
    assert len(first) == 1
    original = first[0]
    assert original.underlying == "XYZ"
    assert original.as_of == clock.utcnow()
    assert not original.is_stale(MAX_AGE, clock.utcnow())
    chain_calls = book.calls.count("get_option_chains")
    assert chain_calls == 1

    clock.t += 30
    hit = await md.get_chain("XYZ")
    assert hit[0] is original
    assert hit[0].as_of == original.as_of
    assert book.calls.count("get_option_chains") == chain_calls
    assert not hit[0].is_stale(MAX_AGE, clock.utcnow())

    clock.t += 80  # 110s from the fetch, past both the TTL cap and max quote age
    book.fail_chains = True
    fallback = await md.get_chain("XYZ")
    assert fallback[0] is original
    assert fallback[0].as_of == original.as_of
    assert book.calls.count("get_option_chains") == chain_calls + 1
    assert fallback[0].is_stale(MAX_AGE, clock.utcnow())
    assert fallback[0].age_seconds(clock.utcnow()) > MAX_AGE

    book.fail_chains = False
    refetched = await md.get_chain("XYZ")
    assert refetched[0] is not original
    assert refetched[0].as_of == clock.utcnow()
    assert not refetched[0].is_stale(MAX_AGE, clock.utcnow())


def test_build_market_data_caps_chain_ttl_at_max_quote_age():
    broker = _RhBook()
    settings = Settings(market_data="robinhood", max_quote_age_seconds=120)
    md = build_market_data(settings, broker)
    assert isinstance(md, RobinhoodMarketData)
    assert md._chain_ttl == 120

    loose = RobinhoodMarketData(broker, chain_ttl_seconds=240, max_quote_age_seconds=300)
    assert loose._chain_ttl == 240
