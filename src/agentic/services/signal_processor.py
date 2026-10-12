"""SignalProcessor: consume queued TradingView signals and route to close (Phase 3).

Runs each monitor cycle. For every NEW signal it: honors the TTL, matches the signal to
open positions (see SignalMatcher), creates a deduped CloseDecision per match, and routes
it — auto rules straight to the executor, approval-gated ones to the ApprovalGate.
"""
from __future__ import annotations

import logging

from ..config import Settings
from ..domain.enums import AuditEventType, RuleType, SignalStatus
from ..domain.models import CloseDecision, Signal, utcnow
from ..rules.signal_rule import SignalMatcher
from ..store.audit import AuditStore
from ..store.decisions import DecisionStore
from ..store.positions import PositionStore
from ..store.signals import SignalStore
from .approval import ApprovalGate
from .executor import OrderExecutor

log = logging.getLogger("agentic.signals")

SIGNAL_RULE_NAME = "tv-signal"


def _enabled_signal_rule(settings: Settings):
    """The live SIGNAL rule, or None when it is missing or turned off.

    Read from ``settings.rules`` on every pass. The processor keeps one Settings
    object, so a dashboard hot-edit is visible here without rebuilding it.
    """
    for rule in settings.rules:
        if rule.rule_type == "SIGNAL" and rule.enabled:
            return rule
    return None


def signal_rule_is_enabled(settings: Settings) -> bool:
    """True only when a SIGNAL rule is present and enabled right now."""
    return _enabled_signal_rule(settings) is not None


def _signal_base_approval(settings: Settings) -> bool:
    rule = _enabled_signal_rule(settings)
    if rule is None:
        return True  # safe default if something still asks; process_pending will not close
    return bool(rule.requires_approval)


class SignalProcessor:
    def __init__(
        self,
        settings: Settings,
        signals: SignalStore,
        positions: PositionStore,
        decisions: DecisionStore,
        executor: OrderExecutor,
        approval_gate: ApprovalGate,
        audit: AuditStore,
        matcher: SignalMatcher | None = None,
    ):
        self.settings = settings
        self.signals = signals
        self.positions = positions
        self.decisions = decisions
        self.executor = executor
        self.approval_gate = approval_gate
        self.audit = audit
        self.matcher = matcher or SignalMatcher(
            base_requires_approval=_signal_base_approval(settings)
        )

    async def process_pending(self) -> int:
        # Re-read the rule before every signal. A batch that starts enabled must
        # not close the rest if tv-signal is turned off halfway through.
        new = self.signals.list_by_status(SignalStatus.NEW)
        for sig in new:
            rule = _enabled_signal_rule(self.settings)
            if rule is None:
                self._expire_one(sig)
                continue
            self.matcher.base_requires_approval = bool(rule.requires_approval)
            try:
                await self._process_one(sig)
            except Exception as exc:  # noqa: BLE001 — one bad signal must not stall the rest
                log.exception("Signal %s processing failed: %s", sig.id, exc)
                self.audit.record(
                    AuditEventType.ERROR, {"where": "signal_processor", "error": str(exc)},
                    source="signals",
                )
        return len(new)

    def _expire_one(self, sig: Signal) -> None:
        """Drop one NEW alert. No match, no close decision, no executor, no approval."""
        if sig.status != SignalStatus.NEW:
            return
        self.signals.set_status(sig.id, SignalStatus.NO_MATCH)
        sig.status = SignalStatus.NO_MATCH
        self.audit.record(
            AuditEventType.SIGNAL,
            {"expired": True, "reason": "signal_rule_disabled", "dedup_key": sig.dedup_key},
            source="signals",
        )

    def _expire_while_disabled(self) -> None:
        """Mark every NEW signal so turning the rule back on does not replay them."""
        if signal_rule_is_enabled(self.settings):
            return
        for sig in self.signals.list_by_status(SignalStatus.NEW):
            self._expire_one(sig)

    async def _process_one(self, sig: Signal) -> None:
        if sig.ttl_expires_at is not None and utcnow() > sig.ttl_expires_at:
            self.signals.set_status(sig.id, SignalStatus.NO_MATCH)
            self.audit.record(
                AuditEventType.SIGNAL, {"expired": True, "dedup_key": sig.dedup_key},
                source="signals",
            )
            return

        open_positions = self.positions.list_open()
        matches = self.matcher.match(sig, open_positions)
        if not matches:
            self.signals.set_status(sig.id, SignalStatus.NO_MATCH)
            self.audit.record(
                AuditEventType.SIGNAL,
                {"matched": 0, "raw": sig.raw}, source="signals",
            )
            return

        for m in matches:
            decision = CloseDecision(
                position_id=m.position.id,
                rule_name=SIGNAL_RULE_NAME,
                rule_type=RuleType.SIGNAL,
                reason=m.reason,
                requires_approval=m.requires_approval,
                dedup_key=f"{m.position.id}:SIGNAL:{sig.dedup_key}",
            )
            if not self.decisions.insert_if_new(decision):
                continue  # already created from this alert
            self.audit.record(
                AuditEventType.DECISION,
                {"rule": SIGNAL_RULE_NAME, "occ": m.position.occ_symbol,
                 "requires_approval": m.requires_approval, "reason": m.reason},
                source="signals", position_id=m.position.id, decision_id=decision.id,
            )
            if m.requires_approval:
                await self.approval_gate.request(m.position, decision)
            else:
                await self.executor.execute_close(m.position, decision)

        self.signals.set_status(sig.id, SignalStatus.MATCHED)
