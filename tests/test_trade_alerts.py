"""Instant trade alerts: payload format, send gates, persistence, and owner-only auth."""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agentic.config import Settings
from agentic.domain.enums import (
    AuditEventType, Direction, OptionType, OrderStatus, Strategy,
)
from agentic.domain.models import EntryDecision, Order, Position
from agentic.notify import trade_alerts as ta_mod
from agentic.notify.trade_alerts import (
    TradeAlerts, build_close_payload, build_open_payload, format_close_line,
    format_open_line, sanitize_error,
)
from agentic.services.executor import OrderExecutor
from agentic.services.killswitch import KillSwitch
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.entry_decisions import EntryDecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app

CONTROL = "test-control-token-ok-long"
PAUSE = "test-pause-token-ok-long"
HOOK = "https://secret.example.test/hook/abc123"


def _alerts(tmp_path, monkeypatch, *, url=None, background=False):
    if url is None:
        monkeypatch.delenv("TRADE_ALERT_URL", raising=False)
    else:
        monkeypatch.setenv("TRADE_ALERT_URL", url)
    db = Database(tmp_path / "alerts.db")
    audit = AuditStore(db)
    settings = Settings(mode="paper", broker="paper", market_data="paper")
    return TradeAlerts(db, audit, settings, background=background), db, audit, settings


def _open_decision_order():
    d = EntryDecision(
        underlying="HIMS", occ_symbol="HIMS261009P00027000", option_id=None,
        strike=27, expiration=date(2026, 10, 9), contracts=2, premium=0.23,
        rule_name="csp", reason="t", dedup_key="k",
    )
    o = Order(
        decision_id=d.id, position_id="", occ_symbol=d.occ_symbol, quantity=2,
        limit_price=0.23, is_paper=True, side="SELL_TO_OPEN", filled_qty=2,
        bid=0.22, ask=0.24, order_type="LIMIT",
    )
    return d, o


def _close_position_order():
    pos = Position(
        occ_symbol="HIMS261009P00026000", underlying="HIMS", option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=3,
        strike=26, expiration=date(2026, 10, 9), credit_received=0.32,
    )
    o = Order(
        decision_id="d", position_id=pos.id, occ_symbol=pos.occ_symbol, quantity=3,
        limit_price=0.13, is_paper=True, side="BUY_TO_CLOSE", filled_qty=3,
        avg_fill_price=0.13, bid=0.12, ask=0.14, order_type="LIMIT",
        status=OrderStatus.FILLED,
    )
    return pos, o


def test_open_line_format():
    line = format_open_line(
        action="STO", contracts=2, ticker="HIMS", strike=27, right="P",
        expiry="10/09", limit_price=0.23, premium=46,
    )
    assert line == "STO 2 HIMS 27P 10/09 @0.23 ($46)"


def test_close_line_format():
    line = format_close_line(
        action="BTC", contracts=3, ticker="HIMS", strike=26, right="P",
        expiry="10/09", limit_price=0.13, realized_pnl=57,
    )
    assert line == "BTC 3 HIMS 26P 10/09 @0.13 (+$57)"
    loss = format_close_line(
        action="BTC", contracts=1, ticker="HIMS", strike=26, right="P",
        expiry="10/09", limit_price=0.40, realized_pnl=-12,
    )
    assert loss.endswith("(-$12)")


def test_open_payload_matches_recorded_prices():
    d, o = _open_decision_order()
    p = build_open_payload(d, o, mode="paper")
    assert p["event"] == "trade_open"
    assert p["trade_id"] == o.id
    assert p["summary"] == "STO 2 HIMS 27P 10/09 @0.23 ($46)"
    assert p["ticker"] == "HIMS"
    assert p["action"] == "STO"
    assert p["contracts"] == 2
    assert p["strike"] == 27
    assert p["right"] == "P"
    assert p["expiry"] == "10/09"
    assert p["limit_price"] == 0.23
    assert p["premium"] == 46.0
    assert p["realized_pnl"] is None
    assert p["order_type"] == "LIMIT"
    assert p["bid"] == 0.22 and p["ask"] == 0.24
    assert p["mode"] == "paper"
    assert p["filled_at"]


def test_close_payload_matches_recorded_pnl():
    pos, o = _close_position_order()
    p = build_close_payload(pos, o, mode="paper")
    assert p["event"] == "trade_close"
    assert p["trade_id"] == o.id
    assert p["summary"] == "BTC 3 HIMS 26P 10/09 @0.13 (+$57)"
    assert p["action"] == "BTC"
    assert p["premium"] is None
    assert p["realized_pnl"] == 57.0
    assert p["limit_price"] == 0.13
    assert p["right"] == "P"


def test_stc_bto_actions():
    d, o = _open_decision_order()
    o.side = "BUY_TO_OPEN"
    assert build_open_payload(d, o, mode="paper")["action"] == "BTO"
    pos, c = _close_position_order()
    c.side = "SELL_TO_CLOSE"
    assert build_close_payload(pos, c, mode="paper")["action"] == "STC"


def test_noop_when_url_unset(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(ta_mod, "_http_post", lambda *a, **k: sent.append(a))
    alerts, *_ = _alerts(tmp_path, monkeypatch, url=None)
    alerts.notify_fill(build_open_payload(*_open_decision_order(), mode="paper"))
    assert sent == []
    assert alerts.status()["webhook_configured"] is False
    assert alerts.status()["last_status"] is None


@pytest.mark.parametrize("mode", ["regular", "off"])
def test_no_send_when_mode_not_instant(tmp_path, monkeypatch, mode):
    sent = []
    monkeypatch.setattr(ta_mod, "_http_post", lambda *a, **k: sent.append(a))
    alerts, *_ = _alerts(tmp_path, monkeypatch, url=HOOK)
    alerts.set_mode(mode)
    alerts.notify_fill(build_open_payload(*_open_decision_order(), mode="paper"))
    assert sent == []
    assert alerts.status()["mode"] == mode
    assert alerts.status()["webhook_configured"] is True


def test_send_when_instant_and_url_set(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(ta_mod, "_http_post", lambda url, payload, timeout=3.0: sent.append(payload))
    alerts, *_ = _alerts(tmp_path, monkeypatch, url=HOOK, background=False)
    payload = build_open_payload(*_open_decision_order(), mode="paper")
    alerts.notify_fill(payload)
    assert len(sent) == 1
    assert sent[0]["event"] == "trade_open"
    assert sent[0]["summary"].startswith("STO ")
    st = alerts.status()
    assert st["last_status"] == "ok"
    assert st["last_error"] is None
    assert st["last_sent_at"]


def test_failures_do_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(ta_mod, "_http_post", lambda *a, **k: (_ for _ in ()).throw(
        OSError(f"connection to {HOOK} failed")
    ))
    alerts, *_ = _alerts(tmp_path, monkeypatch, url=HOOK, background=False)
    alerts.notify_fill(build_open_payload(*_open_decision_order(), mode="paper"))
    st = alerts.status()
    assert st["last_status"] == "error"
    assert st["last_error"]
    assert HOOK not in (st["last_error"] or "")
    assert "secret.example" not in (st["last_error"] or "")


def test_sanitize_error_strips_url():
    err = sanitize_error(OSError(f"POST {HOOK} timed out"), HOOK)
    assert HOOK not in err
    assert "secret.example" not in err
    assert "[redacted]" in err


def test_mode_persists_across_restart(tmp_path, monkeypatch):
    alerts, db, audit, settings = _alerts(tmp_path, monkeypatch)
    assert alerts.mode() == "instant"
    alerts.set_mode("off")
    row = audit.latest(AuditEventType.ALERTS)
    assert row is not None
    assert row["payload"]["mode"] == "off"
    assert row["payload"]["previous"] == "instant"
    revived = TradeAlerts(db, audit, settings)
    assert revived.mode() == "off"
    assert revived.status()["mode"] == "off"


def _client(tmp_path, monkeypatch, *, pause=None):
    if pause:
        monkeypatch.setenv("PAUSE_TOKEN", pause)
    db = Database(tmp_path / "web-al.db")
    audit = AuditStore(db)
    settings = Settings(mode="paper", broker="paper", market_data="paper")
    alerts = TradeAlerts(db, audit, settings, background=False)
    deps = WebDeps(
        settings=settings, signals=SignalStore(db),
        killswitch=KillSwitch(db, audit), approval_gate=None, audit=audit,
        positions=PositionStore(db), orders=OrderStore(db),
        decisions=DecisionStore(db), entry_decisions=EntryDecisionStore(db),
        trade_alerts=alerts,
    )
    return TestClient(create_app(deps)), alerts, audit


def test_alerts_status_on_ops_and_control(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_ALERT_URL", HOOK)
    client, alerts, _ = _client(tmp_path, monkeypatch)
    for path in ("/api/ops", "/control/status"):
        body = client.get(path).json()
        assert body["alerts"]["mode"] == "instant"
        assert body["alerts"]["webhook_configured"] is True
        assert set(body["alerts"]) == {
            "mode", "webhook_configured", "last_sent_at", "last_status", "last_error",
        }
        blob = json.dumps(body)
        assert HOOK not in blob
        assert "secret.example" not in blob
    ops = client.get("/api/ops").json()
    assert "last_error" in ops  # engine field is separate from alerts.last_error


def test_alerts_mode_requires_control_token(tmp_path, monkeypatch):
    client, alerts, audit = _client(tmp_path, monkeypatch, pause=PAUSE)
    assert client.post("/control/alerts-mode?mode=off").status_code == 401
    assert client.post(f"/control/alerts-mode?token={PAUSE}&mode=off").status_code == 401
    assert client.post(
        f"/control/alerts-mode?token={PAUSE}&mode=off", auth=None
    ).status_code == 401
    assert alerts.mode() == "instant"

    ok = client.post(f"/control/alerts-mode?token={CONTROL}&mode=regular")
    assert ok.status_code == 200
    assert ok.json()["ok"] is True
    assert ok.json()["alerts"]["mode"] == "regular"
    assert alerts.mode() == "regular"
    row = audit.latest(AuditEventType.ALERTS, source="control")
    assert row["payload"]["mode"] == "regular"

    bad = client.post(f"/control/alerts-mode?token={CONTROL}&mode=loud")
    assert bad.status_code == 400
    assert alerts.mode() == "regular"


def test_test_alert_requires_control_token(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(ta_mod, "_http_post", lambda url, payload, timeout=3.0: sent.append(payload))
    monkeypatch.setenv("TRADE_ALERT_URL", HOOK)
    client, alerts, _ = _client(tmp_path, monkeypatch, pause=PAUSE)
    assert client.post("/control/test-alert").status_code == 401
    assert client.post(f"/control/test-alert?token={PAUSE}").status_code == 401
    r = client.post(f"/control/test-alert?token={CONTROL}")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert sent and sent[0]["event"] == "test"
    assert sent[0]["summary"].startswith("TEST ")
    assert HOOK not in json.dumps(r.json())


def test_test_alert_reports_unconfigured(tmp_path, monkeypatch):
    client, _, _ = _client(tmp_path, monkeypatch)
    r = client.post(f"/control/test-alert?token={CONTROL}")
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["error"] == "webhook not configured"


def test_dashboard_has_alerts_control(tmp_path, monkeypatch):
    client, _, _ = _client(tmp_path, monkeypatch)
    html = client.get("/").text
    assert 'id="alerts-card"' in html
    assert "Trade alerts" in html
    assert 'data-mode="instant"' in html
    assert "/control/alerts-mode" in html
    assert HOOK not in html


@pytest.mark.asyncio
async def test_fill_hook_failure_does_not_break_trading(tmp_path, monkeypatch):
    from agentic.brokers.paper_broker import PaperBroker
    from agentic.marketdata.base import PaperMarketData
    from agentic.domain.enums import DecisionStatus, PositionStatus, RuleType
    from agentic.domain.models import CloseDecision
    from agentic.brokers import paper_broker as pb

    monkeypatch.setenv("TRADE_ALERT_URL", HOOK)
    monkeypatch.setattr(ta_mod, "_http_post", lambda *a, **k: (_ for _ in ()).throw(
        OSError(f"boom {HOOK}")
    ))
    db = Database(tmp_path / "exec-al.db")
    audit = AuditStore(db)
    positions = PositionStore(db)
    decisions = DecisionStore(db)
    orders = OrderStore(db)
    killswitch = KillSwitch(db, audit)
    settings = Settings(mode="paper", broker="paper", market_data="paper")
    alerts = TradeAlerts(db, audit, settings, background=False)
    pos = pb._default_seed()[0]
    positions.upsert(pos)
    pos = positions.get_by_occ(pos.occ_symbol)
    decision = CloseDecision(
        position_id=pos.id, rule_name="profit-50", rule_type=RuleType.PROFIT_TARGET,
        reason="test", requires_approval=False, dedup_key=f"{pos.id}:PT:today",
    )
    decisions.insert_if_new(decision)
    ex = OrderExecutor(
        settings, PaperBroker(), PaperMarketData(), positions, orders, decisions,
        audit, killswitch, trade_alerts=alerts, poll_interval_seconds=0.001,
    )
    order = await ex.execute_close(pos, decision)
    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert positions.get_by_occ(pos.occ_symbol).status == PositionStatus.CLOSED
    assert decisions.get(decision.id).status == DecisionStatus.DONE
    assert any(e["event_type"] == "ORDER_FILL" for e in audit.recent())
    assert not any(
        e["event_type"] == "ERROR" and "alert" in json.dumps(e["payload"]).lower()
        for e in audit.recent()
    )
    st = alerts.status()
    assert st["last_status"] == "error"
    assert HOOK not in (st["last_error"] or "")


def test_ops_last_error_untouched_by_alert_failure(tmp_path, monkeypatch):
    """Watcher bots alarm on engine last_error; alert delivery must use alerts.last_error."""
    monkeypatch.setenv("TRADE_ALERT_URL", HOOK)
    db = Database(tmp_path / "ops-al.db")
    audit = AuditStore(db)
    settings = Settings(mode="paper")
    alerts = TradeAlerts(db, audit, settings, background=False)
    monkeypatch.setattr(ta_mod, "_http_post", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    alerts.notify_fill(build_open_payload(*_open_decision_order(), mode="paper"))
    deps = WebDeps(
        settings=settings, signals=SignalStore(db),
        killswitch=KillSwitch(db, audit), approval_gate=None, audit=audit,
        positions=PositionStore(db), orders=OrderStore(db), decisions=DecisionStore(db),
        trade_alerts=alerts, scanner=SimpleNamespace(last_scan_at=None, last_error=None, last_skips=[]),
    )
    ops = TestClient(create_app(deps)).get("/api/ops").json()
    assert ops["last_error"] is None
    assert ops["alerts"]["last_status"] == "error"
    assert ops["alerts"]["last_error"]
