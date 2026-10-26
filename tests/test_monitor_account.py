"""Monitor must hand get_option_positions a real account number.

A blank robinhood.account_number is filled from get_accounts. When that lookup
cannot identify one account, the positions tool is not called and the miss is
logged once. Synthetic account ids only.
"""
import logging

import pytest

from agentic.brokers.factory import broker_degraded
from agentic.brokers.robinhood_mcp import RobinhoodMCPBroker
from agentic.config import Settings
from agentic.marketdata.base import PaperMarketData
from agentic.services.killswitch import KillSwitch
from agentic.services.monitor import MonitorLoop
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.positions import PositionStore
from tests.fakes import RecordingReadBroker


def _monitor(tmp_path, broker):
    db = Database(tmp_path / "acct.db")
    audit = AuditStore(db)
    positions = PositionStore(db)
    killswitch = KillSwitch(db, audit)
    settings = Settings()
    loop = MonitorLoop(
        settings, broker, PaperMarketData(), positions, audit, killswitch,
    )
    return loop, audit, settings


@pytest.mark.asyncio
async def test_monitor_passes_account_number_to_option_positions(tmp_path):
    broker = RecordingReadBroker("ACCT-AAA")
    monitor, _audit, settings = _monitor(tmp_path, broker)
    assert await monitor.run_once() == 0
    assert broker.position_calls == [{
        "tool": "get_option_positions",
        "account_number": "ACCT-AAA",
    }]
    assert broker.position_calls[0]["account_number"]
    assert broker_degraded(settings, broker) is False


@pytest.mark.asyncio
async def test_monitor_skips_positions_when_account_missing(tmp_path):
    broker = RecordingReadBroker("")
    monitor, audit, settings = _monitor(tmp_path, broker)
    assert await monitor.run_once() == 0
    assert await monitor.run_once() == 0
    assert broker.position_calls == []
    assert broker_degraded(settings, broker) is True
    assert [e for e in audit.recent() if e["event_type"] == "ERROR"] == []


def test_pick_account_prefers_single_agentic_account():
    picked = RobinhoodMCPBroker._pick_account_number([
        {"account_number": "ACCT-BBB", "agentic_allowed": False},
        {"account_number": "ACCT-AAA", "agentic_allowed": "true"},
    ])
    assert picked == "ACCT-AAA"
    only = RobinhoodMCPBroker._pick_account_number([
        {"account_number": "ACCT-AAA", "agentic_allowed": False},
    ])
    assert only == "ACCT-AAA"
    ambiguous = RobinhoodMCPBroker._pick_account_number([
        {"account_number": "ACCT-AAA", "agentic_allowed": False},
        {"account_number": "ACCT-BBB", "agentic_allowed": False},
    ])
    assert ambiguous == ""
    two_agentic = RobinhoodMCPBroker._pick_account_number([
        {"account_number": "ACCT-AAA", "agentic_allowed": True},
        {"account_number": "ACCT-BBB", "agentic_allowed": True},
    ])
    assert two_agentic == ""


@pytest.mark.asyncio
async def test_monitor_sends_resolved_account_to_get_option_positions(tmp_path, monkeypatch, caplog):
    broker = RobinhoodMCPBroker(account_number="", settings=Settings())
    broker._connected = True
    broker._tools = ["get_accounts", "get_option_positions"]
    broker._roles = {"list_positions": "get_option_positions"}
    seen: list[tuple[str, dict]] = []

    async def fake(name, args):
        seen.append((name, dict(args)))
        if name == "get_accounts":
            return {"data": {"accounts": [
                {"account_number": "ACCT-BBB", "agentic_allowed": False},
                {"account_number": "ACCT-AAA", "agentic_allowed": True},
            ]}}
        return {"data": {"positions": []}}

    monkeypatch.setattr(broker, "_call_tool", fake)
    monitor, _audit, _settings = _monitor(tmp_path, broker)
    with caplog.at_level(logging.INFO, logger="agentic.brokers.rh_mcp"):
        assert await monitor.run_once() == 0
    positions = [args for name, args in seen if name == "get_option_positions"]
    assert positions == [{
        "account_number": "ACCT-AAA", "nonzero": True, "type": "short",
    }]
    assert [name for name, _args in seen].count("get_accounts") == 1
    assert "ACCT-AAA" not in caplog.text
    assert "ACCT-BBB" not in caplog.text


@pytest.mark.asyncio
async def test_missing_account_logs_once_and_skips_the_tool(monkeypatch, caplog, tmp_path):
    broker = RobinhoodMCPBroker(account_number="", settings=Settings())
    broker._connected = True
    broker._tools = ["get_accounts", "get_option_positions", "get_portfolio"]
    broker._roles = {
        "list_positions": "get_option_positions",
        "get_portfolio": "get_portfolio",
    }
    seen: list[str] = []

    async def fake(name, args):
        seen.append(name)
        return {"data": {"accounts": [
            {"account_number": "ACCT-AAA", "agentic_allowed": False},
            {"account_number": "ACCT-BBB", "agentic_allowed": False},
        ]}}

    monkeypatch.setattr(broker, "_call_tool", fake)
    monitor, audit, settings = _monitor(tmp_path, broker)
    with caplog.at_level(logging.WARNING, logger="agentic.brokers.rh_mcp"):
        assert await monitor.run_once() == 0
        assert await broker.get_buying_power() == 0.0
        assert await monitor.run_once() == 0
    assert seen == ["get_accounts"]
    warnings = [r.getMessage() for r in caplog.records if "unset" in r.getMessage()]
    assert warnings == [
        "robinhood.account_number is unset and get_accounts did not identify one account. "
        "Open-position and buying-power reads are skipped."
    ]
    assert "ACCT-AAA" not in warnings[0]
    assert broker_degraded(settings, broker) is True
    assert [e for e in audit.recent() if e["event_type"] == "ERROR"] == []
