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
    {"web": {"port": 9999}},
])
def test_protected_keys_rejected(tmp_path, patch):
    settings = Settings(broker="paper", market_data="paper")
    with pytest.raises(SettingsEditError):
        apply_patch(settings, patch, overlay_path=tmp_path / "o.yaml")
    # Nothing changed and nothing persisted.
    assert settings.mode == "paper"
    assert settings.broker == "paper"
    assert not (tmp_path / "o.yaml").exists()


def test_owner_can_edit_tax_reserve_and_trading_start(tmp_path):
    settings = Settings(broker="paper", market_data="paper")
    ov = tmp_path / "o.yaml"
    apply_patch(settings, {
        "tax_reserve": {"dry_run": False, "pct": 0.3},
        "trading_start": "10:30",
        "entry": {"enabled": True},
        "roll": {"enabled": True},
    }, overlay_path=ov)
    assert settings.tax_reserve.dry_run is False
    assert settings.tax_reserve.pct == 0.3
    assert settings.trading_start == "10:30"
    assert settings.entry.enabled is True
    assert settings.roll.enabled is True
    saved = load_overlay(ov)
    assert saved["tax_reserve"]["dry_run"] is False
    assert saved["trading_start"] == "10:30"


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


def test_overlay_applies_strategy_keys_but_not_mode(tmp_path, monkeypatch, caplog):
    """Overlay may change strategy knobs; mode / broker / live-arming stay file-only."""
    base = tmp_path / "config.yaml"
    base.write_text(
        "mode: paper\n"
        "broker: paper\n"
        "tax_reserve:\n"
        "  enabled: true\n"
        "  dry_run: true\n"
        "  pct: 0.20\n"
        "trading_start: '10:00'\n"
        "entry:\n"
        "  enabled: false\n"
        "  watchlist: [AAPL]\n"
    )
    ov = tmp_path / "overlay.yaml"
    monkeypatch.setattr("agentic.config.OVERLAY_PATH", ov)
    save_overlay({
        "mode": "live",
        "i_understand_live_trading": True,
        "broker": "robinhood_mcp",
        "tax_reserve": {"dry_run": False, "pct": 0.99},
        "trading_start": "09:30",
        "entry": {"enabled": True, "watchlist": ["F"]},
    }, ov)
    with caplog.at_level(logging.WARNING, logger="agentic.config"):
        s = load_config(base)
    assert s.mode == "paper"
    assert s.broker == "paper"
    assert s.is_live is False
    assert s.tax_reserve.dry_run is False
    assert s.tax_reserve.pct == 0.99
    assert s.trading_start == "09:30"
    assert s.entry.enabled is True
    assert s.entry.watchlist == ["F"]
    assert "mode" in caplog.text


def test_execution_leaves_still_editable(tmp_path):
    settings = Settings(broker="paper", market_data="paper")
    apply_patch(settings, {"execution": {"limit_buffer_pct": 0.03}}, overlay_path=tmp_path / "o.yaml")
    assert settings.execution.limit_buffer_pct == 0.03
    saved = load_overlay(tmp_path / "o.yaml")
    assert saved["execution"]["limit_buffer_pct"] == 0.03


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
    assert body["role"] == "owner"
    # The dangerous knobs are reported read-only, never in the editable set.
    assert "mode" not in body["editable"]
    assert "broker" not in body["editable"]
    assert "tax_reserve" in body["editable"]
    assert "trading_start" in body["editable"]
    assert body["from_overlay"] == []
    assert body["overlay"] == {}
    assert "tax_reserve" not in body["readonly"]


def test_post_config_owner_basic_does_not_need_token(client):
    c, settings, audit, scanner = client
    r = c.post("/api/config", json={"entry": {"watchlist": ["AAPL"]}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["role"] == "owner"
    assert settings.entry.watchlist == ["AAPL"]
    assert scanner.settings.entry.watchlist == ["AAPL"]
    saved = load_overlay()
    assert saved["entry"]["watchlist"] == ["AAPL"]
    assert "entry.watchlist" in body["from_overlay"]
    row = audit.latest(AuditEventType.CONFIG_EDIT, source="dashboard")
    assert row["payload"]["who"] == "owner"
    assert row["payload"]["watchlist"] == {"old": [], "new": ["AAPL"]}
    assert row["payload"]["values"]["entry.watchlist"] == {"old": [], "new": ["AAPL"]}
    blob = json.dumps(row)
    assert CONTROL not in blob
    assert PAUSE not in blob


def test_post_config_rejects_pause_token_without_owner(client, monkeypatch):
    monkeypatch.setenv("PAUSE_TOKEN", PAUSE)
    c, settings, *_ = client
    r = c.post(f"/api/config?token={PAUSE}", json={"entry": {"watchlist": ["F"]}}, auth=None)
    assert r.status_code == 401
    assert settings.entry.watchlist == []


def test_post_config_owner_adds_ticker(client):
    """CONTROL_TOKEN still hot-applies watchlist as a scripted alternative."""
    c, settings, audit, scanner = client
    r = c.post(
        f"/api/config?token={CONTROL}",
        json={"entry": {"watchlist": ["AAPL"]}},
        auth=None,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert settings.entry.watchlist == ["AAPL"]
    assert scanner.settings.entry.watchlist == ["AAPL"]
    saved = load_overlay()
    assert saved["entry"]["watchlist"] == ["AAPL"]
    assert "entry.watchlist" in body["from_overlay"]
    assert body["overlay"]["entry"]["watchlist"] == ["AAPL"]
    row = audit.latest(AuditEventType.CONFIG_EDIT, source="dashboard")
    assert row is not None
    assert row["payload"]["who"] == "owner"
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
    {"i_understand_live_trading": True},
    {"web": {"enabled": False}},
])
def test_post_protected_edit_rejected_even_with_control_token(client, patch):
    c, settings, *_ = client
    r = c.post(f"/api/config?token={CONTROL}", json=patch)
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert settings.mode == "paper"
    assert settings.broker == "paper"


def test_post_config_audits_leaf_old_new(client):
    """CONFIG_EDIT records every changed leaf as old→new, never the token."""
    c, settings, audit, _ = client
    r = c.post("/api/config", json={"execution": {"limit_buffer_pct": 0.03}})
    assert r.status_code == 200
    row = audit.latest(AuditEventType.CONFIG_EDIT, source="dashboard")
    assert row is not None
    assert row["payload"]["who"] == "owner"
    assert row["payload"]["changed"] == ["execution"]
    assert row["payload"]["values"]["execution.limit_buffer_pct"] == {"old": 0.02, "new": 0.03}
    blob = json.dumps(row)
    assert CONTROL not in blob
    assert PAUSE not in blob


def test_dashboard_settings_use_owner_login(client):
    c, *_ = client
    html = c.get("/").text
    assert 'const ROLE = "owner";' in html
    assert 'id="wl-token"' not in html
    assert 'id="tn-token"' not in html
    assert 'id="al-token"' not in html
    assert 'id="tn-tr-save"' in html
    assert "Wrong CONTROL_TOKEN (unauthorized)." not in html
    assert "/api/config?token=" not in html
    assert "Dashboard is read-only. Edit config.yaml and restart." not in html
    assert "from_overlay" in html
    assert "view-only login" in html
    assert "postConfig({tax_reserve" in html
