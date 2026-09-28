"""EntryDecision repository with dedup-on-insert (opening-side counterpart to decisions.py).

dedup_key (underlying:expiration:strike:day) has a UNIQUE index, so re-screening the same
contract on the same day does not create a second entry decision.
"""
from __future__ import annotations

from datetime import date, datetime

from ..domain.enums import DecisionStatus
from ..domain.models import EntryDecision
from .db import Database


def _col(r, key, default=None):
    try:
        v = r[key]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


def _row_to_entry(r) -> EntryDecision:
    return EntryDecision(
        id=r["id"],
        underlying=r["underlying"],
        occ_symbol=r["occ_symbol"],
        option_id=r["option_id"],
        strike=r["strike"],
        expiration=date.fromisoformat(r["expiration"]),
        contracts=r["contracts"],
        premium=r["premium"],
        rule_name=r["rule_name"],
        reason=r["reason"],
        dedup_key=r["dedup_key"],
        status=DecisionStatus(r["status"]),
        created_at=datetime.fromisoformat(r["created_at"]),
        decided_at=datetime.fromisoformat(r["decided_at"]) if r["decided_at"] else None,
        order_type=_col(r, "order_type"),
        limit_price=_col(r, "limit_price"),
        bid=_col(r, "bid"),
        ask=_col(r, "ask"),
        mid=_col(r, "mid"),
        time_in_force=_col(r, "time_in_force"),
    )


class EntryDecisionStore:
    def __init__(self, db: Database):
        self.db = db

    def insert_if_new(self, d: EntryDecision) -> bool:
        """Insert the entry decision. Returns True if newly inserted, False if a row with the
        same dedup_key already exists."""
        cur = self.db.conn.execute(
            """INSERT OR IGNORE INTO entry_decisions
                 (id, underlying, occ_symbol, option_id, strike, expiration, contracts,
                  premium, rule_name, reason, status, dedup_key, created_at, decided_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                d.id, d.underlying, d.occ_symbol, d.option_id, d.strike,
                d.expiration.isoformat(), d.contracts, d.premium, d.rule_name, d.reason,
                d.status.value, d.dedup_key, d.created_at.isoformat(),
                d.decided_at.isoformat() if d.decided_at else None,
            ),
        )
        self.db.conn.commit()
        return cur.rowcount > 0

    def set_status(self, decision_id: str, status: DecisionStatus) -> None:
        from ..domain.models import utcnow
        self.db.conn.execute(
            "UPDATE entry_decisions SET status = ?, decided_at = ? WHERE id = ?",
            (status.value, utcnow().isoformat(), decision_id),
        )
        self.db.conn.commit()

    def set_order_snapshot(
        self,
        decision_id: str,
        *,
        order_type: str | None,
        limit_price: float | None,
        bid: float | None,
        ask: float | None,
        mid: float | None,
        time_in_force: str | None,
    ) -> None:
        """Stamp the priced order onto the entry decision (additive; does not change status)."""
        self.db.conn.execute(
            """UPDATE entry_decisions SET
                 order_type = ?, limit_price = ?, bid = ?, ask = ?, mid = ?, time_in_force = ?
               WHERE id = ?""",
            (order_type, limit_price, bid, ask, mid, time_in_force, decision_id),
        )
        self.db.conn.commit()

    def get(self, decision_id: str) -> EntryDecision | None:
        r = self.db.conn.execute(
            "SELECT * FROM entry_decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        return _row_to_entry(r) if r else None

    def recent(self, limit: int = 100) -> list[EntryDecision]:
        rows = self.db.conn.execute(
            "SELECT * FROM entry_decisions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def list_by_status(self, status: DecisionStatus) -> list[EntryDecision]:
        rows = self.db.conn.execute(
            "SELECT * FROM entry_decisions WHERE status = ? ORDER BY created_at DESC",
            (status.value,),
        ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def most_recent_by_occ(self, occ_symbol: str) -> EntryDecision | None:
        """The latest entry decision for a contract (any status), or None."""
        r = self.db.conn.execute(
            "SELECT * FROM entry_decisions WHERE occ_symbol = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (occ_symbol,),
        ).fetchone()
        return _row_to_entry(r) if r else None

    def premium_collected_since(self, since_iso: str) -> float:
        """Total CSP premium (credit) actually collected since ``since_iso`` — DONE entries only.

        premium is per-share credit, so dollars = premium * contracts * 100.
        """
        rows = self.db.conn.execute(
            "SELECT premium, contracts FROM entry_decisions "
            "WHERE status = ? AND created_at >= ?",
            (DecisionStatus.DONE.value, since_iso),
        ).fetchall()
        return round(sum((r["premium"] or 0.0) * (r["contracts"] or 0) * 100 for r in rows), 2)
