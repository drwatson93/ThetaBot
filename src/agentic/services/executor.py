"""OrderExecutor: turn an auto-approved CloseDecision into a filled buy-to-close.

Safety pipeline (every order goes through all of it):
  1. Kill-switch recheck    — refuse if paused since the decision was made.
  2. Live-arming gate        — a non-paper broker only places real orders when
                               ``settings.is_live`` (mode=live AND i_understand_live_trading).
  3. Fresh-quote stale guard — reject missing / invalid / stale quotes (no blind orders).
  4. Limit-price calc        — mid + buffer, capped at ask*(1+slippage_cap), tick-rounded.
  5. Persist-before-submit   — write the Order PENDING keyed by a deterministic
                               client_order_id; reuse it on retry (idempotency).
  6. Submit + await fill     — poll to fill_timeout; bounded re-price on no-fill/partial.
  7. Settle                  — on FILLED mark Position CLOSED + Decision DONE; audit + notify.

The limit math is a free function so it can be unit-tested without a broker.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..config import Settings
from ..brokers.base import ExecutionBroker
from ..errors import describe_exception
from ..domain.enums import AuditEventType, DecisionStatus, OrderStatus, PositionStatus
from ..domain.models import CloseDecision, EntryDecision, Order, Position, utcnow
from ..domain.order_pricing import (
    NonLimitOrderRefused,
    assert_limit_only,
    pricing_kwargs,
    stamp_order_quote,
)
from ..marketdata.base import MarketDataProvider
from ..marketdata.quote import OptionContractQuote, OptionQuote
from ..notify.base import Notifier
from ..store.audit import AuditStore
from ..store.decisions import DecisionStore
from ..store.entry_decisions import EntryDecisionStore
from ..store.orders import OrderStore
from ..store.positions import PositionStore
from ..store.trade_journal import TradeJournalStore
from .killswitch import KillSwitch
from .market_hours import is_order_window
from .stats import position_pnl

log = logging.getLogger("agentic.executor")


def round_to_tick(price: float, tick: float = 0.01) -> float:
    """Round to the nearest option price tick (default 1 cent)."""
    if tick <= 0:
        return round(price, 2)
    return round(round(price / tick) * tick, 4)


def compute_limit_price(
    quote: OptionQuote,
    *,
    buffer_pct: float,
    slippage_cap_pct: float,
    tick: float = 0.01,
    toward_fill: float = 0.0,
) -> float | None:
    """Buy-to-close limit: start at mid (between bid and ask), then step toward the ask.

    ``toward_fill`` is 0..1 (0 = mid, 1 = the ask). Never above ``ask * (1 + slippage_cap)``.
    ``buffer_pct`` is kept for callers; the first submit uses mid, not a percent nudge.
    """
    mid = quote.midpoint
    if mid is None or mid <= 0:
        return None
    ask = quote.ask
    if toward_fill <= 0:
        target = mid
    elif ask is not None and ask > 0:
        target = mid + (ask - mid) * min(1.0, toward_fill)
    else:
        target = mid * (1 + buffer_pct)
    if ask is not None and ask > 0:
        target = min(target, ask * (1 + slippage_cap_pct))
    return round_to_tick(target, tick)


def compute_open_limit_price(
    quote: OptionContractQuote,
    *,
    buffer_pct: float,
    slippage_cap_pct: float,
    tick: float = 0.01,
    toward_fill: float = 0.0,
) -> float | None:
    """Sell-to-open limit: start at mid, then step toward the bid. Never below
    ``bid * (1 - slippage_cap)``.
    """
    mid = quote.midpoint
    if mid is None or mid <= 0:
        return None
    bid = quote.bid
    if toward_fill <= 0:
        target = mid
    elif bid is not None and bid > 0:
        target = mid - (mid - bid) * min(1.0, toward_fill)
    else:
        target = mid * (1 - buffer_pct)
    if bid is not None and bid > 0:
        target = max(target, bid * (1 - slippage_cap_pct))
    return round_to_tick(target, tick)


class OrderExecutor:
    def __init__(
        self,
        settings: Settings,
        broker: ExecutionBroker,
        market_data: MarketDataProvider,
        positions: PositionStore,
        orders: OrderStore,
        decisions: DecisionStore,
        audit: AuditStore,
        killswitch: KillSwitch,
        notifier: Notifier | None = None,
        entry_decisions: EntryDecisionStore | None = None,
        trade_journal: TradeJournalStore | None = None,
        *,
        poll_interval_seconds: float = 2.0,
    ):
        self.settings = settings
        self.broker = broker
        self.market_data = market_data
        self.positions = positions
        self.orders = orders
        self.decisions = decisions
        self.audit = audit
        self.killswitch = killswitch
        self.notifier = notifier
        self.entry_decisions = entry_decisions
        self.trade_journal = trade_journal
        self.poll_interval = poll_interval_seconds

    async def execute_close(
        self, position: Position, decision: CloseDecision, quote: OptionQuote | None = None
    ) -> Order | None:
        """Execute a buy-to-close for ``position`` per ``decision``. Returns the final Order,
        or None if blocked (paused / not armed / unusable quote / nothing to do)."""
        # 1. Kill-switch recheck (state may have changed since the decision was made).
        if self.killswitch.is_paused():
            await self._block(position, decision, "killswitch engaged; order suppressed")
            return None
        hours_block = self._order_window_block()
        if hours_block:
            await self._block(position, decision, hours_block)
            return None

        # 3. Fresh quote + stale guard (refetch unless the caller passed a fresh one).
        if quote is None:
            quote = await self.market_data.get_quote(position)
        bad = self._quote_problem(quote)
        if bad:
            await self._block(position, decision, f"unusable quote: {bad}")
            return None

        # 4. Limit price.
        ex = self.settings.execution
        limit = compute_limit_price(
            quote, buffer_pct=ex.limit_buffer_pct, slippage_cap_pct=ex.slippage_cap_pct
        )
        if limit is None:
            await self._block(position, decision, "could not compute a limit price")
            return None

        # 2. Live-arming gate.
        is_paper = self.broker.capabilities().is_paper
        if not is_paper and not self.settings.is_live:
            await self._block(
                position, decision,
                f"NOT ARMED (mode={self.settings.mode}, i_understand_live_trading="
                f"{self.settings.i_understand_live_trading}); would buy-to-close "
                f"{position.quantity}x {position.occ_symbol} @ {limit:.2f}",
                status=DecisionStatus.PROPOSED,  # leave it actionable once armed
            )
            return None

        # 5. Persist-before-submit (idempotent on a deterministic client_order_id).
        order = Order(
            decision_id=decision.id,
            position_id=position.id,
            occ_symbol=position.occ_symbol,
            option_id=position.option_id,
            quantity=position.quantity,
            limit_price=limit,
            is_paper=is_paper,
            client_order_id=f"close-{decision.id}",
        )
        try:
            self._prepare_submit(order, quote)
        except NonLimitOrderRefused as exc:
            await self._refuse_non_limit(
                exc, order, decision_id=decision.id, position_id=position.id
            )
            self.decisions.set_status(decision.id, DecisionStatus.FAILED)
            return None
        self.decisions.set_order_snapshot(decision.id, **pricing_kwargs(order))
        if not self.orders.insert_if_new(order):
            existing = self.orders.get_by_client_order_id(order.client_order_id)
            if existing is not None:
                if existing.status == OrderStatus.FILLED:
                    return existing  # already done; never double-submit
                order = existing     # resume an in-flight order

        self.decisions.set_status(decision.id, DecisionStatus.EXECUTING)
        self.positions.set_status(position.id, PositionStatus.CLOSING)
        self.audit.record(
            AuditEventType.ORDER_SUBMIT,
            {"occ": order.occ_symbol, "qty": order.quantity, "limit": order.limit_price,
             "limit_price": order.limit_price, "order_type": pricing_kwargs(order)["order_type"],
             "bid": order.bid, "ask": order.ask, "mid": order.mid,
             "time_in_force": order.time_in_force,
             "is_paper": order.is_paper, "client_order_id": order.client_order_id},
            source="executor", position_id=position.id,
            decision_id=decision.id, order_id=order.id,
        )

        # 6. Submit + await fill.
        try:
            order.submitted_at = order.submitted_at or utcnow()
            submitted = await self.broker.submit_close_order(order)
        except Exception as exc:  # noqa: BLE001
            log.exception("submit_close_order failed: %s", exc)
            order.status = OrderStatus.REJECTED
            order.last_status_at = utcnow()
            self.orders.update(order)
            self.decisions.set_status(decision.id, DecisionStatus.FAILED)
            self.audit.record(
                AuditEventType.ERROR, {"where": "executor.submit", **describe_exception(exc)},
                source="executor", position_id=position.id,
                decision_id=decision.id, order_id=order.id,
            )
            await self._notify("Close FAILED", f"{position.occ_symbol}: submit error: {exc}",
                               priority="high")
            return order

        submitted.last_status_at = utcnow()
        self.orders.update(submitted)
        final = await self._await_fill(submitted, position)

        # 7. Settle.
        if final.status == OrderStatus.FILLED:
            self.positions.set_status(position.id, PositionStatus.CLOSED)
            self.decisions.set_status(decision.id, DecisionStatus.DONE)
            self.audit.record(
                AuditEventType.ORDER_FILL,
                {"occ": final.occ_symbol, "filled_qty": final.filled_qty,
                 "avg_fill_price": final.avg_fill_price, "is_paper": final.is_paper},
                source="executor", position_id=position.id,
                decision_id=decision.id, order_id=final.id,
            )
            self._journal_outcome(position, final, decision.rule_name)
            tag = "(paper)" if final.is_paper else "(LIVE)"
            await self._notify(
                f"Closed {position.underlying} {tag}",
                f"{decision.rule_name}: bought to close {final.filled_qty}x "
                f"{position.occ_symbol} @ {final.avg_fill_price}",
                priority="high",
            )
        else:
            cancelled = await self._cancel_working(final)
            if cancelled is not None:
                final = cancelled
            self.decisions.set_status(decision.id, DecisionStatus.FAILED)
            await self._notify(
                "Close did not fill",
                f"{position.occ_symbol}: order {final.status.value} after "
                f"{ex.fill_timeout_seconds}s (broker_id={final.broker_order_id}); "
                f"working close cancelled.",
                priority="high",
            )
        return final

    # ------------------------------------------------------------------ open (entry)
    async def execute_open(
        self, decision: EntryDecision, quote: OptionContractQuote
    ) -> Order | None:
        """Execute a sell-to-open CSP for ``decision`` using the fresh contract ``quote``.

        Same safety pipeline as execute_close, plus a real-time-feed gate: live entry is
        refused on a delayed feed. The opened position is NOT created here — reconcile
        discovers it from the broker and the existing close rules then manage it.
        """
        if self.entry_decisions is None:
            raise RuntimeError("execute_open requires an EntryDecisionStore.")

        # 1. Kill switch.
        if self.killswitch.is_paused():
            await self._block_entry(decision, "killswitch engaged; entry suppressed")
            return None
        hours_block = self._order_window_block()
        if hours_block:
            await self._block_entry(decision, hours_block)
            return None

        # Always try a cache-bypassing quote first — chain cache as_of timestamps are the
        # original fetch time, so a 15–30 minute scan would otherwise look "stale" forever.
        quote = await self._fresh_open_quote(decision, quote)

        # 3. Quote stale/validity guard.
        bad = self._open_quote_problem(quote)
        if bad:
            await self._block_entry(decision, f"unusable quote: {bad}")
            return None

        # 4. Limit price (credit).
        ex = self.settings.execution
        limit = compute_open_limit_price(
            quote, buffer_pct=ex.limit_buffer_pct, slippage_cap_pct=ex.slippage_cap_pct
        )
        if limit is None:
            await self._block_entry(decision, "could not compute a limit price")
            return None

        is_paper = self.broker.capabilities().is_paper
        # 2. Live-arming gate.
        if not is_paper and not self.settings.is_live:
            await self._block_entry(
                decision,
                f"NOT ARMED (mode={self.settings.mode}); would sell-to-open "
                f"{decision.contracts}x {decision.occ_symbol} @ {limit:.2f}",
                status=DecisionStatus.PROPOSED,
            )
            return None
        # 2b. Real-time feed gate — never auto-enter live off a delayed feed.
        if not is_paper and self.settings.is_live and not getattr(
            self.market_data, "is_realtime", False
        ):
            await self._block_entry(
                decision, "live entry requires a real-time (OPRA) feed; refusing on delayed data"
            )
            return None

        # 5. Persist-before-submit (idempotent). No position yet — reconcile creates it.
        order = Order(
            decision_id=decision.id,
            position_id="",
            occ_symbol=decision.occ_symbol,
            option_id=decision.option_id,
            quantity=decision.contracts,
            limit_price=limit,
            is_paper=is_paper,
            client_order_id=f"open-{decision.id}",
            side="SELL_TO_OPEN",
        )
        try:
            self._prepare_submit(order, quote)
        except NonLimitOrderRefused as exc:
            await self._refuse_non_limit(exc, order, decision_id=decision.id)
            self.entry_decisions.set_status(decision.id, DecisionStatus.FAILED)
            return None
        self.entry_decisions.set_order_snapshot(decision.id, **pricing_kwargs(order))
        if not self.orders.insert_if_new(order):
            existing = self.orders.get_by_client_order_id(order.client_order_id)
            if existing is not None:
                if existing.status == OrderStatus.FILLED:
                    return existing
                order = existing

        self.entry_decisions.set_status(decision.id, DecisionStatus.EXECUTING)
        self.audit.record(
            AuditEventType.ORDER_SUBMIT,
            {"open": True, "occ": order.occ_symbol, "qty": order.quantity,
             "limit": order.limit_price, "limit_price": order.limit_price,
             "order_type": pricing_kwargs(order)["order_type"],
             "bid": order.bid, "ask": order.ask, "mid": order.mid,
             "time_in_force": order.time_in_force, "is_paper": order.is_paper},
            source="executor", decision_id=decision.id, order_id=order.id,
        )

        # 6. Submit + await fill.
        try:
            order.submitted_at = order.submitted_at or utcnow()
            submitted = await self.broker.submit_open_order(order)
        except Exception as exc:  # noqa: BLE001
            log.exception("submit_open_order failed: %s", exc)
            order.status = OrderStatus.REJECTED
            self.orders.update(order)
            self.entry_decisions.set_status(decision.id, DecisionStatus.FAILED)
            self.audit.record(
                AuditEventType.ERROR, {"where": "executor.open", **describe_exception(exc)},
                source="executor", decision_id=decision.id, order_id=order.id,
            )
            await self._notify("Entry FAILED", f"{decision.occ_symbol}: submit error: {exc}",
                               priority="high")
            return order

        submitted.last_status_at = utcnow()
        self.orders.update(submitted)
        final = await self._await_open_fill(submitted)

        # 7. Settle.
        if final.status == OrderStatus.FILLED:
            self.entry_decisions.set_status(decision.id, DecisionStatus.DONE)
            self.audit.record(
                AuditEventType.ORDER_FILL,
                {"open": True, "occ": final.occ_symbol, "filled_qty": final.filled_qty,
                 "avg_fill_price": final.avg_fill_price, "is_paper": final.is_paper},
                source="executor", decision_id=decision.id, order_id=final.id,
            )
            tag = "(paper)" if final.is_paper else "(LIVE)"
            await self._notify(
                f"Opened CSP {decision.underlying} {tag}",
                f"{decision.rule_name}: sold to open {final.filled_qty}x "
                f"{decision.occ_symbol} @ {final.avg_fill_price}",
                priority="high",
            )
        else:
            # Order-state confirmation is flaky on the RH MCP — a sub-second fill may not report
            # "filled" via get_order in time (verified live 2026-07-22: a filled entry was mislabeled
            # FAILED). Before declaring failure (and cancelling — which could hit a real fill), check
            # the AUTHORITATIVE position read: if the short is actually open at the broker, it filled.
            opened = False
            try:
                opened = any(p.occ_symbol == decision.occ_symbol
                             for p in await self.broker.get_open_positions())
            except Exception:  # noqa: BLE001 — fall through to the not-filled path
                pass
            if opened:
                self.entry_decisions.set_status(decision.id, DecisionStatus.DONE)
                self.audit.record(
                    AuditEventType.ORDER_FILL,
                    {"open": True, "occ": decision.occ_symbol, "confirmed_via": "position_read"},
                    source="executor", decision_id=decision.id, order_id=final.id,
                )
                await self._notify(
                    f"Opened CSP {decision.underlying} (LIVE)",
                    f"{decision.occ_symbol}: fill confirmed via position read (order-state lagged).",
                    priority="high",
                )
            else:
                try:
                    await self.broker.cancel_order(final)
                except Exception:  # noqa: BLE001
                    pass
                self.entry_decisions.set_status(decision.id, DecisionStatus.FAILED)
                await self._notify(
                    "Entry did not fill",
                    f"{decision.occ_symbol}: order {final.status.value} after "
                    f"{ex.fill_timeout_seconds}s.",
                    priority="normal",
                )
        return final

    async def _await_open_fill(self, order: Order) -> Order:
        """Poll an open order to fill/terminal/timeout. No reprice (re-scan next cycle)."""
        ex = self.settings.execution
        deadline = time.monotonic() + ex.fill_timeout_seconds
        while True:
            if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
                return order
            if time.monotonic() >= deadline:
                return order
            await asyncio.sleep(self.poll_interval)
            try:
                order = await self.broker.get_order(order)
            except Exception as exc:  # noqa: BLE001
                log.warning("get_order failed during open fill poll: %s", exc)
                continue
            order.last_status_at = utcnow()
            self.orders.update(order)

    def _open_quote_problem(self, quote: OptionContractQuote | None) -> str | None:
        if quote is None:
            return "no quote"
        if not quote.is_valid:
            return "invalid (missing/zero bid-ask)"
        if quote.is_stale(self.settings.max_quote_age_seconds):
            return f"stale ({quote.age_seconds():.0f}s > {self.settings.max_quote_age_seconds}s)"
        return None

    async def _block_entry(
        self, decision: EntryDecision, reason: str, status: DecisionStatus | None = None
    ) -> None:
        log.warning("Entry suppressed for %s: %s", decision.occ_symbol, reason)
        if status is not None and self.entry_decisions is not None:
            self.entry_decisions.set_status(decision.id, status)
        self.audit.record(
            AuditEventType.DECISION,
            {"open": True, "executed": False, "reason": reason, "occ": decision.occ_symbol,
             "rule": decision.rule_name},
            source="executor", decision_id=decision.id,
        )
        await self._notify(
            f"Entry NOT placed: {decision.underlying}", f"{decision.rule_name} — {reason}",
            priority="normal",
        )

    # ------------------------------------------------------------------ fill loop
    async def _await_fill(self, order: Order, position: Position) -> Order:
        """Poll the broker until the order fills, is terminally rejected/cancelled, or the
        fill timeout elapses. On no-fill past ``reprice_after_seconds`` it re-prices once
        (cancel + resubmit at a higher limit, still within the slippage cap)."""
        ex = self.settings.execution
        deadline = time.monotonic() + ex.fill_timeout_seconds
        repriced = False

        while True:
            if order.status == OrderStatus.FILLED:
                return order
            if order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                return order
            if time.monotonic() >= deadline:
                return order

            await asyncio.sleep(self.poll_interval)
            try:
                refreshed = await self.broker.get_order(order)
            except Exception as exc:  # noqa: BLE001
                log.warning("get_order failed during fill poll: %s", exc)
                continue
            order = refreshed
            order.last_status_at = utcnow()
            self.orders.update(order)

            elapsed = ex.fill_timeout_seconds - (deadline - time.monotonic())
            if (
                not repriced
                and elapsed >= ex.reprice_after_seconds
                and order.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL)
            ):
                repriced = True
                order = await self._reprice(order, position)
        # unreachable

    async def _reprice(self, order: Order, position: Position) -> Order:
        """Cancel the working order and resubmit the unfilled remainder at a fresh, higher
        limit (still slippage-capped). Returns the new/updated order to keep polling."""
        quote = await self.market_data.get_quote(position)
        if self._quote_problem(quote):
            return order  # can't safely reprice without a good quote; keep waiting
        ex = self.settings.execution
        new_limit = compute_limit_price(
            quote, buffer_pct=ex.limit_buffer_pct, slippage_cap_pct=ex.slippage_cap_pct,
            toward_fill=1.0,
        )
        if new_limit is None or new_limit <= order.limit_price:
            return order
        cancelled = await self._cancel_working(order)
        if cancelled is None or cancelled.status not in (
            OrderStatus.CANCELLED, OrderStatus.REJECTED,
        ):
            log.warning("reprice aborted for %s: cancel not confirmed (status=%s)",
                        order.occ_symbol, getattr(cancelled, "status", None))
            return cancelled or order
        order = cancelled

        remaining = max(order.quantity - order.filled_qty, 0) or order.quantity
        new_order = Order(
            decision_id=order.decision_id,
            position_id=order.position_id,
            occ_symbol=order.occ_symbol,
            option_id=order.option_id,
            quantity=remaining,
            limit_price=new_limit,
            is_paper=order.is_paper,
            client_order_id=f"{order.client_order_id}-r1",
        )
        try:
            self._prepare_submit(new_order, quote)
        except NonLimitOrderRefused as exc:
            await self._refuse_non_limit(
                exc, new_order, decision_id=order.decision_id, position_id=position.id
            )
            return order
        self.orders.insert_if_new(new_order)
        self.audit.record(
            AuditEventType.ORDER_SUBMIT,
            {"reprice": True, "from": order.limit_price, "to": new_limit, "qty": remaining,
             "order_type": pricing_kwargs(new_order)["order_type"],
             "limit_price": new_order.limit_price, "bid": new_order.bid, "ask": new_order.ask,
             "mid": new_order.mid, "time_in_force": new_order.time_in_force},
            source="executor", position_id=position.id,
            decision_id=order.decision_id, order_id=new_order.id,
        )
        new_order.submitted_at = utcnow()
        try:
            submitted = await self.broker.submit_close_order(new_order)
        except Exception as exc:  # noqa: BLE001 — leave the cancelled original; do not half-replace
            log.exception("reprice submit failed: %s", exc)
            new_order.status = OrderStatus.REJECTED
            new_order.last_status_at = utcnow()
            self.orders.update(new_order)
            return order
        submitted.last_status_at = utcnow()
        self.orders.update(submitted)
        return submitted

    # ------------------------------------------------------------------ helpers
    def _is_paper_broker(self) -> bool:
        try:
            return bool(self.broker.capabilities().is_paper)
        except Exception:  # noqa: BLE001
            return False

    def _order_window_block(self) -> str | None:
        """Hard clock gate for every entry and exit, paper or live."""
        start = self.settings.trading_start
        if is_order_window(start=start):
            return None
        return (f"before trading start ({start} America/New_York); "
                f"no entries, exits, or stop-loss orders yet")

    def open_block_reason(
        self, decision: EntryDecision, quote: OptionContractQuote | None
    ) -> str | None:
        """Why execute_open would refuse *before* submitting. Used to avoid half-rolls."""
        if self.killswitch.is_paused():
            return "killswitch engaged; entry suppressed"
        hours = self._order_window_block()
        if hours:
            return hours
        bad = self._open_quote_problem(quote)
        if bad:
            return f"unusable quote: {bad}"
        ex = self.settings.execution
        if compute_open_limit_price(
            quote, buffer_pct=ex.limit_buffer_pct, slippage_cap_pct=ex.slippage_cap_pct
        ) is None:
            return "could not compute a limit price"
        if not self._is_paper_broker() and not self.settings.is_live:
            return (f"NOT ARMED (mode={self.settings.mode}); would sell-to-open "
                    f"{decision.contracts}x {decision.occ_symbol}")
        if (not self._is_paper_broker() and self.settings.is_live
                and not getattr(self.market_data, "is_realtime", False)):
            return "live entry requires a real-time feed; refusing on delayed data"
        return None

    async def _fresh_open_quote(
        self, decision: EntryDecision, quote: OptionContractQuote | None
    ) -> OptionContractQuote | None:
        """Prefer a cache-bypassing quote; fall back to get_chain when the caller is stale."""
        try:
            fresh = await self.market_data.get_fresh_contract_quote(decision.occ_symbol)
            if fresh is not None:
                return fresh
        except Exception as exc:  # noqa: BLE001
            log.warning("execute_open fresh quote failed for %s: %s", decision.occ_symbol, exc)
        if quote is None or quote.is_stale(self.settings.max_quote_age_seconds):
            try:
                chain = await self.market_data.get_chain(decision.underlying)
                hit = next((c for c in chain if c.occ_symbol == decision.occ_symbol), None)
                if hit is not None:
                    return hit
            except Exception as exc:  # noqa: BLE001
                log.warning("execute_open quote refetch failed for %s: %s",
                            decision.occ_symbol, exc)
        return quote

    async def _cancel_working(self, order: Order) -> Order | None:
        """Cancel a working order and confirm the broker actually dropped it.

        Returns the refreshed order when cancel was confirmed (CANCELLED/REJECTED) or the
        order filled during cancel. Returns None if cancel could not be confirmed — callers
        must not submit a replacement in that case.
        """
        if not order.broker_order_id and order.status in (
            OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIAL,
        ):
            try:
                await self.broker.cancel_order(order)
            except Exception as exc:  # noqa: BLE001
                log.warning("cancel failed for %s: %s", order.client_order_id, exc)
                return None
        elif order.broker_order_id:
            try:
                await self.broker.cancel_order(order)
            except Exception as exc:  # noqa: BLE001
                log.warning("cancel failed for %s: %s", order.client_order_id, exc)
                return None
        else:
            return order if order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED,
                                             OrderStatus.FILLED) else None
        try:
            refreshed = await self.broker.get_order(order)
        except Exception as exc:  # noqa: BLE001
            log.warning("get_order after cancel failed for %s: %s", order.client_order_id, exc)
            return None
        refreshed.last_status_at = utcnow()
        self.orders.update(refreshed)
        if refreshed.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.FILLED):
            return refreshed
        return None

    def _quote_problem(self, quote: OptionQuote | None) -> str | None:
        if quote is None:
            return "no quote"
        if not quote.is_valid:
            return "invalid (missing/zero bid-ask)"
        if quote.is_stale(self.settings.max_quote_age_seconds):
            return f"stale ({quote.age_seconds():.0f}s > {self.settings.max_quote_age_seconds}s)"
        return None

    async def _block(
        self,
        position: Position,
        decision: CloseDecision,
        reason: str,
        status: DecisionStatus | None = None,
    ) -> None:
        """Record + notify that an auto-close was not placed, and (optionally) set status."""
        log.warning("Close suppressed for %s: %s", position.occ_symbol, reason)
        if status is not None:
            self.decisions.set_status(decision.id, status)
        self.audit.record(
            AuditEventType.DECISION,
            {"executed": False, "reason": reason, "occ": position.occ_symbol,
             "rule": decision.rule_name},
            source="executor", position_id=position.id, decision_id=decision.id,
        )
        await self._notify(
            f"Close NOT placed: {position.underlying}",
            f"{decision.rule_name} — {reason}",
            priority="normal",
        )

    async def _notify(self, title: str, message: str, *, priority: str = "normal") -> None:
        if self.notifier is not None:
            await self.notifier.send(title, message, priority=priority)

    def _journal_outcome(self, position: Position, close_order: Order, exit_reason: str) -> None:
        """Backfill the open trade-journal row for this position with realized outcome."""
        if self.trade_journal is None:
            return
        je = self.trade_journal.find_open_by_occ(position.occ_symbol)
        if je is None:
            return
        position.status = PositionStatus.CLOSED  # so position_pnl computes the realized branch
        info = position_pnl(position, close_order)
        snap = pricing_kwargs(close_order)
        self.trade_journal.set_outcome(
            je.id, status=info["outcome"], realized_pnl=info["realized_pnl"],
            close_price=info["close_price"], exit_reason=exit_reason, entered_at=je.entered_at,
            mfe_pct=position.peak_profit_pct, mae_pct=position.trough_profit_pct,
            close_order_type=snap["order_type"], close_limit_price=snap["limit_price"],
            close_bid=snap["bid"], close_ask=snap["ask"], close_mid=snap["mid"],
            close_time_in_force=snap["time_in_force"],
        )

    def _prepare_submit(self, order: Order, quote) -> None:
        """Stamp the pricing quote and refuse anything that isn't a limit order."""
        stamp_order_quote(order, quote)
        assert_limit_only(
            order.order_type, where="executor", occ=order.occ_symbol,
            client_order_id=order.client_order_id,
        )

    async def _refuse_non_limit(
        self, exc: NonLimitOrderRefused, order: Order, *,
        decision_id: str, position_id: str | None = None,
    ) -> None:
        """Log loudly + audit ERROR so /api/ops last_error surfaces the refusal."""
        log.error("%s", exc)
        self.audit.record(
            AuditEventType.ERROR,
            {"where": "executor.limit_only", **describe_exception(exc),
             "order_type": order.order_type, "occ": order.occ_symbol,
             "client_order_id": order.client_order_id},
            source="executor", position_id=position_id, decision_id=decision_id, order_id=order.id,
        )
        await self._notify(
            "LIMIT-ONLY RULE: order refused",
            str(exc),
            priority="high",
        )
