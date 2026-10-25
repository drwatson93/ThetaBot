"""Alerts the scanner emits, the scan_runs log, and one scorecard row per alert.

A play's dedup key is ``kind:underlying:occ`` with no date. The row is inserted
once. ``delivery_status`` is pending, sent, or failed. A later scan retries a
row that is not sent, and does not post one that already was.

``status`` starts at new. A later scan marks it taken, stale, or expired.
The scorecard row is written with the alert and graded once, at expiry.
"""
from __future__ import annotations

import uuid
from typing import Any

from ..domain.models import utcnow
from .db import Database


class AlertStore:
    def __init__(self, db: Database):
        self.db = db

    def insert_if_new(self, row: dict[str, Any]) -> str | None:
        """Insert one alert as pending. Returns its id, or None if this play exists."""
        alert_id = uuid.uuid4().hex
        cur = self.db.conn.execute(
            """INSERT OR IGNORE INTO alerts
                 (id, dedup_key, kind, underlying, occ_symbol, strike, expiration,
                  contracts, premium, bid, ask, created_at, delivery_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                alert_id, row["dedup_key"], row["kind"], row["underlying"], row["occ_symbol"],
                row["strike"], row["expiration"], row["contracts"], row.get("premium"),
                row.get("bid"), row.get("ask"), row.get("created_at") or utcnow().isoformat(),
                "pending",
            ),
        )
        if cur.rowcount <= 0:
            self.db.conn.commit()
            return None
        self.db.conn.execute(
            """INSERT OR IGNORE INTO scorecard
                 (alert_id, strike, expiration, contracts, mid, taken)
               VALUES (?,?,?,?,?,0)""",
            (
                alert_id, row["strike"], row["expiration"], row["contracts"],
                row.get("premium"),
            ),
        )
        self.db.conn.commit()
        return alert_id

    def get_by_dedup(self, dedup_key: str) -> dict[str, Any] | None:
        row = self.db.conn.execute(
            "SELECT * FROM alerts WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        return dict(row) if row else None

    def set_delivery_status(self, alert_id: str, status: str) -> None:
        if status not in ("pending", "sent", "failed"):
            raise ValueError(f"bad delivery status {status!r}")
        self.db.conn.execute(
            "UPDATE alerts SET delivery_status = ? WHERE id = ?",
            (status, alert_id),
        )
        self.db.conn.commit()

    def all(self) -> list[dict[str, Any]]:
        rows = self.db.conn.execute("SELECT * FROM alerts ORDER BY rowid").fetchall()
        return [dict(r) for r in rows]

    def open_entries(self) -> list[dict[str, Any]]:
        """CSP and CC alerts still waiting on a later scan. Exit alerts are not these."""
        rows = self.db.conn.execute(
            """SELECT * FROM alerts
               WHERE kind IN ('CSP', 'CC') AND status IN ('new', 'stale')
               ORDER BY rowid"""
        ).fetchall()
        return [dict(r) for r in rows]

    def ungraded_entries(self) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            """SELECT a.* FROM alerts a
               JOIN scorecard s ON s.alert_id = a.id
               WHERE a.kind IN ('CSP', 'CC') AND s.graded_at IS NULL
               ORDER BY a.rowid"""
        ).fetchall()
        return [dict(r) for r in rows]

    def unsent(self, kind: str) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            """SELECT * FROM alerts
               WHERE kind = ? AND delivery_status != 'sent'
               ORDER BY rowid""",
            (kind,),
        ).fetchall()
        return [dict(r) for r in rows]

    def taken_entry_for_occ(self, occ: str) -> dict[str, Any] | None:
        row = self.db.conn.execute(
            """SELECT * FROM alerts
               WHERE occ_symbol = ? AND status = 'taken' AND kind IN ('CSP', 'CC')
               ORDER BY taken_at DESC LIMIT 1""",
            (occ,),
        ).fetchone()
        return dict(row) if row else None

    def mark_taken(
        self, alert_id: str, *, fill_price: float | None, taken_at: str,
        late: bool, partial: bool,
    ) -> None:
        self.db.conn.execute(
            """UPDATE alerts
                 SET status = 'taken', fill_price = ?, taken_at = ?, late = ?, partial = ?
               WHERE id = ? AND status IN ('new', 'stale')""",
            (fill_price, taken_at, 1 if late else 0, 1 if partial else 0, alert_id),
        )
        self.db.conn.execute(
            "UPDATE scorecard SET taken = 1 WHERE alert_id = ?",
            (alert_id,),
        )
        self.db.conn.commit()

    def mark_stale(self, alert_id: str, stale_at: str) -> None:
        self.db.conn.execute(
            """UPDATE alerts SET status = 'stale', stale_at = ?
               WHERE id = ? AND status = 'new'""",
            (stale_at, alert_id),
        )
        self.db.conn.commit()

    def mark_expired(self, alert_id: str) -> None:
        self.db.conn.execute(
            """UPDATE alerts SET status = 'expired'
               WHERE id = ? AND status IN ('new', 'stale')""",
            (alert_id,),
        )
        self.db.conn.commit()

    def grade(
        self, alert_id: str, *, taken: bool, result: str, pnl: float, graded_at: str,
    ) -> None:
        """Write the expiry result once. A second call does not change it."""
        self.db.conn.execute(
            """UPDATE scorecard
                 SET taken = ?, result = ?, pnl = ?, graded_at = ?
               WHERE alert_id = ? AND graded_at IS NULL""",
            (1 if taken else 0, result, pnl, graded_at, alert_id),
        )
        self.db.conn.commit()

    def scorecards(self) -> list[dict[str, Any]]:
        rows = self.db.conn.execute("SELECT * FROM scorecard ORDER BY rowid").fetchall()
        return [dict(r) for r in rows]

    def record_run(
        self, *, status: str, reason: str | None, alerts_sent: int, scanned_at
    ) -> None:
        self.db.conn.execute(
            """INSERT INTO scan_runs (id, scanned_at, status, reason, alerts_sent)
               VALUES (?,?,?,?,?)""",
            (uuid.uuid4().hex, scanned_at.isoformat(), status, reason, int(alerts_sent)),
        )
        self.db.conn.commit()

    def runs(self) -> list[dict[str, Any]]:
        rows = self.db.conn.execute("SELECT * FROM scan_runs ORDER BY rowid").fetchall()
        return [dict(r) for r in rows]

    def latest_run(self) -> dict[str, Any] | None:
        row = self.db.conn.execute(
            "SELECT * FROM scan_runs ORDER BY scanned_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
