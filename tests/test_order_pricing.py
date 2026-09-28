"""Limit-only house rule: order type, limit, bid/ask/mid, TIF on journal + APIs."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from agentic.brokers.paper_broker import PaperBroker
from agentic.config import EntryConfig, EntryCriteria, Settings
from agentic.domain.enums import DecisionStatus, OrderStatus, PositionStatus, RuleType
from agentic.domain.models import CloseDecision, Order, TradeJournalEntry, utcnow
from agentic.domain.order_pricing import (
    CLOSE_PRICING_FIELDS, PRICING_FIELDS, NonLimitOrderRefused, assert_limit_only,
)
from agentic.marketdata.base import MarketDataProvider, PaperMarketData
from agentic.marketdata.quote import OptionContractQuote
from agentic.services.executor import OrderExecutor
from agentic.services.killswitch import KillSwitch
from agentic.services.scanner import OpportunityScanner
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.entry_decisions import EntryDecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.store.trade_journal import TradeJournalStore
from agentic.web.app import WebDeps, create_app


def _occ(underlying, exp, strike):
    return f"{underlying}{exp:%y%m%d}P{int(strike * 1000):08d}"


class _PutChain(MarketDataProvider):
    def __init__(self, chain):
        self._chain = chain

    async def get_quote(self, position):
        return None

    async def get_chain(self, underlying):
        return self._chain if underlying == "X" else []

    async def get_fresh_contract_quote(self, occ_symbol):
        return next((c for c in self._chain if c.occ_symbol == occ_symbol), None)

    async def get_underlying_price(self, underlying):
        return 48.0


def _good_put():
    exp = utcnow().date() + timedelta(days=10)
    return OptionContractQuote(
        occ_symbol=_occ("X", exp, 50), underlying="X", option_id="oid-x",
        option_type="put", strike=50.0, expiration=exp, bid=1.60, ask=1.70, mark=1.65,
        delta=-0.25, iv=0.45, open_interest=500, volume=50,
    )


def _stores(tmp_path):
    db = Database(tmp_path / "pricing.db")
    audit = AuditStore(db)
    return dict(
        db=db, audit=audit, positions=PositionStore(db), decisions=DecisionStore(db),
        orders=OrderStore(db), entry_decisions=EntryDecisionStore(db),
        journal=TradeJournalStore(db), killswitch=KillSwitch(db, audit),
        signals=SignalStore(db),
    )


def _settings():
    return Settings(
        mode="paper", broker="paper", market_data="paper",
        entry=EntryConfig(
            enabled=True, watchlist=["X"],
            criteria=EntryCriteria(
                delta_min=0.20, delta_max=0.30, dte_min=7, dte_max=14,
                min_annualized_yield=0.10, min_open_interest=100, min_volume=10,
                max_spread_pct=0.15, exclude_earnings_days=0,
            ),
        ),
    )


def _client(stores, settings, scanner=None):
    deps = WebDeps(
        settings=settings, signals=stores["signals"], killswitch=stores["killswitch"],
        approval_gate=None, audit=stores["audit"], positions=stores["positions"],
        orders=stores["orders"], decisions=stores["decisions"],
        entry_decisions=stores["entry_decisions"], trade_journal=stores["journal"],
        scanner=scanner,
    )
    return TestClient(create_app(deps))


# --- guard ---------------------------------------------------------------------------------------

def test_assert_limit_only_accepts_limit_casings():
    assert_limit_only("limit", where="test")
    assert_limit_only("LIMIT", where="test")
    assert_limit_only(" Limit ", where="test")


def test_assert_limit_only_refuses_market():
    with pytest.raises(NonLimitOrderRefused, match="LIMIT-ONLY RULE"):
        assert_limit_only("market", where="test", occ="RKLB")
    with pytest.raises(NonLimitOrderRefused, match="missing"):
        assert_limit_only(None, where="test")
    with pytest.raises(NonLimitOrderRefused):
        assert_limit_only("stop", where="test")


@pytest.mark.asyncio
async def test_paper_broker_refuses_market_open_and_close(caplog):
    broker = PaperBroker(seed_positions=[])
    market = Order(
        decision_id="d", position_id="p", occ_symbol="X260928P00050000",
        quantity=1, limit_price=0.87, is_paper=True, order_type="MARKET",
        client_order_id="mkt-open",
    )
    with pytest.raises(NonLimitOrderRefused, match="LIMIT-ONLY RULE"):
        await broker.submit_open_order(market)
    with pytest.raises(NonLimitOrderRefused, match="LIMIT-ONLY RULE"):
        await broker.submit_close_order(market)
    with pytest.raises(NonLimitOrderRefused, match="LIMIT-ONLY RULE"):
        await broker.submit_equity_order(symbol="SGOV", side="buy", dollar_amount=10,
                                         order_type="market", price_hint=100.0)
    assert "LIMIT-ONLY RULE" in caplog.text
    assert await broker.get_open_positions() == []


# --- paper entry + close snapshots + JSON APIs ---------------------------------------------------

@pytest.mark.asyncio
async def test_paper_entry_records_limit_snapshot_on_journal_and_entry_decisions(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    stores = _stores(tmp_path)
    put = _good_put()
    md = _PutChain([put])
    settings = _settings()
    broker = PaperBroker(seed_positions=[], buying_power=100_000.0)
    ex = OrderExecutor(
        settings, broker, md, stores["positions"], stores["orders"], stores["decisions"],
        stores["audit"], stores["killswitch"], entry_decisions=stores["entry_decisions"],
        trade_journal=stores["journal"], poll_interval_seconds=0.001,
    )
    scanner = OpportunityScanner(
        settings, broker, md, stores["entry_decisions"], ex, stores["audit"],
        stores["killswitch"], trade_journal=stores["journal"],
    )
    await scanner.run_once()

    row = stores["journal"].recent()[0]
    assert row.order_type == "limit"
    assert row.limit_price == 1.65          # mid of 1.60 / 1.70
    assert row.bid == 1.60 and row.ask == 1.70 and row.mid == 1.65
    assert row.time_in_force == "gfd"
    for k in CLOSE_PRICING_FIELDS:
        assert getattr(row, k) is None      # still open

    entry = stores["entry_decisions"].recent()[0]
    assert entry.status == DecisionStatus.DONE
    assert entry.order_type == "limit" and entry.limit_price == 1.65
    assert entry.bid == 1.60 and entry.ask == 1.70 and entry.mid == 1.65
    assert entry.time_in_force == "gfd"

    order = stores["orders"].get_by_client_order_id(f"open-{entry.id}")
    assert order is not None and order.status == OrderStatus.FILLED
    assert order.bid == 1.60 and order.ask == 1.70 and order.mid == 1.65
    assert order.time_in_force == "gfd"

    client = _client(stores, settings, scanner=scanner)
    journal = client.get("/api/journal").json()["trades"][0]
    for k in PRICING_FIELDS:
        assert k in journal
    assert journal["order_type"] == "limit"
    assert journal["limit_price"] == 1.65
    assert journal["bid"] == 1.60 and journal["ask"] == 1.70 and journal["mid"] == 1.65
    assert journal["time_in_force"] == "gfd"
    for k in CLOSE_PRICING_FIELDS:
        assert k in journal and journal[k] is None

    entries = client.get("/api/entry-decisions").json()["entries"][0]
    for k in PRICING_FIELDS:
        assert k in entries
    assert entries["order_type"] == "limit" and entries["limit_price"] == 1.65
    assert entries["bid"] == 1.60 and entries["ask"] == 1.70 and entries["mid"] == 1.65
    assert entries["time_in_force"] == "gfd"


@pytest.mark.asyncio
async def test_paper_close_records_close_snapshot_on_journal_and_decisions(tmp_path):
    stores = _stores(tmp_path)
    settings = Settings(mode="paper", broker="paper", market_data="paper")
    broker = PaperBroker()
    pos = (await broker.get_open_positions())[0]
    stores["positions"].upsert(pos)
    stores["journal"].insert(TradeJournalEntry(
        occ_symbol=pos.occ_symbol, underlying=pos.underlying, kind="CC", contracts=1,
        strike=pos.strike, dte=pos.dte(), premium=pos.credit_received,
        order_type="limit", limit_price=2.00, bid=1.95, ask=2.05, mid=2.00,
        time_in_force="gfd",
    ))
    decision = CloseDecision(
        position_id=pos.id, rule_name="profit-50", rule_type=RuleType.PROFIT_TARGET,
        reason="test close", requires_approval=False, dedup_key=f"{pos.id}:PT:today",
    )
    stores["decisions"].insert_if_new(decision)
    ex = OrderExecutor(
        settings, broker, PaperMarketData(), stores["positions"], stores["orders"],
        stores["decisions"], stores["audit"], stores["killswitch"],
        trade_journal=stores["journal"], poll_interval_seconds=0.001,
    )
    order = await ex.execute_close(pos, decision)
    assert order is not None and order.status == OrderStatus.FILLED
    assert order.order_type == "LIMIT"
    assert order.bid is not None and order.ask is not None and order.mid is not None
    assert order.time_in_force == "gfd"
    assert order.limit_price == order.mid

    je = stores["journal"].recent()[0]
    assert je.close_order_type == "limit"
    assert je.close_limit_price == order.limit_price
    assert je.close_bid == order.bid and je.close_ask == order.ask and je.close_mid == order.mid
    assert je.close_time_in_force == "gfd"

    d = stores["decisions"].get(decision.id)
    assert d.order_type == "limit" and d.limit_price == order.limit_price
    assert d.bid == order.bid and d.ask == order.ask and d.mid == order.mid
    assert d.time_in_force == "gfd"

    client = _client(stores, settings)
    trade = client.get("/api/journal").json()["trades"][0]
    assert trade["close_order_type"] == "limit"
    assert trade["close_limit_price"] == order.limit_price
    assert trade["close_bid"] == order.bid and trade["close_ask"] == order.ask
    assert trade["close_mid"] == order.mid and trade["close_time_in_force"] == "gfd"

    dec = client.get("/api/decisions").json()["decisions"][0]
    for k in PRICING_FIELDS:
        assert k in dec
    assert dec["order_type"] == "limit" and dec["limit_price"] == order.limit_price

    pos_row = client.get("/api/positions").json()["positions"][0]
    for k in PRICING_FIELDS + CLOSE_PRICING_FIELDS:
        assert k in pos_row
    assert pos_row["close_order_type"] == "limit"
    assert pos_row["close_limit_price"] == order.limit_price


@pytest.mark.asyncio
async def test_market_order_attempt_is_refused_and_audited(tmp_path, monkeypatch, caplog):
    stores = _stores(tmp_path)
    settings = Settings(mode="paper", broker="paper", market_data="paper")
    broker = PaperBroker()
    pos = (await broker.get_open_positions())[0]
    stores["positions"].upsert(pos)
    decision = CloseDecision(
        position_id=pos.id, rule_name="profit-50", rule_type=RuleType.PROFIT_TARGET,
        reason="test", requires_approval=False, dedup_key=f"{pos.id}:PT:mkt",
    )
    stores["decisions"].insert_if_new(decision)

    from agentic.domain.models import Order as RealOrder

    def _market_order(*args, **kwargs):
        kwargs["order_type"] = "MARKET"
        return RealOrder(*args, **kwargs)

    monkeypatch.setattr("agentic.services.executor.Order", _market_order)
    ex = OrderExecutor(
        settings, broker, PaperMarketData(), stores["positions"], stores["orders"],
        stores["decisions"], stores["audit"], stores["killswitch"],
        poll_interval_seconds=0.001,
    )
    result = await ex.execute_close(pos, decision)
    assert result is None
    assert stores["orders"].get_by_client_order_id(f"close-{decision.id}") is None
    assert stores["positions"].get_by_occ(pos.occ_symbol).status == PositionStatus.OPEN
    assert stores["decisions"].get(decision.id).status == DecisionStatus.FAILED

    events = stores["audit"].recent()
    err = next(e for e in events if e["event_type"] == "ERROR")
    assert err["payload"]["where"] == "executor.limit_only"
    assert "LIMIT-ONLY" in (err["payload"].get("error") or "")
    assert "LIMIT-ONLY RULE" in caplog.text

    client = _client(stores, settings)
    ops = client.get("/api/ops").json()
    assert ops["last_error"]["where"] == "executor.limit_only"
    assert "LIMIT-ONLY" in (ops["last_error"].get("error") or "")
    audit = client.get("/api/audit").json()["events"]
    assert any(e["payload"].get("where") == "executor.limit_only" for e in audit)


def test_legacy_journal_rows_expose_null_pricing_fields(tmp_path):
    stores = _stores(tmp_path)
    stores["journal"].insert(TradeJournalEntry(
        occ_symbol="RKLB261009P00065000", underlying="RKLB", kind="CSP", contracts=1,
        strike=65.0, dte=11, premium=0.87,
    ))
    client = _client(stores, Settings(mode="paper"))
    trade = client.get("/api/journal").json()["trades"][0]
    for k in PRICING_FIELDS + CLOSE_PRICING_FIELDS:
        assert k in trade
        assert trade[k] is None
