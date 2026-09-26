"""PAUSE_TOKEN may only engage the kill switch. It cannot resume or approve."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentic.config import Settings
from agentic.domain.enums import AuditEventType
from agentic.services.killswitch import KillSwitch
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.entry_decisions import EntryDecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web import control as control_mod
from agentic.web.app import WebDeps, create_app

PAUSE = "test-pause-token-ok-long"
CONTROL = "test-control-token-ok-long"


@pytest.fixture(autouse=True)
def _clear_pause_fails():
    control_mod._pause_fail_times.clear()
    yield
    control_mod._pause_fail_times.clear()


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("PAUSE_TOKEN", PAUSE)
    db = Database(tmp_path / "pause.db")
    audit = AuditStore(db)
    killswitch = KillSwitch(db, audit)
    deps = WebDeps(
        settings=Settings(mode="paper", broker="paper", market_data="paper"),
        signals=SignalStore(db),
        killswitch=killswitch,
        approval_gate=None,
        audit=audit,
        positions=PositionStore(db),
        orders=OrderStore(db),
        decisions=DecisionStore(db),
        entry_decisions=EntryDecisionStore(db),
    )
    client = TestClient(create_app(deps))
    return dict(client=client, killswitch=killswitch, audit=audit)


def test_pause_token_engages_killswitch_without_basic_auth(ctx):
    client = ctx["client"]
    r = client.post(
        "/control/pause-only",
        params={"token": PAUSE, "reason": "monitor smoke"},
        auth=None,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "paused"
    assert body["reason"] == "monitor smoke"
    assert body["already_paused"] is False
    assert ctx["killswitch"].is_paused() is True
    assert ctx["killswitch"].reason() == "monitor smoke"
    row = ctx["audit"].latest(AuditEventType.KILLSWITCH, source="pause_token")
    assert row is not None
    assert row["payload"]["paused"] is True
    assert row["payload"]["reason"] == "monitor smoke"


def test_pause_token_header_and_idempotent_repause(ctx):
    client = ctx["client"]
    first = client.post(
        "/control/pause-only",
        headers={"X-Pause-Token": PAUSE},
        params={"reason": "first"},
        auth=None,
    )
    assert first.status_code == 200 and first.json()["already_paused"] is False
    second = client.post(
        "/control/pause-only?token=" + PAUSE,
        params={"reason": "second"},
        auth=None,
    )
    assert second.status_code == 200
    assert second.json() == {
        "status": "paused",
        "reason": "first",
        "already_paused": True,
    }
    assert ctx["killswitch"].reason() == "first"
    rows = [
        e for e in ctx["audit"].recent()
        if e["event_type"] == AuditEventType.KILLSWITCH.value
    ]
    assert len(rows) == 1


def test_wrong_empty_and_control_token_rejected_on_pause_only(ctx):
    client = ctx["client"]
    assert client.post("/control/pause-only?token=wrong", auth=None).status_code == 401
    assert client.post("/control/pause-only?token=", auth=None).status_code == 401
    assert client.post("/control/pause-only", auth=None).status_code == 401
    assert client.post(f"/control/pause-only?token={CONTROL}", auth=None).status_code == 401
    assert ctx["killswitch"].is_paused() is False


def test_pause_token_unset_is_401(tmp_path, monkeypatch):
    monkeypatch.delenv("PAUSE_TOKEN", raising=False)
    db = Database(tmp_path / "unset.db")
    audit = AuditStore(db)
    killswitch = KillSwitch(db, audit)
    client = TestClient(create_app(WebDeps(
        settings=Settings(mode="paper"),
        signals=SignalStore(db),
        killswitch=killswitch,
        approval_gate=None,
        audit=audit,
        positions=PositionStore(db),
        orders=OrderStore(db),
        decisions=DecisionStore(db),
    )))
    assert client.post("/control/pause-only?token=anything-long", auth=None).status_code == 401
    assert killswitch.is_paused() is False


def test_pause_token_placeholder_is_401(tmp_path, monkeypatch):
    monkeypatch.setenv("PAUSE_TOKEN", "change-me-to-something-strong")
    db = Database(tmp_path / "ph.db")
    audit = AuditStore(db)
    killswitch = KillSwitch(db, audit)
    client = TestClient(create_app(WebDeps(
        settings=Settings(mode="paper"),
        signals=SignalStore(db),
        killswitch=killswitch,
        approval_gate=None,
        audit=audit,
        positions=PositionStore(db),
        orders=OrderStore(db),
        decisions=DecisionStore(db),
    )))
    assert client.post(
        "/control/pause-only?token=change-me-to-something-strong", auth=None
    ).status_code == 401
    assert killswitch.is_paused() is False


def test_pause_token_rejected_by_resume_and_approval_routes(ctx):
    client = ctx["client"]
    client.post("/control/pause-only?token=" + PAUSE, auth=None)
    assert ctx["killswitch"].is_paused() is True

    resume = client.post(f"/control/resume?token={PAUSE}")
    assert resume.status_code == 401
    assert ctx["killswitch"].is_paused() is True

    assert client.post("/control/approve/dec-1?t=" + PAUSE).status_code == 401
    assert client.post("/control/reject/dec-1?t=" + PAUSE).status_code == 401
    assert client.post("/control/approve-entry/dec-1?t=" + PAUSE).status_code == 401
    assert client.post("/control/reject-entry/dec-1?t=" + PAUSE).status_code == 401

    # Existing CONTROL_TOKEN pause/resume path still works (and PAUSE_TOKEN cannot use it).
    assert client.post(f"/control/pause?token={PAUSE}").status_code == 401
    ok = client.post(f"/control/resume?token={CONTROL}")
    assert ok.status_code == 200
    assert ok.json()["status"] == "resumed"
    assert ctx["killswitch"].is_paused() is False


def test_failed_pause_token_is_rate_limited(ctx, monkeypatch):
    monkeypatch.setattr(control_mod, "_PAUSE_FAIL_MAX", 2)
    client = ctx["client"]
    assert client.post("/control/pause-only?token=nope", auth=None).status_code == 401
    assert client.post("/control/pause-only?token=nope", auth=None).status_code == 401
    limited = client.post("/control/pause-only?token=nope", auth=None)
    assert limited.status_code == 429
    # A later correct token is not delayed once the window is cleared.
    control_mod._pause_fail_times.clear()
    assert client.post("/control/pause-only?token=" + PAUSE, auth=None).status_code == 200
