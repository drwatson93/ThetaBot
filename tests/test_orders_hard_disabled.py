"""Hard-coded Robinhood write lock: no MCP write tool may hit the network."""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from agentic.brokers.live_guard import LiveOrderBlocked
from agentic.brokers.paper_broker import PaperBroker
from agentic.brokers.robinhood_mcp import (
    ORDERS_HARD_DISABLED,
    READ_ONLY_MCP_TOOLS,
    REAL_ORDERS_LOCK_LABEL,
    RobinhoodMCPBroker,
    RobinhoodWriteBlocked,
    assert_mcp_tool_allowed,
)
from agentic.config import Settings
from agentic.domain.enums import OrderStatus
from agentic.domain.models import EntryDecision, Order
from agentic.main import build_market_data
from agentic.marketdata.quote import OptionContractQuote
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
from agentic.web.rules_view import describe_active_rules


WRITE_TOOLS = (
    "place_option_order",
    "place_equity_order",
    "place_crypto_order",
    "review_option_order",
    "review_equity_order",
    "preview_crypto_order",
    "cancel_option_order",
    "cancel_equity_order",
    "cancel_crypto_order",
    "exercise_option",
    "cancel_option_exercise",
    "add_to_watchlist",
    "create_alert",
    "create_scan",
    "run_scan",
    "brand_new_unknown_write_tool",
)


def _live_settings() -> Settings:
    return Settings(
        mode="live",
        i_understand_live_trading=True,
        broker="robinhood_mcp",
        broker_fallback="paper",
        market_data="robinhood",
        robinhood={"account_number": "LIVE-ACCT"},
        tax_reserve={"enabled": True, "dry_run": False, "allow_sgov_test_buy": True},
        entry={"enabled": True, "watchlist": ["MSFT"]},
    )


def _live_broker() -> RobinhoodMCPBroker:
    b = RobinhoodMCPBroker(account_number="LIVE-ACCT", settings=_live_settings())
    b._connected = True
    b._supports_options = True
    b._tools = list(WRITE_TOOLS) + [
        "get_option_chains", "get_option_quotes", "get_accounts", "get_portfolio",
    ]
    b._roles = {
        "place_option_order": "place_option_order",
        "review_option_order": "review_option_order",
        "cancel_option_order": "cancel_option_order",
        "place_equity_order": "place_equity_order",
        "get_option_order": "get_option_orders",
        "option_chain": "get_option_chains",
        "list_positions": "get_option_positions",
    }
    return b


def _order() -> Order:
    return Order(
        decision_id="d", position_id="p", occ_symbol="F260724P00014000",
        option_id="oid", quantity=1, limit_price=0.14, is_paper=False,
        client_order_id="live-must-not-place", broker_order_id="oid-1",
    )


def test_constant_is_true_and_not_env_driven(monkeypatch):
    monkeypatch.setenv("ORDERS_HARD_DISABLED", "false")
    monkeypatch.setenv("i_understand_live_trading", "true")
    monkeypatch.setenv("MODE", "live")
    assert ORDERS_HARD_DISABLED is True
    assert REAL_ORDERS_LOCK_LABEL == "Real orders: HARD DISABLED in code"
    for name in WRITE_TOOLS:
        with pytest.raises(RobinhoodWriteBlocked):
            assert_mcp_tool_allowed(name)
    assert_mcp_tool_allowed("get_option_chains")
    assert_mcp_tool_allowed("get_accounts")


@pytest.mark.asyncio
async def test_live_armed_write_tools_never_open_a_session(monkeypatch):
    """mode=live + i_understand_live_trading + account_number: still no write network call."""
    sessions = {"n": 0}

    @asynccontextmanager
    async def forbidden_session():
        sessions["n"] += 1
        raise AssertionError("Robinhood MCP session must not open for a write tool")
        yield  # pragma: no cover

    b = _live_broker()
    monkeypatch.setattr(b, "_session", forbidden_session)

    for name in WRITE_TOOLS:
        with pytest.raises(RobinhoodWriteBlocked):
            await b._call_tool(name, {"account_number": "LIVE-ACCT"})
    assert sessions["n"] == 0


@pytest.mark.asyncio
async def test_live_armed_high_level_paths_blocked(monkeypatch):
    sessions = {"n": 0}

    @asynccontextmanager
    async def forbidden_session():
        sessions["n"] += 1
        raise AssertionError("must not open MCP session")
        yield  # pragma: no cover

    b = _live_broker()
    monkeypatch.setattr(b, "_session", forbidden_session)
    order = _order()

    with pytest.raises(RobinhoodWriteBlocked):
        await b.submit_open_order(order)
    with pytest.raises(RobinhoodWriteBlocked):
        await b.submit_close_order(order)
    with pytest.raises(RobinhoodWriteBlocked):
        await b.review_open_order(order)
    with pytest.raises(RobinhoodWriteBlocked):
        await b.review_close_order(order)
    with pytest.raises(RobinhoodWriteBlocked):
        await b.cancel_order(order)
    with pytest.raises(RobinhoodWriteBlocked):
        await b.submit_equity_order(
            symbol="SGOV", side="buy", quantity=1, price_hint=100.0,
        )
    assert sessions["n"] == 0


@pytest.mark.asyncio
async def test_unknown_tool_denied_even_when_live():
    b = _live_broker()
    with pytest.raises(RobinhoodWriteBlocked, match="not_a_real_tool"):
        await b._call_tool("not_a_real_tool", {})
    assert "not_a_real_tool" not in READ_ONLY_MCP_TOOLS


@pytest.mark.asyncio
async def test_read_only_tool_reaches_session(monkeypatch):
    called = []

    class _Sess:
        async def call_tool(self, name, arguments=None):
            called.append(name)
            return type("R", (), {
                "isError": False, "structuredContent": {"ok": True}, "content": [],
            })()

    @asynccontextmanager
    async def fake_session():
        yield _Sess()

    b = _live_broker()
    monkeypatch.setattr(b, "_session", fake_session)
    out = await b._call_tool("get_option_chains", {"underlying_symbol": "MSFT"})
    assert out == {"ok": True}
    assert called == ["get_option_chains"]


@pytest.mark.asyncio
async def test_paper_broker_fills_still_work(tmp_path):
    from datetime import timedelta
    from agentic.domain.models import utcnow

    class _Quote:
        is_realtime = True

        async def get_fresh_contract_quote(self, occ):
            exp = utcnow().date() + timedelta(days=10)
            return OptionContractQuote(
                occ_symbol=occ, underlying="F", option_id="id-1", option_type="put",
                strike=14.0, expiration=exp, bid=0.13, ask=0.15, mark=0.14,
            )

        async def get_quote(self, position):
            return None

        async def get_chain(self, underlying):
            return []

    db = Database(tmp_path / "p.db")
    audit = AuditStore(db)
    settings = Settings(mode="paper", broker="paper", market_data="robinhood")
    broker = PaperBroker(seed_positions=[], buying_power=25_000)
    md = _Quote()
    entries = EntryDecisionStore(db)
    ex = OrderExecutor(
        settings, broker, md, PositionStore(db), OrderStore(db), DecisionStore(db),
        audit, KillSwitch(db, audit), entry_decisions=entries, poll_interval_seconds=0.001,
    )
    exp = utcnow().date() + timedelta(days=10)
    occ = "F" + exp.strftime("%y%m%d") + "P00014000"
    d = EntryDecision(
        underlying="F", occ_symbol=occ, option_id="id-1", strike=14.0, expiration=exp,
        contracts=1, premium=0.14, rule_name="csp-screener", reason="test", dedup_key="F:t",
    )
    entries.insert_if_new(d)
    quote = await md.get_fresh_contract_quote(occ)
    order = await ex.execute_open(d, quote)
    assert order is not None and order.status == OrderStatus.FILLED
    assert order.is_paper is True
    opened = await broker.get_open_positions()
    assert any(p.occ_symbol == occ for p in opened)


def test_health_and_rules_surface_the_lock(tmp_path):
    db = Database(tmp_path / "h.db")
    audit = AuditStore(db)
    settings = Settings(mode="live", i_understand_live_trading=True)
    deps = WebDeps(
        settings=settings, signals=SignalStore(db),
        killswitch=KillSwitch(db, audit), approval_gate=None, audit=audit,
        positions=PositionStore(db), orders=OrderStore(db), decisions=DecisionStore(db),
    )
    body = TestClient(create_app(deps)).get("/health").json()
    assert body["real_orders"] == "HARD DISABLED in code"
    assert body["real_orders_lock"] == "Real orders: HARD DISABLED in code"
    rows = describe_active_rules(settings)
    assert any(
        f"{r['name']}: {r['value']}" == "Real orders: HARD DISABLED in code" for r in rows
    )


def test_paper_runtime_still_uses_paper_execution():
    assert ORDERS_HARD_DISABLED is True
    settings = Settings(mode="paper")
    md = build_market_data(settings, PaperBroker(seed_positions=[]))
    assert md is not None
    with pytest.raises(LiveOrderBlocked):
        from agentic.brokers.live_guard import assert_live_order_allowed
        assert_live_order_allowed(settings, kind="option")
