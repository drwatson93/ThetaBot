"""Scanner alerts: size off buying power, one alert per play, skip a dead feed.

Synthetic tickers and made-up prices. The book records any write and the test
fails if the scan calls one. No live broker is constructed.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from agentic.config import Settings
from agentic.domain.models import EquityHolding, utcnow
from agentic.marketdata.base import MarketDataProvider
from agentic.marketdata.quote import OptionContractQuote
from agentic.notify.trade_alerts import TradeAlerts
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

    def __init__(self, buying_power, account_value, holdings=None, *, connected=True):
        self.writes: list[str] = []
        self.reads: list[str] = []
        self._bp = float(buying_power)
        self._av = float(account_value)
        self._holdings = list(holdings or [])
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
        return []

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


def _scanner(tmp_path, book, chains, watchlist, *, trade_alerts=None, price=100.0):
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


@pytest.fixture()
def _open(monkeypatch):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)


@pytest.mark.asyncio
async def test_csp_sized_to_buying_power_ignores_tax_reserve(tmp_path, _open):
    """$10,000 buying power and a ZZZ 40P size to 2. A $10,000 SGOV holding is not subtracted."""
    put = _contract("ZZZ", "put", 40, 35, -0.25, 1.40, 1.50)
    book = _Book(
        buying_power=10_000, account_value=200_000,
        holdings=[EquityHolding("SGOV", 100, 100.0)],
    )
    # SGOV is a 100-share holding with its own chain so the feed gate does not
    # treat it as an empty chain. Nothing is subtracted for a tax reserve.
    scanner = _scanner(tmp_path, book, {"ZZZ": [put], "SGOV": [put]}, ["ZZZ"], price=100.0)

    n = await scanner.run_once()

    assert n == 1 and book.writes == []
    alerts = scanner.alerts.all()
    assert len(alerts) == 1
    row = alerts[0]
    assert row["kind"] == "CSP" and row["underlying"] == "ZZZ"
    assert row["strike"] == 40.0 and row["contracts"] == 2
    assert row["dedup_key"] == f"CSP:ZZZ:{row['occ_symbol']}"
    # 100 SGOV shares at $100 are not subtracted from buying power or account value.
    assert scanner.last_reserve is None
    assert scanner.last_account_value == 200_000
    runs = scanner.alerts.runs()
    assert len(runs) == 1 and runs[0]["status"] == "ok"
    # No webhook was configured, so the row stays pending and nothing counts as sent.
    assert runs[0]["alerts_sent"] == 0
    assert alerts[0]["delivery_status"] == "pending"


@pytest.mark.asyncio
async def test_cc_only_from_shares_held(tmp_path, _open):
    """250 QQX shares give 2 calls. Zero shares give none. Strike stays at or above cost."""
    call = _contract("QQX", "call", 25, 35, 0.25, 1.00, 1.10)
    held = _Book(
        buying_power=50_000, account_value=50_000,
        holdings=[EquityHolding("QQX", 250, 20.0)],
    )
    scanner = _scanner(tmp_path, held, {"QQX": [call]}, [], price=30.0)
    assert await scanner.run_once() == 1
    alerts = scanner.alerts.all()
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "CC" and alerts[0]["underlying"] == "QQX"
    assert alerts[0]["contracts"] == 2 and alerts[0]["strike"] == 25.0
    assert held.writes == []

    empty = _Book(
        buying_power=50_000, account_value=50_000,
        holdings=[EquityHolding("QQX", 0, 20.0)],
    )
    none = _scanner(tmp_path / "none", empty, {"QQX": [call]}, [], price=30.0)
    assert await none.run_once() == 0
    assert none.alerts.all() == []
    assert empty.writes == []


@pytest.mark.asyncio
async def test_repeat_scan_sends_one_webhook_and_no_second_alert(tmp_path, _open, monkeypatch):
    put = _contract("ZZZ", "put", 40, 35, -0.25, 1.40, 1.50)
    sent: list[tuple[str, dict]] = []
    monkeypatch.setenv("TRADE_ALERT_URL", HOOK)
    monkeypatch.setenv("TRADE_ALERT_HEADER_NAME", HEADER_NAME)
    monkeypatch.setenv("TRADE_ALERT_HEADER_VALUE", HEADER_VALUE)

    def _capture(url, payload, timeout=3.0):
        sent.append((url, payload))

    monkeypatch.setattr("agentic.notify.trade_alerts._http_post", _capture)
    db = Database(tmp_path / "hook.db")
    audit = AuditStore(db)
    alerts_transport = TradeAlerts(db, audit, Settings(mode="paper"), background=False)
    book = _Book(buying_power=10_000, account_value=200_000)
    scanner = OpportunityScanner(
        Settings(mode="paper", broker="paper", market_data="paper",
                 entry={"enabled": True, "watchlist": ["ZZZ"]}),
        book, _Chain({"ZZZ": [put]}), EntryDecisionStore(db), _NoOrders(),
        audit, KillSwitch(db, audit), alerts=AlertStore(db), trade_alerts=alerts_transport,
    )

    assert await scanner.run_once() == 1
    assert len(sent) == 1
    url, payload = sent[0]
    assert url == HOOK
    body = json.dumps(payload)
    assert payload["event"] == "scan_alert"
    assert payload["ticker"] == "ZZZ" and payload["contracts"] == 2
    assert payload["strike"] == 40.0 and payload["right"] == "P"
    for banned in ("buying_power", "account_value", "balance", "cash", HEADER_VALUE, HOOK):
        assert banned not in body

    later = utcnow() + timedelta(days=3)
    put.as_of = later  # the quote is still fresh on that later day
    monkeypatch.setattr("agentic.services.scanner.utcnow", lambda: later)
    assert await scanner.run_once() == 0
    assert len(scanner.alerts.all()) == 1
    assert scanner.alerts.all()[0]["delivery_status"] == "sent"
    assert len(sent) == 1
    assert book.writes == []


@pytest.mark.asyncio
async def test_stale_quote_sends_nothing_and_records_skip(tmp_path, _open, monkeypatch):
    stale = _contract(
        "ZZZ", "put", 40, 35, -0.25, 1.40, 1.50,
        as_of=utcnow() - timedelta(seconds=120),
    )
    sent: list = []
    monkeypatch.setenv("TRADE_ALERT_URL", HOOK)
    monkeypatch.setattr(
        "agentic.notify.trade_alerts._http_post", lambda *a, **k: sent.append(a),
    )
    db = Database(tmp_path / "stale.db")
    audit = AuditStore(db)
    transport = TradeAlerts(db, audit, Settings(mode="paper"), background=False)
    book = _Book(buying_power=10_000, account_value=200_000)
    scanner = OpportunityScanner(
        Settings(mode="paper", broker="paper", market_data="paper",
                 entry={"enabled": True, "watchlist": ["ZZZ"]}),
        book, _Chain({"ZZZ": [stale]}), EntryDecisionStore(db), _NoOrders(),
        audit, KillSwitch(db, audit), alerts=AlertStore(db), trade_alerts=transport,
    )

    assert await scanner.run_once() == 0
    assert scanner.alerts.all() == []
    assert sent == []
    runs = scanner.alerts.runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "skipped" and runs[0]["alerts_sent"] == 0
    assert runs[0]["reason"] == "stale quote"
    assert book.writes == []


@pytest.mark.asyncio
async def test_broker_down_sends_nothing_and_records_skip(tmp_path, _open):
    put = _contract("ZZZ", "put", 40, 35, -0.25, 1.40, 1.50)
    book = _Book(buying_power=10_000, account_value=200_000, connected=False)
    scanner = _scanner(tmp_path, book, {"ZZZ": [put]}, ["ZZZ"])

    assert await scanner.run_once() == 0
    assert scanner.alerts.all() == []
    assert book.reads == [] and book.writes == []
    runs = scanner.alerts.runs()
    assert len(runs) == 1 and runs[0]["status"] == "skipped"
    assert runs[0]["reason"] == "broker degraded" and runs[0]["alerts_sent"] == 0

    # A second disconnected session is the same gate. There is no paper fallback.
    live_settings = Settings(
        broker="robinhood_mcp",
        market_data="paper", entry={"enabled": True, "watchlist": ["ZZZ"]},
    )
    live_book = _Book(buying_power=10_000, account_value=200_000, connected=False)
    db = Database(tmp_path / "live" / "alerts.db")
    audit = AuditStore(db)
    live = OpportunityScanner(
        live_settings, live_book, _Chain({"ZZZ": [put]}), EntryDecisionStore(db),
        _NoOrders(), audit, KillSwitch(db, audit), alerts=AlertStore(db),
    )
    assert await live.run_once() == 0
    assert live.alerts.all() == []
    assert live.alerts.runs()[0]["status"] == "skipped"
    assert live.alerts.runs()[0]["reason"] == "broker degraded"


class _CapsDown:
    """capabilities() raises. The scan must not read the account."""

    def __init__(self):
        self.reads: list[str] = []

    def capabilities(self):
        raise RuntimeError("session probe failed")

    async def get_buying_power(self):
        self.reads.append("buying_power")
        return 10_000.0

    async def get_account_value(self):
        self.reads.append("account_value")
        return 200_000.0

    async def get_open_positions(self):
        self.reads.append("positions")
        return []

    async def get_equity_positions(self):
        self.reads.append("equity")
        return []


@pytest.mark.asyncio
async def test_capabilities_exception_skips_the_scan(tmp_path, _open):
    put = _contract("ZZZ", "put", 40, 35, -0.25, 1.40, 1.50)
    book = _CapsDown()
    scanner = _scanner(tmp_path, book, {"ZZZ": [put]}, ["ZZZ"])

    assert await scanner.run_once() == 0
    assert book.reads == []
    assert scanner.alerts.all() == []
    runs = scanner.alerts.runs()
    assert len(runs) == 1 and runs[0]["status"] == "skipped"
    assert runs[0]["reason"] == "broker degraded" and runs[0]["alerts_sent"] == 0


@pytest.mark.asyncio
async def test_empty_chain_and_empty_quote_map_skip_the_scan(tmp_path, _open):
    put = _contract("ZZZ", "put", 40, 35, -0.25, 1.40, 1.50)
    book = _Book(buying_power=10_000, account_value=200_000)
    scanner = _scanner(tmp_path, book, {}, ["ZZZ"])

    assert await scanner.run_once() == 0
    assert scanner.alerts.all() == []
    assert "ZZZ" in scanner.last_context
    runs = scanner.alerts.runs()
    assert len(runs) == 1 and runs[0]["status"] == "skipped"
    assert runs[0]["reason"] == "empty chain" and runs[0]["alerts_sent"] == 0

    # A chain that stores no quotes is the same gate, and it records a skip.
    blank = _contract("QQX", "call", 25, 35, 0.25, 1.00, 1.10)
    blank.occ_symbol = ""
    held = _Book(
        buying_power=50_000, account_value=50_000,
        holdings=[EquityHolding("QQX", 100, 20.0)],
    )
    quiet = _scanner(tmp_path / "quotes", held, {"QQX": [blank]}, [])
    assert await quiet.run_once() == 0
    assert quiet.alerts.all() == []
    qruns = quiet.alerts.runs()
    assert len(qruns) == 1 and qruns[0]["status"] == "skipped"
    assert qruns[0]["reason"] == "empty quote map"
    assert quiet._feed_problem({}, {}) is None


@pytest.mark.asyncio
async def test_failed_webhook_retries_on_the_next_scan(tmp_path, _open, monkeypatch):
    """Production background delivery: a failed POST stays unsent and is retried once."""
    import threading

    put = _contract("ZZZ", "put", 40, 35, -0.25, 1.40, 1.50)
    monkeypatch.setenv("TRADE_ALERT_URL", HOOK)
    attempts: list[str] = []
    statuses_during_post: list[str] = []

    db = Database(tmp_path / "retry.db")

    def _post(url, payload, timeout=3.0):
        row = db.conn.execute("SELECT delivery_status FROM alerts").fetchone()
        statuses_during_post.append(row["delivery_status"])
        attempts.append(threading.current_thread().name)
        # _deliver tries twice per scan. Fail the first scan's pair, then accept.
        if len(attempts) <= 2:
            raise OSError("webhook down")

    monkeypatch.setattr("agentic.notify.trade_alerts._http_post", _post)
    audit = AuditStore(db)
    transport = TradeAlerts(db, audit, Settings(mode="paper"), background=True)
    book = _Book(buying_power=10_000, account_value=200_000)
    scanner = OpportunityScanner(
        Settings(mode="paper", broker="paper", market_data="paper",
                 entry={"enabled": True, "watchlist": ["ZZZ"]}),
        book, _Chain({"ZZZ": [put]}), EntryDecisionStore(db), _NoOrders(),
        audit, KillSwitch(db, audit), alerts=AlertStore(db), trade_alerts=transport,
    )

    assert await scanner.run_once() == 1
    assert len(scanner.alerts.all()) == 1
    assert scanner.alerts.all()[0]["delivery_status"] == "failed"
    assert scanner.alerts.runs()[-1]["alerts_sent"] == 0
    assert statuses_during_post == ["pending", "pending"]
    assert attempts == ["trade-alert", "trade-alert"]

    assert await scanner.run_once() == 0
    assert len(scanner.alerts.all()) == 1
    assert scanner.alerts.all()[0]["delivery_status"] == "sent"
    assert scanner.alerts.runs()[-1]["alerts_sent"] == 1
    assert len(attempts) == 3

    assert await scanner.run_once() == 0
    assert len(scanner.alerts.all()) == 1
    assert len(attempts) == 3
    assert scanner.alerts.runs()[-1]["alerts_sent"] == 0
    assert book.writes == []


@pytest.mark.asyncio
async def test_main_wiring_reads_robinhood_holdings(tmp_path, _open, monkeypatch):
    """The process scanner sizes off the Robinhood read client, not the paper book."""
    from datetime import date, timedelta

    from agentic.brokers.robinhood_mcp import (
        READ_ONLY_MCP_TOOLS, RobinhoodMCPBroker, assert_mcp_tool_allowed,
    )
    from agentic.main import build_market_data, connect_brokers, make_scanner

    exp = (date.today() + timedelta(days=15)).isoformat()
    called: list[str] = []

    async def fake_connect(self):
        self._connected = True
        self._tools = [
            "get_portfolio", "get_option_positions", "get_equity_positions",
            "get_option_instruments", "get_option_chains", "get_option_quotes",
            "get_equity_quotes", "get_equity_historicals",
        ]
        self._roles = RobinhoodMCPBroker._resolve_roles(self._tools)

    async def fake_call(self, name, arguments):
        assert_mcp_tool_allowed(name)
        called.append(name)
        if name == "get_portfolio":
            return {
                "buying_power": {"buying_power": "50000.0000"},
                "total_value": "80000.00",
            }
        if name == "get_option_positions":
            return {"positions": []}
        if name == "get_equity_positions":
            return {"positions": [{
                "symbol": "QQX", "quantity": "250", "average_buy_price": "20.00",
            }]}
        if name == "get_equity_quotes":
            sym = (arguments.get("symbols") or ["QQX"])[0]
            return {"results": [{"symbol": sym, "quote": {"last_trade_price": "24.00"}}]}
        if name == "get_option_chains":
            return {"data": {"chains": [{"expiration_dates": [exp]}]}}
        if name == "get_option_instruments":
            if arguments.get("type") != "call":
                return {"results": []}
            return {"results": [{"id": "synth-qqx-call", "strike_price": "25"}]}
        if name == "get_option_quotes":
            return {"results": [{"quote": {
                "instrument_id": "synth-qqx-call",
                "bid_price": "1.00", "ask_price": "1.10",
                "adjusted_mark_price": "1.05", "delta": "0.25",
                "implied_volatility": "0.35", "open_interest": "400", "volume": "80",
            }}]}
        return {"results": []}

    monkeypatch.setattr(RobinhoodMCPBroker, "connect", fake_connect)
    monkeypatch.setattr(RobinhoodMCPBroker, "_call_tool", fake_call)
    settings = Settings(
        mode="paper", broker="paper", market_data="robinhood",
        paper_seed_positions=False, paper_buying_power=100.0,
        entry={
            "enabled": True, "watchlist": [], "earnings_gate": False,
            "criteria": {"dte_min": 10, "dte_max": 20},
            "cc_criteria": {"dte_min": 10, "dte_max": 20},
        },
    )
    db = Database(tmp_path / "wire.db")
    audit = AuditStore(db)
    broker, data, session = await connect_brokers(settings)
    scanner = make_scanner(
        settings, broker, build_market_data(settings, data or broker), session,
        EntryDecisionStore(db), _NoOrders(), audit, KillSwitch(db, audit),
    )

    assert isinstance(broker, RobinhoodMCPBroker)
    assert scanner.read_broker is session and scanner.broker is broker
    assert broker is session

    assert await scanner.run_once() == 1
    alerts = scanner.alerts.all()
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "CC" and alerts[0]["underlying"] == "QQX"
    assert alerts[0]["contracts"] == 2 and alerts[0]["strike"] == 25.0
    assert scanner.last_holdings[0].symbol == "QQX"
    assert scanner.last_holdings[0].quantity == 250
    assert {"get_portfolio", "get_option_positions", "get_equity_positions"} <= set(called)
    assert set(called) <= READ_ONLY_MCP_TOOLS

    # A disconnected session is the same object the scan reads, and it skips.
    called.clear()

    async def disconnected(self):
        self._connected = False

    async def refuse(self, name, arguments):
        called.append(name)
        raise AssertionError(f"read a disconnected session via {name}")

    monkeypatch.setattr(RobinhoodMCPBroker, "connect", disconnected)
    monkeypatch.setattr(RobinhoodMCPBroker, "_call_tool", refuse)
    down_db = Database(tmp_path / "down.db")
    down_audit = AuditStore(down_db)
    down_broker, down_data, down_session = await connect_brokers(settings)
    down = make_scanner(
        settings, down_broker, build_market_data(settings, down_data or down_broker),
        down_session, EntryDecisionStore(down_db), _NoOrders(), down_audit,
        KillSwitch(down_db, down_audit),
    )
    assert down_session is not None and down_session._connected is False
    assert down.read_broker is down_session
    assert await down.run_once() == 0
    assert called == []
    assert down.alerts.all() == []
    run = down.alerts.runs()[0]
    assert run["status"] == "skipped" and run["reason"] == "broker degraded"
    assert run["alerts_sent"] == 0
