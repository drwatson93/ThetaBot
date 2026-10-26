"""Entrypoint: build the app graph and run the monitor and scanner loops.

The process reads Robinhood and sends alerts. It does not place orders.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from .brokers.factory import build_broker, build_paper_runtime
from .config import Settings, load_config, require_runtime_secrets
from .logging_setup import setup_logging
from .marketdata.alpaca_md import AlpacaMarketData
from .marketdata.base import MarketDataProvider, PaperMarketData
from .marketdata.earnings import build_earnings_provider
from .marketdata.company_data import build_company_data
from .marketdata.news import build_news_provider
from .notify.factory import build_notifier
from .notify.trade_alerts import TradeAlerts
from .rules.engine import RulesEngine
from .services.killswitch import KillSwitch
from .services.monitor import MonitorLoop
from .services.reporting import ReportingLoop
from .services.scanner import OpportunityScanner
from .services.signal_processor import SignalProcessor
from .ai.client import build_reviewer_client
from .ai.reviewer import AIReviewer
from .store.ai_reviews import AIReviewStore
from .store.audit import AuditStore
from .store.db import Database
from .store.decisions import DecisionStore
from .store.entry_candidates import EntryCandidateStore
from .store.briefs import BriefStore
from .store.setup_events import SetupEventStore
from .store.entry_decisions import EntryDecisionStore
from .store.positions import PositionStore
from .store.signals import SignalStore
from .store.trade_journal import TradeJournalStore
from .store.news import NewsStore
from .store.tv_indicators import TVIndicatorStore

log = logging.getLogger("agentic.main")


async def connect_brokers(settings: Settings):
    """Return ``(execution, data_or_none, read_session_or_none)``.

    ``execution`` is what orders would use. ``data`` is a connected Robinhood
    session for chains and quotes, or None. ``read_session`` is that Robinhood
    session even when the probe failed, so the scanner can fail closed on it.
    """
    return await build_paper_runtime(settings)


def make_scanner(
    settings: Settings,
    broker,
    market_data,
    read_session,
    entry_decisions,
    executor,
    audit,
    killswitch,
    **kwargs,
):
    """Build the process scanner.

    When ``market_data`` is robinhood, account reads (buying power, open
    options, share holdings) and the health gate use ``read_session``. The
    execution broker stays the one orders would use.
    """
    read_broker = read_session if settings.market_data == "robinhood" else None
    return OpportunityScanner(
        settings, broker, market_data, entry_decisions, executor, audit, killswitch,
        read_broker=read_broker, **kwargs,
    )


def build_market_data(settings: Settings, broker=None) -> MarketDataProvider:
    if settings.market_data == "robinhood":
        # Reuse the trading broker's Robinhood MCP session for market data — one login, no Alpaca.
        from .marketdata.robinhood_md import RobinhoodMarketData
        if broker is not None and hasattr(broker, "_call_tool"):
            return RobinhoodMarketData(broker)
        log.warning("market_data=robinhood but the broker is not a Robinhood MCP broker; "
                    "falling back to paper data.")
        return PaperMarketData()
    if settings.market_data == "alpaca":
        return AlpacaMarketData(feed=settings.entry.feed)
    return PaperMarketData()


def build_web_server(settings, signals, killswitch, approval_gate, audit,
                     positions, orders, decisions, entry_decisions, scanner, trade_journal,
                     tv_indicators=None, ai_reviews=None, notifier=None, entry_candidates=None,
                     news=None, briefs=None, practice=None, trade_alerts=None):
    """Build a uvicorn Server for the control/webhook/dashboard API, or None if disabled."""
    if not settings.web.enabled:
        log.info("Web API disabled (web.enabled=false).")
        return None
    try:
        import uvicorn

        from .config import get_secret
        from .web.app import WebDeps, create_app
    except ImportError:
        log.warning("Web API requested but the 'web' extra is not installed; skipping.")
        return None

    from .config import is_usable_secret
    if not is_usable_secret(get_secret("DASHBOARD_PASSWORD")):
        raise RuntimeError(
            "DASHBOARD_PASSWORD is unset or a placeholder — refusing to start the dashboard. "
            "Set a real password in the environment."
        )

    deps = WebDeps(
        settings=settings, signals=signals, killswitch=killswitch,
        approval_gate=approval_gate, audit=audit,
        positions=positions, orders=orders, decisions=decisions,
        entry_decisions=entry_decisions, scanner=scanner, trade_journal=trade_journal,
        tv_indicators=tv_indicators, ai_reviews=ai_reviews, notifier=notifier,
        entry_candidates=entry_candidates, news=news, briefs=briefs,
        practice=practice, trade_alerts=trade_alerts,
    )
    app = create_app(deps)
    config = uvicorn.Config(
        app, host=settings.web.host, port=settings.http_bind_port(), log_level="info"
    )
    return uvicorn.Server(config)


async def main_async(config_path: str | None = None) -> None:
    setup_logging()
    require_runtime_secrets()
    settings = load_config(config_path)

    db = Database(settings.db_path)
    audit = AuditStore(db)
    positions = PositionStore(db)
    decisions = DecisionStore(db)
    entry_decisions = EntryDecisionStore(db)
    entry_candidates = EntryCandidateStore(db)
    setup_events = SetupEventStore(db)
    trade_journal = TradeJournalStore(db)
    tv_indicators = TVIndicatorStore(db)
    news = NewsStore(db)
    briefs = BriefStore(db)
    ai_reviews = AIReviewStore(db)
    ai_reviewer = AIReviewer(settings.ai, build_reviewer_client(settings.ai))
    signals = SignalStore(db)
    killswitch = KillSwitch(db, audit, auto_trip_threshold=settings.auto_trip_after_errors)

    broker, data_broker, read_session = await connect_brokers(settings)
    market_data = build_market_data(settings, data_broker or broker)
    rh_connected = bool(data_broker is not None and getattr(data_broker, "_connected", False))
    if settings.market_data == "robinhood" and not rh_connected:
        log.warning(
            "health: robinhood_connected=false — practice fills will not see real chains."
        )
    notifier = build_notifier(settings)
    trade_alerts = TradeAlerts(db, audit, settings)
    rules_engine = RulesEngine.from_configs(settings.rules)
    scanner = make_scanner(
        settings, broker, market_data, read_session, entry_decisions, None, audit,
        killswitch,
        trade_journal=trade_journal, ai_reviewer=ai_reviewer, tv_indicators=tv_indicators,
        ai_reviews=ai_reviews, earnings=build_earnings_provider(settings, data_broker or broker),
        company_data=build_company_data(settings, data_broker or broker),
        entry_candidates=entry_candidates,
        news_provider=build_news_provider(settings), news=news,
        setup_events=setup_events, trade_alerts=trade_alerts,
    )
    signal_processor = SignalProcessor(
        settings, signals, positions, decisions, None, None, audit
    )

    caps = broker.capabilities()
    md_effective = settings.market_data
    if settings.market_data == "robinhood" and not rh_connected:
        md_effective = "paper"
    practice = {
        "execution_broker": caps.name,
        "market_data": md_effective,
        "robinhood_connected": rh_connected,
        "practice": True,
    }
    log.info(
        "Starting AgenticRobinhood: broker=%s options=%s data=%s robinhood_connected=%s",
        caps.name, caps.supports_options_orders, md_effective, rh_connected,
    )

    monitor = MonitorLoop(
        settings, broker, market_data, positions, audit, killswitch,
        rules_engine=rules_engine, decisions=decisions, notifier=notifier,
        signal_processor=signal_processor,
    )

    web_server = build_web_server(
        settings, signals, killswitch, None, audit,
        positions, None, decisions, entry_decisions, scanner, trade_journal,
        tv_indicators=tv_indicators, ai_reviews=ai_reviews, notifier=notifier,
        entry_candidates=entry_candidates, news=news, briefs=briefs,
        practice=practice, trade_alerts=trade_alerts,
    )

    reporting = ReportingLoop(
        settings, positions, None, decisions, notifier,
        scanner=scanner, trade_journal=trade_journal, tv_indicators=tv_indicators,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop(*_a) -> None:
        log.info("Shutdown requested.")
        stop_event.set()
        monitor.stop()
        scanner.stop()
        reporting.stop()
        if web_server is not None:
            web_server.should_exit = True

    # Signal handlers (POSIX). On Windows, KeyboardInterrupt handles Ctrl+C.
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, AttributeError):
            pass

    tasks = [asyncio.create_task(monitor.run())]
    tasks.append(asyncio.create_task(reporting.run()))
    if settings.entry.enabled:
        log.info("Entry scanner ENABLED (watchlist=%d, feed=%s).",
                 len(settings.entry.watchlist), settings.entry.feed)
        tasks.append(asyncio.create_task(scanner.run()))
    if web_server is not None:
        log.info("Control/webhook API on http://%s:%d", settings.web.host, settings.http_bind_port())
        tasks.append(asyncio.create_task(web_server.serve()))
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        _request_stop()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        db.close()
        log.info("Stopped.")


def run() -> None:
    """Console-script entrypoint (``agentic``)."""
    import sys

    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        asyncio.run(main_async(config_path))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
