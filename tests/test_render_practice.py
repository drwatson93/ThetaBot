"""Paper-fills-on-real-data + config path / OAuth reseed (Render practice week)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentic.brokers.factory import build_paper_runtime
from agentic.brokers.live_guard import LiveOrderBlocked
from agentic.brokers.paper_broker import PaperBroker
from agentic.brokers.robinhood_mcp import RobinhoodMCPBroker
from agentic.config import Settings, load_config, resolve_config_path
from agentic.domain.enums import OrderStatus
from agentic.domain.models import EntryDecision, Order
from agentic.main import build_market_data
from agentic.marketdata.base import PaperMarketData
from agentic.marketdata.quote import OptionContractQuote
from agentic.marketdata.robinhood_md import RobinhoodMarketData
from agentic.services.executor import OrderExecutor
from agentic.services.killswitch import KillSwitch
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.entry_decisions import EntryDecisionStore
from agentic.store.orders import OrderStore
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
    exec_b, data_b = await build_paper_runtime(settings)
    assert exec_b.capabilities().is_paper is True
    assert isinstance(exec_b, PaperBroker)
    assert data_b is not None and data_b._connected is True
    md = build_market_data(settings, data_b)
    assert isinstance(md, RobinhoodMarketData)
    assert md.is_realtime is True
    order = Order(
        decision_id="d", position_id="p", occ_symbol="F260724P00014000",
        option_id="oid", quantity=1, limit_price=0.14, is_paper=False,
        client_order_id="must-not-place",
    )
    with pytest.raises(LiveOrderBlocked):
        await data_b.submit_open_order(order)


@pytest.mark.asyncio
async def test_paper_runtime_without_rh_is_synthetic(monkeypatch):
    async def fake_connect(self):
        self._connected = False

    monkeypatch.setattr(RobinhoodMCPBroker, "connect", fake_connect)
    settings = Settings(mode="paper", broker="robinhood_mcp", market_data="robinhood",
                        paper_seed_positions=False)
    exec_b, data_b = await build_paper_runtime(settings)
    assert exec_b.capabilities().is_paper is True
    assert data_b is None
    md = build_market_data(settings, data_b or exec_b)
    assert isinstance(md, PaperMarketData)


@pytest.mark.asyncio
async def test_paper_fill_on_real_quote(tmp_path):
    """A paper broker fill using a live-shaped quote — RH is never asked to place."""
    from datetime import timedelta
    from agentic.domain.models import utcnow

    class _RHQuote:
        is_realtime = True

        async def get_fresh_contract_quote(self, occ):
            exp = utcnow().date() + timedelta(days=10)
            return OptionContractQuote(
                occ_symbol=occ, underlying="F", option_id="id-1", option_type="put",
                strike=14.0, expiration=exp, bid=0.13, ask=0.15, mark=0.14,
            )

        async def get_quote(self, position):
            return None

        async def get_chain(self, underlying):
            return []

    db = Database(tmp_path / "p.db")
    audit = AuditStore(db)
    settings = Settings(mode="paper", broker="paper", market_data="robinhood")
    broker = PaperBroker(seed_positions=[], buying_power=25_000)
    md = _RHQuote()
    entries = EntryDecisionStore(db)
    ex = OrderExecutor(
        settings, broker, md, PositionStore(db), OrderStore(db), DecisionStore(db),
        audit, KillSwitch(db, audit), entry_decisions=entries, poll_interval_seconds=0.001,
    )
    exp = utcnow().date() + timedelta(days=10)
    occ = "F" + exp.strftime("%y%m%d") + "P00014000"
    d = EntryDecision(
        underlying="F", occ_symbol=occ, option_id="id-1", strike=14.0, expiration=exp,
        contracts=1, premium=0.14, rule_name="csp-screener", reason="test", dedup_key="F:t",
    )
    entries.insert_if_new(d)
    quote = await md.get_fresh_contract_quote(occ)
    order = await ex.execute_open(d, quote)
    assert order is not None and order.status == OrderStatus.FILLED
    assert order.is_paper is True
    opened = await broker.get_open_positions()
    assert any(p.occ_symbol == occ for p in opened)


def test_load_config_from_thetabot_config_env(tmp_path, monkeypatch):
    secret = tmp_path / "config.yaml"
    secret.write_text("mode: paper\ntrading_start: '10:30'\npaper_seed_positions: false\n",
                      encoding="utf-8")
    monkeypatch.setenv("THETABOT_CONFIG", str(secret))
    assert resolve_config_path() == Path(secret)
    s = load_config()
    assert s.mode == "paper"
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
    assert s.mode == "paper"
    assert s.trading_start == "10:00"
    assert s.paper_seed_positions is False


def test_http_bind_port_honors_env(monkeypatch):
    s = Settings(mode="paper")
    monkeypatch.delenv("PORT", raising=False)
    assert s.http_bind_port() == 8000
    monkeypatch.setenv("PORT", "10000")
    assert s.http_bind_port() == 10000


def test_public_base_url_uses_render_external(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://thetabot.onrender.com")
    s = Settings(mode="paper")
    assert s.public_base_url == "https://thetabot.onrender.com"


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
    assert body["mode"] == "paper"
    assert body["live_armed"] is False
    assert body["robinhood_connected"] is False
    assert body["market_data"] == "paper"
    assert body["practice"] is True
