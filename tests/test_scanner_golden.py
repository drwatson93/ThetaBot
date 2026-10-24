"""Golden pins for today's CSP and CC scanner picks, plus the order lock.

The fixture is synthetic (fake tickers, made-up prices). The scan runs through
OpportunityScanner.run_once — the same screen, rank, and size path the bot uses —
and the executor double records the pick and returns without placing an order.
No broker write method is callable from this test.

Captured against the scanner as it stands: buying power 20_000, account value
110_000, tax-reserve holding absent (reserve value 0), default criteria and sizing.
The 10% buying-power reserve is what holds BBB at 1 contract; the per-name cap
would allow 2.
"""
from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic.brokers.robinhood_mcp import (
    ORDERS_HARD_DISABLED,
    RobinhoodWriteBlocked,
    assert_mcp_tool_allowed,
)
from agentic.config import Settings
from agentic.domain.enums import Direction, OptionType, PositionStatus, Strategy
from agentic.domain.models import EquityHolding, Position
from agentic.marketdata.base import MarketDataProvider
from agentic.marketdata.quote import OptionContractQuote
from agentic.services.killswitch import KillSwitch
from agentic.services.scanner import OpportunityScanner
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.entry_candidates import EntryCandidateStore
from agentic.store.entry_decisions import EntryDecisionStore

FROZEN_NOW = datetime(2026, 6, 15, 16, 0, tzinfo=timezone.utc)
FROZEN_DAY = FROZEN_NOW.date()

# Ranked picks produced by the current scanner on the fixture below.
GOLDEN_CSP = (
    {"underlying": "CCC", "strike": 40.0, "expiration": date(2026, 7, 20), "contracts": 2},
    {"underlying": "BBB", "strike": 55.0, "expiration": date(2026, 7, 25), "contracts": 1},
)
GOLDEN_CC = (
    {"underlying": "EEE", "strike": 18.0, "expiration": date(2026, 7, 20), "contracts": 1},
    {"underlying": "DDD", "strike": 30.0, "expiration": date(2026, 7, 21), "contracts": 4},
    {"underlying": "BBB", "strike": 75.0, "expiration": date(2026, 7, 20), "contracts": 2},
)


def _frozen_now() -> datetime:
    return FROZEN_NOW


def _contract(underlying, option_type, strike, dte, delta, bid, ask, theta, *, oi=400, vol=80):
    exp = FROZEN_DAY + timedelta(days=dte)
    side = "P" if option_type == "put" else "C"
    occ = f"{underlying}{exp:%y%m%d}{side}{int(round(strike * 1000)):08d}"
    return OptionContractQuote(
        occ_symbol=occ, underlying=underlying, option_id=None,
        option_type=option_type, strike=float(strike), expiration=exp,
        bid=bid, ask=ask, mark=round((bid + ask) / 2, 4),
        delta=delta, iv=0.35, theta=theta, open_interest=oi, volume=vol,
    )


def _chains() -> dict[str, list[OptionContractQuote]]:
    """One frozen book. Decoys sit next to the winner so a loosened gate changes the pin."""
    return {
        "AAA": [
            # Best theta on the book, but AAA already has a short put, so it is not a pick.
            _contract("AAA", "put", 45, 32, -0.25, 1.20, 1.30, -0.12),
        ],
        "BBB": [
            _contract("BBB", "put", 55, 40, -0.24, 1.50, 1.60, -0.09),
            _contract("BBB", "put", 50, 40, -0.22, 1.30, 1.40, -0.05),  # passes, loses rank
            # Higher annualized yield, lower theta-efficiency. Wins only if rank leaves theta.
            _contract("BBB", "put", 45, 30, -0.23, 1.40, 1.56, -0.05),
            _contract("BBB", "put", 60, 35, -0.55, 2.40, 2.50, -0.40),  # delta too high
            _contract("BBB", "put", 52, 5, -0.22, 1.40, 1.50, -0.30),   # DTE too short
            _contract("BBB", "call", 75, 35, 0.25, 1.70, 1.80, -0.06),
            _contract("BBB", "call", 80, 35, 0.22, 1.80, 1.90, -0.04),  # passes, loses rank
            _contract("BBB", "call", 65, 35, 0.28, 2.40, 2.50, -0.20),  # below cost basis 70
        ],
        "CCC": [
            _contract("CCC", "put", 40, 35, -0.26, 1.40, 1.50, -0.08),
            _contract("CCC", "put", 35, 35, -0.21, 1.00, 1.08, -0.04),  # passes, loses rank
            _contract("CCC", "put", 38, 35, -0.25, 1.80, 1.90, -0.50, oi=0, vol=0),  # no market
            _contract("CCC", "put", 42, 35, -0.24, 0.40, 1.40, -0.30),  # spread too wide
        ],
        "DDD": [
            _contract("DDD", "call", 30, 36, 0.24, 0.90, 0.96, -0.05),
            _contract("DDD", "call", 22, 36, 0.26, 1.50, 1.60, -0.15),  # below cost basis 25
        ],
        "EEE": [
            # 200 shares, one short call already on: one contract left.
            _contract("EEE", "call", 18, 35, 0.23, 0.55, 0.60, -0.04),
        ],
        "FFF": [
            # 100 shares already covered by one short call: no new CC.
            _contract("FFF", "call", 15, 35, 0.25, 0.70, 0.76, -0.08),
        ],
        "GGG": [
            # 50 shares: under one contract, never screened.
            _contract("GGG", "call", 12, 35, 0.25, 0.80, 0.86, -0.10),
        ],
        "HHH": [
            # Single fault: annualized yield under the 20% floor. Delta, DTE, spread,
            # and liquidity all pass, and theta would outrank CCC. Dropping the floor
            # makes HHH a pick.
            _contract("HHH", "put", 40, 35, -0.25, 0.36, 0.40, -0.20),
        ],
    }


class _ReadOnlyBook:
    """Account snapshot. Any order method raises — this test must not place one."""

    def __init__(self):
        self.writes: list[str] = []
        self.positions = [
            Position(
                occ_symbol="AAA-held-put", underlying="AAA", option_type=OptionType.PUT,
                strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=1,
                strike=40.0, expiration=FROZEN_DAY + timedelta(days=20), credit_received=1.0,
                status=PositionStatus.OPEN,
            ),
            Position(
                occ_symbol="EEE-held-call", underlying="EEE", option_type=OptionType.CALL,
                strategy=Strategy.COVERED_CALL, direction=Direction.SHORT, quantity=1,
                strike=18.0, expiration=FROZEN_DAY + timedelta(days=20), credit_received=0.5,
                status=PositionStatus.OPEN,
            ),
            Position(
                occ_symbol="FFF-held-call", underlying="FFF", option_type=OptionType.CALL,
                strategy=Strategy.COVERED_CALL, direction=Direction.SHORT, quantity=1,
                strike=15.0, expiration=FROZEN_DAY + timedelta(days=20), credit_received=0.4,
                status=PositionStatus.OPEN,
            ),
        ]
        self.holdings = [
            EquityHolding(symbol="BBB", quantity=250, average_cost=70.0),
            EquityHolding(symbol="CCC", quantity=90, average_cost=30.0),
            EquityHolding(symbol="DDD", quantity=400, average_cost=25.0),
            EquityHolding(symbol="EEE", quantity=200, average_cost=15.0),
            EquityHolding(symbol="FFF", quantity=100, average_cost=12.0),
            EquityHolding(symbol="GGG", quantity=50, average_cost=10.0),
        ]

    async def get_buying_power(self) -> float:
        # 10% reserve leaves $18,000 usable. After CCC (2 x $4,000), $10,000 remains,
        # which sizes BBB's $5,500 contract to 1. With no reserve, BBB sizes to 2.
        return 20_000.0

    async def get_account_value(self) -> float:
        # 10% per-name cap is $11,000, enough for 2 of BBB's contracts.
        return 110_000.0

    async def get_open_positions(self):
        return list(self.positions)

    async def get_equity_positions(self):
        return list(self.holdings)

    async def resolve_option_id(self, occ_symbol: str):
        return None

    def _refuse(self, name: str):
        self.writes.append(name)
        raise AssertionError(f"golden scan tried to place an order via {name}")

    async def submit_open_order(self, order):
        self._refuse("submit_open_order")

    async def submit_close_order(self, order):
        self._refuse("submit_close_order")

    async def submit_equity_order(self, **kwargs):
        self._refuse("submit_equity_order")

    async def cancel_order(self, order):
        self._refuse("cancel_order")


class _ChainBook(MarketDataProvider):
    def __init__(self, chains):
        self._chains = chains

    async def get_quote(self, position):
        return None

    async def get_chain(self, underlying: str):
        return list(self._chains.get(underlying, []))

    async def get_underlying_bars(self, underlying: str, lookback_days: int = 260):
        return []

    async def get_underlying_price(self, underlying: str):
        return None


class _RecordPicks:
    """Stands in for the executor. Records the sized pick and does not build an order."""

    def __init__(self):
        self.opened = []

    async def execute_open(self, decision, quote):
        self.opened.append(decision)
        return None


def _pick(decision) -> dict:
    return {
        "underlying": decision.underlying,
        "strike": decision.strike,
        "expiration": decision.expiration,
        "contracts": decision.contracts,
    }


@pytest.fixture()
def _frozen_clock(monkeypatch):
    monkeypatch.setattr("agentic.services.scanner.utcnow", _frozen_now)
    monkeypatch.setattr("agentic.entry.screener.utcnow", _frozen_now)
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    monkeypatch.setattr("agentic.services.scanner.is_order_window", lambda *a, **k: True)


@pytest.mark.asyncio
async def test_golden_csp_and_cc_picks(tmp_path, _frozen_clock):
    db = Database(tmp_path / "golden.db")
    audit = AuditStore(db)
    book = _ReadOnlyBook()
    picks = _RecordPicks()
    settings = Settings(
        mode="paper",
        broker="paper",
        market_data="paper",
        entry={"enabled": True, "watchlist": ["AAA", "BBB", "CCC", "HHH"]},
    )
    scanner = OpportunityScanner(
        settings, book, _ChainBook(_chains()), EntryDecisionStore(db), picks,
        audit, KillSwitch(db, audit), entry_candidates=EntryCandidateStore(db),
    )

    await scanner.run_once()

    csp = [_pick(d) for d in picks.opened if d.rule_name == "csp-screener"]
    cc = [_pick(d) for d in picks.opened if d.rule_name == "cc-screener"]
    assert csp == [dict(row) for row in GOLDEN_CSP]
    assert cc == [dict(row) for row in GOLDEN_CC]
    assert book.writes == []

    logged = list(db.conn.execute(
        """SELECT kind, underlying, strike, expiration, contracts
           FROM entry_candidates WHERE approved = 1 ORDER BY rowid"""
    ))
    expected_logged = [
        (kind, row["underlying"], row["strike"], row["expiration"].isoformat(), row["contracts"])
        for kind, rows in (("CSP", GOLDEN_CSP), ("CC", GOLDEN_CC))
        for row in rows
    ]
    assert [tuple(r) for r in logged] == expected_logged

    by_strike = {
        (r["underlying"], r["strike"]): r
        for r in db.conn.execute("SELECT * FROM entry_candidates ORDER BY rowid")
    }
    # Higher yield than the BBB put we actually pick, so a yield-based rank key would choose it.
    decoy, winner = by_strike[("BBB", 45.0)], by_strike[("BBB", 55.0)]
    assert decoy["approved"] == 0 and "already approved" in decoy["reason"]
    assert winner["approved"] == 1
    assert decoy["annualized_ror"] > winner["annualized_ror"]
    assert ("HHH", 40.0) not in by_strike


_LOCK_SOURCE = Path(__file__).resolve().parents[1] / "src" / "agentic" / "brokers" / "robinhood_mcp.py"
_BLOCKED_TOOLS = (
    "place_option_order",
    "place_equity_order",
    "cancel_option_order",
    "not_a_real_tool",
)


def _target_names(node: ast.AST):
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            yield from _target_names(elt)
    elif isinstance(node, ast.Starred):
        yield from _target_names(node.value)


def _orders_lock_stores(tree: ast.AST) -> list[ast.AST]:
    """Every assignment that stores ORDERS_HARD_DISABLED, including nested ones."""
    stores: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets = [node.target]
        else:
            continue
        if any(name == "ORDERS_HARD_DISABLED" for t in targets for name in _target_names(t)):
            stores.append(node)
    return stores


def _is_guard_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "assert_mcp_tool_allowed"
    return isinstance(func, ast.Attribute) and func.attr == "assert_mcp_tool_allowed"


def test_orders_hard_disabled_stays_true():
    """The module stores the lock once, as True, and _call_tool still asks the guard.

    A later `if` or function that assigns False, a guard that only names one write
    tool, or a _call_tool that no longer calls the guard all fail this test.
    """
    tree = ast.parse(_LOCK_SOURCE.read_text(encoding="utf-8"))
    stores = _orders_lock_stores(tree)
    assert len(stores) == 1
    store = stores[0]
    assert store in tree.body  # module level, not inside a function or conditional
    value = store.value
    assert isinstance(value, ast.Constant) and value.value is True

    methods = [
        item
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RobinhoodMCPBroker"
        for item in node.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "_call_tool"
    ]
    assert len(methods) == 1
    method = methods[0]
    tool_param = next(arg.arg for arg in method.args.args if arg.arg != "self")
    direct_calls = [
        stmt.value for stmt in method.body
        if isinstance(stmt, ast.Expr) and _is_guard_call(stmt.value)
    ]
    assert direct_calls
    assert any(
        call.args and isinstance(call.args[0], ast.Name) and call.args[0].id == tool_param
        for call in direct_calls
    )

    assert ORDERS_HARD_DISABLED is True
    for name in _BLOCKED_TOOLS:
        with pytest.raises(RobinhoodWriteBlocked):
            assert_mcp_tool_allowed(name)
    assert_mcp_tool_allowed("get_option_chains")
