"""A disabled tv-signal rule must not close, and must not replay after it is turned back on."""
from datetime import timedelta

import pytest

from agentic.config import RuleConfig, Settings
from agentic.domain.enums import Direction, OptionType, SignalStatus, Strategy
from agentic.domain.models import Position, Signal, utcnow
from agentic.rules.signal_rule import SignalMatcher
from agentic.services.signal_processor import SignalProcessor
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore


class _Exec:
    def __init__(self):
        self.closes = []

    async def execute_close(self, position, decision, quote=None):
        self.closes.append(decision)


class _Gate:
    def __init__(self):
        self.requests = []

    async def request(self, position, decision):
        self.requests.append(decision)


def _rule(*, enabled: bool, requires_approval: bool) -> RuleConfig:
    return RuleConfig(
        name="tv-signal", rule_type="SIGNAL", enabled=enabled,
        requires_approval=requires_approval, params={"match": "underlying"},
    )


def _pos() -> Position:
    return Position(
        occ_symbol="ZZTEST261218P00010000", underlying="ZZTEST", option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=1,
        strike=10.0, expiration=utcnow().date() + timedelta(days=14), credit_received=1.0,
    )


def _wire(tmp_path, *, enabled: bool, requires_approval: bool):
    db = Database(tmp_path / "sig.db")
    audit = AuditStore(db)
    positions = PositionStore(db)
    decisions = DecisionStore(db)
    signals = SignalStore(db)
    positions.upsert(_pos())
    settings = Settings(
        mode="paper", broker="paper", market_data="paper",
        rules=[_rule(enabled=enabled, requires_approval=requires_approval)],
    )
    executor = _Exec()
    gate = _Gate()
    processor = SignalProcessor(
        settings, signals, positions, decisions, executor, gate, audit,
        matcher=SignalMatcher(base_requires_approval=requires_approval),
    )
    return settings, signals, decisions, executor, gate, processor


def _queue(signals: SignalStore, dedup: str) -> Signal:
    sig = Signal(raw={"action": "close", "symbol": "ZZTEST"}, dedup_key=dedup)
    assert signals.insert_if_new(sig)
    return sig


@pytest.mark.asyncio
@pytest.mark.parametrize("requires_approval", [False, True])
async def test_enabled_signal_rule_routes_and_disabled_does_not(tmp_path, requires_approval):
    settings, signals, decisions, executor, gate, processor = _wire(
        tmp_path, enabled=True, requires_approval=requires_approval,
    )
    _queue(signals, "live-1")
    await processor.process_pending()
    assert len(decisions.recent()) == 1
    if requires_approval:
        assert len(gate.requests) == 1
        assert executor.closes == []
    else:
        assert len(executor.closes) == 1
        assert gate.requests == []

    # Hot edit on the shared Settings object, after the processor was built.
    settings.rules = [_rule(enabled=False, requires_approval=requires_approval)]
    queued = _queue(signals, "while-off")
    await processor.process_pending()
    assert len(executor.closes) == (0 if requires_approval else 1)
    assert len(gate.requests) == (1 if requires_approval else 0)
    assert len(decisions.recent()) == 1
    assert signals.get(queued.id).status == SignalStatus.NO_MATCH

    # Re-enable must not replay the signal that arrived while the rule was off.
    settings.rules = [_rule(enabled=True, requires_approval=requires_approval)]
    await processor.process_pending()
    assert len(decisions.recent()) == 1
    assert len(executor.closes) == (0 if requires_approval else 1)
    assert len(gate.requests) == (1 if requires_approval else 0)

    _queue(signals, "after-on")
    await processor.process_pending()
    assert len(decisions.recent()) == 2
    if requires_approval:
        assert len(gate.requests) == 2
        assert executor.closes == []
    else:
        assert len(executor.closes) == 2
        assert gate.requests == []
