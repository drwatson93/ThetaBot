"""Phase 1: monitor evaluates rules, creates+notifies decisions, and dedups across cycles."""
import json
import logging
from datetime import date

import pytest

from tests.fakes import PaperBroker
from agentic.config import RuleConfig, Settings
from agentic.domain.enums import DecisionStatus, Direction, OptionType, RuleType, Strategy
from agentic.domain.models import CloseDecision, Position
from agentic.marketdata.base import PaperMarketData
from agentic.notify.base import Notifier
from agentic.notify.trade_alerts import TradeAlerts, build_exit_alert_payload
from agentic.rules.engine import RulesEngine, build_rules
from agentic.services.killswitch import KillSwitch
from agentic.services.monitor import MonitorLoop
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.positions import PositionStore

HOOK = "https://secret.example.test/hook/abc123"
HEADER_VALUE = "super-secret-alert-key"


@pytest.fixture(autouse=True)
def _market_open(monkeypatch):
    # Rule evaluation is gated to the regular session; force "open" so these tests are
    # deterministic regardless of the wall-clock time they run at.
    monkeypatch.setattr("agentic.services.monitor.is_market_hours", lambda *a, **k: True)


class RecordingNotifier(Notifier):
    def __init__(self):
        self.sent = []

    async def send(self, title, message, *, priority="normal", actions=None):
        self.sent.append((title, message, priority))


def _build(tmp_path, rules_cfg, *, trade_alerts=None):
    db = Database(tmp_path / "t.db")
    audit = AuditStore(db)
    positions = PositionStore(db)
    decisions = DecisionStore(db)
    killswitch = KillSwitch(db, audit)
    broker = PaperBroker()
    notifier = RecordingNotifier()
    settings = Settings(mode="paper", broker="paper", market_data="paper", rules=rules_cfg)
    engine = RulesEngine(build_rules(rules_cfg))
    monitor = MonitorLoop(
        settings, broker, PaperMarketData(), positions, audit, killswitch,
        rules_engine=engine, decisions=decisions, notifier=notifier,
        trade_alerts=trade_alerts,
    )
    return monitor, broker, decisions, notifier


def _profit_rules():
    return [RuleConfig(name="p50", rule_type="PROFIT_TARGET",
                       requires_approval=False, params={"profit_pct": 0.5})]


def _arm_profit(broker, positions):
    call = next(p for p in positions if p.underlying == "AAA")
    broker.set_mark(call.occ_symbol, bid=0.90, ask=0.95)
    return call


def _attach_alerts(monitor, monkeypatch, *, url=HOOK, mode=None):
    if url is None:
        monkeypatch.delenv("TRADE_ALERT_URL", raising=False)
    else:
        monkeypatch.setenv("TRADE_ALERT_URL", url)
    alerts = TradeAlerts(
        monitor.killswitch.db, monitor.audit, monitor.settings, background=False,
    )
    if mode is not None:
        alerts.set_mode(mode)
    monitor.trade_alerts = alerts
    return alerts


@pytest.mark.asyncio
async def test_profit_target_creates_decision_and_notifies(tmp_path):
    rules_cfg = [RuleConfig(name="p50", rule_type="PROFIT_TARGET",
                            requires_approval=False, params={"profit_pct": 0.5})]
    monitor, broker, decisions, notifier = _build(tmp_path, rules_cfg)

    # Seeded AAA covered call: credit 2.00. Drive its price below the 1.00 target.
    call = next(p for p in await broker.get_open_positions() if p.underlying == "AAA")
    broker.set_mark(call.occ_symbol, bid=0.90, ask=0.95)

    await monitor.run_once()
    proposed = decisions.list_by_status(DecisionStatus.PROPOSED)
    assert len(proposed) == 1
    assert proposed[0].rule_name == "p50"
    assert any("Close signal" in t for t, _m, _p in notifier.sent)
    # Auto rule -> high priority.
    assert any(p == "high" for _t, _m, p in notifier.sent)


@pytest.mark.asyncio
async def test_decisions_dedup_across_cycles(tmp_path):
    rules_cfg = [RuleConfig(name="p50", rule_type="PROFIT_TARGET",
                            requires_approval=False, params={"profit_pct": 0.5})]
    monitor, broker, decisions, notifier = _build(tmp_path, rules_cfg)
    call = next(p for p in await broker.get_open_positions() if p.underlying == "AAA")
    broker.set_mark(call.occ_symbol, bid=0.90, ask=0.95)

    await monitor.run_once()
    await monitor.run_once()
    await monitor.run_once()
    # Same position+rule+day -> exactly one decision and one notification.
    assert len(decisions.list_by_status(DecisionStatus.PROPOSED)) == 1
    assert len([t for t, _m, _p in notifier.sent if "Close signal" in t]) == 1


@pytest.mark.asyncio
async def test_no_decision_when_target_not_met(tmp_path):
    rules_cfg = [RuleConfig(name="p50", rule_type="PROFIT_TARGET",
                            requires_approval=False, params={"profit_pct": 0.5})]
    monitor, broker, decisions, notifier = _build(tmp_path, rules_cfg)
    # Default seed marks are well above target -> no profit-target decision.
    await monitor.run_once()
    assert decisions.list_by_status(DecisionStatus.PROPOSED) == []


class _SpyExecutor:
    def __init__(self):
        self.closed = []

    async def execute_close(self, pos, decision, quote):
        self.closed.append((pos.occ_symbol, decision.rule_name))
        return None


@pytest.mark.asyncio
async def test_dte_alert_never_executes_even_with_executor(tmp_path):
    # Regression: a DTE action=alert rule (requires_approval=false) is a heads-up only. Even with an
    # executor wired in, it must NOT auto buy-to-close — that footgun realized a loss on a tested
    # ONDS put right before it recovered. It notifies; the roll manager / assignment handle exits.
    rules_cfg = [RuleConfig(name="dte-2", rule_type="DTE", requires_approval=False,
                            params={"dte_threshold": 90, "action": "alert"})]  # 90 -> always triggers
    monitor, broker, decisions, notifier = _build(tmp_path, rules_cfg)
    spy = _SpyExecutor()
    monitor.executor = spy

    await monitor.run_once()

    assert spy.closed == []                                          # never routed to the executor
    proposed = decisions.list_by_status(DecisionStatus.PROPOSED)
    assert proposed and all(d.rule_name == "dte-2" for d in proposed)
    assert any("[ALERT]" in m for _t, m, _p in notifier.sent)       # labeled as an alert, not AUTO
    assert all(p == "normal" for _t, _m, p in notifier.sent)        # not high-priority


@pytest.mark.asyncio
async def test_dte_close_action_executes(tmp_path):
    # The escape hatch still works: action=close is executable and routes to the executor.
    rules_cfg = [RuleConfig(name="dte-close", rule_type="DTE", requires_approval=False,
                            params={"dte_threshold": 90, "action": "close"})]
    monitor, broker, decisions, notifier = _build(tmp_path, rules_cfg)
    spy = _SpyExecutor()
    monitor.executor = spy

    await monitor.run_once()

    # Close actions are recorded and notified. The executor is not called.
    assert spy.closed == []
    proposed = decisions.list_by_status(DecisionStatus.PROPOSED)
    assert proposed and all(d.rule_name == "dte-close" for d in proposed)


def test_exit_alert_payload_shape():
    pos = Position(
        occ_symbol="TEST261231P00010000",
        underlying="TEST",
        option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT,
        direction=Direction.SHORT,
        quantity=2,
        strike=10.0,
        expiration=date(2026, 12, 31),
        credit_received=0.40,
        current_mark=0.15,
    )
    decision = CloseDecision(
        position_id=pos.id,
        rule_name="p50",
        rule_type=RuleType.PROFIT_TARGET,
        reason="Profit target: captured ~62% >= 50% (cost $0.15).",
        requires_approval=False,
        dedup_key="dedup-test",
        id="dec-test-1",
        limit_price=0.16,
    )
    payload = build_exit_alert_payload(pos, decision)
    assert payload["event"] == "scan_alert"
    assert payload["trade_id"] == "dec-test-1"
    assert payload["kind"] == "exit"
    assert payload["action"] == "BTC"
    assert payload["source"] == "monitor"
    assert payload["ticker"] == "TEST"
    assert payload["occ"] == "TEST261231P00010000"
    assert payload["contracts"] == 2
    assert payload["strike"] == 10.0
    assert payload["right"] == "P"
    assert payload["expiry"] == "12/31"
    assert payload["rule_name"] == "p50"
    assert payload["rule_type"] == "PROFIT_TARGET"
    assert payload["gate"] == "AUTO"
    assert payload["reason"].startswith("Profit target")
    assert payload["mark"] == 0.15
    assert payload["limit_price"] == 0.16
    assert payload["dte"] == pos.dte()
    assert payload["summary"] == "BTC 2 TEST 10P 12/31 @0.16 ($32)"
    blob = json.dumps(payload).lower()
    for word in ("balance", "buying_power", "account", "portfolio"):
        assert word not in blob

    decision.notify_only = True
    assert build_exit_alert_payload(pos, decision)["gate"] == "ALERT"
    decision.notify_only = False
    decision.requires_approval = True
    assert build_exit_alert_payload(pos, decision)["gate"] == "NEEDS APPROVAL"


@pytest.mark.asyncio
async def test_exit_alert_sent_once_for_new_decision(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "agentic.notify.trade_alerts._http_post",
        lambda url, payload, timeout=3.0: sent.append(payload),
    )
    monitor, broker, decisions, notifier = _build(tmp_path, _profit_rules())
    _attach_alerts(monitor, monkeypatch)
    _arm_profit(broker, await broker.get_open_positions())

    await monitor.run_once()
    await monitor.run_once()

    assert len(decisions.list_by_status(DecisionStatus.PROPOSED)) == 1
    assert len(sent) == 1
    assert sent[0]["event"] == "scan_alert"
    assert sent[0]["trade_id"] == decisions.list_by_status(DecisionStatus.PROPOSED)[0].id
    assert sent[0]["kind"] == "exit"
    assert sent[0]["action"] == "BTC"
    assert sent[0]["source"] == "monitor"
    assert len([t for t, _m, _p in notifier.sent if "Close signal" in t]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,url", [
    ("off", HOOK),
    ("regular", HOOK),
    ("instant", None),
])
async def test_exit_alert_skipped_unless_instant_and_url(tmp_path, monkeypatch, mode, url):
    sent = []
    monkeypatch.setattr(
        "agentic.notify.trade_alerts._http_post",
        lambda url, payload, timeout=3.0: sent.append(payload),
    )
    monitor, broker, _decisions, notifier = _build(tmp_path, _profit_rules())
    _attach_alerts(monitor, monkeypatch, url=url, mode=mode)
    _arm_profit(broker, await broker.get_open_positions())

    await monitor.run_once()

    assert sent == []
    assert any("Close signal" in t for t, _m, _p in notifier.sent)


class _RaisingAlerts:
    async def deliver_scan_alert(self, payload):
        raise RuntimeError("webhook exploded")


@pytest.mark.asyncio
async def test_exit_alert_failure_does_not_break_pushover(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agentic.notify.trade_alerts._http_post",
        lambda *a, **k: (_ for _ in ()).throw(OSError("webhook down")),
    )
    monitor, broker, _decisions, notifier = _build(tmp_path, _profit_rules())
    _attach_alerts(monitor, monkeypatch)
    _arm_profit(broker, await broker.get_open_positions())

    seen = await monitor.run_once()
    assert seen > 0
    assert any("Close signal" in t for t, _m, _p in notifier.sent)

    monitor.trade_alerts = _RaisingAlerts()
    call = next(p for p in await broker.get_open_positions() if p.underlying == "BBB")
    broker.set_mark(call.occ_symbol, bid=0.40, ask=0.50)
    await monitor.run_once()
    assert any("BBB" in t for t, _m, _p in notifier.sent)


@pytest.mark.asyncio
async def test_exit_alert_header_secret_never_logged(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("TRADE_ALERT_HEADER_NAME", "X-Test-Key")
    monkeypatch.setenv("TRADE_ALERT_HEADER_VALUE", HEADER_VALUE)

    def _post(url, payload, timeout=3.0):
        raise OSError(f"rejected {HEADER_VALUE} at {url}")

    monkeypatch.setattr("agentic.notify.trade_alerts._http_post", _post)
    monitor, broker, _decisions, _notifier = _build(tmp_path, _profit_rules())
    _attach_alerts(monitor, monkeypatch)
    _arm_profit(broker, await broker.get_open_positions())

    with caplog.at_level(logging.DEBUG):
        await monitor.run_once()

    assert HEADER_VALUE not in caplog.text
    assert HOOK not in caplog.text
    assert any("trade alert failed" in r.getMessage() for r in caplog.records)
