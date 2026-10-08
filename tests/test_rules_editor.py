"""Owner-only exit-rule edits: ranges, identity, stop triggers, audit, overlay, engine."""
from __future__ import annotations

import copy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agentic.brokers.paper_broker import PaperBroker
from agentic.config import RuleConfig, Settings, load_config, load_overlay
from agentic.domain.enums import AuditEventType, DecisionStatus, Direction, OptionType, Strategy
from agentic.domain.models import Position, utcnow
from agentic.marketdata.base import PaperMarketData
from agentic.marketdata.quote import OptionQuote
from agentic.rules.engine import RulesEngine
from agentic.services.approval import ApprovalGate
from agentic.services.executor import OrderExecutor
from agentic.services.killswitch import KillSwitch
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app
from agentic.web.rules_view import describe_active_rules

VIEWER_USER = "viewer"
VIEWER_PASS = "test-viewer-password-ok"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.config.OVERLAY_PATH", tmp_path / "overlay.yaml")
    db = Database(tmp_path / "rules.db")
    audit = AuditStore(db)
    positions = PositionStore(db)
    decisions = DecisionStore(db)
    orders = OrderStore(db)
    settings = Settings(mode="paper", broker="paper", market_data="paper")
    killswitch = KillSwitch(db, audit)
    broker = PaperBroker(seed_positions=[])
    executor = OrderExecutor(
        settings, broker, PaperMarketData(), positions, orders, decisions, audit,
        killswitch, poll_interval_seconds=0.001,
    )
    deps = WebDeps(
        settings=settings, signals=SignalStore(db), killswitch=killswitch,
        approval_gate=ApprovalGate(settings, decisions, positions, executor, audit),
        audit=audit, positions=positions, orders=orders, decisions=decisions,
        scanner=SimpleNamespace(settings=settings),
    )
    return TestClient(create_app(deps)), settings, audit, deps.scanner


def _rules() -> list[RuleConfig]:
    return [
        RuleConfig(name="profit-target", rule_type="PROFIT_TARGET", enabled=True,
                   requires_approval=False, params={"profit_pct": 0.5}),
        RuleConfig(name="stop-loss", rule_type="STOP_LOSS", enabled=True,
                   requires_approval=False, params={"loss_mult": 2.0, "delta_stop": 0.5}),
        RuleConfig(name="dte-close", rule_type="DTE", enabled=True,
                   requires_approval=False, params={"dte_threshold": 2, "action": "close"}),
        RuleConfig(name="tv-signal", rule_type="SIGNAL", enabled=True,
                   requires_approval=True, params={"match": "underlying"}),
    ]


def _seed(settings: Settings) -> None:
    settings.rules = _rules()


def _dump(settings: Settings) -> list[dict]:
    return copy.deepcopy(settings.model_dump(mode="json")["rules"])


def _edit(rules: list[dict], name: str, *, enabled: bool | None = None, **params):
    for rule in rules:
        if rule["name"] == name:
            if enabled is not None:
                rule["enabled"] = enabled
            rule["params"].update(params)
            return rules
    raise AssertionError(name)


def _audit_count(audit) -> int:
    row = audit.db.conn.execute(
        "SELECT COUNT(*) FROM audit WHERE event_type = ?",
        (AuditEventType.CONFIG_EDIT.value,),
    ).fetchone()
    return int(row[0])


def _pos():
    return Position(
        occ_symbol="ZZTEST261218P00010000", underlying="ZZTEST", option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=1,
        strike=10.0, expiration=utcnow().date() + timedelta(days=14), credit_received=1.0,
        current_bid=2.0, current_ask=2.2, current_mark=2.1, delta=-0.2,
    )


def _quote(*, bid: float, ask: float, delta: float) -> OptionQuote:
    return OptionQuote(
        occ_symbol="ZZTEST261218P00010000", bid=bid, ask=ask,
        mark=(bid + ask) / 2, delta=delta,
    )


def test_rules_tab_reuses_owner_gate(client):
    c, *_ = client
    html = c.get("/").text
    assert 'id="rules-save"' in html
    assert "owner-only" in html and 'id="rules-save"' in html
    assert 'id="rules-confirm"' in html
    assert "Old → new" in html
    assert "Null means that trigger is off" in html
    assert "#pane-rules input, #pane-rules button" in html
    assert "const ROLE = \"owner\";" in html
    # Mode, live-arming, and broker are not fields on this editor.
    assert 'id="rules-mode"' not in html
    assert "mode: live" not in html


def test_viewer_page_is_read_only_markup(client, monkeypatch):
    monkeypatch.setenv("VIEWER_USER", VIEWER_USER)
    monkeypatch.setenv("VIEWER_PASSWORD", VIEWER_PASS)
    c, *_ = client
    page = c.get("/", auth=(VIEWER_USER, VIEWER_PASS))
    assert page.status_code == 200
    assert 'const ROLE = "viewer";' in page.text
    assert "view-only login" in page.text
    assert 'id="rules-save"' in page.text
    assert "body.role-viewer .owner-only{display:none" in page.text


def test_owner_can_toggle_and_tune_rules(client):
    c, settings, audit, _scanner = client
    _seed(settings)
    rules = _edit(_dump(settings), "profit-target", profit_pct=0.6)
    _edit(rules, "stop-loss", enabled=False)
    r = c.post("/api/config", json={"rules": rules})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["role"] == "owner"
    assert settings.rules[0].params["profit_pct"] == 0.6
    assert settings.rules[1].enabled is False
    assert settings.mode == "paper"
    assert settings.i_understand_live_trading is False
    assert "mode" not in body["editable"]
    assert "broker" not in body["editable"]
    row = audit.latest(AuditEventType.CONFIG_EDIT, source="dashboard")
    assert row["payload"]["who"] == "owner"
    assert row["payload"]["changed"] == ["rules"]
    values = row["payload"]["values"]
    assert values["rules.profit-target.params.profit_pct"] == {"old": 0.5, "new": 0.6}
    assert values["rules.stop-loss.enabled"] == {"old": True, "new": False}
    blob = str(row)
    assert "test-control-token" not in blob
    assert VIEWER_PASS not in blob


def test_viewer_rules_save_is_403_and_changes_nothing(client, monkeypatch):
    monkeypatch.setenv("VIEWER_USER", VIEWER_USER)
    monkeypatch.setenv("VIEWER_PASSWORD", VIEWER_PASS)
    c, settings, audit, _scanner = client
    _seed(settings)
    rules = _edit(_dump(settings), "stop-loss", enabled=False)
    r = c.post("/api/config", json={"rules": rules}, auth=(VIEWER_USER, VIEWER_PASS))
    assert r.status_code == 403
    assert settings.rules[1].enabled is True
    assert settings.rules[0].params["profit_pct"] == 0.5
    assert _audit_count(audit) == 0
    assert load_overlay() == {}


def test_noop_rules_post_writes_audit_with_empty_values(client):
    """Same convention as every other /api/config save: the row is written, values is empty."""
    c, settings, audit, _scanner = client
    _seed(settings)
    r = c.post("/api/config", json={"rules": _dump(settings)})
    assert r.status_code == 200, r.text
    row = audit.latest(AuditEventType.CONFIG_EDIT, source="dashboard")
    assert row["payload"]["changed"] == ["rules"]
    assert row["payload"]["values"] == {}
    assert settings.rules[1].params["loss_mult"] == 2.0


@pytest.mark.parametrize("mutate,needle", [
    (lambda rules: _edit(rules, "profit-target", profit_pct=0.04), "between"),
    (lambda rules: _edit(rules, "profit-target", profit_pct=0.96), "between"),
    (lambda rules: _edit(rules, "profit-target", profit_pct=True), "must be a number"),
    (lambda rules: _edit(rules, "stop-loss", loss_mult=0), "between"),
    (lambda rules: _edit(rules, "stop-loss", loss_mult=0.5), "between"),
    (lambda rules: _edit(rules, "stop-loss", loss_mult=10.01), "between"),
    (lambda rules: _edit(rules, "stop-loss", delta_stop=0.09), "between"),
    (lambda rules: _edit(rules, "stop-loss", delta_stop=1.01), "between"),
    (lambda rules: _edit(rules, "dte-close", dte_threshold=-1), "whole number"),
    (lambda rules: _edit(rules, "dte-close", dte_threshold=31), "whole number"),
    (lambda rules: _edit(rules, "dte-close", dte_threshold=2.5), "whole number"),
    (lambda rules: _edit(rules, "dte-close", dte_threshold="2"), "must be a number"),
    (lambda rules: _edit(rules, "stop-loss", loss_mult=None, delta_stop=None), "both triggers"),
])
def test_invalid_rules_rejected_with_no_change(client, mutate, needle):
    c, settings, audit, _scanner = client
    _seed(settings)
    rules = _dump(settings)
    mutate(rules)
    # A sibling edit in the same body must not stick when rules are rejected.
    r = c.post("/api/config", json={"rules": rules, "paper_buying_power": 111.0})
    assert r.status_code == 400, r.text
    assert needle in r.json()["error"]
    assert r.json()["ok"] is False
    assert settings.rules[0].params["profit_pct"] == 0.5
    assert settings.rules[1].params["loss_mult"] == 2.0
    assert settings.rules[1].params["delta_stop"] == 0.5
    assert settings.rules[2].params["dte_threshold"] == 2
    assert settings.paper_buying_power == 100_000.0
    assert _audit_count(audit) == 0
    assert load_overlay() == {}


def test_boundary_values_are_accepted(client):
    c, settings, *_ = client
    _seed(settings)
    rules = _dump(settings)
    _edit(rules, "profit-target", profit_pct=0.05)
    _edit(rules, "stop-loss", loss_mult=1.0, delta_stop=1.0)
    _edit(rules, "dte-close", dte_threshold=0)
    r = c.post("/api/config", json={"rules": rules})
    assert r.status_code == 200, r.text
    assert settings.rules[0].params["profit_pct"] == 0.05
    assert settings.rules[1].params["loss_mult"] == 1.0
    assert settings.rules[1].params["delta_stop"] == 1.0
    assert settings.rules[2].params["dte_threshold"] == 0
    rules = _dump(settings)
    _edit(rules, "profit-target", profit_pct=0.95)
    _edit(rules, "stop-loss", loss_mult=10, delta_stop=0.1)
    _edit(rules, "dte-close", dte_threshold=30)
    r = c.post("/api/config", json={"rules": rules})
    assert r.status_code == 200, r.text
    assert settings.rules[0].params["profit_pct"] == 0.95
    assert settings.rules[1].params["loss_mult"] == 10
    assert settings.rules[1].params["delta_stop"] == 0.1
    assert settings.rules[2].params["dte_threshold"] == 30


@pytest.mark.parametrize("mutate,needle", [
    (lambda rules: rules[1].__setitem__("name", "nope"), "Unknown rule name"),
    (lambda rules: rules[1].__setitem__("rule_type", "NOPE"), "Unknown rule type"),
    (lambda rules: rules[1].__setitem__("rule_type", "DTE"), "Cannot change rule_type"),
    (lambda rules: rules.append(copy.deepcopy(rules[0]) | {"name": "extra"}), "Cannot add or remove"),
    (lambda rules: rules.pop(), "Cannot add or remove"),
    (lambda rules: rules.__setitem__(slice(None), [rules[1], rules[0], rules[2], rules[3]]),
     "same names"),
    (lambda rules: rules[2]["params"].__setitem__("action", "alert"), "Cannot change"),
    (lambda rules: rules[0]["params"].__setitem__("nope", 1), "Unknown parameter"),
])
def test_identity_changes_rejected_with_no_change(client, mutate, needle):
    c, settings, audit, _scanner = client
    _seed(settings)
    rules = _dump(settings)
    mutate(rules)
    r = c.post("/api/config", json={"rules": rules})
    assert r.status_code == 400, r.text
    assert needle in r.json()["error"]
    assert [rule.name for rule in settings.rules] == [
        "profit-target", "stop-loss", "dte-close", "tv-signal",
    ]
    assert settings.rules[1].rule_type == "STOP_LOSS"
    assert settings.rules[2].params["action"] == "close"
    assert _audit_count(audit) == 0
    assert load_overlay() == {}


def test_one_stop_trigger_off_keeps_the_other(client):
    c, settings, audit, _scanner = client
    _seed(settings)
    rules = _edit(_dump(settings), "stop-loss", loss_mult=None)
    r = c.post("/api/config", json={"rules": rules})
    assert r.status_code == 200, r.text
    assert settings.rules[1].enabled is True
    assert settings.rules[1].params["loss_mult"] is None
    assert settings.rules[1].params["delta_stop"] == 0.5
    row = audit.latest(AuditEventType.CONFIG_EDIT, source="dashboard")
    assert row["payload"]["values"]["rules.stop-loss.params.loss_mult"] == {
        "old": 2.0, "new": None,
    }
    engine = RulesEngine.from_configs(settings.rules)
    pos = _pos()
    assert engine.evaluate(pos, _quote(bid=3.0, ask=3.2, delta=-0.2)) == []
    delta_hits = engine.evaluate(pos, _quote(bid=1.1, ask=1.2, delta=-0.6))
    assert len(delta_hits) == 1 and "Delta" in delta_hits[0].reason

    rules = _edit(_dump(settings), "stop-loss", delta_stop=None, loss_mult=2.0)
    r = c.post("/api/config", json={"rules": rules})
    assert r.status_code == 200, r.text
    engine.refresh(settings.rules)
    assert engine.evaluate(pos, _quote(bid=1.1, ask=1.2, delta=-0.9)) == []
    loss_hits = engine.evaluate(pos, _quote(bid=2.0, ask=2.2, delta=-0.9))
    assert len(loss_hits) == 1 and "Stop-loss" in loss_hits[0].reason


def test_disabling_the_rule_is_how_both_triggers_turn_off(client):
    c, settings, *_ = client
    _seed(settings)
    rules = _edit(_dump(settings), "stop-loss", enabled=False, loss_mult=None, delta_stop=None)
    r = c.post("/api/config", json={"rules": rules})
    assert r.status_code == 200, r.text
    assert settings.rules[1].enabled is False
    assert settings.rules[1].params["loss_mult"] is None
    assert settings.rules[1].params["delta_stop"] is None


def test_refresh_stops_a_disabled_rule_from_firing(client):
    c, settings, *_ = client
    _seed(settings)
    engine = RulesEngine.from_configs(settings.rules)
    pos = _pos()
    quote = _quote(bid=2.0, ask=2.2, delta=-0.2)
    assert any(d.rule_name == "stop-loss" for d in engine.evaluate(pos, quote))
    rules = _edit(_dump(settings), "stop-loss", enabled=False)
    assert c.post("/api/config", json={"rules": rules}).status_code == 200
    assert engine.refresh(settings.rules) is True
    assert engine.evaluate(pos, quote) == []
    assert "stop-loss" not in {rule.name for rule in engine.rules}


@pytest.mark.asyncio
async def test_monitor_poll_refreshes_and_skips_disabled_rule(tmp_path):
    """The monitor calls refresh before evaluate. No executor, so this cannot place an order."""
    from agentic.brokers.paper_broker import PaperBroker
    from agentic.marketdata.base import PaperMarketData
    from agentic.services.killswitch import KillSwitch
    from agentic.services.monitor import MonitorLoop
    from agentic.store.audit import AuditStore
    from agentic.store.db import Database
    from agentic.store.decisions import DecisionStore
    from agentic.store.positions import PositionStore

    db = Database(tmp_path / "m.db")
    audit = AuditStore(db)
    decisions = DecisionStore(db)
    settings = Settings(mode="paper", broker="paper", market_data="paper", rules=_rules())
    engine = RulesEngine.from_configs(settings.rules)
    settings.rules = _rules()
    settings.rules[1] = RuleConfig(
        name="stop-loss", rule_type="STOP_LOSS", enabled=False, requires_approval=False,
        params={"loss_mult": 2.0, "delta_stop": 0.5},
    )
    pos = _pos()
    broker = PaperBroker(seed_positions=[pos], buying_power=25_000.0)
    monitor = MonitorLoop(
        settings, broker, PaperMarketData(), PositionStore(db), audit, KillSwitch(db, audit),
        rules_engine=engine, decisions=decisions,
    )
    await monitor.run_once()
    assert decisions.list_by_status(DecisionStatus.PROPOSED) == []
    assert "stop-loss" not in {rule.name for rule in engine.rules}


def test_overlay_persists_rules_across_reload(client, tmp_path):
    c, settings, *_ = client
    _seed(settings)
    base = tmp_path / "base.yaml"
    base.write_text(
        "mode: paper\nbroker: paper\nmarket_data: paper\n"
        "rules:\n"
        "  - {name: profit-target, rule_type: PROFIT_TARGET, enabled: true,\n"
        "     requires_approval: false, params: {profit_pct: 0.5}}\n"
        "  - {name: stop-loss, rule_type: STOP_LOSS, enabled: true,\n"
        "     requires_approval: false, params: {loss_mult: 2.0, delta_stop: 0.5}}\n"
        "  - {name: dte-close, rule_type: DTE, enabled: true,\n"
        "     requires_approval: false, params: {dte_threshold: 2, action: close}}\n"
        "  - {name: tv-signal, rule_type: SIGNAL, enabled: true,\n"
        "     requires_approval: true, params: {match: underlying}}\n",
        encoding="utf-8",
    )
    rules = _edit(_dump(settings), "stop-loss", loss_mult=None)
    _edit(rules, "dte-close", dte_threshold=7)
    assert c.post("/api/config", json={"rules": rules}).status_code == 200
    saved = load_overlay()
    assert saved["rules"][1]["params"]["loss_mult"] is None
    assert saved["rules"][1]["params"]["delta_stop"] == 0.5
    assert "mode" not in saved
    reloaded = load_config(base)
    assert reloaded.mode == "paper"
    assert reloaded.rules[1].params["loss_mult"] is None
    assert reloaded.rules[1].params["delta_stop"] == 0.5
    assert reloaded.rules[1].enabled is True
    assert reloaded.rules[2].params == {"dte_threshold": 7, "action": "close"}
    assert reloaded.rules[3].params["match"] == "underlying"
    assert reloaded.rules[3].requires_approval is True


def test_rules_view_describes_off_trigger_and_disabled_rule():
    settings = Settings(rules=[
        RuleConfig(name="stop-loss", rule_type="STOP_LOSS", enabled=True,
                   requires_approval=False, params={"loss_mult": None, "delta_stop": 0.4}),
        RuleConfig(name="profit-target", rule_type="PROFIT_TARGET", enabled=False,
                   requires_approval=False, params={"profit_pct": 0.5}),
    ])
    rows = describe_active_rules(settings)
    stop = next(r for r in rows if r["name"] == "stop-loss")
    assert "off" in stop["detail"]
    assert "0.40" in stop["detail"]
    assert "midpoint" in stop["detail"]
    assert "never a market order at the ask" in stop["detail"]
    profit = next(r for r in rows if r["name"] == "profit-target")
    assert profit["value"] == "off"
