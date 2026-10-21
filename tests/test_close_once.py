"""One buy-to-close per position.

Two exit rules used to each submit a close for the same position in one monitor
tick. These tests use synthetic names (AAA) and made-up prices only.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import date, datetime, timedelta, timezone

import pytest

from agentic.brokers.base import BrokerCapabilities
from agentic.brokers.paper_broker import PaperBroker
from agentic.brokers.robinhood_mcp import (
    ORDERS_HARD_DISABLED,
    REAL_ORDERS_LOCK_LABEL,
    RobinhoodWriteBlocked,
    assert_mcp_tool_allowed,
)
from agentic.config import RuleConfig, Settings
from agentic.domain.enums import (
    DecisionStatus,
    Direction,
    OptionType,
    OrderStatus,
    PositionStatus,
    RuleType,
    Strategy,
)
from agentic.domain.models import CloseDecision, Order, Position, utcnow
from agentic.marketdata.base import PaperMarketData
from agentic.marketdata.quote import OptionQuote
from agentic.rules.engine import RulesEngine
from agentic.services.close_guard import CloseGuard
from agentic.services.executor import OrderExecutor
from agentic.services.killswitch import KillSwitch
from agentic.services.monitor import MonitorLoop
from agentic.services.reconcile import ReconcileLoop
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.orders import LIVE_CLOSE_STATUSES, OrderStore
from agentic.store.positions import PositionStore

OCC = "AAA261231P00050000"


def _position() -> Position:
    return Position(
        occ_symbol=OCC,
        underlying="AAA",
        option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT,
        direction=Direction.SHORT,
        quantity=1,
        strike=50.0,
        expiration=date.today() + timedelta(days=10),
        credit_received=2.00,
        open_avg_price=2.00,
        current_bid=0.40,
        current_ask=0.50,
        current_mark=0.45,
        delta=-0.20,
        iv=0.30,
        status=PositionStatus.OPEN,
        broker_position_id="paper-aaa-csp",
    )


def _quote() -> OptionQuote:
    return OptionQuote(OCC, bid=0.40, ask=0.50, mark=0.45, delta=-0.20, iv=0.30)


def _decision(pos: Position, name: str, rule_type: RuleType) -> CloseDecision:
    return CloseDecision(
        position_id=pos.id,
        rule_name=name,
        rule_type=rule_type,
        reason=f"{name} fired",
        requires_approval=False,
        dedup_key=f"{pos.id}:{rule_type.value}:close-once",
    )


def _wiring(tmp_path):
    db = Database(tmp_path / "close-once.db")
    audit = AuditStore(db)
    positions = PositionStore(db)
    decisions = DecisionStore(db)
    orders = OrderStore(db)
    killswitch = KillSwitch(db, audit)
    return db, audit, positions, decisions, orders, killswitch


def _executor(wiring, broker, settings=None):
    _db, audit, positions, decisions, orders, killswitch = wiring
    return OrderExecutor(
        settings or Settings(mode="paper", broker="paper", market_data="paper"),
        broker,
        PaperMarketData(),
        positions,
        orders,
        decisions,
        audit,
        killswitch,
        poll_interval_seconds=0.001,
    )


def _live_closes(orders: OrderStore, position_id: str) -> list[Order]:
    return [
        o for o in orders.list_by_position(position_id)
        if o.side == "BUY_TO_CLOSE" and o.status in LIVE_CLOSE_STATUSES
    ]


class _GateBroker:
    """Counts close submits. The first one waits so another task can race."""

    def __init__(self):
        self.submits: list[str] = []
        self.started = threading.Event()
        self.hold = threading.Event()
        self._mu = threading.Lock()

    def capabilities(self):
        return BrokerCapabilities("paper-gate", supports_options_orders=True, is_paper=True)

    async def connect(self):
        return None

    async def get_open_positions(self):
        return []

    async def get_order(self, order):
        return order

    async def cancel_order(self, order):
        order.status = OrderStatus.CANCELLED

    async def submit_close_order(self, order):
        with self._mu:
            self.submits.append(order.client_order_id)
            first = len(self.submits) == 1
        if first:
            self.started.set()
            await asyncio.to_thread(self.hold.wait, 3)
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.avg_fill_price = order.limit_price
        order.broker_order_id = "sim-" + order.client_order_id[:8]
        return order


class _ScriptedBroker:
    """First close is rejected or left working; the next one fills. Position stays open until fill."""

    def __init__(self, pos: Position, *, first: str):
        self.pos = pos
        self.first = first
        self.submits: list[str] = []

    def capabilities(self):
        return BrokerCapabilities("paper-script", supports_options_orders=True, is_paper=True)

    async def connect(self):
        return None

    async def get_open_positions(self):
        if self.pos.status == PositionStatus.CLOSED:
            return []
        return [self.pos]

    async def get_order(self, order):
        return order

    async def cancel_order(self, order):
        order.status = OrderStatus.CANCELLED

    async def submit_close_order(self, order):
        self.submits.append(order.client_order_id)
        if len(self.submits) == 1 and self.first == "reject":
            order.status = OrderStatus.REJECTED
            return order
        if len(self.submits) == 1 and self.first == "working":
            order.status = OrderStatus.SUBMITTED
            order.broker_order_id = "sim-working"
            return order
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.avg_fill_price = order.limit_price
        order.broker_order_id = "sim-filled"
        self.pos.status = PositionStatus.CLOSED
        return order


def _rules(*names: str) -> list[RuleConfig]:
    out = []
    for name in names:
        if name == "profit-target":
            out.append(RuleConfig(
                name="profit-target", rule_type="PROFIT_TARGET", requires_approval=False,
                params={"profit_pct": 0.5},
            ))
        else:
            out.append(RuleConfig(
                name="dte-close", rule_type="DTE", requires_approval=False,
                params={"dte_threshold": 45, "action": "close"},
            ))
    return out


def _monitor(tmp_path, broker, rules):
    wiring = _wiring(tmp_path)
    _db, audit, positions, decisions, orders, killswitch = wiring
    settings = Settings(mode="paper", broker="paper", market_data="paper", rules=rules)
    engine = RulesEngine.from_configs(rules)
    executor = _executor(wiring, broker, settings)
    monitor = MonitorLoop(
        settings, broker, PaperMarketData(), positions, audit, killswitch,
        rules_engine=engine, decisions=decisions, executor=executor,
    )
    return monitor, decisions, orders, positions, audit


def _suppressed(audit: AuditStore) -> list[dict]:
    return [e["payload"] for e in audit.recent(50) if e["payload"].get("suppressed")]


# ----------------------------------------------------------------- sequential, one tick
@pytest.mark.asyncio
async def test_sequential_rules_submit_one_close_and_suppress_the_other(tmp_path, caplog):
    """Profit-target is listed first, so it closes; the DTE close is logged and not sent."""
    caplog.set_level("INFO")
    pos = _position()
    broker = PaperBroker(seed_positions=[pos])
    monitor, decisions, orders, positions, audit = _monitor(
        tmp_path, broker, _rules("profit-target", "dte-close"),
    )

    await monitor.run_once()

    stored = positions.get_by_occ(OCC)
    assert stored is not None
    assert stored.status == PositionStatus.CLOSED
    live = _live_closes(orders, stored.id)
    assert len(live) == 1
    assert live[0].status == OrderStatus.FILLED
    assert live[0].order_type == "LIMIT"
    assert len(broker._orders) == 1
    rows = decisions.recent()
    assert [d.rule_name for d in rows] == ["profit-target"]
    assert rows[0].status.value == "DONE"
    suppressed = _suppressed(audit)
    assert suppressed and suppressed[0]["rule"] == "dte-close"
    assert suppressed[0]["kept"] == "profit-target"
    assert any("Close suppressed" in r.message and "dte-close" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_first_configured_rule_wins(tmp_path):
    """Config order is the deterministic tie-break, so listing DTE first selects DTE."""
    pos = _position()
    broker = PaperBroker(seed_positions=[pos])
    monitor, decisions, orders, positions, audit = _monitor(
        tmp_path, broker, _rules("dte-close", "profit-target"),
    )

    await monitor.run_once()

    stored = positions.get_by_occ(OCC)
    assert len(_live_closes(orders, stored.id)) == 1
    assert [d.rule_name for d in decisions.recent()] == ["dte-close"]
    assert _suppressed(audit)[0]["rule"] == "profit-target"
    assert _suppressed(audit)[0]["kept"] == "dte-close"


# ----------------------------------------------------------------- concurrent
@pytest.mark.asyncio
async def test_concurrent_gather_submits_one_close(tmp_path):
    wiring = _wiring(tmp_path)
    _db, _audit, positions, decisions, orders, _ks = wiring
    pos = _position()
    positions.upsert(pos)
    pos = positions.get(pos.id)
    d_profit = _decision(pos, "profit-target", RuleType.PROFIT_TARGET)
    d_dte = _decision(pos, "dte-close", RuleType.DTE)
    decisions.insert_if_new(d_profit)
    decisions.insert_if_new(d_dte)
    broker = _GateBroker()
    ex = _executor(wiring, broker)
    quote = _quote()

    pending = asyncio.gather(
        ex.execute_close(pos, d_profit, quote),
        ex.execute_close(pos, d_dte, quote),
    )
    # waiting in a worker thread leaves the event loop free to run both closes
    assert await asyncio.to_thread(broker.started.wait, 3)
    broker.hold.set()
    results = await pending

    assert broker.submits == [broker.submits[0]]
    assert len(broker.submits) == 1
    assert len(_live_closes(orders, pos.id)) == 1
    filled = [r for r in results if r is not None and r.status == OrderStatus.FILLED]
    assert len(filled) == 1
    assert positions.get(pos.id).status == PositionStatus.CLOSED


def test_concurrent_threads_submit_one_close(tmp_path):
    """Both threads enter orders.update at once. The connection lock keeps that safe."""
    wiring = _wiring(tmp_path)
    _db, _audit, positions, decisions, orders, _ks = wiring
    pos = _position()
    positions.upsert(pos)
    pos = positions.get(pos.id)
    d_profit = _decision(pos, "profit-target", RuleType.PROFIT_TARGET)
    d_dte = _decision(pos, "dte-close", RuleType.DTE)
    decisions.insert_if_new(d_profit)
    decisions.insert_if_new(d_dte)
    side = Order(
        decision_id="d-side", position_id=pos.id, occ_symbol=OCC, quantity=1,
        limit_price=2.0, is_paper=True, client_order_id="side-ticket",
        side="SELL_TO_OPEN", status=OrderStatus.FILLED, filled_qty=1,
    )
    assert orders.insert_if_new(side)
    broker = _GateBroker()
    ex = _executor(wiring, broker)
    quote = _quote()
    errors: list[BaseException] = []
    finished = threading.Semaphore(0)
    barrier = threading.Barrier(2)
    inside_update = threading.Event()
    overlapped = threading.Event()
    seen = threading.local()
    real_update = orders.update

    def _update(order):
        if not getattr(seen, "hit", False):
            seen.hit = True
            inside_update.set()
            barrier.wait(3)
            overlapped.set()
        return real_update(order)

    orders.update = _update

    def _run(decision):
        try:
            asyncio.run(ex.execute_close(pos, decision, quote))
        except BaseException as exc:  # noqa: BLE001 — surface thread failures in the assert
            errors.append(exc)
        finally:
            finished.release()

    def _race_update():
        try:
            orders.update(orders.get_by_client_order_id("side-ticket"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            finished.release()

    threads = [
        threading.Thread(target=_run, args=(d_profit,)),
        threading.Thread(target=_run, args=(d_dte,)),
    ]
    for thread in threads:
        thread.start()
    assert broker.started.wait(3)
    racer = threading.Thread(target=_race_update)
    racer.start()
    # The racer is blocked inside orders.update. Releasing the winner makes its
    # orders.update run at the same time, instead of after the loser has finished.
    assert inside_update.wait(3)
    broker.hold.set()
    assert finished.acquire(timeout=3)
    assert finished.acquire(timeout=3)
    assert finished.acquire(timeout=3)
    for thread in (*threads, racer):
        thread.join(3)

    assert errors == []
    assert overlapped.is_set()
    assert not any(thread.is_alive() for thread in threads)
    assert len(broker.submits) == 1
    assert len(_live_closes(orders, pos.id)) == 1


# ----------------------------------------------------------------- reject / cancel clears the guard
@pytest.mark.asyncio
async def test_rejected_close_can_be_retried(tmp_path):
    pos = _position()
    broker = _ScriptedBroker(pos, first="reject")
    monitor, decisions, orders, positions, _audit = _monitor(
        tmp_path, broker, _rules("profit-target", "dte-close"),
    )

    await monitor.run_once()

    stored = positions.get_by_occ(OCC)
    assert broker.submits and len(broker.submits) == 1
    assert stored.status == PositionStatus.OPEN
    assert orders.get_close_claim(stored.id) is None
    assert _live_closes(orders, stored.id) == []
    failed = decisions.recent()
    assert len(failed) == 1 and failed[0].status.value == "FAILED"

    await monitor.run_once()

    stored = positions.get_by_occ(OCC)
    assert len(broker.submits) == 2
    assert broker.submits[0] != broker.submits[1]
    assert broker.submits[1].endswith("-r1")
    assert stored.status == PositionStatus.CLOSED
    live = _live_closes(orders, stored.id)
    assert len(live) == 1 and live[0].status == OrderStatus.FILLED
    assert orders.get_close_claim(stored.id) == live[0].client_order_id


@pytest.mark.asyncio
async def test_cancelled_close_clears_guard_for_retry(tmp_path):
    wiring = _wiring(tmp_path)
    _db, _audit, positions, decisions, orders, _ks = wiring
    pos = _position()
    positions.upsert(pos)
    pos = positions.get(pos.id)
    decision = _decision(pos, "profit-target", RuleType.PROFIT_TARGET)
    decisions.insert_if_new(decision)
    broker = _ScriptedBroker(pos, first="working")
    settings = Settings(
        mode="paper", broker="paper", market_data="paper",
        execution={"fill_timeout_seconds": 0, "reprice_after_seconds": 3600},
    )
    ex = _executor(wiring, broker, settings)

    first = await ex.execute_close(pos, decision, _quote())
    assert first is not None
    assert first.status == OrderStatus.CANCELLED
    assert orders.get_close_claim(pos.id) is None
    assert positions.get(pos.id).status == PositionStatus.OPEN
    assert len(broker.submits) == 1

    second = await ex.execute_close(pos, decision, _quote())
    assert second is not None and second.status == OrderStatus.FILLED
    assert len(broker.submits) == 2
    assert len(_live_closes(orders, pos.id)) == 1
    assert positions.get(pos.id).status == PositionStatus.CLOSED


def test_filled_claim_blocks_a_new_guard_after_restart(tmp_path):
    """A new in-process guard (as after a restart) still sees the persisted fill."""
    wiring = _wiring(tmp_path)
    _db, _audit, positions, _decisions, orders, _ks = wiring
    pos = _position()
    positions.upsert(pos)
    pos = positions.get(pos.id)
    filled = Order(
        decision_id="d-filled", position_id=pos.id, occ_symbol=OCC, quantity=1,
        limit_price=0.45, is_paper=True, client_order_id="close-filled",
        side="BUY_TO_CLOSE", status=OrderStatus.FILLED, filled_qty=1, avg_fill_price=0.45,
    )
    assert orders.insert_if_new(filled)
    orders.upsert_close_claim(filled)

    fresh = CloseGuard(orders)
    challenger = Order(
        decision_id="d-other", position_id=pos.id, occ_symbol=OCC, quantity=1,
        limit_price=0.45, is_paper=True, client_order_id="close-other",
        side="BUY_TO_CLOSE",
    )
    with fresh.claim(challenger) as (result, owned):
        assert result == "filled"
        assert owned is not None and owned.client_order_id == "close-filled"
    assert orders.get_by_client_order_id("close-other") is None
    assert len(_live_closes(orders, pos.id)) == 1


def test_unique_index_rejects_a_second_live_close(tmp_path):
    _db, _audit, _positions, _decisions, orders, _ks = _wiring(tmp_path)
    first = Order(
        decision_id="d1", position_id="pos-aaa", occ_symbol=OCC, quantity=1,
        limit_price=0.45, is_paper=True, client_order_id="close-1",
        side="BUY_TO_CLOSE", status=OrderStatus.PENDING,
    )
    second = Order(
        decision_id="d2", position_id="pos-aaa", occ_symbol=OCC, quantity=1,
        limit_price=0.46, is_paper=True, client_order_id="close-2",
        side="BUY_TO_CLOSE", status=OrderStatus.SUBMITTED,
    )
    opened = Order(
        decision_id="d3", position_id="pos-aaa", occ_symbol=OCC, quantity=1,
        limit_price=2.00, is_paper=True, client_order_id="open-1",
        side="SELL_TO_OPEN", status=OrderStatus.FILLED,
    )
    failed = Order(
        decision_id="d4", position_id="pos-aaa", occ_symbol=OCC, quantity=1,
        limit_price=0.47, is_paper=True, client_order_id="close-failed",
        side="BUY_TO_CLOSE", status=OrderStatus.REJECTED,
    )
    assert orders.insert_if_new(first) is True
    assert orders.insert_if_new(second) is False
    assert orders.get_by_client_order_id("close-2") is None
    assert orders.insert_if_new(opened) is True
    assert orders.insert_if_new(failed) is True


class _Recorder:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    async def send(self, title, message, *, priority="normal", actions=None):
        self.messages.append((title, message))


class _PartialCancelBroker:
    """First close cancels after one contract fills. The next close fills whatever is left."""

    def __init__(self):
        self.submits: list[tuple[str, int]] = []

    def capabilities(self):
        return BrokerCapabilities("paper-partial", supports_options_orders=True, is_paper=True)

    async def connect(self):
        return None

    async def get_open_positions(self):
        return []

    async def get_order(self, order):
        return order

    async def cancel_order(self, order):
        order.status = OrderStatus.CANCELLED

    async def submit_close_order(self, order):
        self.submits.append((order.client_order_id, order.quantity))
        if len(self.submits) == 1:
            order.status = OrderStatus.CANCELLED
            order.filled_qty = 1
            order.broker_order_id = "sim-partial"
            return order
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.avg_fill_price = order.limit_price
        order.broker_order_id = "sim-rest"
        return order


class _FillDuringCancelBroker:
    def __init__(self):
        self.submits: list[str] = []
        self.cancels = 0

    def capabilities(self):
        return BrokerCapabilities("paper-fill-cancel", supports_options_orders=True, is_paper=True)

    async def connect(self):
        return None

    async def get_open_positions(self):
        return []

    async def get_order(self, order):
        return order

    async def cancel_order(self, order):
        self.cancels += 1
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.avg_fill_price = order.limit_price

    async def submit_close_order(self, order):
        self.submits.append(order.client_order_id)
        order.status = OrderStatus.SUBMITTED
        order.broker_order_id = "sim-working"
        return order


class _PollBroker:
    """get_order fills. submit_close_order records a call and must not be used."""

    def __init__(self):
        self.submits: list[str] = []
        self.polls = 0

    def capabilities(self):
        return BrokerCapabilities("paper-poll", supports_options_orders=True, is_paper=True)

    async def connect(self):
        return None

    async def get_open_positions(self):
        return []

    async def get_order(self, order):
        self.polls += 1
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.avg_fill_price = order.limit_price
        return order

    async def cancel_order(self, order):
        return None

    async def submit_close_order(self, order):
        self.submits.append(order.client_order_id)
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.avg_fill_price = order.limit_price
        order.broker_order_id = "sim-should-not-submit"
        return order


def _loss_position() -> Position:
    pos = _position()
    pos.quantity = 1
    pos.credit_received = 2.00
    pos.current_bid = 4.00
    pos.current_ask = 4.20
    pos.current_mark = 4.10
    pos.delta = -0.60
    return pos


# ----------------------------------------------------------------- review fixes
@pytest.mark.asyncio
async def test_paper_retry_after_cancel_uses_a_fresh_client_id(tmp_path):
    """PaperBroker is idempotent on client_order_id, so a retry must not reuse a cancelled id."""
    wiring = _wiring(tmp_path)
    _db, _audit, positions, decisions, orders, _ks = wiring
    pos = _position()
    positions.upsert(pos)
    pos = positions.get(pos.id)
    decision = _decision(pos, "profit-target", RuleType.PROFIT_TARGET)
    decisions.insert_if_new(decision)
    cancelled_id = f"close-{decision.id}"
    cancelled = Order(
        decision_id=decision.id, position_id=pos.id, occ_symbol=OCC, quantity=1,
        limit_price=0.45, is_paper=True, client_order_id=cancelled_id,
        side="BUY_TO_CLOSE", status=OrderStatus.CANCELLED, broker_order_id="paper-old",
    )
    assert orders.insert_if_new(cancelled)
    broker = PaperBroker()
    broker._orders[cancelled_id] = cancelled
    ex = _executor(wiring, broker)

    result = await ex.execute_close(pos, decision, _quote())

    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert result.client_order_id != cancelled_id
    assert result.client_order_id.endswith("-r1")
    assert broker._orders[cancelled_id].status == OrderStatus.CANCELLED
    stored_old = orders.get_by_client_order_id(cancelled_id)
    assert stored_old is not None and stored_old.status == OrderStatus.CANCELLED
    assert len(_live_closes(orders, pos.id)) == 1
    assert _live_closes(orders, pos.id)[0].client_order_id == result.client_order_id


@pytest.mark.asyncio
async def test_partial_fill_then_cancel_keeps_the_fill_and_closes_the_rest(tmp_path):
    wiring = _wiring(tmp_path)
    _db, _audit, positions, decisions, orders, _ks = wiring
    pos = _position()
    pos.quantity = 2
    positions.upsert(pos)
    pos = positions.get(pos.id)
    decision = _decision(pos, "profit-target", RuleType.PROFIT_TARGET)
    decisions.insert_if_new(decision)
    broker = _PartialCancelBroker()
    settings = Settings(
        mode="paper", broker="paper", market_data="paper",
        execution={"fill_timeout_seconds": 0, "reprice_after_seconds": 3600},
    )
    ex = _executor(wiring, broker, settings)

    first = await ex.execute_close(pos, decision, _quote())
    assert first is not None and first.status == OrderStatus.CANCELLED
    assert first.filled_qty == 1
    assert orders.get_close_claim(pos.id) is None
    assert positions.get(pos.id).status == PositionStatus.OPEN
    # The filled contract stays reserved, so a second live close is still just one row
    # once the remainder is working. Nothing is live between the cancel and the retry.
    assert _live_closes(orders, pos.id) == []

    second = await ex.execute_close(pos, decision, _quote())
    assert second is not None and second.status == OrderStatus.FILLED
    assert broker.submits[0][1] == 2
    assert broker.submits[1][1] == 1
    assert broker.submits[0][0] != broker.submits[1][0]
    original = orders.get_by_client_order_id(broker.submits[0][0])
    assert original is not None and original.filled_qty == 1
    assert original.status == OrderStatus.CANCELLED
    live = _live_closes(orders, pos.id)
    assert len(live) == 1 and live[0].quantity == 1 and live[0].status == OrderStatus.FILLED
    extra = Order(
        decision_id="d-extra", position_id=pos.id, occ_symbol=OCC, quantity=1,
        limit_price=0.45, is_paper=True, client_order_id="close-extra",
        side="BUY_TO_CLOSE", status=OrderStatus.PENDING,
    )
    assert orders.insert_if_new(extra) is False


@pytest.mark.asyncio
async def test_unsubmitted_pending_resumes_in_a_new_process(tmp_path):
    """A new process has an empty in-memory guard. A PENDING row with no broker id is submitted."""
    wiring = _wiring(tmp_path)
    _db, _audit, positions, decisions, orders, _ks = wiring
    pos = _position()
    positions.upsert(pos)
    pos = positions.get(pos.id)
    decision = _decision(pos, "profit-target", RuleType.PROFIT_TARGET)
    decisions.insert_if_new(decision)
    decisions.set_status(decision.id, DecisionStatus.EXECUTING)
    pending_id = f"close-{decision.id}"
    pending = Order(
        decision_id=decision.id, position_id=pos.id, occ_symbol=OCC, quantity=1,
        limit_price=0.45, is_paper=True, client_order_id=pending_id,
        side="BUY_TO_CLOSE", status=OrderStatus.PENDING, last_status_at=utcnow(),
    )
    assert orders.insert_if_new(pending)
    orders.upsert_close_claim(pending)
    positions.set_status(pos.id, PositionStatus.CLOSING)
    broker = _PollBroker()
    fresh = _executor(wiring, broker)

    result = await fresh.execute_close(pos, decision, _quote())

    assert broker.submits == [pending_id]
    assert broker.polls == 0
    assert result is not None and result.status == OrderStatus.FILLED
    assert result.client_order_id == pending_id
    assert positions.get(pos.id).status == PositionStatus.CLOSED


@pytest.mark.asyncio
async def test_submitted_close_is_polled_not_resubmitted(tmp_path):
    wiring = _wiring(tmp_path)
    _db, _audit, positions, decisions, orders, _ks = wiring
    pos = _position()
    positions.upsert(pos)
    pos = positions.get(pos.id)
    decision = _decision(pos, "profit-target", RuleType.PROFIT_TARGET)
    decisions.insert_if_new(decision)
    working_id = f"close-{decision.id}"
    working = Order(
        decision_id=decision.id, position_id=pos.id, occ_symbol=OCC, quantity=1,
        limit_price=0.45, is_paper=True, client_order_id=working_id,
        side="BUY_TO_CLOSE", status=OrderStatus.SUBMITTED, broker_order_id="sim-already",
    )
    assert orders.insert_if_new(working)
    orders.upsert_close_claim(working)
    broker = _PollBroker()
    fresh = _executor(wiring, broker)

    result = await fresh.execute_close(pos, decision, _quote())

    assert broker.submits == []
    assert broker.polls == 1
    assert result is not None and result.status == OrderStatus.FILLED
    assert result.client_order_id == working_id
    assert positions.get(pos.id).status == PositionStatus.CLOSED


@pytest.mark.asyncio
async def test_reconcile_expires_a_never_submitted_pending_close(tmp_path):
    wiring = _wiring(tmp_path)
    db, audit, positions, decisions, orders, killswitch = wiring
    pos = _position()
    pos.status = PositionStatus.CLOSING
    positions.upsert(pos)
    pos = positions.get(pos.id)
    decision = _decision(pos, "profit-target", RuleType.PROFIT_TARGET)
    decisions.insert_if_new(decision)
    decisions.set_status(decision.id, DecisionStatus.EXECUTING)
    stale = Order(
        decision_id=decision.id, position_id=pos.id, occ_symbol=OCC, quantity=1,
        limit_price=0.45, is_paper=True, client_order_id=f"close-{decision.id}",
        side="BUY_TO_CLOSE", status=OrderStatus.PENDING,
        last_status_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    assert orders.insert_if_new(stale)
    orders.upsert_close_claim(stale)
    fresh_pos = _position()
    fresh_pos.occ_symbol = "BBB261231P00050000"
    fresh_pos.underlying = "BBB"
    fresh_pos.status = PositionStatus.CLOSING
    positions.upsert(fresh_pos)
    fresh_pos = positions.get(fresh_pos.id)
    fresh = Order(
        decision_id="d-fresh", position_id=fresh_pos.id, occ_symbol=fresh_pos.occ_symbol,
        quantity=1, limit_price=0.45, is_paper=True, client_order_id="close-fresh",
        side="BUY_TO_CLOSE", status=OrderStatus.PENDING, last_status_at=utcnow(),
    )
    assert orders.insert_if_new(fresh)
    orders.upsert_close_claim(fresh)
    broker = PaperBroker(seed_positions=[_position(), fresh_pos])
    settings = Settings(mode="paper", broker="paper", market_data="paper")
    reconcile = ReconcileLoop(
        settings, broker, positions, audit, orders=orders, killswitch=killswitch,
        decisions=decisions,
    )

    await reconcile.run_once()

    expired = orders.get_by_client_order_id(stale.client_order_id)
    assert expired is not None and expired.status == OrderStatus.CANCELLED
    assert orders.get_close_claim(pos.id) is None
    assert positions.get(pos.id).status == PositionStatus.OPEN
    assert decisions.get(decision.id).status == DecisionStatus.FAILED
    still = orders.get_by_client_order_id("close-fresh")
    assert still is not None and still.status == OrderStatus.PENDING
    assert orders.get_close_claim(fresh_pos.id) == "close-fresh"


@pytest.mark.asyncio
async def test_notify_only_alert_still_fires_beside_one_close(tmp_path):
    pos = _position()
    broker = PaperBroker(seed_positions=[pos])
    rules = [
        RuleConfig(
            name="profit-target", rule_type="PROFIT_TARGET", requires_approval=False,
            params={"profit_pct": 0.5},
        ),
        RuleConfig(
            name="dte-alert", rule_type="DTE", requires_approval=False,
            params={"dte_threshold": 45, "action": "alert"},
        ),
    ]
    wiring = _wiring(tmp_path)
    _db, audit, positions, decisions, orders, killswitch = wiring
    settings = Settings(mode="paper", broker="paper", market_data="paper", rules=rules)
    notifier = _Recorder()
    monitor = MonitorLoop(
        settings, broker, PaperMarketData(), positions, audit, killswitch,
        rules_engine=RulesEngine.from_configs(rules), decisions=decisions,
        executor=_executor(wiring, broker, settings), notifier=notifier,
    )

    await monitor.run_once()

    stored = positions.get_by_occ(OCC)
    assert len(_live_closes(orders, stored.id)) == 1
    assert len(broker._orders) == 1
    names = {d.rule_name: d.status for d in decisions.recent()}
    assert names["profit-target"] == DecisionStatus.DONE
    assert names["dte-alert"] == DecisionStatus.PROPOSED
    assert any("ALERT" in message for _title, message in notifier.messages)
    assert not any(payload.get("rule") == "dte-alert" and payload.get("suppressed")
                   for payload in _suppressed(audit))


@pytest.mark.asyncio
async def test_approval_rule_ahead_of_stop_loss_does_not_suppress_it(tmp_path):
    pos = _loss_position()
    broker = PaperBroker(seed_positions=[pos])
    rules = [
        RuleConfig(
            name="dte-approval", rule_type="DTE", requires_approval=True,
            params={"dte_threshold": 45, "action": "close"},
        ),
        RuleConfig(
            name="stop-loss", rule_type="STOP_LOSS", requires_approval=False,
            params={"loss_mult": 2.0},
        ),
    ]
    wiring = _wiring(tmp_path)
    _db, audit, positions, decisions, orders, killswitch = wiring
    settings = Settings(mode="paper", broker="paper", market_data="paper", rules=rules)
    notifier = _Recorder()
    monitor = MonitorLoop(
        settings, broker, PaperMarketData(), positions, audit, killswitch,
        rules_engine=RulesEngine.from_configs(rules), decisions=decisions,
        executor=_executor(wiring, broker, settings), notifier=notifier,
    )

    await monitor.run_once()

    stored = positions.get_by_occ(OCC)
    live = _live_closes(orders, stored.id)
    assert len(live) == 1 and live[0].status == OrderStatus.FILLED
    by_name = {d.rule_name: d for d in decisions.recent()}
    assert by_name["stop-loss"].status == DecisionStatus.DONE
    assert by_name["dte-approval"].status == DecisionStatus.PROPOSED
    assert any("NEEDS APPROVAL" in message for _title, message in notifier.messages)
    assert _suppressed(audit) == []


@pytest.mark.asyncio
async def test_fill_during_cancel_settles_as_filled(tmp_path):
    wiring = _wiring(tmp_path)
    _db, _audit, positions, decisions, orders, _ks = wiring
    pos = _position()
    positions.upsert(pos)
    pos = positions.get(pos.id)
    decision = _decision(pos, "profit-target", RuleType.PROFIT_TARGET)
    decisions.insert_if_new(decision)
    broker = _FillDuringCancelBroker()
    settings = Settings(
        mode="paper", broker="paper", market_data="paper",
        execution={"fill_timeout_seconds": 0, "reprice_after_seconds": 3600},
    )
    ex = _executor(wiring, broker, settings)

    result = await ex.execute_close(pos, decision, _quote())

    assert broker.cancels == 1
    assert result is not None and result.status == OrderStatus.FILLED
    assert positions.get(pos.id).status == PositionStatus.CLOSED
    assert decisions.get(decision.id).status == DecisionStatus.DONE
    assert orders.get_close_claim(pos.id) == result.client_order_id


@pytest.mark.asyncio
async def test_blocked_second_decision_is_a_terminal_skip(tmp_path):
    wiring = _wiring(tmp_path)
    _db, audit, positions, decisions, orders, killswitch = wiring
    pos = _position()
    positions.upsert(pos)
    pos = positions.get(pos.id)
    first = _decision(pos, "profit-target", RuleType.PROFIT_TARGET)
    second = _decision(pos, "dte-close", RuleType.DTE)
    decisions.insert_if_new(first)
    decisions.insert_if_new(second)
    notifier = _Recorder()
    ex = _executor(wiring, PaperBroker())
    ex.notifier = notifier
    quote = _quote()

    filled = await ex.execute_close(pos, first, quote)
    assert filled is not None and filled.status == OrderStatus.FILLED
    after_fill = len(notifier.messages)
    skipped = await ex.execute_close(pos, second, quote)
    assert skipped is None
    assert decisions.get(second.id).status == DecisionStatus.SKIPPED
    assert len(notifier.messages) == after_fill + 1

    settings = Settings(mode="paper", broker="paper", market_data="paper")
    monitor = MonitorLoop(
        settings, PaperBroker(), PaperMarketData(), positions, audit, killswitch,
        decisions=decisions, executor=ex,
    )
    again = _decision(pos, "dte-close", RuleType.DTE)
    again.dedup_key = second.dedup_key
    assert monitor._admit_decision(again) == "skip"
    assert len(notifier.messages) == after_fill + 1


@pytest.mark.asyncio
async def test_suppressed_skip_is_audited_once_per_day(tmp_path):
    pos = _position()
    broker = _ScriptedBroker(pos, first="reject")
    monitor, _decisions, _orders, _positions, audit = _monitor(
        tmp_path, broker, _rules("profit-target", "dte-close"),
    )

    await monitor.run_once()
    await monitor.run_once()

    suppressed = [p for p in _suppressed(audit) if p.get("rule") == "dte-close"]
    assert len(suppressed) == 1


def test_duplicate_live_closes_are_quarantined_and_the_index_is_created(tmp_path):
    db, _audit, _positions, _decisions, orders, _ks = _wiring(tmp_path)
    db.conn.execute("DROP INDEX IF EXISTS idx_orders_one_live_close")
    db.conn.commit()
    pending = Order(
        decision_id="d-pending", position_id="pos-aaa", occ_symbol=OCC, quantity=2,
        limit_price=0.45, is_paper=True, client_order_id="close-pending",
        side="BUY_TO_CLOSE", status=OrderStatus.PENDING, filled_qty=1,
    )
    submitted = Order(
        decision_id="d-submitted", position_id="pos-aaa", occ_symbol=OCC, quantity=2,
        limit_price=0.46, is_paper=True, client_order_id="close-submitted",
        side="BUY_TO_CLOSE", status=OrderStatus.SUBMITTED, broker_order_id="sim-kept",
    )
    assert orders.insert_if_new(pending) is True
    assert orders.insert_if_new(submitted) is True
    orders.upsert_close_claim(pending)

    db.migrate()

    live = _live_closes(orders, "pos-aaa")
    assert [o.client_order_id for o in live] == ["close-submitted"]
    quarantined = orders.get_by_client_order_id("close-pending")
    assert quarantined is not None
    assert quarantined.status == OrderStatus.CANCELLED
    assert quarantined.filled_qty == 1
    assert orders.get_close_claim("pos-aaa") == "close-submitted"
    index = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_orders_one_live_close'"
    ).fetchone()
    assert index is not None
    extra = Order(
        decision_id="d-extra", position_id="pos-aaa", occ_symbol=OCC, quantity=1,
        limit_price=0.47, is_paper=True, client_order_id="close-extra",
        side="BUY_TO_CLOSE", status=OrderStatus.PENDING,
    )
    assert orders.insert_if_new(extra) is False


def test_real_orders_lock_unchanged():
    """The hard-coded read-only order lock is not part of this fix and stays on."""
    assert ORDERS_HARD_DISABLED is True
    assert REAL_ORDERS_LOCK_LABEL == "Real orders: HARD DISABLED in code"
    with pytest.raises(RobinhoodWriteBlocked):
        assert_mcp_tool_allowed("place_option_order")
    assert_mcp_tool_allowed("get_accounts")
