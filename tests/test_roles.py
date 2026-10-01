"""Owner vs view-only dashboard logins: default-deny mutations, CONTROL_TOKEN alternative."""
from __future__ import annotations

import json
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from agentic.config import Settings, require_runtime_secrets
from agentic.domain.enums import AuditEventType
from agentic.services.killswitch import KillSwitch
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app

VIEWER_USER = "viewer"
VIEWER_PASS = "test-viewer-password-ok"
OWNER_USER = "admin"
OWNER_PASS = "test-dashboard-password-ok"
CONTROL = "test-control-token-ok-long"


def _app(tmp_path, monkeypatch) -> tuple:
    monkeypatch.setattr("agentic.config.OVERLAY_PATH", tmp_path / "overlay.yaml")
    db = Database(tmp_path / "roles.db")
    audit = AuditStore(db)
    deps = WebDeps(
        settings=Settings(mode="paper", broker="paper", market_data="paper"),
        signals=SignalStore(db),
        killswitch=KillSwitch(db, audit),
        approval_gate=None,
        audit=audit,
        positions=PositionStore(db),
        orders=OrderStore(db),
        decisions=DecisionStore(db),
    )
    return TestClient(create_app(deps)), audit


@pytest.fixture()
def viewer_env(monkeypatch):
    monkeypatch.setenv("VIEWER_USER", VIEWER_USER)
    monkeypatch.setenv("VIEWER_PASSWORD", VIEWER_PASS)


def test_viewer_login_disabled_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("VIEWER_USER", raising=False)
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    client, _ = _app(tmp_path, monkeypatch)
    assert client.get("/api/stats", auth=(VIEWER_USER, VIEWER_PASS)).status_code == 401
    assert client.get("/api/stats", auth=(OWNER_USER, OWNER_PASS)).status_code == 200


def test_viewer_reads_and_owner_page_roles(tmp_path, viewer_env, monkeypatch):
    client, _ = _app(tmp_path, monkeypatch)
    owner_page = client.get("/", auth=(OWNER_USER, OWNER_PASS))
    assert owner_page.status_code == 200
    assert 'const ROLE = "owner";' in owner_page.text
    assert "view-badge" in owner_page.text

    viewer_page = client.get("/", auth=(VIEWER_USER, VIEWER_PASS))
    assert viewer_page.status_code == 200
    assert 'const ROLE = "viewer";' in viewer_page.text
    assert "view-only login" in viewer_page.text

    cfg = client.get("/api/config", auth=(VIEWER_USER, VIEWER_PASS))
    assert cfg.status_code == 200
    body = cfg.json()
    assert body["role"] == "viewer"
    assert "from_overlay" in body
    assert "overlay" in body
    assert "tax_reserve" in body["editable"]
    assert "trading_start" in body["editable"]
    assert "mode" not in body["editable"]
    assert body["readonly"]["mode"] == "paper"


def test_owner_edits_without_control_token(tmp_path, viewer_env, monkeypatch):
    client, audit = _app(tmp_path, monkeypatch)
    r = client.post(
        "/api/config",
        json={"entry": {"watchlist": ["F"]}, "tax_reserve": {"pct": 0.25}, "trading_start": "10:30"},
        auth=(OWNER_USER, OWNER_PASS),
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["role"] == "owner"
    assert r.json()["editable"]["entry"]["watchlist"] == ["F"]
    assert r.json()["editable"]["tax_reserve"]["pct"] == 0.25
    assert r.json()["editable"]["trading_start"] == "10:30"
    row = audit.latest(AuditEventType.CONFIG_EDIT, source="dashboard")
    assert row["payload"]["who"] == "owner"
    assert row["payload"]["values"]["entry.watchlist"] == {"old": [], "new": ["F"]}
    assert row["payload"]["values"]["tax_reserve.pct"] == {"old": 0.2, "new": 0.25}
    assert row["payload"]["values"]["trading_start"] == {"old": "10:00", "new": "10:30"}
    blob = json.dumps(row)
    assert CONTROL not in blob
    assert VIEWER_PASS not in blob
    assert OWNER_PASS not in blob


def test_control_token_is_script_alternative(tmp_path, monkeypatch):
    client, _ = _app(tmp_path, monkeypatch)
    r = client.post(
        f"/api/config?token={CONTROL}",
        json={"paper_buying_power": 12345},
        auth=None,
    )
    assert r.status_code == 200
    assert r.json()["editable"]["paper_buying_power"] == 12345
    paused = client.post(f"/control/pause?token={CONTROL}", auth=None)
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"


def test_viewer_cannot_change_state(tmp_path, viewer_env, monkeypatch):
    client, _ = _app(tmp_path, monkeypatch)
    r = client.post(
        "/api/config",
        json={"entry": {"watchlist": ["HACK"]}},
        auth=(VIEWER_USER, VIEWER_PASS),
    )
    assert r.status_code == 403
    cfg = client.get("/api/config", auth=(VIEWER_USER, VIEWER_PASS)).json()
    assert cfg["editable"]["entry"]["watchlist"] == []
    assert client.post("/control/pause", auth=(VIEWER_USER, VIEWER_PASS)).status_code == 403
    assert client.post("/control/resume", auth=(VIEWER_USER, VIEWER_PASS)).status_code == 403
    assert client.post("/control/alerts-mode?mode=off", auth=(VIEWER_USER, VIEWER_PASS)).status_code == 403
    # CONTROL_TOKEN does not elevate a view-only login.
    assert client.post(
        f"/api/config?token={CONTROL}",
        json={"entry": {"watchlist": ["HACK"]}},
        auth=(VIEWER_USER, VIEWER_PASS),
    ).status_code == 403


def _fill_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _iter_api_routes(routes):
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from _iter_api_routes(nested.routes)


def test_viewer_forbidden_on_every_non_get_route(tmp_path, viewer_env, monkeypatch):
    """Default-deny: every registered mutation returns 403 for viewer. pause-only excepted."""
    client, _ = _app(tmp_path, monkeypatch)
    checked = []
    for route in _iter_api_routes(client.app.router.routes):
        for method in route.methods:
            if method in {"GET", "HEAD", "OPTIONS"}:
                continue
            path = route.path
            if path.rstrip("/") == "/control/pause-only":
                continue
            url = _fill_path(path)
            r = client.request(method, url, auth=(VIEWER_USER, VIEWER_PASS), json={})
            assert r.status_code == 403, f"{method} {url} -> {r.status_code} {r.text}"
            checked.append((method, path))
    assert any(p.endswith("/config") and m == "POST" for m, p in checked)
    assert any(p.rstrip("/").endswith("/pause") for m, p in checked)
    assert checked


def test_pause_only_still_token_only(tmp_path, viewer_env, monkeypatch):
    monkeypatch.setenv("PAUSE_TOKEN", "test-pause-token-ok-long")
    client, _ = _app(tmp_path, monkeypatch)
    # Viewer without pause token is 401 (not 403) — this route is the exception.
    r = client.post("/control/pause-only", auth=(VIEWER_USER, VIEWER_PASS))
    assert r.status_code == 401
    ok = client.post(
        "/control/pause-only",
        headers={"X-Pause-Token": "test-pause-token-ok-long"},
        auth=None,
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "paused"


def test_startup_refuses_same_viewer_and_owner_user(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", OWNER_PASS)
    monkeypatch.setenv("CONTROL_TOKEN", CONTROL)
    monkeypatch.setenv("VIEWER_USER", "admin")
    monkeypatch.setenv("VIEWER_PASSWORD", VIEWER_PASS)
    with pytest.raises(SystemExit, match="VIEWER_USER"):
        require_runtime_secrets()


def test_mode_still_immutable_for_owner(tmp_path, monkeypatch):
    client, _ = _app(tmp_path, monkeypatch)
    r = client.post("/api/config", json={"mode": "live", "i_understand_live_trading": True})
    assert r.status_code == 400
    assert "mode" in r.json()["error"]
    assert client.get("/api/config").json()["readonly"]["mode"] == "paper"
