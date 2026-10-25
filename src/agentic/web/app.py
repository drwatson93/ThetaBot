"""FastAPI app factory.

``create_app(deps)`` builds the application from a ``WebDeps`` bundle so the same wiring
is used by ``main.py`` (live) and tests (via ``fastapi.testclient``). fastapi is an
optional dependency (the ``web`` extra); importing this module requires it, but the core
service runs without it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from ..config import Settings
from ..services.killswitch import KillSwitch
from ..store.audit import AuditStore
from ..store.decisions import DecisionStore
from ..store.entry_decisions import EntryDecisionStore
from ..store.positions import PositionStore
from ..store.signals import SignalStore
from ..store.trade_journal import TradeJournalStore
from ..store.tv_indicators import TVIndicatorStore
from .control import make_control_router
from .dashboard import make_dashboard_router
from .health import build_health
from .settings import make_settings_router
from .webhook import make_webhook_router


@dataclass
class WebDeps:
    settings: Settings
    signals: SignalStore
    killswitch: KillSwitch
    approval_gate: Any | None
    audit: AuditStore
    positions: PositionStore
    orders: Any | None
    decisions: DecisionStore
    entry_decisions: EntryDecisionStore | None = None
    scanner: Any | None = None   # OpportunityScanner (loose type to avoid an import cycle)
    trade_journal: TradeJournalStore | None = None
    tv_indicators: TVIndicatorStore | None = None
    ai_reviews: Any | None = None   # store.ai_reviews.AIReviewStore
    notifier: Any | None = None     # notify.base.Notifier (for /control/test-notify)
    entry_candidates: Any | None = None  # store.entry_candidates.EntryCandidateStore
    news: Any | None = None         # store.news.NewsStore (advisory news/catalyst channel)
    briefs: Any | None = None       # store.briefs.BriefStore (saved weekly briefs)
    practice: dict | None = None     # read-only snapshot (data source + RH connect)
    trade_alerts: Any | None = None  # notify.trade_alerts.TradeAlerts


def create_app(deps: WebDeps) -> FastAPI:
    app = FastAPI(title="AgenticRobinhood", version="0.1.0")

    @app.get("/health")
    async def health() -> dict:
        # 200 whenever the process is up. A closed market or an old quote is a field.
        return build_health(deps)

    app.include_router(make_webhook_router(deps))
    app.include_router(make_control_router(deps))
    app.include_router(make_settings_router(deps))
    app.include_router(make_dashboard_router(deps))
    return app
