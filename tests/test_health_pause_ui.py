"""Health, pause, and the alerts list. Synthetic tickers and made-up strikes only."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from agentic.brokers.robinhood_mcp import ORDERS_HARD_DISABLED
from agentic.config import Settings
from agentic.domain.models import utcnow
from agentic.marketdata.quote import OptionContractQuote
from agentic.notify.trade_alerts import TradeAlerts
from agentic.services.killswitch import KillSwitch
from agentic.services.scanner import OpportunityScanner
from agentic.store.alerts import AlertStore
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.entry_decisions import EntryDecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app

VIEWER_USER = "viewer"
VIEWER_PASS = "test-viewer-password-ok"
OWNER = ("admin", "test-dashboard-password-ok")

_BANNED = {
    "mode", "live_armed", "buying_power", "account_value", "balance",
    "password", "token", "secret", "oauth", "premium", "fill_price", "pnl",
}


def _walk_keys(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield str(key)
            yield from _walk_keys(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_keys(value)


def _quote(underlying: str, *, age_s: float = 5.0) -> OptionContractQuote:
    exp = utcnow().date() + timedelta(days=30)
    occ = f"{underlying}{exp:%y%m%d}P{int(10 * 1000):08d}"
    quote = OptionContractQuote(
        occ_symbol=occ, underlying=underlying, option_id=None, option_type="put",
        strike=10.0, expiration=exp, bid=0.40, ask=0.50, mark=0.45,
        delta=-0.2, iv=0.3, open_interest=100, volume=10,
    )
    quote.as_of = utcnow() - timedelta(seconds=age_s)
    return quote


def _deps(tmp_path, *, scanner=None, practice=None, paused=False):
    db = Database(tmp_path / "health.db")
    audit = AuditStore(db)
    killswitch = KillSwitch(db, audit)
    if paused:
        killswitch.pause("test")
    deps = WebDeps(
        settings=Settings(mode="live", i_understand_live_trading=True),
        signals=SignalStore(db),
        killswitch=killswitch,
        approval_gate=None,
        audit=audit,
        positions=PositionStore(db),
        orders=OrderStore(db),
        decisions=DecisionStore(db),
        scanner=scanner,
        practice=practice,
    )
    return deps, db, audit


def _seed_alerts(store: AlertStore) -> None:
    rows = [
        ("CSP", "AAA", "AAA261120P00010000", 10.0, "2026-11-20", 1),
        ("CSP", "BBB", "BBB261120P00020000", 20.0, "2026-11-20", 1),
        ("CSP", "XYZ", "XYZ261120P00030000", 30.0, "2026-11-20", 1),
        ("CC", "AAA", "AAA261218C00015000", 15.0, "2026-12-18", 2),
        ("CC", "BBB", "BBB261218C00025000", 25.0, "2026-12-18", 2),
    ]
    ids = []
    for kind, underlying, occ, strike, expiration, contracts in rows:
        alert_id = store.insert_if_new({
            "dedup_key": f"{kind}:{underlying}:{occ}",
            "kind": kind, "underlying": underlying, "occ_symbol": occ,
            "strike": strike, "expiration": expiration, "contracts": contracts,
            "premium": 0.45, "bid": 0.40, "ask": 0.50,
        })
        assert alert_id
        ids.append(alert_id)
    store.mark_taken(ids[1], fill_price=0.40, taken_at=utcnow().isoformat(), late=False, partial=False)
    store.mark_stale(ids[2], utcnow().isoformat())
    store.mark_expired(ids[3])
    store.mark_stale(ids[4], utcnow().isoformat())
    store.mark_taken(ids[4], fill_price=0.55, taken_at=utcnow().isoformat(), late=True, partial=True)


class _Book:
    def __init__(self):
        self.writes: list[str] = []

    async def get_buying_power(self) -> float:
        return 10_000.0

    async def get_account_value(self) -> float:
        return 200_000.0

    async def get_open_positions(self):
        return []

    async def get_equity_positions(self):
        return []

    def _refuse(self, name: str):
        self.writes.append(name)
        raise AssertionError(name)

    async def submit_open_order(self, order):
        self._refuse("submit_open_order")

    async def submit_close_order(self, order):
        self._refuse("submit_close_order")

    async def submit_equity_order(self, **kwargs):
        self._refuse("submit_equity_order")

    async def cancel_order(self, order):
        self._refuse("cancel_order")


class _Chain:
    def __init__(self, quote):
        self._quote = quote

    async def get_quote(self, position):
        return None

    async def get_chain(self, underlying: str):
        return [self._quote] if underlying == self._quote.underlying else []

    async def get_underlying_price(self, underlying: str):
        return 40.0

    async def get_underlying_bars(self, underlying: str, lookback_days: int = 260):
        return []


class _NoOrders:
    async def execute_open(self, decision, quote):
        raise AssertionError("scan tried to execute an order")


def test_health_reports_lock_scanner_and_feed_without_mode_or_balances(tmp_path):
    scanner = type("S", (), {})()
    scanner.alerts = None
    scanner.last_quotes = {"AAA": _quote("AAA", age_s=4)}
    deps, _, _ = _deps(tmp_path, scanner=scanner, practice={
        "execution_broker": "paper",
        "market_data": "paper",
        "robinhood_connected": False,
        "practice": True,
        "mode": "paper",
        "buying_power": 99999,
    })
    store = AlertStore(deps.killswitch.db)
    store.record_run(status="skipped", reason="stale quote", alerts_sent=0, scanned_at=utcnow())
    deps.scanner.alerts = store
    response = TestClient(create_app(deps)).get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["orders_hard_disabled"] is True
    assert body["orders_hard_disabled"] is ORDERS_HARD_DISABLED
    assert body["real_orders"] == "HARD DISABLED in code"
    assert body["scanner"]["up"] is True
    assert body["scanner"]["last_status"] == "skipped"
    assert body["feed_age_seconds"] >= 4
    assert body["feed_age_seconds"] < 30
    assert "mode" not in body and "live_armed" not in body
    assert "buying_power" not in body
    assert _BANNED.isdisjoint(set(_walk_keys(body)))


def test_health_stays_200_when_feed_is_stale_or_no_scan_yet(tmp_path):
    stale = type("S", (), {})()
    stale.alerts = None
    stale.last_quotes = {"XYZ": _quote("XYZ", age_s=7200)}
    deps, _, _ = _deps(tmp_path, scanner=stale)
    client = TestClient(create_app(deps))
    stale_body = client.get("/health")
    assert stale_body.status_code == 200
    assert stale_body.json()["feed_age_seconds"] > 3600
    assert stale_body.json()["scanner"]["up"] is False

    empty = type("S", (), {})()
    empty.alerts = AlertStore(deps.killswitch.db)
    empty.last_quotes = {}
    deps.scanner = empty
    fresh = TestClient(create_app(deps)).get("/health")
    assert fresh.status_code == 200
    body = fresh.json()
    assert body["scanner"]["up"] is False
    assert body["scanner"]["last_scan_at"] is None
    assert body["feed_age_seconds"] is None
    assert body["status"] == "ok"


def test_dashboard_renders_alert_chips_health_banner_and_drops_actions(tmp_path, monkeypatch):
    monkeypatch.setenv("VIEWER_USER", VIEWER_USER)
    monkeypatch.setenv("VIEWER_PASSWORD", VIEWER_PASS)
    db = Database(tmp_path / "dash.db")
    store = AlertStore(db)
    _seed_alerts(store)
    store.record_run(status="ok", reason=None, alerts_sent=0, scanned_at=utcnow())
    scanner = type("S", (), {})()
    scanner.alerts = store
    scanner.last_quotes = {"AAA": _quote("AAA", age_s=3)}
    audit = AuditStore(db)
    deps = WebDeps(
        settings=Settings(mode="paper"),
        signals=SignalStore(db),
        killswitch=KillSwitch(db, audit),
        approval_gate=None,
        audit=audit,
        positions=PositionStore(db),
        orders=OrderStore(db),
        decisions=DecisionStore(db),
        scanner=scanner,
    )
    client = TestClient(create_app(deps))
    html = client.get("/dashboard", auth=OWNER).text
    for status in ("new", "taken", "stale", "expired"):
        assert f'data-status="{status}"' in html
    assert 'data-flag="late"' in html
    assert 'data-flag="partial"' in html
    assert 'id="health-banner"' in html
    assert 'id="scan-alerts"' in html
    assert 'data-scanner="up"' in html
    assert 'data-lock="on"' in html
    assert "Orders locked" in html
    assert 'id="pause-btn"' in html and 'id="pause-ctl"' in html
    assert "owner-only" in html
    for gone in (
        "/control/approve", "/control/reject", "/control/approve-entry",
        "/control/reject-entry", "/control/roll",
    ):
        assert gone not in html
    alerts = client.get("/api/alerts", auth=OWNER).json()["alerts"]
    assert {row["status"] for row in alerts} == {"new", "taken", "stale", "expired"}
    assert any(row["late"] and row["partial"] for row in alerts)
    raw = json.dumps(alerts)
    for banned in ("premium", "bid", "ask", "fill_price", "pnl", "buying_power"):
        assert banned not in raw
    assert {row["underlying"] for row in alerts} <= {"AAA", "BBB", "XYZ"}

    assert client.post("/control/pause", auth=(VIEWER_USER, VIEWER_PASS)).status_code == 403
    assert client.post("/control/resume", auth=(VIEWER_USER, VIEWER_PASS)).status_code == 403
    paused = client.post("/control/pause", auth=OWNER)
    assert paused.status_code == 200 and paused.json()["status"] == "paused"
    viewer_page = client.get("/", auth=(VIEWER_USER, VIEWER_PASS)).text
    assert 'const ROLE = "viewer"' in viewer_page
    assert 'id="pause-btn"' in viewer_page
    owner_page = client.get("/", auth=OWNER).text
    assert 'const ROLE = "owner"' in owner_page
    assert 'data-paused="1"' in owner_page

    for path in (
        "/control/approve/x",
        "/control/reject/x",
        "/control/approve-entry/x",
        "/control/reject-entry/x",
        "/control/roll/x",
    ):
        assert client.post(path, auth=OWNER).status_code in (404, 405)


@pytest.mark.asyncio
async def test_pause_holds_webhook_then_sends_once(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    sent: list[dict] = []

    def _capture(url, payload, timeout=3.0):
        sent.append(payload)

    monkeypatch.setenv("TRADE_ALERT_URL", "https://alerts.example.test/hook/scan")
    monkeypatch.setattr("agentic.notify.trade_alerts._http_post", _capture)
    put = _quote("ZZZ", age_s=1)
    put.strike = 40.0
    put.bid, put.ask, put.mark = 1.40, 1.50, 1.45
    put.delta = -0.25
    db = Database(tmp_path / "pause.db")
    audit = AuditStore(db)
    killswitch = KillSwitch(db, audit)
    transport = TradeAlerts(db, audit, Settings(mode="paper"), background=False)
    book = _Book()
    scanner = OpportunityScanner(
        Settings(mode="paper", broker="paper", market_data="paper",
                 entry={"enabled": True, "watchlist": ["ZZZ"]}),
        book, _Chain(put), EntryDecisionStore(db), _NoOrders(),
        audit, killswitch, alerts=AlertStore(db), trade_alerts=transport,
    )

    killswitch.pause("test")
    assert await scanner.run_once() == 1
    assert sent == []
    assert book.writes == []
    held = scanner.alerts.all()
    assert len(held) == 1 and held[0]["delivery_status"] == "pending"
    assert held[0]["underlying"] == "ZZZ"

    assert await scanner.run_once() == 0
    assert sent == []
    assert len(scanner.alerts.all()) == 1

    killswitch.resume("test")
    assert await scanner.run_once() == 0
    assert len(sent) == 1
    assert sent[0]["event"] == "scan_alert"
    assert sent[0]["ticker"] == "ZZZ"
    assert scanner.alerts.all()[0]["delivery_status"] == "sent"
    body = json.dumps(sent[0])
    assert "buying_power" not in body and "200000" not in body

    assert await scanner.run_once() == 0
    assert len(sent) == 1
    assert len(scanner.alerts.all()) == 1
