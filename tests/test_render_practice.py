"""Paper-fills-on-real-data + config path / OAuth reseed (Render practice week)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentic.brokers.factory import build_paper_runtime
from agentic.brokers.robinhood_mcp import RobinhoodMCPBroker, RobinhoodWriteBlocked
from agentic.config import Settings, load_config, resolve_config_path
from agentic.domain.enums import OrderStatus
from agentic.domain.models import EntryDecision, Order
from agentic.main import build_market_data
from agentic.marketdata.base import PaperMarketData
from agentic.marketdata.quote import OptionContractQuote
from agentic.marketdata.robinhood_md import RobinhoodMarketData
from tests.fakes import OrderExecutor
from agentic.services.killswitch import KillSwitch
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.entry_decisions import EntryDecisionStore
from tests.fakes import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app


@pytest.mark.asyncio
async def test_paper_runtime_uses_rh_data_and_paper_execution(monkeypatch):
    async def fake_connect(self):
        self._connected = True
        self._supports_options = True
        self._tools = ["get_option_chains", "place_option_order"]
        self._roles = {"option_chain": "get_option_chains",
                       "place_option_order": "place_option_order"}

    monkeypatch.setattr(RobinhoodMCPBroker, "connect", fake_connect)
    settings = Settings(
        mode="paper", broker="robinhood_mcp", market_data="robinhood",
        paper_seed_positions=False, paper_buying_power=25_000,
    )
    exec_b, data_b, read_b = await build_paper_runtime(settings)
    assert exec_b.capabilities().is_paper is False
    assert isinstance(exec_b, RobinhoodMCPBroker)
    assert data_b is not None and data_b._connected is True
    assert read_b is data_b and read_b is exec_b
    md = build_market_data(settings, data_b)
    assert isinstance(md, RobinhoodMarketData)
    assert md.is_realtime is True
    order = Order(
        decision_id="d", position_id="p", occ_symbol="F260724P00014000",
        option_id="oid", quantity=1, limit_price=0.14, is_paper=False,
        client_order_id="must-not-place",
    )
    with pytest.raises(RobinhoodWriteBlocked):
        await data_b.submit_open_order(order)


@pytest.mark.asyncio
async def test_paper_runtime_without_rh_is_synthetic(monkeypatch):
    async def fake_connect(self):
        self._connected = False

    monkeypatch.setattr(RobinhoodMCPBroker, "connect", fake_connect)
    settings = Settings(mode="paper", broker="robinhood_mcp", market_data="robinhood",
                        paper_seed_positions=False)
    exec_b, data_b, read_b = await build_paper_runtime(settings)
    assert isinstance(exec_b, RobinhoodMCPBroker)
    assert exec_b.capabilities().is_paper is False
    assert data_b is None
    assert read_b is exec_b and read_b._connected is False
    md = build_market_data(settings, data_b or exec_b)
    assert isinstance(md, RobinhoodMarketData)




def test_load_config_from_thetabot_config_env(tmp_path, monkeypatch):
    secret = tmp_path / "config.yaml"
    secret.write_text("mode: paper\ntrading_start: '10:30'\npaper_seed_positions: false\n",
                      encoding="utf-8")
    monkeypatch.setenv("THETABOT_CONFIG", str(secret))
    assert resolve_config_path() == Path(secret)
    s = load_config()
    assert not hasattr(s, "mode")
    assert s.trading_start == "10:30"
    assert s.paper_seed_positions is False


def test_load_config_from_secret_file(tmp_path, monkeypatch):
    """Render Secret File at /etc/secrets/config.yaml is used when no env path is set."""
    import agentic.config as cfg

    secret = tmp_path / "config.yaml"
    secret.write_text("mode: paper\ntrading_start: '10:00'\npaper_seed_positions: false\n",
                      encoding="utf-8")
    monkeypatch.delenv("THETABOT_CONFIG", raising=False)
    monkeypatch.delenv("CONFIG_PATH", raising=False)
    monkeypatch.setattr(cfg, "RENDER_SECRET_CONFIG", secret)
    assert cfg.resolve_config_path() == secret
    s = cfg.load_config()
    assert not hasattr(s, "mode")
    assert s.trading_start == "10:00"
    assert s.paper_seed_positions is False


def test_http_bind_port_honors_env(monkeypatch):
    s = Settings(mode="paper")
    monkeypatch.delenv("PORT", raising=False)
    assert s.http_bind_port() == 8000
    monkeypatch.setenv("PORT", "10000")
    assert s.http_bind_port() == 10000


def test_oauth_reseed_overwrites_existing(tmp_path, monkeypatch):
    from agentic.brokers.rh_oauth import maybe_seed_oauth_from_env
    path = tmp_path / "rh_oauth.json"
    path.write_text('{"tokens":{"refresh_token":"old"}}', encoding="utf-8")
    monkeypatch.setenv("RH_OAUTH_JSON", '{"tokens":{"refresh_token":"new"}}')
    monkeypatch.delenv("RH_OAUTH_RESEED", raising=False)
    assert maybe_seed_oauth_from_env(path) is False
    assert "old" in path.read_text(encoding="utf-8")
    monkeypatch.setenv("RH_OAUTH_RESEED", "1")
    assert maybe_seed_oauth_from_env(path) is True
    assert "new" in path.read_text(encoding="utf-8")


def test_health_reports_synthetic_when_rh_down(tmp_path):
    db = Database(tmp_path / "h.db")
    audit = AuditStore(db)
    settings = Settings(mode="paper")
    deps = WebDeps(
        settings=settings, signals=SignalStore(db),
        killswitch=KillSwitch(db, audit), approval_gate=None, audit=audit,
        positions=PositionStore(db), orders=OrderStore(db), decisions=DecisionStore(db),
        practice={
            "execution_broker": "paper",
            "market_data": "paper",
            "robinhood_connected": False,
            "practice": True,
        },
    )
    body = TestClient(create_app(deps)).get("/health").json()
    assert body["status"] == "ok"
    assert "mode" not in body and "live_armed" not in body
    assert body["orders_hard_disabled"] is True
    assert body["robinhood_connected"] is False
    assert body["market_data"] == "paper"
    assert body["practice"] is True
