"""Positions shown and evaluated are the current broker read.

Tickers and account ids here are synthetic (XYZ, ABC). The positions table is
a fixture only: live reads must not use it.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import anyio
import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient

from agentic.brokers.base import BrokerCapabilities
from agentic.brokers.robinhood_mcp import RobinhoodMCPBroker
from agentic.config import RuleConfig, Settings
from agentic.domain.enums import (
    AuditEventType, DecisionStatus, Direction, OptionType, PositionStatus, RuleType, Strategy,
)
from agentic.domain.models import CloseDecision, Position, utcnow
from agentic.marketdata.base import PaperMarketData
from agentic.rules.engine import RulesEngine
from agentic.services.killswitch import KillSwitch
from agentic.services.live_positions import (
    BROKER_READ_FAILED,
    LiveBookError,
    fetch_broker_positions,
    format_broker_failure,
    load_live_positions,
    ny_today,
    select_live,
    stable_position_id,
    still_held,
)
from agentic.services.monitor import MonitorLoop
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app
from tests.fakes import OrderStore

NY_DAY = date(2026, 11, 11)


def _rules():
    return [
        RuleConfig(
            name="profit-target", rule_type="PROFIT_TARGET", enabled=True,
            requires_approval=False, params={"profit_pct": 0.5},
        ),
        RuleConfig(
            name="stop-loss", rule_type="STOP_LOSS", enabled=True,
            requires_approval=False, params={"loss_mult": 2.0, "delta_stop": 0.5},
        ),
    ]


def _pos(occ, underlying, expiration, *, mark=0.20, credit=1.00,
         delta=-0.20, bid=None, ask=None) -> Position:
    if bid is None and mark is not None:
        bid = round(mark - 0.02, 4)
    if ask is None and mark is not None:
        ask = round(mark + 0.02, 4)
    return Position(
        occ_symbol=occ,
        underlying=underlying,
        option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT,
        direction=Direction.SHORT,
        quantity=1,
        strike=10.0,
        expiration=expiration,
        credit_received=credit,
        current_bid=bid,
        current_ask=ask,
        current_mark=mark,
        delta=delta,
        status=PositionStatus.OPEN,
    )


class Book:
    """Fake broker. ``rows`` is replaced between reads. ``fail`` raises."""

    def __init__(self, rows, *, fail=False, connected=None, roles=None):
        self.rows = list(rows)
        self.fail = fail
        self.reads = 0
        if connected is not None:
            self._connected = connected
        if roles is not None:
            self._roles = roles

    async def get_open_positions(self):
        self.reads += 1
        if self.fail:
            raise RuntimeError("synthetic broker down")
        return list(self.rows)


def _count(db: Database) -> int:
    return int(db.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0])


def _app(db, settings, broker):
    audit = AuditStore(db)
    return TestClient(create_app(WebDeps(
        settings=settings,
        signals=SignalStore(db),
        killswitch=KillSwitch(db, audit),
        approval_gate=None,
        audit=audit,
        positions=PositionStore(db),
        orders=OrderStore(db),
        decisions=DecisionStore(db),
        scanner=type("S", (), {
            "broker": broker,
            "market_data": PaperMarketData(),
            "last_context": {},
            "last_scan_at": None,
        })(),
    )))


def _pin_ny(monkeypatch):
    def _today(now=None):
        return NY_DAY

    monkeypatch.setattr("agentic.services.live_positions.ny_today", _today)
    monkeypatch.setattr("agentic.web.dashboard.ny_today", _today)
    monkeypatch.setattr("agentic.services.monitor.is_market_hours", lambda *a, **k: True)
    monkeypatch.setattr("agentic.services.monitor.is_order_window", lambda *a, **k: True)


def test_ny_today_is_america_new_york():
    # 02:00 UTC on Nov 12 is still Nov 11 in New York (EST, UTC-5).
    assert ny_today(datetime(2026, 11, 12, 2, 0, tzinfo=timezone.utc)) == date(2026, 11, 11)
    assert ny_today(datetime(2026, 11, 12, 5, 0, tzinfo=timezone.utc)) == date(2026, 11, 12)


def test_select_live_keeps_today_and_drops_yesterday():
    today = _pos("XYZ261111P00010000", "XYZ", NY_DAY)
    yesterday = _pos("ABC261110P00010000", "ABC", NY_DAY - timedelta(days=1))
    kept = select_live([yesterday, today], today=NY_DAY)
    assert [p.underlying for p in kept] == ["XYZ"]


def test_expired_contract_is_excluded(tmp_path, monkeypatch):
    _pin_ny(monkeypatch)
    expired = _pos("XYZ261106P00010000", "XYZ", NY_DAY - timedelta(days=5), mark=0.20)
    today = _pos("ABC261111P00010000", "ABC", NY_DAY, mark=0.90, credit=1.00)
    later = _pos("XYZ261218P00010000", "XYZ", NY_DAY + timedelta(days=37), mark=0.90)
    db = Database(tmp_path / "live.db")
    # A stored copy of the expired contract must not leak into the response.
    PositionStore(db).upsert(expired)
    broker = Book([expired, today, later])
    body = _app(db, Settings(mode="paper", rules=_rules()), broker).get("/api/positions").json()
    assert body["error"] is None
    occs = {row["occ_symbol"] for row in body["positions"]}
    assert expired.occ_symbol not in occs
    assert occs == {today.occ_symbol, later.occ_symbol}
    by = {row["occ_symbol"]: row for row in body["positions"]}
    assert by[today.occ_symbol]["dte"] == 0
    assert by[today.occ_symbol]["status"] == "OPEN"
    assert "dte-close" not in {row["rule"] for row in body["positions"]}


def test_contract_missing_from_the_next_read_drops_off(tmp_path, monkeypatch):
    _pin_ny(monkeypatch)
    held = _pos("XYZ261218P00010000", "XYZ", NY_DAY + timedelta(days=20), mark=0.90)
    gone = _pos("ABC261218P00010000", "ABC", NY_DAY + timedelta(days=20), mark=0.90)
    db = Database(tmp_path / "drop.db")
    store = PositionStore(db)
    store.upsert(gone)
    broker = Book([held, gone])
    client = _app(db, Settings(mode="paper", rules=_rules()), broker)
    first = {row["underlying"] for row in client.get("/api/positions").json()["positions"]}
    assert first == {"XYZ", "ABC"}
    broker.rows = [held]
    second = client.get("/api/positions").json()
    assert second["error"] is None
    assert [row["underlying"] for row in second["positions"]] == ["XYZ"]
    # The stored ABC row is still on disk and still not shown.
    assert any(p.occ_symbol == gone.occ_symbol for p in store.list_all())


def test_broker_failure_returns_no_positions(tmp_path, monkeypatch):
    _pin_ny(monkeypatch)
    stale = _pos("XYZ261218P00010000", "XYZ", NY_DAY + timedelta(days=20), mark=0.20)
    db = Database(tmp_path / "fail.db")
    PositionStore(db).upsert(stale)
    DecisionStore(db).insert_if_new(CloseDecision(
        position_id=stale.id, rule_name="dte-close", rule_type=RuleType.DTE,
        reason="old", requires_approval=False, dedup_key="stale-dte",
        status=DecisionStatus.DONE,
    ))
    broker = Book([stale], fail=True)
    body = _app(db, Settings(mode="paper", rules=_rules()), broker).get("/api/positions").json()
    assert body["positions"] == []
    assert body["error"] == BROKER_READ_FAILED
    assert "dte-close" not in str(body)


@pytest.mark.asyncio
async def test_broker_failure_skips_the_monitor_cycle(tmp_path, monkeypatch):
    _pin_ny(monkeypatch)
    stale = _pos("XYZ261218P00010000", "XYZ", NY_DAY + timedelta(days=20), mark=0.20)
    db = Database(tmp_path / "mon-fail.db")
    store = PositionStore(db)
    store.upsert(stale)
    decisions = DecisionStore(db)
    audit = AuditStore(db)
    monitor = MonitorLoop(
        Settings(mode="paper", rules=_rules()),
        Book([stale], fail=True), PaperMarketData(), store, audit, KillSwitch(db, audit),
        rules_engine=RulesEngine.from_configs(_rules()),
        decisions=decisions,
    )
    before = _count(db)
    with pytest.raises(LiveBookError):
        await monitor.run_once()
    assert decisions.recent() == []
    assert _count(db) == before


@pytest.mark.asyncio
async def test_exits_evaluate_held_unexpired_contracts(tmp_path, monkeypatch):
    _pin_ny(monkeypatch)
    winner = _pos("XYZ261218P00010000", "XYZ", NY_DAY + timedelta(days=20), mark=0.20)
    loser = _pos(
        "ABC261218P00010000", "ABC", NY_DAY + timedelta(days=12),
        mark=2.10, bid=2.00, ask=2.20, delta=-0.20,
    )
    expired = _pos("XYZ261106P00010000", "XYZ", NY_DAY - timedelta(days=5), mark=0.20)
    db = Database(tmp_path / "exits.db")
    store = PositionStore(db)
    decisions = DecisionStore(db)
    audit = AuditStore(db)
    monitor = MonitorLoop(
        Settings(mode="paper", rules=_rules()),
        Book([winner, loser, expired]), PaperMarketData(), store, audit,
        KillSwitch(db, audit),
        rules_engine=RulesEngine.from_configs(_rules()),
        decisions=decisions,
    )
    seen = await monitor.run_once()
    assert seen == 2
    names = sorted(d.rule_name for d in decisions.list_by_status(DecisionStatus.PROPOSED))
    assert names == ["profit-target", "stop-loss"]
    assert "dte-close" not in names
    assert store.list_all() == []


def test_rule_label_is_fresh_and_not_dte_close(tmp_path, monkeypatch):
    _pin_ny(monkeypatch)
    winner = _pos("XYZ261218P00010000", "XYZ", NY_DAY + timedelta(days=20), mark=0.20)
    quiet = _pos("ABC261218P00010000", "ABC", NY_DAY + timedelta(days=20), mark=0.90)
    db = Database(tmp_path / "label.db")
    DecisionStore(db).insert_if_new(CloseDecision(
        position_id=stable_position_id(winner.occ_symbol),
        rule_name="dte-close", rule_type=RuleType.DTE, reason="removed",
        requires_approval=False, dedup_key="old-dte", status=DecisionStatus.DONE,
    ))
    body = _app(db, Settings(mode="paper", rules=_rules()), Book([winner, quiet])).get(
        "/api/positions"
    ).json()
    by = {row["underlying"]: row for row in body["positions"]}
    assert set(by) == {"XYZ", "ABC"}
    assert by["XYZ"]["rule"] == "profit-target"
    assert by["XYZ"]["pnl_pct"] == 80
    assert by["XYZ"]["outcome"] == "open"
    assert by["ABC"]["rule"] is None
    assert "dte-close" not in {row["rule"] for row in body["positions"]}


@pytest.mark.asyncio
async def test_nothing_is_persisted_between_two_reads(tmp_path, monkeypatch):
    _pin_ny(monkeypatch)
    held = _pos("XYZ261218P00010000", "XYZ", NY_DAY + timedelta(days=20), mark=0.40)
    stale = _pos("ABC261106P00010000", "ABC", NY_DAY - timedelta(days=12), mark=0.20)
    db = Database(tmp_path / "persist.db")
    store = PositionStore(db)
    store.upsert(stale)
    before = _count(db)
    assert before == 1
    broker = Book([held])
    client = _app(db, Settings(mode="paper", rules=_rules()), broker)
    audit = AuditStore(db)
    monitor = MonitorLoop(
        Settings(mode="paper", rules=_rules()), broker, PaperMarketData(), store, audit,
        KillSwitch(db, audit), rules_engine=RulesEngine.from_configs(_rules()),
        decisions=DecisionStore(db),
    )
    first = client.get("/api/positions").json()
    await monitor.run_once()
    second = client.get("/api/positions").json()
    await monitor.run_once()
    assert _count(db) == before
    assert [row["underlying"] for row in first["positions"]] == ["XYZ"]
    assert [row["underlying"] for row in second["positions"]] == ["XYZ"]
    assert stale.occ_symbol not in {row["occ_symbol"] for row in second["positions"]}
    # Re-open the same file. Old rows still load; they are still not the live book.
    db.close()
    reopened = Database(tmp_path / "persist.db")
    loaded = PositionStore(reopened).list_all()
    assert [p.occ_symbol for p in loaded] == [stale.occ_symbol]
    reopened.close()


def test_datetime_expiration_compares_its_date():
    exp = datetime(2026, 11, 11, 21, 0, tzinfo=timezone.utc)
    pos = _pos("XYZ261111P00010000", "XYZ", NY_DAY)
    pos.expiration = exp
    assert still_held(pos, today=date(2026, 11, 11)) is True
    assert still_held(pos, today=date(2026, 11, 12)) is False


def test_disconnected_broker_is_not_an_empty_book(tmp_path, monkeypatch):
    """A session that is down returns no rows from the broker method. That is an error."""
    _pin_ny(monkeypatch)
    held = _pos("XYZ261218P00010000", "XYZ", NY_DAY + timedelta(days=20), mark=0.40)
    db = Database(tmp_path / "down.db")
    broker = Book([held], connected=False)
    body = _app(db, Settings(mode="paper", rules=_rules()), broker).get("/api/positions").json()
    assert body["positions"] == []
    assert body["error"] == BROKER_READ_FAILED
    assert broker.reads == 0


def test_missing_list_positions_is_not_an_empty_book(tmp_path, monkeypatch):
    _pin_ny(monkeypatch)
    held = _pos("XYZ261218P00010000", "XYZ", NY_DAY + timedelta(days=20), mark=0.40)
    db = Database(tmp_path / "role.db")
    broker = Book([held], connected=True, roles={"option_chain": "get_option_chains"})
    body = _app(db, Settings(mode="paper", rules=_rules()), broker).get("/api/positions").json()
    assert body["positions"] == []
    assert body["error"] == BROKER_READ_FAILED
    assert broker.reads == 0


@pytest.mark.asyncio
async def test_disconnected_broker_skips_the_monitor_cycle(tmp_path, monkeypatch):
    _pin_ny(monkeypatch)
    held = _pos("XYZ261218P00010000", "XYZ", NY_DAY + timedelta(days=20), mark=0.20)
    db = Database(tmp_path / "down-mon.db")
    store = PositionStore(db)
    decisions = DecisionStore(db)
    audit = AuditStore(db)
    broker = Book([held], connected=False)
    monitor = MonitorLoop(
        Settings(mode="paper", rules=_rules()), broker, PaperMarketData(), store, audit,
        KillSwitch(db, audit), rules_engine=RulesEngine.from_configs(_rules()),
        decisions=decisions,
    )
    with pytest.raises(LiveBookError):
        await monitor.run_once()
    assert decisions.recent() == []
    assert broker.reads == 0


@pytest.mark.asyncio
async def test_connected_empty_book_is_a_successful_read():
    class Empty:
        _connected = True
        _roles = {"list_positions": "get_option_positions"}

        async def ensure_account_number(self, account_number=None):
            return "ACCT-XYZ"

        async def get_open_positions(self, account_number=None):
            assert account_number == "ACCT-XYZ"
            return []

    live, err = await load_live_positions(Empty())
    assert live == [] and err is None


def test_unexpected_position_error_is_an_empty_book(tmp_path, monkeypatch):
    async def boom(_broker):
        raise RuntimeError("synthetic")

    monkeypatch.setattr("agentic.web.dashboard.load_live_positions", boom)
    db = Database(tmp_path / "boom.db")
    body = _app(db, Settings(mode="paper", rules=_rules()), Book([])).get("/api/positions").json()
    assert body["positions"] == []
    assert body["error"] == BROKER_READ_FAILED


@pytest.mark.asyncio
async def test_prior_decision_for_the_same_contract_is_not_repeated(tmp_path, monkeypatch):
    """A decision stored under the old random id still dedups the stable id."""
    _pin_ny(monkeypatch)
    held = _pos("XYZ261218P00010000", "XYZ", NY_DAY + timedelta(days=20), mark=0.20)
    db = Database(tmp_path / "dedup.db")
    store = PositionStore(db)
    store.upsert(held)
    decisions = DecisionStore(db)
    day = utcnow().date().isoformat()
    decisions.insert_if_new(CloseDecision(
        position_id=held.id, rule_name="profit-target", rule_type=RuleType.PROFIT_TARGET,
        reason="already alerted", requires_approval=False,
        dedup_key=f"{held.id}:PROFIT_TARGET:{day}", status=DecisionStatus.PROPOSED,
    ))
    fresh = _pos(held.occ_symbol, "XYZ", held.expiration, mark=0.20)
    other = _pos("ABC261218P00010000", "ABC", held.expiration, mark=0.20)
    audit = AuditStore(db)
    monitor = MonitorLoop(
        Settings(mode="paper", rules=_rules()), Book([fresh, other]), PaperMarketData(),
        store, audit, KillSwitch(db, audit),
        rules_engine=RulesEngine.from_configs(_rules()), decisions=decisions,
    )
    await monitor.run_once()
    rows = decisions.recent()
    # ABC is new, so it alerts. XYZ was already alerted under the old id.
    assert len(rows) == 2
    assert held.id in {row.position_id for row in rows}
    assert stable_position_id(held.occ_symbol) not in {row.position_id for row in rows}
    assert stable_position_id(other.occ_symbol) in {row.position_id for row in rows}


def _empty_connect_group():
    """ExceptionGroup wrapping httpx.ConnectError with an empty message.

    ``ConnectError`` requires the message argument; ``""`` is that empty message.
    """
    return ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [httpx.ConnectError("")],
    )


def _nested_closed_resource():
    """Nested TaskGroup group: ClosedResourceError caused by an empty ConnectError."""
    try:
        raise httpx.ConnectError("")
    except httpx.ConnectError as conn:
        try:
            raise anyio.ClosedResourceError() from conn
        except anyio.ClosedResourceError as closed:
            return ExceptionGroup(
                "unhandled errors in a TaskGroup",
                [ExceptionGroup("unhandled errors in a TaskGroup", [closed])],
            )


def _patch_sleep(monkeypatch):
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("agentic.services.live_positions.asyncio.sleep", _sleep)
    return slept


def test_format_empty_connect_error_names_the_type():
    text = format_broker_failure(_empty_connect_group())
    assert text == "ExceptionGroup -> ConnectError"
    assert text != "failed"


def test_format_nested_group_includes_closed_resource_and_connect_error():
    text = format_broker_failure(_nested_closed_resource())
    assert text == (
        "ExceptionGroup -> ExceptionGroup -> ClosedResourceError -> ConnectError"
    )
    assert text != "failed"


def test_format_sibling_connect_error_is_included():
    exc = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [anyio.ClosedResourceError(), httpx.ConnectError("")],
    )
    assert format_broker_failure(exc) == (
        "ExceptionGroup -> ClosedResourceError -> ConnectError"
    )


@pytest.mark.asyncio
async def test_read_failures_are_distinct_and_not_retried(monkeypatch):
    slept = _patch_sleep(monkeypatch)

    class Disconnected:
        def __init__(self):
            self.reads = 0
            self._connected = False

        async def get_open_positions(self, account_number=None):
            self.reads += 1
            return []

    down = Disconnected()
    with pytest.raises(LiveBookError, match="^broker disconnected$"):
        await fetch_broker_positions(down)
    assert down.reads == 0

    class MissingTool:
        _connected = True
        _roles = {"option_chain": "get_option_chains"}

        def __init__(self):
            self.reads = 0

        async def get_open_positions(self, account_number=None):
            self.reads += 1
            return []

    missing = MissingTool()
    with pytest.raises(LiveBookError, match="^list_positions tool missing$"):
        await fetch_broker_positions(missing)
    assert missing.reads == 0

    class Blank:
        _connected = True
        _roles = {"list_positions": "get_option_positions"}

        async def ensure_account_number(self, account_number=None):
            return "   "

        async def get_open_positions(self, account_number=None):
            raise AssertionError("positions tool must not be called")

    with pytest.raises(LiveBookError, match="^blank account$"):
        await fetch_broker_positions(Blank())

    class NoPayload:
        async def get_open_positions(self):
            return None

    with pytest.raises(LiveBookError, match="^broker returned no payload$"):
        await fetch_broker_positions(NoPayload())
    assert slept == []


@pytest.mark.asyncio
async def test_connect_group_error_is_not_the_word_failed(monkeypatch, caplog):
    _patch_sleep(monkeypatch)

    class Boom:
        def __init__(self):
            self.reads = 0

        async def get_open_positions(self):
            self.reads += 1
            raise _empty_connect_group()

    broker = Boom()
    with caplog.at_level(logging.WARNING, logger="agentic.live_positions"):
        with pytest.raises(LiveBookError) as ei:
            await fetch_broker_positions(broker)
    text = str(ei.value)
    assert text == "ExceptionGroup -> ConnectError"
    assert text != "failed"
    assert "ConnectError" in text
    assert broker.reads == 2
    assert "ConnectError" in caplog.text
    assert "live position read failed:" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("raise_error", [
    _empty_connect_group,
    lambda: httpcore.ConnectError(),
    lambda: anyio.ClosedResourceError(),
    _nested_closed_resource,
])
async def test_transient_connect_retries_once_then_returns_broker_rows(
    monkeypatch, tmp_path, raise_error,
):
    slept = _patch_sleep(monkeypatch)
    stale = _pos("XYZ261218P00010000", "XYZ", NY_DAY + timedelta(days=20))
    db = Database(tmp_path / "retry.db")
    PositionStore(db).upsert(stale)

    class Flaky:
        def __init__(self):
            self.reads = 0

        async def get_open_positions(self):
            self.reads += 1
            if self.reads == 1:
                raise raise_error()
            return []

    broker = Flaky()
    assert await fetch_broker_positions(broker) == []
    assert broker.reads == 2
    assert len(slept) == 1 and 1 <= slept[0] <= 2
    # The stored row is still on disk and was not returned as the book.
    assert [p.occ_symbol for p in PositionStore(db).list_all()] == [stale.occ_symbol]


@pytest.mark.asyncio
async def test_non_connect_error_is_not_retried(monkeypatch):
    slept = _patch_sleep(monkeypatch)

    class Boom:
        def __init__(self):
            self.reads = 0

        async def get_open_positions(self):
            self.reads += 1
            raise ExceptionGroup("unhandled errors in a TaskGroup", [ValueError("synthetic")])

    broker = Boom()
    with pytest.raises(LiveBookError) as ei:
        await fetch_broker_positions(broker)
    assert str(ei.value) == "ExceptionGroup -> ValueError: synthetic"
    assert broker.reads == 1
    assert slept == []


@pytest.mark.asyncio
async def test_broker_failure_text_redacts_secrets(monkeypatch, caplog):
    _patch_sleep(monkeypatch)
    token = "synthetic-token-not-real"
    account = "88442211"
    url = "https://alerts.example.test/hook/synthetic"
    header = "synthetic-header-value-xyz"
    monkeypatch.setenv("TRADE_ALERT_HEADER_VALUE", header)

    class Leak:
        async def get_open_positions(self):
            raise RuntimeError(
                f"account_number={account} Bearer {token} at {url} "
                f"header {header} ref {account} " + ("x" * 400)
            )

    with caplog.at_level(logging.WARNING, logger="agentic.live_positions"):
        with pytest.raises(LiveBookError) as ei:
            await fetch_broker_positions(Leak())
    text = str(ei.value)
    assert "RuntimeError" in text
    assert len(text) <= 300
    assert "[redacted]" in text
    for secret in (token, account, url, "alerts.example.test", header):
        assert secret not in text
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_monitor_run_records_connect_error_detail(tmp_path, monkeypatch):
    """/api/ops last_error and the audit row name ConnectError, and skip the stored book."""
    _patch_sleep(monkeypatch)
    stale = _pos("XYZ261218P00010000", "XYZ", NY_DAY + timedelta(days=20), mark=0.20)
    db = Database(tmp_path / "ops-conn.db")
    store = PositionStore(db)
    store.upsert(stale)
    before = _count(db)
    decisions = DecisionStore(db)
    audit = AuditStore(db)
    killswitch = KillSwitch(db, audit)

    class ConnectBroker:
        def __init__(self):
            self.reads = 0

        def capabilities(self):
            return BrokerCapabilities("paper", False, True)

        async def get_open_positions(self):
            self.reads += 1
            raise _empty_connect_group()

    broker = ConnectBroker()
    settings = Settings(mode="paper", rules=_rules())
    monitor = MonitorLoop(
        settings, broker, PaperMarketData(), store, audit, killswitch,
        rules_engine=RulesEngine.from_configs(_rules()),
        decisions=decisions,
    )

    async def _once():
        try:
            return await MonitorLoop.run_once(monitor)
        finally:
            monitor._stop.set()

    monkeypatch.setattr(monitor, "run_once", _once)
    await monitor.run()

    assert broker.reads == 2
    assert decisions.recent() == []
    assert _count(db) == before
    row = audit.latest(AuditEventType.ERROR)
    assert row is not None
    detail = row["payload"]["error"]
    assert row["payload"]["where"] == "monitor"
    assert detail == "LiveBookError: ExceptionGroup -> ConnectError"
    assert detail != "failed"
    assert "ConnectError" in detail

    client = TestClient(create_app(WebDeps(
        settings=settings,
        signals=SignalStore(db),
        killswitch=killswitch,
        approval_gate=None,
        audit=audit,
        positions=store,
        orders=OrderStore(db),
        decisions=decisions,
    )))
    ops = client.get("/api/ops").json()
    assert ops["last_error"]["where"] == "monitor"
    assert ops["last_error"]["error"] == detail
    assert "ConnectError" in ops["last_error"]["error"]
    assert ops["last_error"]["error"] != "failed"


def test_cap_keeps_the_leaf_connect_error():
    try:
        raise httpx.ConnectError("")
    except httpx.ConnectError as conn:
        err = RuntimeError("y" * 500)
        err.__cause__ = conn
        text = format_broker_failure(err)
    assert len(text) <= 300
    assert text.endswith("-> ConnectError")


def test_cause_and_context_same_object_is_listed_once():
    conn = httpx.ConnectError("")
    closed = anyio.ClosedResourceError()
    closed.__cause__ = conn
    closed.__context__ = conn
    text = format_broker_failure(closed)
    assert text == "ClosedResourceError -> ConnectError"
    assert text.count("ConnectError") == 1


def test_ssl_reason_survives_formatting():
    text = format_broker_failure(RuntimeError("[SSL: CERTIFICATE_VERIFY_FAILED] eof"))
    assert "CERTIFICATE_VERIFY_FAILED" in text
    assert "[SSL: [redacted]" not in text


@pytest.mark.asyncio
async def test_suppressed_context_connect_error_is_not_retried(monkeypatch):
    slept = _patch_sleep(monkeypatch)

    class Boom:
        def __init__(self):
            self.reads = 0

        async def get_open_positions(self):
            self.reads += 1
            try:
                raise httpx.ConnectError("")
            except httpx.ConnectError:
                raise ValueError("synthetic") from None

    broker = Boom()
    with pytest.raises(LiveBookError) as ei:
        await fetch_broker_positions(broker)
    assert str(ei.value) == "ValueError: synthetic"
    assert "ConnectError" not in str(ei.value)
    assert broker.reads == 1
    assert slept == []


@pytest.mark.asyncio
@pytest.mark.parametrize("message,secret", [
    ("token = 'quoted secret tail'", "quoted secret tail"),
    ('token = "quoted secret tail"', "quoted secret tail"),
    ("id_token=id-value-synthetic", "id-value-synthetic"),
    ("session_token=session-value-synthetic", "session-value-synthetic"),
    ("access_token=access-value-synthetic", "access-value-synthetic"),
    ("refresh_token=refresh-value-synthetic", "refresh-value-synthetic"),
    ("account_id=5PY12345", "5PY12345"),
    ("Bearer synthetic-token-not-real", "synthetic-token-not-real"),
])
async def test_live_book_error_hides_each_secret(monkeypatch, message, secret):
    _patch_sleep(monkeypatch)

    class Leak:
        async def get_open_positions(self):
            raise RuntimeError(message)

    with pytest.raises(LiveBookError) as ei:
        await fetch_broker_positions(Leak())
    assert secret not in str(ei.value)
    assert "[redacted]" in str(ei.value)


@pytest.mark.asyncio
async def test_monitor_log_does_not_print_the_raw_cause(tmp_path, monkeypatch, caplog):
    _patch_sleep(monkeypatch)
    db = Database(tmp_path / "log-redact.db")
    store = PositionStore(db)
    audit = AuditStore(db)
    killswitch = KillSwitch(db, audit)
    secret_tail = "quoted secret tail"
    account_id = "5PY12345"
    bearer = "synthetic-token-not-real"

    class LeakBroker:
        def capabilities(self):
            return BrokerCapabilities("paper", False, True)

        async def get_open_positions(self):
            raise RuntimeError(
                f"token = '{secret_tail}' account_id={account_id} Bearer {bearer}"
            )

    settings = Settings(mode="paper", rules=_rules())
    monitor = MonitorLoop(
        settings, LeakBroker(), PaperMarketData(), store, audit, killswitch,
    )

    async def _once():
        try:
            return await MonitorLoop.run_once(monitor)
        finally:
            monitor._stop.set()

    monkeypatch.setattr(monitor, "run_once", _once)
    with caplog.at_level(logging.DEBUG):
        await monitor.run()
    blob = caplog.text + str(audit.latest(AuditEventType.ERROR))
    for secret in (secret_tail, account_id, bearer):
        assert secret not in blob
    detail = audit.latest(AuditEventType.ERROR)["payload"]["error"]
    assert detail.startswith("LiveBookError:")
    assert "[redacted]" in detail

    client = TestClient(create_app(WebDeps(
        settings=settings,
        signals=SignalStore(db),
        killswitch=killswitch,
        approval_gate=None,
        audit=audit,
        positions=store,
        orders=OrderStore(db),
        decisions=DecisionStore(db),
    )))
    ops_err = client.get("/api/ops").json()["last_error"]["error"]
    for secret in (secret_tail, account_id, bearer):
        assert secret not in ops_err


@pytest.mark.asyncio
async def test_cancel_and_keyboard_interrupt_propagate(monkeypatch):
    slept = _patch_sleep(monkeypatch)

    class Boom:
        def __init__(self, exc):
            self.exc = exc
            self.reads = 0

        async def get_open_positions(self):
            self.reads += 1
            raise self.exc

    for exc in (asyncio.CancelledError(), KeyboardInterrupt()):
        broker = Boom(exc)
        with pytest.raises(type(exc)):
            await fetch_broker_positions(broker)
        assert broker.reads == 1
    assert slept == []


@pytest.mark.asyncio
async def test_mixed_base_exception_group_propagates(monkeypatch):
    slept = _patch_sleep(monkeypatch)

    class Boom:
        def __init__(self):
            self.reads = 0

        async def get_open_positions(self):
            self.reads += 1
            raise BaseExceptionGroup("mixed", [ValueError("synthetic"), KeyboardInterrupt()])

    broker = Boom()
    with pytest.raises(BaseExceptionGroup) as ei:
        await fetch_broker_positions(broker)
    assert type(ei.value) is BaseExceptionGroup
    assert not isinstance(ei.value, LiveBookError)
    assert any(isinstance(sub, KeyboardInterrupt) for sub in ei.value.exceptions)
    assert any(isinstance(sub, ValueError) for sub in ei.value.exceptions)
    assert broker.reads == 1
    assert slept == []


@pytest.mark.asyncio
async def test_retry_skips_sleep_when_monitor_stop_is_set(monkeypatch):
    slept = _patch_sleep(monkeypatch)
    stop = asyncio.Event()
    stop.set()

    class Flaky:
        def __init__(self):
            self.reads = 0

        async def get_open_positions(self):
            self.reads += 1
            if self.reads == 1:
                raise _empty_connect_group()
            return []

    broker = Flaky()
    assert await fetch_broker_positions(broker, stop_event=stop) == []
    assert broker.reads == 2
    assert slept == []


@pytest.mark.asyncio
async def test_account_lookup_connect_error_is_retried_not_blank(monkeypatch):
    slept = _patch_sleep(monkeypatch)
    broker = RobinhoodMCPBroker(account_number="", settings=Settings())
    broker._connected = True
    broker._tools = ["get_accounts", "get_option_positions"]
    broker._roles = {"list_positions": "get_option_positions"}
    calls: list[str] = []

    async def fake(name, args):
        calls.append(name)
        if name == "get_accounts" and calls.count("get_accounts") == 1:
            raise _empty_connect_group()
        if name == "get_accounts":
            return {"data": {"accounts": [
                {"account_number": "ACCT-SYN", "agentic_allowed": True},
            ]}}
        return {"data": {"positions": []}}

    monkeypatch.setattr(broker, "_call_tool", fake)
    assert await fetch_broker_positions(broker) == []
    assert calls.count("get_accounts") == 2
    assert len(slept) == 1
    assert "ACCT-SYN" not in "".join(calls)


@pytest.mark.asyncio
async def test_account_lookup_connect_error_is_not_reported_as_blank(monkeypatch):
    _patch_sleep(monkeypatch)
    broker = RobinhoodMCPBroker(account_number="", settings=Settings())
    broker._connected = True
    broker._tools = ["get_accounts", "get_option_positions"]
    broker._roles = {"list_positions": "get_option_positions"}

    async def fake(name, args):
        raise _empty_connect_group()

    monkeypatch.setattr(broker, "_call_tool", fake)
    with pytest.raises(LiveBookError) as ei:
        await fetch_broker_positions(broker)
    text = str(ei.value)
    assert "ConnectError" in text
    assert text != "blank account"
    assert "blank account" not in text


@pytest.mark.asyncio
async def test_ambiguous_account_stays_blank_without_retry(monkeypatch):
    slept = _patch_sleep(monkeypatch)
    broker = RobinhoodMCPBroker(account_number="", settings=Settings())
    broker._connected = True
    broker._tools = ["get_accounts", "get_option_positions"]
    broker._roles = {"list_positions": "get_option_positions"}
    calls: list[str] = []

    async def fake(name, args):
        calls.append(name)
        return {"data": {"accounts": [
            {"account_number": "ACCT-SYN", "agentic_allowed": False},
            {"account_number": "ACCT-OTH", "agentic_allowed": False},
        ]}}

    monkeypatch.setattr(broker, "_call_tool", fake)
    with pytest.raises(LiveBookError, match="^blank account$"):
        await fetch_broker_positions(broker)
    assert calls == ["get_accounts"]
    assert slept == []
