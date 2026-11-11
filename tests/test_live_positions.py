"""Positions shown and evaluated are the current broker read.

Tickers and account ids here are synthetic (XYZ, ABC). The positions table is
a fixture only: live reads must not use it.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agentic.config import RuleConfig, Settings
from agentic.domain.enums import (
    DecisionStatus, Direction, OptionType, PositionStatus, RuleType, Strategy,
)
from agentic.domain.models import CloseDecision, Position, utcnow
from agentic.marketdata.base import PaperMarketData
from agentic.rules.engine import RulesEngine
from agentic.services.killswitch import KillSwitch
from agentic.services.live_positions import (
    BROKER_READ_FAILED,
    LiveBookError,
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
