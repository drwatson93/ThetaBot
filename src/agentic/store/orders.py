"""Order repository.

The executor persists an Order row in PENDING state **before** it submits to the broker,
keyed by ``client_order_id`` (UNIQUE). This is the idempotency backstop: if we crash
between persist and submit, the row tells us a submit may be in flight, and reusing the
same ``client_order_id`` on retry prevents a duplicate broker order.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from ..domain.enums import OrderStatus
from ..domain.models import Order, utcnow
from .db import Database

# Statuses that mean a buy-to-close has started or finished. A second close for the
# same position is refused while any of these rows exist. REJECTED and CANCELLED are
# absent so a failed close can be retried. Keep the partial index in db.py in sync.
LIVE_CLOSE_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.PARTIAL,
    OrderStatus.FILLED,
)
_LIVE_CLOSE_SQL = ",".join("?" for _ in LIVE_CLOSE_STATUSES)


def _col(r, key, default=None):
    try:
        v = r[key]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


def _row_to_order(r) -> Order:
    return Order(
        id=r["id"],
        decision_id=r["decision_id"],
        position_id=r["position_id"],
        client_order_id=r["client_order_id"],
        broker_order_id=r["broker_order_id"],
        option_id=r["option_id"],
        occ_symbol=r["occ_symbol"],
        side=r["side"],
        order_type=r["order_type"],
        quantity=r["quantity"],
        limit_price=r["limit_price"],
        filled_qty=r["filled_qty"],
        avg_fill_price=r["avg_fill_price"],
        status=OrderStatus(r["status"]),
        is_paper=bool(r["is_paper"]),
        bid=_col(r, "bid"),
        ask=_col(r, "ask"),
        mid=_col(r, "mid"),
        time_in_force=_col(r, "time_in_force"),
        submitted_at=datetime.fromisoformat(r["submitted_at"]) if r["submitted_at"] else None,
        last_status_at=datetime.fromisoformat(r["last_status_at"]) if r["last_status_at"] else None,
    )


class OrderStore:
    def __init__(self, db: Database):
        self.db = db

    def insert_if_new(self, o: Order) -> bool:
        """Persist a PENDING order. Returns True if newly inserted, False if a row with the
        same client_order_id already exists (idempotent submit guard)."""
        cur = self.db.conn.execute(
            """INSERT OR IGNORE INTO orders
                 (id, decision_id, position_id, client_order_id, broker_order_id, option_id,
                  occ_symbol, side, order_type, quantity, limit_price, filled_qty, avg_fill_price,
                  status, is_paper, submitted_at, last_status_at, bid, ask, mid, time_in_force)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                o.id, o.decision_id, o.position_id, o.client_order_id, o.broker_order_id,
                o.option_id, o.occ_symbol, o.side, o.order_type, o.quantity, o.limit_price,
                o.filled_qty, o.avg_fill_price, o.status.value, 1 if o.is_paper else 0,
                o.submitted_at.isoformat() if o.submitted_at else None,
                o.last_status_at.isoformat() if o.last_status_at else None,
                o.bid, o.ask, o.mid, o.time_in_force,
            ),
        )
        self.db.conn.commit()
        return cur.rowcount > 0

    def begin_live_close(self, o: Order, *, busy: bool) -> tuple[str, Order | None]:
        """Atomically claim ``o`` as the one live buy-to-close for its position.

        Returns ``(result, order)``:

        * ``inserted`` — this row is the new PENDING close.
        * ``resume`` — the same ``client_order_id`` is already the live close, or a
          rejected/cancelled row for that id was reactivated to PENDING.
        * ``filled`` — a buy-to-close for this position already filled.
        * ``blocked`` — another close is pending, submitted, partial, or this process
          is already inside ``execute_close`` for the position (``busy``).

        ``busy`` is the in-process owner flag. It closes the gap where the working
        order has been cancelled for a reprice and the replacement is not inserted yet.
        """
        conn = self.db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            live = _row_to_order(row) if (row := _fetch_live_close(conn, o.position_id)) else None
            if live is not None:
                if live.status == OrderStatus.FILLED:
                    _upsert_claim_tx(conn, live)
                    conn.commit()
                    return "filled", live
                if busy or live.client_order_id != o.client_order_id:
                    conn.commit()
                    return "blocked", live
                _upsert_claim_tx(conn, live)
                conn.commit()
                return "resume", live

            if busy:
                claimed = _order_for_claim(conn, o.position_id)
                conn.commit()
                return "blocked", claimed

            conn.execute("DELETE FROM close_claims WHERE position_id = ?", (o.position_id,))
            existing_row = conn.execute(
                "SELECT * FROM orders WHERE client_order_id = ?", (o.client_order_id,)
            ).fetchone()
            if existing_row is not None:
                existing = _row_to_order(existing_row)
                if existing.status == OrderStatus.FILLED:
                    _upsert_claim_tx(conn, existing)
                    conn.commit()
                    return "filled", existing
                if existing.status in LIVE_CLOSE_STATUSES:
                    _upsert_claim_tx(conn, existing)
                    conn.commit()
                    return "resume", existing
                if existing.status not in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
                    conn.commit()
                    return "blocked", existing
                _reactivate_tx(conn, o)
                refreshed = _row_to_order(conn.execute(
                    "SELECT * FROM orders WHERE client_order_id = ?", (o.client_order_id,)
                ).fetchone())
                _upsert_claim_tx(conn, refreshed)
                conn.commit()
                return "resume", refreshed

            _insert_order_tx(conn, o)
            _upsert_claim_tx(conn, o)
            conn.commit()
            return "inserted", o
        except sqlite3.IntegrityError:
            conn.rollback()
        except Exception:
            conn.rollback()
            raise
        live = _row_to_order(row) if (row := _fetch_live_close(conn, o.position_id)) else None
        return "blocked", live

    def upsert_close_claim(self, o: Order) -> None:
        """Point the position's persisted claim at ``o`` (the order now working or filled)."""
        _upsert_claim_tx(self.db.conn, o)
        self.db.conn.commit()

    def clear_close_claim(self, position_id: str) -> None:
        self.db.conn.execute(
            "DELETE FROM close_claims WHERE position_id = ?", (position_id,)
        )
        self.db.conn.commit()

    def get_close_claim(self, position_id: str) -> str | None:
        row = self.db.conn.execute(
            "SELECT client_order_id FROM close_claims WHERE position_id = ?",
            (position_id,),
        ).fetchone()
        return row["client_order_id"] if row else None

    def update(self, o: Order) -> None:
        """Persist the latest broker-reported state of an order."""
        self.db.conn.execute(
            """UPDATE orders SET
                 broker_order_id = ?, status = ?, filled_qty = ?, avg_fill_price = ?,
                 limit_price = ?, submitted_at = ?, last_status_at = ?,
                 bid = ?, ask = ?, mid = ?, time_in_force = ?
               WHERE client_order_id = ?""",
            (
                o.broker_order_id, o.status.value, o.filled_qty, o.avg_fill_price,
                o.limit_price,
                o.submitted_at.isoformat() if o.submitted_at else None,
                o.last_status_at.isoformat() if o.last_status_at else None,
                o.bid, o.ask, o.mid, o.time_in_force,
                o.client_order_id,
            ),
        )
        self.db.conn.commit()

    def get_by_client_order_id(self, client_order_id: str) -> Order | None:
        r = self.db.conn.execute(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        return _row_to_order(r) if r else None

    def list_all(self) -> list[Order]:
        """All orders, newest first — for the dashboard / P&L attribution."""
        rows = self.db.conn.execute(
            "SELECT * FROM orders ORDER BY COALESCE(submitted_at, '') DESC"
        ).fetchall()
        return [_row_to_order(r) for r in rows]

    def list_by_position(self, position_id: str) -> list[Order]:
        """All orders for a position (for close-P&L attribution during reconcile settlement)."""
        rows = self.db.conn.execute(
            "SELECT * FROM orders WHERE position_id = ? ORDER BY submitted_at", (position_id,)
        ).fetchall()
        return [_row_to_order(r) for r in rows]

    def list_by_status(self, *statuses: OrderStatus) -> list[Order]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        rows = self.db.conn.execute(
            f"SELECT * FROM orders WHERE status IN ({placeholders}) ORDER BY submitted_at",
            tuple(s.value for s in statuses),
        ).fetchall()
        return [_row_to_order(r) for r in rows]


def _order_insert_params(o: Order) -> tuple:
    return (
        o.id, o.decision_id, o.position_id, o.client_order_id, o.broker_order_id,
        o.option_id, o.occ_symbol, o.side, o.order_type, o.quantity, o.limit_price,
        o.filled_qty, o.avg_fill_price, o.status.value, 1 if o.is_paper else 0,
        o.submitted_at.isoformat() if o.submitted_at else None,
        o.last_status_at.isoformat() if o.last_status_at else None,
        o.bid, o.ask, o.mid, o.time_in_force,
    )


def _insert_order_tx(conn, o: Order) -> None:
    conn.execute(
        """INSERT INTO orders
             (id, decision_id, position_id, client_order_id, broker_order_id, option_id,
              occ_symbol, side, order_type, quantity, limit_price, filled_qty, avg_fill_price,
              status, is_paper, submitted_at, last_status_at, bid, ask, mid, time_in_force)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        _order_insert_params(o),
    )


def _fetch_live_close(conn, position_id: str):
    """The filled close if there is one, otherwise any in-flight buy-to-close."""
    return conn.execute(
        f"""SELECT * FROM orders
            WHERE position_id = ? AND side = 'BUY_TO_CLOSE'
              AND status IN ({_LIVE_CLOSE_SQL})
            ORDER BY CASE status
                       WHEN 'FILLED' THEN 0
                       WHEN 'PARTIAL' THEN 1
                       WHEN 'SUBMITTED' THEN 2
                       ELSE 3
                     END
            LIMIT 1""",
        (position_id, *(s.value for s in LIVE_CLOSE_STATUSES)),
    ).fetchone()


def _order_for_claim(conn, position_id: str) -> Order | None:
    claim = conn.execute(
        "SELECT client_order_id FROM close_claims WHERE position_id = ?",
        (position_id,),
    ).fetchone()
    if claim is None:
        return None
    row = conn.execute(
        "SELECT * FROM orders WHERE client_order_id = ?",
        (claim["client_order_id"],),
    ).fetchone()
    return _row_to_order(row) if row else None


def _upsert_claim_tx(conn, o: Order) -> None:
    conn.execute(
        """INSERT INTO close_claims (position_id, client_order_id, decision_id, claimed_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(position_id) DO UPDATE SET
             client_order_id = excluded.client_order_id,
             decision_id = excluded.decision_id,
             claimed_at = excluded.claimed_at""",
        (o.position_id, o.client_order_id, o.decision_id, utcnow().isoformat()),
    )


def _reactivate_tx(conn, o: Order) -> None:
    """Turn a rejected or cancelled row back into the single PENDING close."""
    conn.execute(
        """UPDATE orders SET
             status = ?, limit_price = ?, bid = ?, ask = ?, mid = ?,
             time_in_force = ?, quantity = ?, broker_order_id = NULL,
             filled_qty = 0, avg_fill_price = NULL, submitted_at = NULL,
             last_status_at = ?
           WHERE client_order_id = ?""",
        (
            OrderStatus.PENDING.value, o.limit_price, o.bid, o.ask, o.mid,
            o.time_in_force, o.quantity, utcnow().isoformat(), o.client_order_id,
        ),
    )
