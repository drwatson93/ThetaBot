"""Broker selection.

The only broker is the Robinhood MCP client, used for reads. Old config values
``broker: paper``, ``alpaca``, and ``robinstocks`` still load; those execution
brokers are gone, so startup always opens the Robinhood session.
"""
from __future__ import annotations

import logging

from ..config import Settings
from .base import ExecutionBroker
from .robinhood_mcp import RobinhoodMCPBroker

log = logging.getLogger("agentic.brokers.factory")


async def build_broker(settings: Settings) -> ExecutionBroker:
    """Connect the read-only Robinhood session."""
    broker = RobinhoodMCPBroker(
        account_number=settings.robinhood.account_number, settings=settings,
    )
    await broker.connect()
    caps = broker.capabilities()
    log.info("Broker '%s': %s", caps.name, caps.notes)
    return broker


async def build_paper_runtime(
    settings: Settings,
) -> tuple[ExecutionBroker, ExecutionBroker | None, ExecutionBroker | None]:
    """Read-only Robinhood session.

    Returns ``(broker, data, read_session)``. ``data`` is the session only when
    the probe connected. ``read_session`` is that same object either way, so
    the scanner can fail closed. There is no paper execution broker.
    """
    broker = await build_broker(settings)
    read_session = broker
    data = broker if getattr(broker, "_connected", False) else None
    if data is None and settings.market_data == "robinhood":
        log.warning(
            "Robinhood MCP is NOT connected. The scanner will not see real option chains."
        )
    return broker, data, read_session


def broker_degraded(settings: Settings, broker: ExecutionBroker) -> bool:
    """No paper simulator remains to fall back to."""
    return False
