"""Monitor loop: read the broker book, price it, and evaluate exit rules.

Each cycle reads option positions from the broker and keeps only contracts the
broker still reports that have not expired in America/New_York. Quotes and exit
rules run on that list. Nothing is written to the positions table. A failed
broker read skips the cycle instead of reusing a stored book.
"""
from __future__ import annotations

import asyncio
import logging

from ..config import Settings
from ..domain.enums import AuditEventType, DecisionStatus
from ..domain.models import utcnow
from ..marketdata.base import MarketDataProvider
from ..brokers.base import ExecutionBroker
from ..notify.base import Notifier
from ..notify.trade_alerts import build_exit_alert_payload, sanitize_error
from ..store.audit import AuditStore
from ..store.decisions import DecisionStore
from ..store.positions import PositionStore
from .killswitch import KillSwitch
from .live_positions import attach_quote, fetch_broker_positions, select_live, snapshot
from .market_hours import is_market_hours, is_order_window

log = logging.getLogger("agentic.monitor")


class MonitorLoop:
    def __init__(
        self,
        settings: Settings,
        broker: ExecutionBroker,
        market_data: MarketDataProvider,
        positions: PositionStore,
        audit: AuditStore,
        killswitch: KillSwitch,
        rules_engine=None,        # set in Phase 1
        decisions: DecisionStore | None = None,  # set in Phase 1
        notifier: Notifier | None = None,        # set in Phase 1
        executor=None,            # set in Phase 2
        signal_processor=None,    # set in Phase 3
        approval_gate=None,       # set in Phase 3
        roll_manager=None,        # tested-put roll defense
        trade_alerts=None,        # instant TRADE_ALERT_URL webhook; optional
    ):
        self.settings = settings
        self.broker = broker
        self.market_data = market_data
        self.positions = positions
        self.audit = audit
        self.killswitch = killswitch
        self.rules_engine = rules_engine
        self.decisions = decisions
        self.notifier = notifier
        self.executor = executor
        self.signal_processor = signal_processor
        self.approval_gate = approval_gate
        self.roll_manager = roll_manager
        self.trade_alerts = trade_alerts
        self._stop = asyncio.Event()

    async def run(self) -> None:
        log.info("Monitor loop started (broker=%s).", self.broker.capabilities().name)
        while not self._stop.is_set():
            try:
                await self.run_once()
                self.killswitch.record_success()
            except Exception as exc:  # noqa: BLE001 — a bad cycle must not kill the loop
                log.exception("Monitor cycle error: %s", exc)
                self.audit.record(AuditEventType.ERROR, {"where": "monitor", "error": str(exc)})
                self.killswitch.record_broker_error("monitor")
            delay = (
                self.settings.poll_interval_seconds
                if is_market_hours()
                else self.settings.poll_interval_closed_seconds
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> int:
        """One monitor cycle. Returns how many held, unexpired contracts were seen.

        A failed broker read raises so the loop skips this cycle. The positions
        table is not consulted and not updated.
        """
        # Pick up any live rule edits (settings API) without a restart.
        if self.rules_engine is not None:
            self.rules_engine.refresh(self.settings.rules)
        # Raises LiveBookError when the read fails. Do not substitute stored rows.
        broker_positions = await self._open_positions()
        live = [snapshot(p) for p in select_live(broker_positions)]
        # Exit rules run only in the order window. After hours a quote is not
        # actionable, and repeating the alert every closed-market poll is noise.
        can_trade = is_order_window(start=self.settings.trading_start)
        for pos in live:
            quote = await attach_quote(pos, self.market_data)
            if can_trade and self.rules_engine is not None and self.decisions is not None:
                await self._evaluate(pos, quote)

        self.audit.record(
            AuditEventType.POLL,
            {"open_positions": len(live),
             "symbols": [p.occ_symbol for p in live],
             "paused": self.killswitch.is_paused()},
            source="monitor",
        )

        # A signal close waits for the order window. Expiring alerts that arrived
        # while tv-signal is off does not: otherwise they stay NEW until the window
        # opens and close if the rule was turned back on in between.
        if self.signal_processor is not None:
            from .signal_processor import signal_rule_is_enabled
            if not signal_rule_is_enabled(self.settings):
                self.signal_processor._expire_while_disabled()
            elif can_trade:
                await self.signal_processor.process_pending(live)

        log.info("Polled %d open position(s).", len(live))
        return len(live)

    async def _open_positions(self):
        """Open option positions for this cycle, from the broker only.

        The Robinhood session resolves ``account_number`` (config, else get_accounts)
        and the monitor passes that value through. A book with no resolver (the test
        paper book) is read with no argument. A blank account is not sent to
        ``get_option_positions``. A failed read raises; it does not return stored rows.
        """
        return await fetch_broker_positions(self.broker)

    async def _evaluate(self, pos, quote) -> None:
        """Evaluate rules for one position.

        New decisions are persisted (dedup'd), audited, and notified. Auto rules
        (``requires_approval`` false, not notify-only) go to the executor. When
        several of those fire in one tick, the first in config order is the one
        that may close. Approval-gated and notify-only decisions are not closes:
        they are still notified, and they do not suppress an auto rule. A close
        that already failed today is retried once the guard is clear.
        """
        decisions = self._one_close_per_tick(pos, self.rules_engine.evaluate(pos, quote))
        for decision in decisions:
            action = self._admit_decision(decision)
            if action == "skip":
                continue
            if action == "new":
                self.audit.record(
                    AuditEventType.DECISION,
                    {"rule": decision.rule_name, "reason": decision.reason,
                     "requires_approval": decision.requires_approval,
                     "occ": pos.occ_symbol},
                    source="monitor",
                    position_id=pos.id,
                    decision_id=decision.id,
                )
            else:
                self.audit.record(
                    AuditEventType.DECISION,
                    {"retry": True, "rule": decision.rule_name, "occ": pos.occ_symbol,
                     "reason": "prior close was rejected or cancelled"},
                    source="monitor",
                    position_id=pos.id,
                    decision_id=decision.id,
                )
            # Decisions are recorded and notified. Nothing here places an order.
            await self._notify_decision(pos, decision)

    def _executes_close(self, decision) -> bool:
        """A rule that will actually send a buy-to-close this tick.

        Approval-gated rules wait for a person. Notify-only alerts never send.
        Neither one should take the single close slot from an auto rule.
        """
        return not decision.notify_only and not decision.requires_approval

    def _one_close_per_tick(self, pos, decisions):
        """Pick one executable close per position per tick.

        Winner is the first decision in the list that will actually execute.
        That list is the rules-engine order, which is config order. Approval
        and alert decisions stay in the list so they still notify. With no
        executor wired, every decision is still recorded (nothing submits).
        """
        if self.executor is None:
            return decisions
        executable = [(i, d) for i, d in enumerate(decisions) if self._executes_close(d)]
        if len(executable) <= 1:
            return decisions
        chosen_at, chosen = executable[0]
        for _index, skipped in executable[1:]:
            self._audit_suppressed(pos, skipped, chosen)
        return [
            d for i, d in enumerate(decisions)
            if i == chosen_at or not self._executes_close(d)
        ]

    def _audit_suppressed(self, pos, skipped, chosen) -> None:
        """Log a suppressed auto-close once per position, rule, and UTC day."""
        if self._suppressed_already(pos.id, skipped.rule_name):
            return
        log.info(
            "Close suppressed for %s: rule %s skipped; %s already selected this tick",
            pos.occ_symbol, skipped.rule_name, chosen.rule_name,
        )
        self.audit.record(
            AuditEventType.DECISION,
            {"suppressed": True, "rule": skipped.rule_name, "kept": chosen.rule_name,
             "occ": pos.occ_symbol,
             "reason": "another exit rule already selected this tick"},
            source="monitor", position_id=pos.id,
        )

    def _suppressed_already(self, position_id: str, rule_name: str) -> bool:
        day = utcnow().date().isoformat()
        row = self.audit.db.conn.execute(
            """SELECT 1 FROM audit
               WHERE source = 'monitor' AND position_id = ?
                 AND event_type = ?
                 AND substr(ts, 1, 10) = ?
                 AND json_extract(payload, '$.suppressed') = 1
                 AND json_extract(payload, '$.rule') = ?
               LIMIT 1""",
            (position_id, AuditEventType.DECISION.value, day, rule_name),
        ).fetchone()
        return row is not None

    def _admit_decision(self, decision) -> str:
        """``new`` if this is the first time today, ``retry`` if the prior close failed,
        ``skip`` if it was already handled."""
        if self.decisions.insert_if_new(decision):
            return "new"
        if self.executor is None or decision.notify_only or decision.requires_approval:
            return "skip"
        existing = self.decisions.get_by_dedup(decision.dedup_key)
        if existing is None or existing.status != DecisionStatus.FAILED:
            return "skip"
        # Same decision row. The executor mints a new client_order_id when the
        # last close was cancelled or rejected; a never-submitted pending id is reused.
        decision.id = existing.id
        log.info(
            "Retrying close for %s rule %s after a rejected or cancelled close",
            decision.position_id, decision.rule_name,
        )
        return "retry"

    async def _notify_decision(self, pos, decision) -> None:
        if self.notifier is None:
            return
        if decision.notify_only:
            gate = "ALERT"
        elif decision.requires_approval:
            gate = "NEEDS APPROVAL"
        else:
            gate = "AUTO"
        title = f"Close signal: {pos.underlying} {pos.occ_symbol}"
        message = (
            f"[{gate}] {decision.rule_name}\n{decision.reason}\n"
            f"qty={pos.quantity} mark={pos.current_mark} dte={pos.dte()} "
            "(no order placed)"
        )
        priority = "high" if (not decision.requires_approval and not decision.notify_only) else "normal"
        await self.notifier.send(title, message, priority=priority)
        self.audit.record(
            AuditEventType.APPROVAL_REQ if decision.requires_approval else AuditEventType.DECISION,
            {"notified": True, "rule": decision.rule_name},
            source="monitor", position_id=pos.id, decision_id=decision.id,
        )
        if self.trade_alerts is None:
            return
        try:
            await self.trade_alerts.deliver_scan_alert(
                build_exit_alert_payload(pos, decision)
            )
        except Exception as exc:  # noqa: BLE001 — webhook must not break the monitor
            log.warning("exit alert dispatch failed: %s", sanitize_error(exc))

    def stop(self) -> None:
        self._stop.set()
