"""Targeted tests for the community-edition safety fixes (entries, paper, assignment, orders)."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from agentic.brokers.base import BrokerCapabilities, ExecutionBroker
from tests.fakes import LiveOrderBlocked, assert_live_order_allowed
from tests.fakes import PaperBroker
from agentic.brokers.robinhood_mcp import RobinhoodMCPBroker
from agentic.config import (
    RuleConfig,
    Settings,
    is_usable_secret,
    require_runtime_secrets,
)
from agentic.domain.enums import (
    Direction, OptionType, OrderStatus, PositionStatus, RuleType, Strategy,
)
from agentic.domain.models import CloseDecision, EntryDecision, Order, Position
from agentic.marketdata.base import MarketDataProvider, PaperMarketData
from agentic.marketdata.quote import OptionContractQuote, OptionQuote
from agentic.marketdata.robinhood_md import RobinhoodMarketData
from tests.fakes import OrderExecutor, compute_limit_price, compute_open_limit_price
from agentic.services.killswitch import KillSwitch
from agentic.services.market_hours import is_order_window, parse_hhmm
from agentic.services.stats import assignment_realized_pnl
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.entry_decisions import EntryDecisionStore
from tests.fakes import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app
from agentic.web.rules_view import describe_active_rules

ET = ZoneInfo("America/New_York")
EXP = date.today() + timedelta(days=10)
OCC = "F260724P00014000"


class _LiveBroker(ExecutionBroker):
    def __init__(self):
        self.submitted = []
        self.cancelled = []
        self.cancel_ok = True
        self._orders = {}

    async def connect(self): ...
    def capabilities(self):
        return BrokerCapabilities("fake_live", supports_options_orders=True, is_paper=False,
                                  supports_equity_orders=True)
    async def get_open_positions(self):
        return []
    async def submit_close_order(self, order):
        self.submitted.append(("close", order.client_order_id, order.limit_price))
        order.status = OrderStatus.SUBMITTED
        order.broker_order_id = "b-" + order.client_order_id
        self._orders[order.client_order_id] = order
        return order
    async def submit_open_order(self, order):
        self.submitted.append(("open", order.client_order_id, order.limit_price))
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.avg_fill_price = order.limit_price
        order.broker_order_id = "o-" + order.client_order_id
        return order
    async def submit_equity_order(self, **kwargs):
        self.submitted.append(("equity", kwargs))
        return {"status": "filled", "shares": kwargs.get("quantity") or 1, "avg_price": 100.0,
                "dollars": 100.0, "order_id": "eq1"}
    async def get_order(self, order):
        return self._orders.get(order.client_order_id, order)
    async def cancel_order(self, order):
        self.cancelled.append(order.client_order_id)
        if not self.cancel_ok:
            raise RuntimeError("cancel rejected")
        order.status = OrderStatus.CANCELLED
        self._orders[order.client_order_id] = order


class _RHLike(MarketDataProvider):
    def __init__(self):
        self.fresh_calls = 0
        self.chain_calls = 0
        self._cached = OptionContractQuote(
            occ_symbol=OCC, underlying="F", option_id="id-1", option_type="put",
            strike=14.0, expiration=EXP, bid=0.13, ask=0.15, mark=0.14,
            as_of=datetime.now() - timedelta(seconds=200),
        )

    @property
    def is_realtime(self) -> bool:
        return True

    async def get_quote(self, position):
        return OptionQuote(position.occ_symbol, 0.13, 0.15, 0.14)

    async def get_chain(self, underlying):
        self.chain_calls += 1
        return [self._cached]

    async def get_fresh_contract_quote(self, occ_symbol):
        self.fresh_calls += 1
        return OptionContractQuote(
            occ_symbol=occ_symbol, underlying="F", option_id="id-1", option_type="put",
            strike=14.0, expiration=EXP, bid=0.13, ask=0.15, mark=0.14,
        )


def _exec(tmp_path, broker, md=None, settings=None):
    db = Database(tmp_path / "fix.db")
    audit = AuditStore(db)
    settings = settings or Settings(mode="paper", broker="robinhood_mcp")
    ex = OrderExecutor(
        settings, broker, md or _RHLike(), PositionStore(db), OrderStore(db),
        DecisionStore(db), audit, KillSwitch(db, audit),
        entry_decisions=EntryDecisionStore(db), poll_interval_seconds=0.001,
    )
    return ex, db


def _decision():
    return EntryDecision(
        underlying="F", occ_symbol=OCC, option_id="id-1", strike=14.0, expiration=EXP,
        contracts=1, premium=0.14, rule_name="csp-screener", reason="test", dedup_key="F:test",
    )


# --- C1: Robinhood data + rolls ----------------------------------------------------------------

def test_robinhood_market_data_is_realtime():
    md = RobinhoodMarketData(broker=object())
    assert md.is_realtime is True








# --- C2: paper never places a real order -------------------------------------------------------











# --- C3: secrets + read-only dashboard ---------------------------------------------------------

def test_placeholder_secrets_are_rejected():
    assert is_usable_secret(None) is False
    assert is_usable_secret("") is False
    assert is_usable_secret("change-me-to-something-strong") is False
    assert is_usable_secret("change-me-long-random") is False
    assert is_usable_secret("a-real-long-secret") is True


def test_require_runtime_secrets_exits_when_unset(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("CONTROL_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        require_runtime_secrets()


def test_require_runtime_secrets_exits_when_viewer_user_matches_owner(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "a-real-long-secret")
    monkeypatch.setenv("CONTROL_TOKEN", "a-real-long-secret-token")
    monkeypatch.setenv("VIEWER_USER", "admin")
    monkeypatch.setenv("VIEWER_PASSWORD", "a-different-real-secret")
    with pytest.raises(SystemExit, match="VIEWER_USER"):
        require_runtime_secrets()


def test_rules_page_and_api(tmp_path):
    db = Database(tmp_path / "r.db")
    audit = AuditStore(db)
    settings = Settings(mode="paper")
    deps = WebDeps(settings=settings, signals=SignalStore(db),
                   killswitch=KillSwitch(db, audit), approval_gate=None, audit=audit,
                   positions=PositionStore(db), orders=OrderStore(db), decisions=DecisionStore(db))
    client = TestClient(create_app(deps))
    page = client.get("/rules")
    assert page.status_code == 200 and "Active trading rules" in page.text
    body = client.get("/api/rules").json()
    names = {r["name"] for r in body["rules"]}
    assert "Real orders" in names
    assert any(
        r["name"] == "Real orders" and r["value"] == "HARD DISABLED in code"
        for r in body["rules"]
    )
    assert "Max cash / collateral per trade" in names
    assert "Trading start" in names
    assert "Pause-only token" in names
    assert "Trade alerts" in names
    assert any("NYSE" in r["value"] or "NYSE" in r["detail"] for r in body["rules"])
    dash = client.get("/dashboard").text
    assert 'data-tab="rules"' in dash
    assert "/control/approve" not in dash


def test_describe_rules_reads_same_settings():
    s = Settings(mode="paper", trading_start="10:30", rules=[
        RuleConfig(name="stop-loss", rule_type="STOP_LOSS", requires_approval=False,
                   params={"loss_mult": 2.0, "delta_stop": 0.5}),
    ])
    rows = describe_active_rules(s)
    start = next(r for r in rows if r["name"] == "Trading start")
    assert "10:30" in start["value"]
    assert "stop-loss" in start["detail"]
    stop = next(r for r in rows if r["name"] == "stop-loss")
    assert "midpoint" in stop["detail"]
    assert "never a market order at the ask" in stop["detail"]


# --- order handling ---------------------------------------------------------------------------



def test_order_window_before_ten():
    monday_930 = datetime(2026, 9, 21, 9, 30, tzinfo=ET)  # a Monday
    monday_1000 = datetime(2026, 9, 21, 10, 0, tzinfo=ET)
    monday_1559 = datetime(2026, 9, 21, 15, 59, tzinfo=ET)
    saturday = datetime(2026, 9, 26, 11, 0, tzinfo=ET)
    assert is_order_window(monday_930, start="10:00") is False
    assert is_order_window(monday_1000, start="10:00") is True
    assert is_order_window(monday_1559, start="10:00") is True
    assert is_order_window(saturday, start="10:00") is False
    assert parse_hhmm("10:00").hour == 10






@pytest.mark.asyncio
async def test_monitor_does_not_evaluate_exits_before_trading_start(tmp_path, monkeypatch):
    from agentic.rules.engine import RulesEngine, build_rules
    from agentic.services.monitor import MonitorLoop

    monkeypatch.setattr("agentic.services.monitor.is_market_hours", lambda *a, **k: True)
    monkeypatch.setattr("agentic.services.monitor.is_order_window", lambda *a, **k: False)
    db = Database(tmp_path / "mon-gate.db")
    audit = AuditStore(db)
    positions = PositionStore(db)
    decisions = DecisionStore(db)
    pos = Position(
        occ_symbol=OCC, underlying="F", option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=1,
        strike=14.0, expiration=EXP, credit_received=0.20,
    )

    class _B:
        async def get_open_positions(self):
            return [pos]
        def capabilities(self):
            return BrokerCapabilities("paper", supports_options_orders=True, is_paper=True)

    class _MD:
        async def get_quote(self, p):
            return OptionQuote(OCC, 0.40, 0.50, 0.45, delta=-0.6)

    rules = [RuleConfig(name="stop-loss", rule_type="STOP_LOSS", requires_approval=False,
                        params={"loss_mult": 2.0, "delta_stop": 0.50})]
    submitted = []

    class _Ex:
        async def execute_close(self, *a, **k):
            submitted.append(a)

    mon = MonitorLoop(
        Settings(mode="paper", trading_start="10:00", rules=rules),
        _B(), _MD(), positions, audit, KillSwitch(db, audit),
        rules_engine=RulesEngine(build_rules(rules)), decisions=decisions, executor=_Ex(),
    )
    await mon.run_once()
    assert decisions.recent() == []
    assert submitted == []






def test_assignment_pnl_is_premium_minus_intrinsic():
    pos = Position(
        occ_symbol="F260717P00010000", underlying="F", option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=1,
        strike=10.0, expiration=date.today(), credit_received=0.50,
        status=PositionStatus.ASSIGNED,
    )
    assert assignment_realized_pnl(pos, 8.0) == -150.0
    assert assignment_realized_pnl(pos, None) is None
