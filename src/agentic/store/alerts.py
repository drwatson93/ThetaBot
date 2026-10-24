"""Alerts the scanner emits, and the scan_runs log.

A play's dedup key is ``kind:underlying:occ`` with no date. Inserting the same
key again is a no-op, so a repeat scan does not create a second alert.
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
        """Insert one alert. Returns its id, or None if this play was already alerted."""
        alert_id = uuid.uuid4().hex
        cur = self.db.conn.execute(
            """INSERT OR IGNORE INTO alerts
                 (id, dedup_key, kind, underlying, occ_symbol, strike, expiration,
                  contracts, premium, bid, ask, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                alert_id, row["dedup_key"], row["kind"], row["underlying"], row["occ_symbol"],
                row["strike"], row["expiration"], row["contracts"], row.get("premium"),
                row.get("bid"), row.get("ask"), row.get("created_at") or utcnow().isoformat(),
            ),
        )
        self.db.conn.commit()
        if cur.rowcount <= 0:
            return None
        return alert_id

    def all(self) -> list[dict[str, Any]]:
        rows = self.db.conn.execute("SELECT * FROM alerts ORDER BY rowid").fetchall()
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
