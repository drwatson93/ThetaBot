"""Broker-fallback safety net: detect + surface the live-but-on-paper degraded state."""
from types import SimpleNamespace

from agentic.brokers.base import BrokerCapabilities
from agentic.brokers.factory import broker_degraded
from agentic.config import Settings
from agentic.services.reporting import build_daily_digest


def _broker(name, is_paper):
    return SimpleNamespace(
        capabilities=lambda: BrokerCapabilities(name, supports_options_orders=True,
                                                is_paper=is_paper))


def test_broker_degraded_only_when_live_and_fallen_to_paper():
    """Paper execution is gone, so a session is never flagged as a paper fallback."""
    rh = Settings(broker="robinhood_mcp", market_data="paper")
    paper_cfg = Settings(broker="paper", market_data="paper")
    assert broker_degraded(rh, _broker("paper", True)) is False
    assert broker_degraded(rh, _broker("robinhood_mcp", False)) is False
    assert broker_degraded(paper_cfg, _broker("paper", True)) is False


def test_digest_flags_degraded_state():
    _title, msg = build_daily_digest(stats={}, rows=[], scanner=None, mode="live", degraded=True)
    assert "ON SIMULATOR" in msg and "real account" in msg
    # No false alarm when healthy.
    _t2, ok = build_daily_digest(stats={}, rows=[], scanner=None, mode="live", degraded=False)
    assert "ON SIMULATOR" not in ok
