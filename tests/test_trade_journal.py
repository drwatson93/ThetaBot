"""Trade journal: store basics, IV-history upsert, entry capture, outcome backfill."""
from datetime import date, timedelta

import pytest

from tests.fakes import PaperBroker
from agentic.config import EntryConfig, EntryCriteria, Settings
from agentic.domain.enums import Direction, OptionType, OrderStatus, PositionStatus, Strategy
from agentic.domain.models import EquityHolding, Order, Position, TradeJournalEntry, utcnow
from agentic.marketdata.base import MarketDataProvider, PaperMarketData
from agentic.marketdata.quote import OptionContractQuote
from tests.fakes import OrderExecutor
from agentic.services.killswitch import KillSwitch
from agentic.services.scanner import OpportunityScanner
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.entry_decisions import EntryDecisionStore
from tests.fakes import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.trade_journal import TradeJournalStore


def _stores(tmp_path, name="j.db"):
    db = Database(tmp_path / name)
    return (db, AuditStore(db), PositionStore(db), DecisionStore(db), OrderStore(db),
            EntryDecisionStore(db), TradeJournalStore(db), KillSwitch(db, AuditStore(db)))


def test_store_insert_find_outcome(tmp_path):
    *_, journal, _ks = _stores(tmp_path)
    e = TradeJournalEntry(occ_symbol="X1", underlying="X", kind="CSP", contracts=2,
                          strike=50.0, dte=10, delta=-0.25, premium=1.5)
    journal.insert(e)
    found = journal.find_open_by_occ("X1")
    assert found is not None and found.status == "open" and found.contracts == 2
    journal.set_outcome(found.id, status="win", realized_pnl=120.0, exit_reason="profit-50",
                        entered_at=found.entered_at)
    assert journal.find_open_by_occ("X1") is None       # no longer open
    assert journal.recent()[0].status == "win" and journal.recent()[0].realized_pnl == 120.0


def test_set_outcome_records_mfe_mae_into_context(tmp_path):
    *_, journal, _ks = _stores(tmp_path)
    e = TradeJournalEntry(occ_symbol="X2", underlying="X", kind="CSP", contracts=1,
                          strike=50.0, dte=10, delta=-0.25, premium=1.5,
                          context={"rsi": 55})           # pre-existing context is preserved
    journal.insert(e)
    fid = journal.find_open_by_occ("X2").id
    journal.set_outcome(fid, status="win", realized_pnl=75.0, entered_at=e.entered_at,
                        mfe_pct=0.82, mae_pct=-0.15)      # reached +82%, dipped to -15%
    ctx = journal.recent()[0].context
    assert ctx["mfe_pct"] == 0.82 and ctx["mae_pct"] == -0.15
    assert ctx["rsi"] == 55                              # original context not clobbered


def test_iv_history_upsert(tmp_path):
    *_, journal, _ks = _stores(tmp_path)
    journal.record_iv("X", date(2026, 6, 30), 0.45)
    journal.record_iv("X", date(2026, 6, 30), 0.50)   # same day -> upsert, not duplicate
    journal.record_iv("X", date(2026, 6, 29), 0.40)
    hist = journal.iv_history("X")
    assert len(hist) == 2
    assert dict(hist)["2026-06-30"] == 0.50
