"""Golden pins for today's CSP and CC scanner picks, plus the order lock.

The fixture is synthetic (fake tickers, made-up prices). The scan runs through
OpportunityScanner.run_once — the same screen, rank, and size path the bot uses —
and the executor double records the pick and returns without placing an order.
No broker write method is callable from this test.

Captured against the scanner as it stands: buying power 25_000, account value
80_000, tax-reserve holding absent (reserve value 0), default criteria and sizing.
"""
from __future__ import annotations

import re
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
            # Yield too thin to pass the screen.
            _contract("AAA", "put", 48, 35, -0.26, 0.10, 0.12, -0.20),
        ],
        "BBB": [
            _contract("BBB", "put", 55, 40, -0.24, 1.50, 1.60, -0.09),
            _contract("BBB", "put", 50, 40, -0.22, 1.30, 1.40, -0.05),  # passes, loses rank
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
        return 25_000.0

    async def get_account_value(self) -> float:
        return 80_000.0

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
        entry={"enabled": True, "watchlist": ["AAA", "BBB", "CCC"]},
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

    logged = [
        (r["kind"], r["underlying"], r["strike"], r["expiration"], r["contracts"])
        for r in EntryCandidateStore(db).recent(50)
        if r["approved"]
    ]
    expected_logged = (
        [("CSP", row["underlying"], row["strike"], row["expiration"].isoformat(), row["contracts"])
         for row in GOLDEN_CSP]
        + [("CC", row["underlying"], row["strike"], row["expiration"].isoformat(), row["contracts"])
           for row in GOLDEN_CC]
    )
    assert sorted(logged) == sorted(expected_logged)


def test_orders_hard_disabled_stays_true():
    """The source assignment stays True, and a write tool is still refused with no network call."""
    path = Path(__file__).resolve().parents[1] / "src" / "agentic" / "brokers" / "robinhood_mcp.py"
    text = path.read_text(encoding="utf-8")
    assignments = re.findall(r"^ORDERS_HARD_DISABLED\s*=\s*(.+?)\s*$", text, re.M)
    assert assignments == ["True"]
    assert ORDERS_HARD_DISABLED is True
    with pytest.raises(RobinhoodWriteBlocked):
        assert_mcp_tool_allowed("place_option_order")
    assert_mcp_tool_allowed("get_option_chains")
