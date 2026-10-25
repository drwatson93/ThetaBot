"""Phase 0 end-to-end (paper): monitor sync + reconcile discovery/closed-detection."""
import pytest

from tests.fakes import PaperBroker
from agentic.config import Settings
from agentic.marketdata.base import PaperMarketData
from agentic.services.killswitch import KillSwitch
from agentic.services.monitor import MonitorLoop
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.positions import PositionStore


@pytest.fixture()
def wiring(tmp_path):
    db = Database(tmp_path / "test.db")
    audit = AuditStore(db)
    positions = PositionStore(db)
    killswitch = KillSwitch(db, audit)
    broker = PaperBroker()
    settings = Settings(mode="paper", broker="paper", market_data="paper")
    return db, audit, positions, killswitch, broker, settings


@pytest.mark.asyncio
async def test_monitor_syncs_positions_into_store(wiring):
    db, audit, positions, killswitch, broker, settings = wiring
    monitor = MonitorLoop(settings, broker, PaperMarketData(), positions, audit, killswitch)
    seen = await monitor.run_once()
    assert seen == 2
    stored = positions.list_open()
    assert len(stored) == 2
    # Quote enrichment populated marks.
    assert all(p.current_mark is not None for p in stored)
    # A POLL audit event was recorded.
    assert any(e["event_type"] == "POLL" for e in audit.recent())


def test_killswitch_persists(wiring):
    db, audit, positions, killswitch, broker, settings = wiring
    assert killswitch.is_paused() is False
    killswitch.pause("test")
    assert killswitch.is_paused() is True
    # New KillSwitch on same DB sees persisted state.
    assert KillSwitch(db, audit).is_paused() is True
    killswitch.resume("test")
    assert killswitch.is_paused() is False
