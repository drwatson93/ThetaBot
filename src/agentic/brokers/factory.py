"""Broker selection + capability-aware fallback.

build_broker() constructs the configured broker, connects it (which probes
capabilities), and — if it cannot place option orders and a fallback is configured —
swaps to the fallback. Returns the chosen broker plus a human-readable summary for
logging/audit.
"""
from __future__ import annotations

import logging

from ..config import REPO_ROOT, Settings
from .alpaca_broker import AlpacaOptionsBroker
from .base import ExecutionBroker
from .paper_broker import PaperBroker
from .robinhood_mcp import RobinhoodMCPBroker
from .robinstocks_broker import RobinStocksBroker

log = logging.getLogger("agentic.brokers.factory")

_REGISTRY = {
    "paper": PaperBroker,
    "robinhood_mcp": RobinhoodMCPBroker,
    "robinstocks": RobinStocksBroker,
    "alpaca": AlpacaOptionsBroker,
}


def _construct(name: str, settings: Settings) -> ExecutionBroker:
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown broker '{name}'. Options: {list(_REGISTRY)}")
    if cls is RobinhoodMCPBroker:
        return cls(account_number=settings.robinhood.account_number, settings=settings)
    if cls is PaperBroker:
        # None -> broker uses its built-in demo seed; [] -> start empty (real-soak / no phantom
        # collateral blocking the entry sizer). See Settings.paper_seed_positions.
        seed = None if settings.paper_seed_positions else []
        # Persist to the data volume so a redeploy doesn't wipe open paper positions (which
        # reconcile would otherwise book as spurious closes).
        return cls(buying_power=settings.paper_buying_power, seed_positions=seed,
                   persist_path=REPO_ROOT / "data" / "paper_broker.json")
    return cls()


async def build_paper_runtime(settings: Settings) -> tuple[ExecutionBroker, ExecutionBroker | None]:
    """Paper-mode runtime: simulated fills, optional live Robinhood market-data session.

    Orders always go to ``PaperBroker`` (the live-order guard still blocks any RH write).
    When ``market_data: robinhood`` and the MCP session connects, that session is returned
    as the data broker so chains/quotes/earnings use real Robinhood data.
    """
    data: ExecutionBroker | None = None
    if settings.market_data == "robinhood":
        rh = RobinhoodMCPBroker(
            account_number=settings.robinhood.account_number, settings=settings
        )
        await rh.connect()
        if getattr(rh, "_connected", False):
            data = rh
            log.info(
                "Practice mode: live Robinhood market data; every order stays on the "
                "paper simulator (no real trades)."
            )
        else:
            log.warning(
                "Practice mode: Robinhood MCP is NOT connected — using SYNTHETIC paper "
                "data. The scanner will not see real option chains. Set RH_OAUTH_JSON "
                "or run `python -m agentic.tools.rh_login` on a desktop."
            )
    paper = _construct("paper", settings)
    await paper.connect()
    return paper, data


async def build_broker(settings: Settings) -> ExecutionBroker:
    """Construct, connect, and (if needed) fall back to a capable broker."""
    broker = _construct(settings.broker, settings)
    await broker.connect()
    caps = broker.capabilities()
    log.info("Primary broker '%s': %s", caps.name, caps.notes)

    if not caps.supports_options_orders and settings.broker_fallback:
        log.warning(
            "Broker '%s' cannot place option orders; falling back to '%s'.",
            caps.name, settings.broker_fallback,
        )
        fallback = _construct(settings.broker_fallback, settings)
        await fallback.connect()
        fb_caps = fallback.capabilities()
        log.info("Fallback broker '%s': %s", fb_caps.name, fb_caps.notes)
        if fb_caps.supports_options_orders:
            return fallback
        log.error("Fallback '%s' also lacks option orders; staying read-only.", fb_caps.name)

    return broker


def broker_degraded(settings: Settings, broker: ExecutionBroker) -> bool:
    """True when the bot is LIVE-armed but the active broker fell back to the paper simulator.

    This is the dangerous "looks live but isn't" state: mode=live + a configured real broker, yet
    the running broker is paper (the RH connect probe failed at startup). It means the bot is NOT
    trading/managing the real account. Callers surface it loudly (startup push, /api/ops, digest).
    """
    if not (settings.is_live and settings.broker != "paper"):
        return False
    try:
        return bool(broker.capabilities().is_paper)
    except Exception:  # noqa: BLE001 — never let a health check break anything
        return False
