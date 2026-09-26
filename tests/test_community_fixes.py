"""Targeted tests for the community-edition safety fixes (entries, paper, assignment, orders)."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from agentic.brokers.base import BrokerCapabilities, ExecutionBroker
from agentic.brokers.live_guard import LiveOrderBlocked, assert_live_order_allowed
from agentic.brokers.paper_broker import PaperBroker
from agentic.brokers.robinhood_mcp import RobinhoodMCPBroker
from agentic.config import (
    RuleConfig,
    Settings,
    TaxReserveConfig,
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
from agentic.services.executor import OrderExecutor, compute_limit_price, compute_open_limit_price
from agentic.services.killswitch import KillSwitch
from agentic.services.market_hours import is_order_window, parse_hhmm
from agentic.services.stats import assignment_realized_pnl
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.entry_decisions import EntryDecisionStore
from agentic.store.orders import OrderStore
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


@pytest.mark.asyncio
async def test_execute_open_uses_fresh_quote_not_stale_cache(tmp_path):
    broker = PaperBroker(seed_positions=[], buying_power=50_000)
    md = _RHLike()
    ex, _ = _exec(tmp_path, broker, md=md, settings=Settings(mode="paper", broker="paper"))
    d = _decision()
    ex.entry_decisions.insert_if_new(d)
    order = await ex.execute_open(d, md._cached)
    assert md.fresh_calls == 1
    assert order is not None and order.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_live_entry_allowed_on_robinhood_realtime_feed(tmp_path):
    """The old OPRA-only gate must not block RobinhoodMarketData (is_realtime=True)."""
    broker = _LiveBroker()
    settings = Settings(mode="live", i_understand_live_trading=True, broker="robinhood_mcp",
                        market_data="robinhood")
    md = _RHLike()
    ex, _ = _exec(tmp_path, broker, md=md, settings=settings)
    d = _decision()
    ex.entry_decisions.insert_if_new(d)
    reason = ex.open_block_reason(d, await md.get_fresh_contract_quote(OCC))
    # May still block on the 10:00 ET clock if this runs overnight on a weekday;
    # the realtime feed itself must not be the reason.
    assert reason is None or "real-time" not in reason


@pytest.mark.asyncio
async def test_roll_preflight_leaves_position_untouched(tmp_path):
    from agentic.config import RollConfig
    from agentic.services.roll import RollManager

    db = Database(tmp_path / "roll.db")
    audit = AuditStore(db)
    broker = _LiveBroker()
    settings = Settings(mode="paper", broker="robinhood_mcp",
                        roll=RollConfig(enabled=True, roll_dte=3, roll_delta=0.45))
    md = _RHLike()
    ex, _ = _exec(tmp_path, broker, md=md, settings=settings)
    pos = Position(
        occ_symbol="SOFI260715P00018000", underlying="SOFI", option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=1,
        strike=18.0, expiration=date.today() + timedelta(days=2), credit_received=0.36, delta=-0.5,
    )
    quote = OptionQuote(pos.occ_symbol, 0.68, 0.72, 0.70, delta=-0.5)
    target = OptionContractQuote(
        occ_symbol="SOFI" + (date.today() + timedelta(days=10)).strftime("%y%m%d") + "P00017000",
        underlying="SOFI", option_id="new-id", option_type="put", strike=17.0,
        expiration=date.today() + timedelta(days=10), bid=0.77, ask=0.83, mark=0.80, delta=-0.25,
    )

    class _Chain(_RHLike):
        async def get_chain(self, underlying):
            return [target]
        async def get_fresh_contract_quote(self, occ):
            return target

    rm = RollManager(settings, broker=broker, market_data=_Chain(), executor=ex,
                     decisions=DecisionStore(db), entry_decisions=EntryDecisionStore(db),
                     audit=audit)
    assert await rm.try_roll(pos, quote) is False
    assert broker.submitted == []  # never bought back the old leg


# --- C2: paper never places a real order -------------------------------------------------------

def test_live_guard_blocks_paper_mode():
    with pytest.raises(LiveOrderBlocked):
        assert_live_order_allowed(Settings(mode="paper"), kind="option")


def test_live_guard_allows_armed_live():
    assert_live_order_allowed(Settings(mode="live", i_understand_live_trading=True))


def test_live_guard_sgov_test_buy_only_when_flagged():
    off = Settings(mode="paper", tax_reserve=TaxReserveConfig(allow_sgov_test_buy=False))
    with pytest.raises(LiveOrderBlocked):
        assert_live_order_allowed(off, kind="equity", symbol="SGOV", quantity=1, side="buy")
    on = Settings(mode="paper", tax_reserve=TaxReserveConfig(allow_sgov_test_buy=True))
    assert_live_order_allowed(on, kind="equity", symbol="SGOV", quantity=1, side="buy")
    with pytest.raises(LiveOrderBlocked):
        assert_live_order_allowed(on, kind="equity", symbol="SGOV", quantity=2, side="buy")


@pytest.mark.asyncio
async def test_robinhood_broker_refuses_orders_without_live(monkeypatch):
    b = RobinhoodMCPBroker(account_number="1", settings=Settings(mode="paper"))
    b._supports_options = True
    b._roles = {"place_option_order": "place_option_order"}
    order = Order(decision_id="d", position_id="p", occ_symbol=OCC, option_id="oid",
                  quantity=1, limit_price=0.14, is_paper=False, client_order_id="c1")
    with pytest.raises(LiveOrderBlocked):
        await b.submit_open_order(order)
    with pytest.raises(LiveOrderBlocked):
        await b.submit_close_order(order)
    with pytest.raises(LiveOrderBlocked):
        await b.submit_equity_order(symbol="SGOV", dollar_amount=10, price_hint=100.0)


@pytest.mark.asyncio
async def test_tax_reserve_paper_plus_real_broker_does_not_buy(tmp_path):
    from agentic.services.tax_reserve import TaxReserveLoop
    from agentic.store.tax_reserve import TaxReserveStore

    class _J:
        def realized_since(self, since):
            return 500.0, 3

    class _Real(PaperBroker):
        def capabilities(self):
            c = super().capabilities()
            c.is_paper = False
            return c
        async def submit_equity_order(self, **kw):
            raise AssertionError("must not place a real tax-reserve order in paper mode")

    db = Database(tmp_path / "tr.db")
    settings = Settings(mode="paper", tax_reserve=TaxReserveConfig(enabled=True, dry_run=False))
    class _MD:
        async def get_underlying_price(self, symbol):
            return 100.0

    audit = AuditStore(db)
    loop = TaxReserveLoop(settings, _Real(seed_positions=[], buying_power=10_000),
                          _MD(), _J(), TaxReserveStore(db), audit, KillSwitch(db, audit))
    friday = datetime(2026, 9, 11, 15, 41, tzinfo=ET)
    r = await loop.run_once(now=friday)
    assert r["status"] == "dry_run"
    assert "paper mode" in r["why"]


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
    assert "Trading mode" in names
    assert "Real orders" in names
    assert any(
        r["name"] == "Real orders" and r["value"] == "HARD DISABLED in code"
        for r in body["rules"]
    )
    assert "Max cash / collateral per trade" in names
    assert "Trading start" in names
    assert any("paper" in r["value"].lower() for r in body["rules"] if r["name"] == "Trading mode")
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

def test_open_limit_starts_at_mid_and_steps_to_bid():
    q = OptionContractQuote(
        occ_symbol=OCC, underlying="F", option_id="x", option_type="put",
        strike=14.0, expiration=EXP, bid=1.00, ask=1.10, mark=1.05,
    )
    assert compute_open_limit_price(q, buffer_pct=0.02, slippage_cap_pct=0.05) == 1.05
    assert compute_open_limit_price(q, buffer_pct=0.02, slippage_cap_pct=0.05, toward_fill=1.0) == 1.00


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
async def test_real_broker_blocked_before_trading_start(tmp_path, monkeypatch):
    broker = _LiveBroker()
    settings = Settings(mode="live", i_understand_live_trading=True, trading_start="10:00")
    md = _RHLike()
    ex, _ = _exec(tmp_path, broker, md=md, settings=settings)
    monkeypatch.setattr("agentic.services.executor.is_order_window", lambda *a, **k: False)
    d = _decision()
    ex.entry_decisions.insert_if_new(d)
    assert await ex.execute_open(d, await md.get_fresh_contract_quote(OCC)) is None
    assert broker.submitted == []


@pytest.mark.asyncio
async def test_all_exits_blocked_before_trading_start(tmp_path, monkeypatch):
    """Stop-loss, profit-target, and every other close wait for trading_start — paper too."""
    broker = PaperBroker(seed_positions=[], buying_power=50_000)
    settings = Settings(mode="paper", broker="paper", trading_start="10:00")
    md = _RHLike()
    ex, db = _exec(tmp_path, broker, md=md, settings=settings)
    monkeypatch.setattr("agentic.services.executor.is_order_window", lambda *a, **k: False)
    pos = Position(
        occ_symbol=OCC, underlying="F", option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=1,
        strike=14.0, expiration=EXP, credit_received=0.20,
    )
    quote = OptionQuote(OCC, 0.40, 0.50, 0.45)
    for rule_type, name in (
        (RuleType.STOP_LOSS, "stop-loss"),
        (RuleType.PROFIT_TARGET, "profit-50"),
        (RuleType.DTE, "dte-2"),
        (RuleType.SIGNAL, "tv-signal"),
    ):
        dec = CloseDecision(
            position_id="p", rule_name=name, rule_type=rule_type, reason="test",
            requires_approval=False, dedup_key=f"{name}:gate",
        )
        DecisionStore(db).insert_if_new(dec)
        assert await ex.execute_close(pos, dec, quote) is None
    assert broker._orders == {}


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


@pytest.mark.asyncio
async def test_stop_loss_close_limit_starts_at_mid(tmp_path, monkeypatch):
    class _FillClose(_LiveBroker):
        async def submit_close_order(self, order):
            await super().submit_close_order(order)
            order.status = OrderStatus.FILLED
            order.filled_qty = order.quantity
            order.avg_fill_price = order.limit_price
            self._orders[order.client_order_id] = order
            return order

    broker = _FillClose()
    settings = Settings(mode="live", i_understand_live_trading=True)
    md = _RHLike()
    ex, db = _exec(tmp_path, broker, md=md, settings=settings)
    monkeypatch.setattr("agentic.services.executor.is_order_window", lambda *a, **k: True)
    pos = Position(
        occ_symbol=OCC, underlying="F", option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=1,
        strike=14.0, expiration=EXP, credit_received=0.20,
    )
    quote = OptionQuote(OCC, bid=1.00, ask=1.20, mark=1.10)
    dec = CloseDecision(
        position_id="p", rule_name="stop-loss", rule_type=RuleType.STOP_LOSS,
        reason="stop", requires_approval=False, dedup_key="stop:mid",
    )
    DecisionStore(db).insert_if_new(dec)
    order = await ex.execute_close(pos, dec, quote)
    assert order is not None
    assert order.limit_price == 1.10  # mid, not the 1.20 ask
    assert any(s[0] == "close" and s[2] == 1.10 for s in broker.submitted)


@pytest.mark.asyncio
async def test_reprice_does_not_submit_when_cancel_fails(tmp_path, monkeypatch):
    broker = _LiveBroker()
    broker.cancel_ok = False
    settings = Settings(mode="live", i_understand_live_trading=True)
    md = _RHLike()
    ex, _ = _exec(tmp_path, broker, md=md, settings=settings)
    monkeypatch.setattr("agentic.services.executor.is_order_window", lambda *a, **k: True)
    pos = Position(
        occ_symbol=OCC, underlying="F", option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=1,
        strike=14.0, expiration=EXP, credit_received=0.20,
    )
    working = Order(decision_id="d", position_id="p", occ_symbol=OCC, option_id="id-1",
                    quantity=1, limit_price=0.10, is_paper=False, client_order_id="c-old",
                    status=OrderStatus.SUBMITTED, broker_order_id="b-old")
    broker._orders["c-old"] = working
    out = await ex._reprice(working, pos)
    assert out.client_order_id == "c-old"
    assert not any(s[0] == "close" and s[1] == "c-old-r1" for s in broker.submitted)


def test_assignment_pnl_is_premium_minus_intrinsic():
    pos = Position(
        occ_symbol="F260717P00010000", underlying="F", option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=1,
        strike=10.0, expiration=date.today(), credit_received=0.50,
        status=PositionStatus.ASSIGNED,
    )
    assert assignment_realized_pnl(pos, 8.0) == -150.0
    assert assignment_realized_pnl(pos, None) is None
