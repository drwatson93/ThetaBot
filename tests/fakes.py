"""In-test stand-ins for brokers and stores this release removed.

The application no longer has a paper broker or an orders store. Tests that
still need a book of positions or an empty order list use these. They do not
place orders.
"""
from __future__ import annotations

from datetime import timedelta

from agentic.brokers.base import BrokerCapabilities
from agentic.domain.enums import Direction, OptionType, PositionStatus, Strategy
from agentic.domain.models import EquityHolding, Position, utcnow


def _default_seed() -> list[Position]:
    today = utcnow().date()
    return [
        Position(
            occ_symbol="AAA" + (today + timedelta(days=30)).strftime("%y%m%d") + "C00250000",
            underlying="AAA",
            option_type=OptionType.CALL,
            strategy=Strategy.COVERED_CALL,
            direction=Direction.SHORT,
            quantity=1,
            strike=250.0,
            expiration=today + timedelta(days=30),
            credit_received=2.00,
            open_avg_price=2.00,
            current_bid=1.45,
            current_ask=1.55,
            current_mark=1.50,
            delta=0.30,
            iv=0.28,
            status=PositionStatus.OPEN,
            broker_position_id="paper-aaa-cc",
            opened_at=utcnow(),
        ),
        Position(
            occ_symbol="BBB" + (today + timedelta(days=15)).strftime("%y%m%d") + "P00400000",
            underlying="BBB",
            option_type=OptionType.PUT,
            strategy=Strategy.CASH_SECURED_PUT,
            direction=Direction.SHORT,
            quantity=2,
            strike=400.0,
            expiration=today + timedelta(days=15),
            credit_received=3.50,
            open_avg_price=3.50,
            current_bid=2.40,
            current_ask=2.50,
            current_mark=2.45,
            delta=-0.18,
            iv=0.31,
            status=PositionStatus.OPEN,
            broker_position_id="paper-bbb-csp",
            opened_at=utcnow(),
        ),
    ]


class PaperBroker:
    """Read-only book. ``submit_*`` raises so a test cannot silently place an order."""

    def __init__(
        self,
        seed_positions: list[Position] | None = None,
        buying_power: float = 100_000.0,
        holdings: list[EquityHolding] | None = None,
        persist_path=None,
    ):
        self._positions = {}
        self._buying_power = buying_power
        self._holdings = list(holdings or [])
        for p in (seed_positions if seed_positions is not None else _default_seed()):
            self._positions[p.occ_symbol] = p

    async def connect(self) -> None:
        return None

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            name="paper", supports_options_orders=False, is_paper=True,
            notes="Test book. Does not place orders.",
        )

    async def get_open_positions(self) -> list[Position]:
        return [
            p for p in self._positions.values()
            if p.status in (PositionStatus.OPEN, PositionStatus.CLOSING)
        ]

    async def get_buying_power(self) -> float:
        return self._buying_power

    async def get_account_value(self) -> float:
        return self._buying_power

    async def get_equity_positions(self) -> list[EquityHolding]:
        return list(self._holdings)

    async def get_order(self, order):
        return order

    def set_mark(self, occ_symbol: str, bid: float, ask: float) -> None:
        pos = self._positions.get(occ_symbol)
        if pos:
            pos.current_bid = bid
            pos.current_ask = ask
            pos.current_mark = round((bid + ask) / 2, 4)

    def _refuse(self, name: str):
        raise AssertionError(f"test book refuses {name}")

    async def submit_open_order(self, order):
        self._refuse("submit_open_order")

    async def submit_close_order(self, order):
        self._refuse("submit_close_order")

    async def cancel_order(self, order):
        self._refuse("cancel_order")

    async def submit_equity_order(self, **kwargs):
        self._refuse("submit_equity_order")


class RecordingReadBroker:
    """Records the account_number the monitor passes into the positions read.

    Stands in for the Robinhood session. ``get_open_positions`` stores the argument
    the live broker sends to ``get_option_positions``. Synthetic ids only.
    """

    def __init__(self, account_number: str = "", positions: list[Position] | None = None):
        self.account_number = str(account_number or "").strip()
        self._positions = list(positions or [])
        self.position_calls: list[dict] = []
        self._account_unresolved = not bool(self.account_number)

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            name="recording", supports_options_orders=False, is_paper=False,
            notes="recording fake",
        )

    async def ensure_account_number(self, account_number: str | None = None) -> str:
        explicit = str(account_number or "").strip()
        if explicit:
            return explicit
        return self.account_number

    async def get_open_positions(self, account_number: str | None = None):
        acct = str(account_number or "").strip()
        self.position_calls.append({
            "tool": "get_option_positions",
            "account_number": acct,
        })
        if not acct:
            return []
        return list(self._positions)

    async def get_order(self, order):
        return order


class OrderStore:
    """Empty stand-in. The orders table still exists in the schema; this is not that store."""

    def __init__(self, db=None):
        self.db = db
        self._rows: list = []

    def list_all(self):
        return list(self._rows)

    def list_by_position(self, position_id):
        return [r for r in self._rows if getattr(r, "position_id", None) == position_id]

    def list_by_status(self, status):
        return []

    def insert_if_new(self, order) -> bool:
        self._rows.append(order)
        return True

    def update(self, order) -> None:
        return None

    def get_by_client_order_id(self, client_order_id):
        return None


class OrderExecutor:
    """Stand-in so the lock test can still assert a paper fill without the app executor.

    The application no longer has this class. ``execute_open`` records a filled
    paper order on the book it was given. ``execute_close`` does not place an order.
    """

    def __init__(self, *args, **kwargs):
        self.broker = args[1] if len(args) > 1 else None
        self.entry_decisions = kwargs.get("entry_decisions")
        self.notifier = kwargs.get("notifier")
        self.closes: list = []

    async def execute_open(self, decision, quote):
        from agentic.domain.enums import Direction, OptionType, OrderStatus, PositionStatus, Strategy
        from agentic.domain.models import Order

        order = Order(
            decision_id=getattr(decision, "id", "d"),
            position_id="paper-fill",
            occ_symbol=decision.occ_symbol,
            quantity=int(decision.contracts),
            limit_price=float(quote.mark),
            is_paper=True,
            status=OrderStatus.FILLED,
            option_id=getattr(decision, "option_id", None),
            filled_qty=int(decision.contracts),
            avg_fill_price=float(quote.mark),
        )
        broker = self.broker
        if broker is not None and hasattr(broker, "_positions"):
            broker._positions[decision.occ_symbol] = Position(
                occ_symbol=decision.occ_symbol,
                underlying=decision.underlying,
                option_type=OptionType.PUT,
                strategy=Strategy.CASH_SECURED_PUT,
                direction=Direction.SHORT,
                quantity=int(decision.contracts),
                strike=float(decision.strike),
                expiration=decision.expiration,
                credit_received=float(quote.mark),
                status=PositionStatus.OPEN,
                is_paper=True,
            )
        return order

    async def execute_close(self, *args, **kwargs):
        raise AssertionError("executor is gone")


class ApprovalGate:
    def __init__(self, *args, **kwargs):
        self.requests = []

    async def request(self, position, decision):
        self.requests.append(decision)

    def expire_stale(self) -> int:
        return 0


class LiveOrderBlocked(RuntimeError):
    pass


def assert_live_order_allowed(*args, **kwargs):
    raise LiveOrderBlocked("live orders are not a feature")


def compute_limit_price(*args, **kwargs):
    raise AssertionError("order pricing moved out with the executor")


def compute_open_limit_price(*args, **kwargs):
    raise AssertionError("order pricing moved out with the executor")


def round_to_tick(*args, **kwargs):
    raise AssertionError("order pricing moved out with the executor")
