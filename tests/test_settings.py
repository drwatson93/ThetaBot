"""Stage 2 Increment 1: in-app settings editor — overlay persistence, hot-apply, guardrails."""
import json
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agentic.config import Settings, load_config, load_overlay, save_overlay
from agentic.domain.enums import AuditEventType
from agentic.entry.risk import RiskSizer
from agentic.services.approval import ApprovalGate
from agentic.services.executor import OrderExecutor
from agentic.services.killswitch import KillSwitch
from agentic.brokers.paper_broker import PaperBroker
from agentic.marketdata.base import PaperMarketData
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app
from agentic.web.settings import SettingsEditError, apply_patch

CONTROL = "test-control-token-ok-long"
PAUSE = "test-pause-token-ok-long"


# --- apply_patch unit tests (the core logic) ---------------------------------------------------

def test_hot_apply_reaches_live_sizer(tmp_path):
    """A sizing edit must reach a RiskSizer that captured settings.entry.sizing by reference."""
    settings = Settings(broker="paper", market_data="paper")
    sizer = RiskSizer(settings.entry.sizing)  # holds the SAME sizing object (as the scanner does)
    ov = tmp_path / "overlay.yaml"

    changed = apply_patch(
        settings, {"entry": {"sizing": {"max_position_size_pct": 0.25}}}, overlay_path=ov
    )

    assert changed == ["entry"]
    assert settings.entry.sizing.max_position_size_pct == 0.25
    # In-place mutation (not replacement) — the running sizer sees the new value:
    assert sizer.sizing.max_position_size_pct == 0.25


def test_hot_apply_new_tuning_knobs(tmp_path):
    """The Tuning-panel levers round-trip through /api/config: multi-CSP cap, the entry gates, and
    a per-ticker override — validated, hot-applied in place, and persisted."""
    settings = Settings(broker="paper", market_data="paper")
    sizer = RiskSizer(settings.entry.sizing)
    ov = tmp_path / "overlay.yaml"

    apply_patch(settings, {"entry": {
        "sizing": {"max_pct_per_underlying": 0.15},
        "criteria": {"min_strike_expected_moves": 1.0, "min_iv_rv_ratio": 1.1},
        "per_ticker": {"BULL": {"delta_max": 0.18, "min_iv_rank": 40}},
    }}, overlay_path=ov)

    assert sizer.sizing.max_pct_per_underlying == 0.15          # reached the live sizer by reference
    assert settings.entry.criteria.min_strike_expected_moves == 1.0
    assert settings.entry.criteria.min_iv_rv_ratio == 1.1
    assert settings.entry.per_ticker["BULL"]["delta_max"] == 0.18
    saved = load_overlay(ov)
    assert saved["entry"]["sizing"]["max_pct_per_underlying"] == 0.15   # persisted across restarts

    # And turning the cap back off (null) is accepted.
    apply_patch(settings, {"entry": {"sizing": {"max_pct_per_underlying": None}}}, overlay_path=ov)
    assert settings.entry.sizing.max_pct_per_underlying is None


def test_edit_persists_and_merges_overlay(tmp_path):
    settings = Settings(broker="paper", market_data="paper")
    ov = tmp_path / "overlay.yaml"

    apply_patch(settings, {"paper_buying_power": 30000}, overlay_path=ov)
    apply_patch(settings, {"entry": {"watchlist": ["F", "SOFI"]}}, overlay_path=ov)

    saved = load_overlay(ov)
    assert saved["paper_buying_power"] == 30000
    assert saved["entry"]["watchlist"] == ["F", "SOFI"]


@pytest.mark.parametrize("patch", [
    {"mode": "live"},
    {"i_understand_live_trading": True},
    {"broker": "robinhood_mcp"},
    {"robinhood": {"account_number": "999"}},
    {"market_data": "alpaca"},
    {"tax_reserve": {"dry_run": False}},
    {"tax_reserve": {"allow_sgov_test_buy": True, "pct": 0.5}},
    {"entry": {"enabled": True}},
    {"entry": {"feed": "opra"}},
    {"roll": {"enabled": True}},
    {"trading_start": "09:30"},
    {"trading_end": "17:00"},
    {"execution": {"order_type": "market"}},
    {"execution": {"limit_only": False}},
    {"execution": {"session_open": "08:00"}},
    {"nyse_holidays": []},
])
def test_protected_keys_rejected(tmp_path, patch):
    settings = Settings(broker="paper", market_data="paper")
    with pytest.raises(SettingsEditError):
        apply_patch(settings, patch, overlay_path=tmp_path / "o.yaml")
    # Nothing changed and nothing persisted.
    assert settings.mode == "paper"
    assert settings.tax_reserve.dry_run is True
    assert settings.entry.enabled is False
    assert settings.roll.enabled is False
    assert settings.trading_start == "10:00"
    assert not (tmp_path / "o.yaml").exists()


def test_invalid_value_rejected_and_not_applied(tmp_path):
    settings = Settings(broker="paper", market_data="paper")
    before = settings.entry.sizing.max_position_size_pct
    with pytest.raises(SettingsEditError):
        apply_patch(
            settings,
            {"entry": {"sizing": {"max_position_size_pct": "not-a-number"}}},
            overlay_path=tmp_path / "o.yaml",
        )
    assert settings.entry.sizing.max_position_size_pct == before


def test_empty_patch_rejected(tmp_path):
    settings = Settings(broker="paper", market_data="paper")
    with pytest.raises(SettingsEditError):
        apply_patch(settings, {}, overlay_path=tmp_path / "o.yaml")


# --- load_config overlay merge -----------------------------------------------------------------

def test_load_config_merges_overlay(tmp_path, monkeypatch):
    base = tmp_path / "config.yaml"
    base.write_text("mode: paper\npaper_buying_power: 1500\nentry:\n  watchlist: [\"AAPL\"]\n")
    ov = tmp_path / "overlay.yaml"
    monkeypatch.setattr("agentic.config.OVERLAY_PATH", ov)
    save_overlay({"paper_buying_power": 25000, "entry": {"watchlist": ["F", "SOFI"]}}, ov)

    s = load_config(base)
    assert s.paper_buying_power == 25000
    assert s.entry.watchlist == ["F", "SOFI"]
    # Base value not touched by the overlay stays put.
    assert s.mode == "paper"


def test_overlay_tax_reserve_dry_run_has_no_effect(tmp_path, monkeypatch, caplog):
    """An old overlay must not flip tax_reserve.dry_run (or other locked buy-path keys)."""
    base = tmp_path / "config.yaml"
    base.write_text(
        "mode: paper\n"
        "tax_reserve:\n"
        "  enabled: true\n"
        "  dry_run: true\n"
        "  allow_sgov_test_buy: false\n"
        "  pct: 0.20\n"
        "entry:\n"
        "  enabled: false\n"
        "  feed: indicative\n"
        "  watchlist: [AAPL]\n"
        "roll:\n"
        "  enabled: false\n"
    )
    ov = tmp_path / "overlay.yaml"
    monkeypatch.setattr("agentic.config.OVERLAY_PATH", ov)
    save_overlay({
        "tax_reserve": {"dry_run": False, "allow_sgov_test_buy": True, "pct": 0.99},
        "entry": {"enabled": True, "feed": "opra", "watchlist": ["F"]},
        "roll": {"enabled": True},
    }, ov)
    with caplog.at_level(logging.WARNING, logger="agentic.config"):
        s = load_config(base)
    assert s.tax_reserve.dry_run is True
    assert s.tax_reserve.allow_sgov_test_buy is False
    assert s.tax_reserve.pct == 0.20
    assert s.entry.enabled is False
    assert s.entry.feed == "indicative"
    assert s.entry.watchlist == ["F"]  # non-locked overlay still applies
    assert s.roll.enabled is False
    assert "tax_reserve" in caplog.text


def test_trading_window_patch_rejected(tmp_path):
    """House rule: no entries before 10:00 ET — trading_start is file/env only."""
    settings = Settings(broker="paper", market_data="paper")
    with pytest.raises(SettingsEditError, match="trading_start"):
        apply_patch(settings, {"trading_start": "09:30"}, overlay_path=tmp_path / "o.yaml")
    assert settings.trading_start == "10:00"
    assert not (tmp_path / "o.yaml").exists()


def test_limit_only_patch_rejected(tmp_path):
    """House rule: limit-only / never-market — not a dashboard knob."""
    settings = Settings(broker="paper", market_data="paper")
    ov = tmp_path / "o.yaml"
    with pytest.raises(SettingsEditError, match="execution.order_type"):
        apply_patch(settings, {"execution": {"order_type": "market"}}, overlay_path=ov)
    with pytest.raises(SettingsEditError, match="execution.limit_only"):
        apply_patch(settings, {"execution": {"limit_only": False}}, overlay_path=ov)
    assert settings.execution.limit_buffer_pct == 0.02
    assert not ov.exists()


def test_execution_non_house_rule_leaves_still_editable(tmp_path):
    """Parent `execution` stays editable; only order-type / session leaves are locked."""
    settings = Settings(broker="paper", market_data="paper")
    apply_patch(settings, {"execution": {"limit_buffer_pct": 0.03}}, overlay_path=tmp_path / "o.yaml")
    assert settings.execution.limit_buffer_pct == 0.03
    saved = load_overlay(tmp_path / "o.yaml")
    assert saved["execution"]["limit_buffer_pct"] == 0.03
    assert "order_type" not in saved["execution"]
    assert "limit_only" not in saved["execution"]


def test_overlay_trading_start_has_no_effect(tmp_path, monkeypatch, caplog):
    """An old overlay must not move the 10:00 ET entry window."""
    base = tmp_path / "config.yaml"
    base.write_text(
        "mode: paper\n"
        "trading_start: '10:00'\n"
        "paper_buying_power: 1500\n"
        "entry:\n"
        "  watchlist: [AAPL]\n"
    )
    ov = tmp_path / "overlay.yaml"
    monkeypatch.setattr("agentic.config.OVERLAY_PATH", ov)
    save_overlay({
        "trading_start": "09:30",
        "trading_end": "17:00",
        "execution": {"order_type": "market", "limit_only": False, "limit_buffer_pct": 0.03},
        "nyse_holidays": ["2026-01-01"],
        "paper_buying_power": 25000,
    }, ov)
    with caplog.at_level(logging.WARNING, logger="agentic.config"):
        s = load_config(base)
    assert s.trading_start == "10:00"
    assert s.paper_buying_power == 25000
    assert s.execution.limit_buffer_pct == 0.03
    assert not hasattr(s.execution, "order_type")
    assert "trading_start" in caplog.text


# --- endpoint tests ----------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.config.OVERLAY_PATH", tmp_path / "overlay.yaml")
    db = Database(tmp_path / "s.db")
    audit = AuditStore(db)
    positions = PositionStore(db)
    decisions = DecisionStore(db)
    orders = OrderStore(db)
    signals = SignalStore(db)
    killswitch = KillSwitch(db, audit)
    settings = Settings(mode="paper", broker="paper", market_data="paper")
    scanner = SimpleNamespace(settings=settings)  # same object the live scanner reads
    broker = PaperBroker(seed_positions=[])
    executor = OrderExecutor(settings, broker, PaperMarketData(), positions, orders,
                             decisions, audit, killswitch, poll_interval_seconds=0.001)
    approval_gate = ApprovalGate(settings, decisions, positions, executor, audit)
    deps = WebDeps(settings=settings, signals=signals, killswitch=killswitch,
                   approval_gate=approval_gate, audit=audit,
                   positions=positions, orders=orders, decisions=decisions,
                   scanner=scanner)
    return TestClient(create_app(deps)), settings, audit, scanner


def test_get_config_returns_editable_and_readonly(client):
    c, *_ = client
    r = c.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert "entry" in body["editable"]
    assert body["readonly"]["mode"] == "paper"
    assert body["readonly"]["live_armed"] is False
    # The dangerous knobs are reported read-only, never in the editable set.
    assert "mode" not in body["editable"]
    assert "tax_reserve" not in body["editable"]
    assert body["from_overlay"] == []
    assert body["overlay"] == {}
    assert "tax_reserve" in body["readonly"]
    assert body["readonly"]["tax_reserve"]["dry_run"] is True
    assert body["readonly"]["trading_start"] == "10:00"
    assert "trading_start" not in body["editable"]


def test_post_config_requires_control_token(client):
    c, settings, *_ = client
    r = c.post("/api/config", json={"entry": {"watchlist": ["F", "SOFI", "T"]}})
    assert r.status_code == 401
    assert r.json()["ok"] is False
    assert r.json().get("status") == "unauthorized"
    assert settings.entry.watchlist == []


def test_post_config_rejects_pause_token(client, monkeypatch):
    monkeypatch.setenv("PAUSE_TOKEN", PAUSE)
    c, settings, *_ = client
    r = c.post(f"/api/config?token={PAUSE}", json={"entry": {"watchlist": ["F"]}})
    assert r.status_code == 401
    assert r.json()["ok"] is False
    assert settings.entry.watchlist == []


def test_post_config_owner_adds_ticker(client):
    """Valid CONTROL_TOKEN hot-applies watchlist to the live scanner object and persists overlay."""
    c, settings, audit, scanner = client
    r = c.post(
        f"/api/config?token={CONTROL}",
        json={"entry": {"watchlist": ["AAPL"]}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert settings.entry.watchlist == ["AAPL"]
    assert scanner.settings.entry.watchlist == ["AAPL"]  # same Settings the scanner reads each cycle
    saved = load_overlay()
    assert saved["entry"]["watchlist"] == ["AAPL"]
    assert "entry.watchlist" in body["from_overlay"]
    assert body["overlay"]["entry"]["watchlist"] == ["AAPL"]
    row = audit.latest(AuditEventType.CONFIG_EDIT, source="dashboard")
    assert row is not None
    assert row["payload"]["changed"] == ["entry"]
    assert row["payload"]["watchlist"] == {"old": [], "new": ["AAPL"]}
    assert row["payload"]["values"]["entry.watchlist"] == {"old": [], "new": ["AAPL"]}
    blob = json.dumps(row)
    assert CONTROL not in blob
    assert PAUSE not in blob
    assert "token" not in json.dumps(row["payload"])


@pytest.mark.parametrize("patch", [
    {"mode": "live"},
    {"broker": "robinhood_mcp"},
    {"tax_reserve": {"dry_run": False}},
    {"tax_reserve": {"pct": 0.5, "allow_sgov_test_buy": True}},
    {"entry": {"enabled": True}},
    {"roll": {"enabled": True}},
    {"trading_start": "09:30"},
    {"execution": {"order_type": "market"}},
    {"execution": {"limit_only": False}},
])
def test_post_protected_edit_rejected_even_with_control_token(client, patch):
    c, settings, *_ = client
    r = c.post(f"/api/config?token={CONTROL}", json=patch)
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert settings.mode == "paper"
    assert settings.broker == "paper"
    assert settings.tax_reserve.dry_run is True
    assert settings.entry.enabled is False
    assert settings.roll.enabled is False
    assert settings.trading_start == "10:00"


def test_post_trading_window_patch_rejected(client):
    c, settings, audit, _ = client
    r = c.post(f"/api/config?token={CONTROL}", json={"trading_start": "09:30"})
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert "trading_start" in r.json()["error"]
    assert settings.trading_start == "10:00"
    assert audit.latest(AuditEventType.CONFIG_EDIT) is None


def test_post_limit_only_patch_rejected(client):
    c, settings, audit, _ = client
    r = c.post(
        f"/api/config?token={CONTROL}",
        json={"execution": {"order_type": "market", "limit_only": False}},
    )
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert "execution.order_type" in r.json()["error"]
    assert "execution.limit_only" in r.json()["error"]
    assert settings.execution.limit_buffer_pct == 0.02
    assert audit.latest(AuditEventType.CONFIG_EDIT) is None


def test_post_config_audits_leaf_old_new(client):
    """CONFIG_EDIT records every changed leaf as old→new, never the token."""
    c, settings, audit, _ = client
    r = c.post(
        f"/api/config?token={CONTROL}",
        json={"execution": {"limit_buffer_pct": 0.03}},
    )
    assert r.status_code == 200
    row = audit.latest(AuditEventType.CONFIG_EDIT, source="dashboard")
    assert row is not None
    assert row["payload"]["changed"] == ["execution"]
    assert row["payload"]["values"]["execution.limit_buffer_pct"] == {"old": 0.02, "new": 0.03}
    blob = json.dumps(row)
    assert CONTROL not in blob
    assert PAUSE not in blob


def test_dashboard_settings_use_control_token(client):
    c, *_ = client
    html = c.get("/").text
    assert 'id="wl-token"' in html
    assert 'id="tn-token"' in html
    assert "Wrong CONTROL_TOKEN (unauthorized)." in html
    assert "/api/config?token=" in html
    assert "Dashboard is read-only. Edit config.yaml and restart." not in html
    assert "from_overlay" in html
    assert "Tax reserve is file-only" in html or "File-only, same as mode" in html
    assert "postConfig({tax_reserve" not in html
