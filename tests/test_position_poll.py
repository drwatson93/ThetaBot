"""Option-position poll shape and the scanner's account_number.

Synthetic tickers (TSTA/TSTB) and account 000000123456 only. No network.
"""
from __future__ import annotations

import logging

import pytest

from agentic.brokers.robinhood_mcp import RobinhoodMCPBroker
from agentic.config import Settings
from agentic.domain.enums import OptionType
from agentic.domain.models import Order
from agentic.services.killswitch import KillSwitch
from agentic.services.scanner import OpportunityScanner
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.entry_decisions import EntryDecisionStore

ACCT = "000000123456"
LAST4 = "3456"


def _broker(account_number: str = ACCT) -> RobinhoodMCPBroker:
    broker = RobinhoodMCPBroker(account_number=account_number, settings=Settings())
    broker._connected = True
    broker._tools = [
        "get_accounts", "get_option_positions", "get_option_instruments",
        "get_portfolio", "get_equity_positions", "get_option_orders", "get_equity_orders",
    ]
    broker._roles = {
        "list_positions": "get_option_positions",
        "option_instruments": "get_option_instruments",
        "get_portfolio": "get_portfolio",
        "equity_positions": "get_equity_positions",
        "get_option_order": "get_option_orders",
        "get_equity_orders": "get_equity_orders",
    }
    return broker


def _short(symbol: str, oid: str, *, side: str = "short", qty: str = "1",
           price: str | None = "88.50", exp: str | None = "2026-11-20",
           strike: str | None = "10.0000") -> dict:
    rec = {
        "option_id": oid, "type": side, "quantity": qty, "id": f"pos-{oid}",
    }
    if price is not None:
        rec["average_price"] = price
        rec["trade_value_multiplier"] = 100
    if exp is not None:
        rec["expiration_date"] = exp
    if symbol:
        rec["chain_symbol"] = symbol
    return rec


def _inst(symbol: str, oid: str, strike: str = "10.0000", typ: str = "put") -> dict:
    return {
        "id": oid, "strike_price": strike, "type": typ,
        "chain_symbol": symbol, "expiration_date": "2026-11-20",
    }


def _install(monkeypatch, broker, payloads: dict):
    seen: list[tuple[str, dict]] = []

    async def fake(name, args):
        if "account_number" in args and not str(args.get("account_number") or "").strip():
            raise RuntimeError(
                'McpError: invalid params required: missing properties: ["account_number"]'
            )
        seen.append((name, dict(args)))
        return payloads[name](args) if callable(payloads[name]) else payloads[name]

    monkeypatch.setattr(broker, "_call_tool", fake)
    return seen


class _Md:
    async def get_chain(self, underlying: str):
        return []

    async def get_underlying_bars(self, underlying: str, lookback_days: int = 260):
        return []

    async def get_underlying_price(self, underlying: str):
        return None

    async def get_quote(self, position):
        return None


def _scanner(tmp_path, broker):
    db = Database(tmp_path / "poll.db")
    audit = AuditStore(db)
    settings = Settings(
        market_data="robinhood",
        entry={"enabled": True, "watchlist": [], "earnings_gate": False},
        macro={"enabled": False},
    )
    return OpportunityScanner(
        settings, broker, _Md(), EntryDecisionStore(db), None,
        audit, KillSwitch(db, audit), read_broker=broker,
    )


@pytest.mark.asyncio
async def test_normal_reply_maps_short_put(monkeypatch):
    broker = _broker()
    rec = _short("TSTA", "oid-good")
    _install(monkeypatch, broker, {
        "get_option_positions": {"data": {"positions": [rec]}, "guide": "skip"},
        "get_option_instruments": {"results": [_inst("TSTA", "oid-good")]},
    })
    positions = await broker.get_open_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.underlying == "TSTA"
    assert pos.option_type is OptionType.PUT
    assert pos.quantity == 1
    assert pos.strike == 10.0
    assert pos.credit_received == pytest.approx(0.885)


@pytest.mark.asyncio
async def test_option_positions_key_is_unwrapped(monkeypatch, caplog):
    broker = _broker()
    rec = _short("TSTA", "oid-opt")
    _install(monkeypatch, broker, {
        "get_option_positions": {"data": {"option_positions": [rec]}},
        "get_option_instruments": {"results": [_inst("TSTA", "oid-opt", typ="call")]},
    })
    with caplog.at_level(logging.INFO, logger="agentic.brokers.rh_mcp"):
        positions = await broker.get_open_positions()
    assert len(positions) == 1
    assert positions[0].underlying == "TSTA"
    assert positions[0].option_type is OptionType.CALL
    assert not any("unrecognized list key" in r.getMessage() for r in caplog.records)
    lines = [
        r.getMessage() for r in caplog.records if "Option position poll" in r.getMessage()
    ]
    assert len(lines) == 1
    assert "inner=option_positions" in lines[0]
    assert "TSTA" not in lines[0]
    assert ACCT not in lines[0]


@pytest.mark.asyncio
async def test_long_rows_are_dropped(monkeypatch):
    broker = _broker()
    long_row = _short("TSTB", "oid-long", side="long")
    seen = _install(monkeypatch, broker, {
        "get_option_positions": {"data": {"positions": [long_row]}},
        "get_option_instruments": {"results": [_inst("TSTB", "oid-long")]},
    })
    assert await broker.get_open_positions() == []
    calls = [args for name, args in seen if name == "get_option_positions"]
    assert calls == [{"account_number": ACCT, "nonzero": True, "type": "short"}]


def test_unrecognized_list_key_is_position_only(caplog):
    RobinhoodMCPBroker._unknown_record_keys_logged.discard("held_contracts")
    raw = {"guide": "skip", "held_contracts": [{"option_id": "oid-x"}]}
    # Shared parser stays on main: the wrapper is the record, not the unknown list.
    assert RobinhoodMCPBroker._iter_records(raw) == [raw]
    with caplog.at_level(logging.WARNING, logger="agentic.brokers.rh_mcp"):
        recs = RobinhoodMCPBroker._position_records(raw)
    assert recs == [{"option_id": "oid-x"}]
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == ["Tool reply used unrecognized list key held_contracts"]
    assert "oid-x" not in warnings[0]
    # An empty list is not a collection (all([]) is true, but it must not unwrap).
    empty = {"held_contracts": []}
    assert RobinhoodMCPBroker._position_records(empty) == [empty]
    assert RobinhoodMCPBroker._iter_records(empty) == [empty]


@pytest.mark.asyncio
async def test_scanner_resolves_blank_account_before_portfolio(tmp_path, monkeypatch):
    """The 10:44 ET scanner error was get_buying_power with no account_number."""
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    broker = _broker(account_number="")
    seen = _install(monkeypatch, broker, {
        "get_accounts": {"data": {"accounts": [
            {"account_number": ACCT, "agentic_allowed": True},
        ]}},
        "get_portfolio": {"data": {
            "buying_power": {"buying_power": "1000.00"}, "total_value": "5000.00",
        }},
        "get_option_positions": {"data": {"positions": []}},
        "get_equity_positions": {"data": {"positions": []}},
    })
    scanner = _scanner(tmp_path, broker)
    assert await scanner.run_once() == 0
    portfolio = [args for name, args in seen if name == "get_portfolio"]
    assert portfolio
    assert all(args.get("account_number") == ACCT for args in portfolio)
    positions = [args for name, args in seen if name == "get_option_positions"]
    assert positions == [{"account_number": ACCT, "nonzero": True, "type": "short"}]


@pytest.mark.asyncio
async def test_scanner_does_not_call_portfolio_when_account_stays_blank(
    tmp_path, monkeypatch, caplog,
):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    broker = _broker(account_number="")
    seen = _install(monkeypatch, broker, {
        "get_accounts": {"data": {"accounts": [
            {"account_number": "000000111111", "agentic_allowed": False},
            {"account_number": "000000222222", "agentic_allowed": False},
        ]}},
        "get_portfolio": {"data": {}},
        "get_option_positions": {"data": {"positions": []}},
        "get_equity_positions": {"data": {"positions": []}},
    })
    scanner = _scanner(tmp_path, broker)
    with caplog.at_level(logging.WARNING, logger="agentic.brokers.rh_mcp"):
        assert await scanner.run_once() == 0
    assert [name for name, _args in seen] == ["get_accounts"]
    assert any("account_number is unset" in r.getMessage() for r in caplog.records)
    assert ACCT not in caplog.text
    assert "000000111111" not in caplog.text


def _order() -> Order:
    return Order(
        decision_id="d", position_id="p", occ_symbol="TSTA261120P00010000",
        quantity=1, limit_price=0.20, is_paper=False, option_id="oid-good",
        client_order_id="close-1", broker_order_id="ord-1",
    )


@pytest.mark.asyncio
async def test_get_order_resolves_blank_account(monkeypatch):
    broker = _broker(account_number="")
    seen = _install(monkeypatch, broker, {
        "get_accounts": {"data": {"accounts": [
            {"account_number": ACCT, "agentic_allowed": True},
        ]}},
        "get_option_orders": {"data": {"results": []}},
    })
    await broker.get_order(_order())
    orders = [args for name, args in seen if name == "get_option_orders"]
    assert orders == [{"account_number": ACCT}]


@pytest.mark.asyncio
async def test_get_order_skips_call_when_account_unresolved(monkeypatch, caplog):
    broker = _broker(account_number="")
    seen = _install(monkeypatch, broker, {
        "get_accounts": {"data": {"accounts": [
            {"account_number": "000000111111", "agentic_allowed": False},
            {"account_number": "000000222222", "agentic_allowed": False},
        ]}},
        "get_option_orders": {"data": {"results": []}},
    })
    with caplog.at_level(logging.WARNING, logger="agentic.brokers.rh_mcp"):
        out = await broker.get_order(_order())
    assert out.broker_order_id == "ord-1"
    assert [name for name, _args in seen] == ["get_accounts"]
    assert any("get_option_orders" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_get_equity_order_resolves_blank_account(monkeypatch):
    broker = _broker(account_number="")
    seen = _install(monkeypatch, broker, {
        "get_accounts": {"data": {"accounts": [
            {"account_number": ACCT, "agentic_allowed": True},
        ]}},
        "get_equity_orders": {"data": {"results": [
            {"id": "eq-1", "state": "filled", "cumulative_quantity": "1", "average_price": "10"},
        ]}},
    })
    out = await broker.get_equity_order("eq-1")
    assert out["order_id"] == "eq-1"
    assert [args for name, args in seen if name == "get_equity_orders"] == [
        {"account_number": ACCT, "order_id": "eq-1"},
    ]


@pytest.mark.asyncio
async def test_get_equity_order_raises_without_sending_when_unresolved(monkeypatch, caplog):
    broker = _broker(account_number="")
    seen = _install(monkeypatch, broker, {
        "get_accounts": {"data": {"accounts": []}},
        "get_equity_orders": {"data": {"results": []}},
    })
    with caplog.at_level(logging.WARNING, logger="agentic.brokers.rh_mcp"):
        with pytest.raises(RuntimeError, match="could not be resolved"):
            await broker.get_equity_order("eq-1")
    assert [name for name, _args in seen] == ["get_accounts"]
    assert any("get_equity_orders" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_poll_log_has_counts_and_last4_only(monkeypatch, caplog):
    broker = _broker()
    rows = [
        _short("TSTA", "oid-good"),
        _short("TSTB", "oid-long", side="long"),
        _short("TSTA", "oid-zero", qty="0"),
        _short("", "oid-miss", price=None, exp=None, strike=None),
        _short("", "oid-bare", price=None, exp=None),
    ]
    # oid-bare has no option_id lookup target and no symbol/expiry/price: missing fields.
    rows[-1].pop("option_id")
    _install(monkeypatch, broker, {
        "get_option_positions": {"data": {"positions": rows}, "guide": "skip"},
        "get_option_instruments": {"results": [_inst("TSTA", "oid-good")]},
    })
    with caplog.at_level(logging.INFO, logger="agentic.brokers.rh_mcp"):
        positions = await broker.get_open_positions()
    assert len(positions) == 1
    assert positions[0].underlying == "TSTA"
    lines = [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.INFO and "Option position poll" in r.getMessage()
    ]
    assert len(lines) == 1
    line = lines[0]
    assert "raw=5" in line
    assert "keys=data,guide inner=positions" in line
    assert "mapped=1" in line
    assert "not_short:1" in line
    assert "qty_0:1" in line
    assert "missing_fields:1" in line
    assert "instrument_lookup:1" in line
    assert "other:0" in line
    assert f"****{LAST4}" in line
    assert ACCT not in line
    assert "TSTA" not in line
    assert "TSTB" not in line
    assert "88.50" not in line

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="agentic.brokers.rh_mcp"):
        await broker.get_open_positions()
    again = [
        r.getMessage() for r in caplog.records
        if "Option position poll" in r.getMessage()
    ]
    assert again == []


@pytest.mark.asyncio
async def test_poll_log_always_when_rows_do_not_map(monkeypatch, caplog):
    broker = _broker()
    long_row = _short("TSTB", "oid-long", side="long")
    _install(monkeypatch, broker, {
        "get_option_positions": {"data": {"positions": [long_row]}},
        "get_option_instruments": {"results": [_inst("TSTB", "oid-long")]},
    })
    with caplog.at_level(logging.INFO, logger="agentic.brokers.rh_mcp"):
        assert await broker.get_open_positions() == []
        assert await broker.get_open_positions() == []
    lines = [
        r.getMessage() for r in caplog.records
        if "Option position poll" in r.getMessage()
    ]
    assert len(lines) == 2
    assert all("raw=1" in line and "mapped=0" in line and "not_short:1" in line for line in lines)
    assert all("TSTB" not in line and ACCT not in line for line in lines)


@pytest.mark.asyncio
async def test_position_fallback_maps_unknown_nonempty_list(monkeypatch, caplog):
    RobinhoodMCPBroker._unknown_record_keys_logged.discard("held_contracts")
    broker = _broker()
    rec = _short("TSTA", "oid-x")
    _install(monkeypatch, broker, {
        "get_option_positions": {"data": {"held_contracts": [rec]}, "guide": "skip"},
        "get_option_instruments": {"results": [_inst("TSTA", "oid-x")]},
    })
    with caplog.at_level(logging.WARNING, logger="agentic.brokers.rh_mcp"):
        positions = await broker.get_open_positions()
    assert len(positions) == 1
    assert positions[0].underlying == "TSTA"
    assert positions[0].strike == 10.0
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == ["Tool reply used unrecognized list key held_contracts"]
    assert "TSTA" not in warnings[0]
    assert "88.50" not in warnings[0]


@pytest.mark.asyncio
async def test_portfolio_equities_list_keeps_buying_power(monkeypatch, caplog):
    """A sole equities list must not become the portfolio record."""
    for equities in ([{"symbol": "TSTA", "quantity": "1"}], []):
        broker = _broker()
        _install(monkeypatch, broker, {
            "get_portfolio": {"data": {
                "buying_power": {"buying_power": "1500.00"},
                "total_value": "8000.00",
                "equities": equities,
            }},
        })
        with caplog.at_level(logging.WARNING, logger="agentic.brokers.rh_mcp"):
            assert await broker.get_buying_power() == 1500.0
            assert await broker.get_account_value() == 8000.0
        assert not any("unrecognized list key" in r.getMessage() for r in caplog.records)
        caplog.clear()


@pytest.mark.asyncio
async def test_sole_bars_list_still_parses(monkeypatch):
    from agentic.marketdata.robinhood_md import RobinhoodMarketData

    broker = _broker()
    payload = {"data": {"bars": [{
        "open_price": "9", "high_price": "11", "low_price": "8",
        "close_price": "10", "volume": "100", "session": "reg",
    }]}}
    assert RobinhoodMCPBroker._iter_records(payload)[0].get("bars")

    async def fake(name, args):
        assert name == "get_equity_historicals"
        assert args["symbols"] == ["TSTA"]
        return payload

    monkeypatch.setattr(broker, "_call_tool", fake)
    bars = await RobinhoodMarketData(broker).get_underlying_bars("TSTA", lookback_days=5)
    assert bars == [{"o": 9.0, "h": 11.0, "l": 8.0, "c": 10.0, "v": 100}]


@pytest.mark.asyncio
async def test_poll_log_repeats_after_fifteen_minutes(monkeypatch, caplog):
    clock = {"t": 1_000.0}
    monkeypatch.setattr(
        "agentic.brokers.robinhood_mcp.time.monotonic", lambda: clock["t"],
    )
    broker = _broker()
    _install(monkeypatch, broker, {
        "get_option_positions": {"data": {"positions": [_short("TSTA", "oid-good")]}},
        "get_option_instruments": {"results": [_inst("TSTA", "oid-good")]},
    })

    def poll_lines():
        return [
            r.getMessage() for r in caplog.records
            if "Option position poll" in r.getMessage()
        ]

    with caplog.at_level(logging.INFO, logger="agentic.brokers.rh_mcp"):
        await broker.get_open_positions()
    assert len(poll_lines()) == 1
    assert f"****{LAST4}" in poll_lines()[0]
    assert "TSTA" not in poll_lines()[0]
    caplog.clear()

    clock["t"] += 14 * 60
    with caplog.at_level(logging.INFO, logger="agentic.brokers.rh_mcp"):
        await broker.get_open_positions()
    assert poll_lines() == []

    clock["t"] += 60
    with caplog.at_level(logging.INFO, logger="agentic.brokers.rh_mcp"):
        await broker.get_open_positions()
    lines = poll_lines()
    assert len(lines) == 1
    assert "raw=1" in lines[0] and "mapped=1" in lines[0]
    assert f"****{LAST4}" in lines[0]
    assert ACCT not in lines[0]
    assert "TSTA" not in lines[0]
    assert "88.50" not in lines[0]
