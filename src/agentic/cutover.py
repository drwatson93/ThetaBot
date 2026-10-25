"""One-time cutover: archive old practice tables, then empty them.

Nothing here runs on startup, on a timer, or by itself. Mario's team runs it
after deploy, against a database that still has the old tables.

Order (step 1 is on the OLD deployed version, before this code is deployed):

1. Close the open practice put with that version's existing close control.
   This build removes the close buttons and the executor, so the close has to
   happen first.
2. Deploy this code. The old tables stay in the schema.
3. Export the archive and save it (``GET /control/legacy-archive`` or
   ``python -m agentic.cutover export --dir ...``).
4. Verify the manifest row counts match the live counts.
5. Wipe (``POST /control/legacy-wipe`` or ``python -m agentic.cutover wipe``).
   Wipe refuses unless the manifest counts match the live counts right then.

The archive is one CSV and one JSON per table, plus ``manifest.json``. Open
them with any spreadsheet or text editor. Export only reads. Wipe deletes
rows in the legacy tables and does not drop schema, and it does not touch
alerts, scan_runs, scorecard, audit, or any other table.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Practice-history tables. Schema stays; only rows are archived and emptied.
# close_claims is the order-path "one close owner" table. It has no writer
# once the executor is gone, and it is not part of alerts / scan_runs /
# scorecard / audit.
LEGACY_TABLES: tuple[str, ...] = (
    "orders",
    "positions",
    "trade_journal",
    "entry_decisions",
    "decisions",
    "tax_reserve",
    "close_claims",
)

# Never wiped, even if a manifest names them.
PROTECTED_TABLES: frozenset[str] = frozenset({
    "alerts",
    "scan_runs",
    "scorecard",
    "audit",
})


class CutoverRefused(Exception):
    """Wipe refused. The database is unchanged."""

    def __init__(self, message: str, *, mismatches: dict[str, Any] | None = None):
        super().__init__(message)
        self.mismatches = mismatches or {}


def _connect(db) -> Any:
    conn = getattr(db, "conn", db)
    if conn is None:
        raise CutoverRefused("no database connection")
    return conn


def _columns(conn, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [row[1] for row in rows]


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def live_counts(db) -> dict[str, int]:
    """Row counts for the legacy tables. Read-only."""
    conn = _connect(db)
    counts: dict[str, int] = {}
    for table in LEGACY_TABLES:
        if not _table_exists(conn, table):
            raise CutoverRefused(f"legacy table {table} is not in the schema")
        counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return counts


def _rows(conn, table: str) -> tuple[list[str], list[dict[str, Any]]]:
    columns = _columns(conn, table)
    if not columns:
        raise CutoverRefused(f"legacy table {table} has no columns")
    fetched = conn.execute(f"SELECT * FROM {table}").fetchall()
    rows: list[dict[str, Any]] = []
    for record in fetched:
        if isinstance(record, sqlite3.Row):
            rows.append({key: record[key] for key in record.keys()})
        else:
            rows.append({col: record[i] for i, col in enumerate(columns)})
    return columns, rows


def export_archive(db, out_dir: str | Path) -> dict[str, Any]:
    """Write one CSV and one JSON per legacy table, plus manifest.json.

    Reads every row and column. Does not write to the database.
    """
    conn = _connect(db)
    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)
    exported_at = datetime.now(timezone.utc).isoformat()
    tables: dict[str, Any] = {}
    for table in LEGACY_TABLES:
        if not _table_exists(conn, table):
            raise CutoverRefused(f"legacy table {table} is not in the schema")
        columns, rows = _rows(conn, table)
        csv_name = f"{table}.csv"
        json_name = f"{table}.json"
        with (dest / csv_name).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in columns})
        (dest / json_name).write_text(
            json.dumps(rows, indent=2, default=str),
            encoding="utf-8",
        )
        tables[table] = {
            "row_count": len(rows),
            "files": [csv_name, json_name],
            "exported_at": exported_at,
        }
    manifest = {"exported_at": exported_at, "tables": tables}
    (dest / "manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    return manifest


def _declared_counts(manifest: dict[str, Any] | None) -> dict[str, int]:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("tables"), dict):
        raise CutoverRefused("manifest missing")
    tables = manifest["tables"]
    declared: dict[str, int] = {}
    for table in LEGACY_TABLES:
        info = tables.get(table)
        if not isinstance(info, dict) or "row_count" not in info:
            raise CutoverRefused(f"manifest missing row_count for {table}")
        try:
            declared[table] = int(info["row_count"])
        except (TypeError, ValueError) as exc:
            raise CutoverRefused(f"manifest row_count for {table} is not an integer") from exc
    protected = set(tables) & PROTECTED_TABLES
    if protected:
        raise CutoverRefused(
            "manifest names protected tables: " + ", ".join(sorted(protected))
        )
    return declared


def wipe_legacy(db, manifest: dict[str, Any] | None) -> dict[str, int]:
    """Delete every row in the legacy tables.

    Refuses when there is no manifest, or when any live count differs from the
    manifest. Counts are read inside the write transaction, so a row that
    landed after the archive was taken is not deleted. Does not drop tables.
    Does not touch alerts, scan_runs, scorecard, audit, or any table outside
    the legacy list.
    """
    declared = _declared_counts(manifest)
    conn = _connect(db)
    conn.execute("BEGIN IMMEDIATE")
    try:
        live: dict[str, int] = {}
        for table in LEGACY_TABLES:
            if not _table_exists(conn, table):
                raise CutoverRefused(f"legacy table {table} is not in the schema")
            live[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        mismatches = {
            table: {"live": live[table], "manifest": declared[table]}
            for table in LEGACY_TABLES
            if live[table] != declared[table]
        }
        if mismatches:
            raise CutoverRefused("row counts do not match", mismatches=mismatches)
        for table in LEGACY_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return live


class _ConnDB:
    """Give a raw sqlite connection the ``.conn`` attribute AuditStore expects."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn


def record_cutover(db, action: str, counts: dict[str, int]) -> None:
    """Audit one export or wipe. Counts only — no row contents."""
    from .domain.enums import AuditEventType
    from .store.audit import AuditStore

    target = _ConnDB(db) if isinstance(db, sqlite3.Connection) else db
    AuditStore(target).record(
        AuditEventType.RECONCILE,
        {"cutover": action, "counts": {name: int(n) for name, n in counts.items()}},
        source="cutover",
    )


def _open(path: Path, *, readonly: bool):
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _default_db() -> Path:
    from .config import REPO_ROOT
    return REPO_ROOT / "data" / "agentic.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agentic.cutover",
        description=(
            "Archive or empty the old practice tables. Does not run unless invoked. "
            "Close the open practice put on the previous deploy before exporting."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    export_p = sub.add_parser("export", help="write CSV, JSON, and manifest.json")
    export_p.add_argument("--dir", required=True, help="directory to write the archive into")
    export_p.add_argument("--db", default=None, help="sqlite file (default: data/agentic.db)")
    wipe_p = sub.add_parser("wipe", help="empty legacy tables when manifest counts match")
    wipe_p.add_argument("--dir", required=True, help="directory that contains manifest.json")
    wipe_p.add_argument("--db", default=None, help="sqlite file (default: data/agentic.db)")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else _default_db()
    if not db_path.is_file():
        print(f"database file not found: {db_path}", file=sys.stderr)
        return 2

    if args.cmd == "export":
        read = _open(db_path, readonly=True)
        try:
            manifest = export_archive(read, args.dir)
        except CutoverRefused as exc:
            print(str(exc), file=sys.stderr)
            return 1
        finally:
            read.close()
        counts = {name: info["row_count"] for name, info in manifest["tables"].items()}
        write = _open(db_path, readonly=False)
        try:
            record_cutover(write, "export", counts)
        finally:
            write.close()
        print(json.dumps({"ok": True, "exported_at": manifest["exported_at"], "counts": counts}))
        return 0

    manifest_path = Path(args.dir) / "manifest.json"
    if not manifest_path.is_file():
        print("manifest.json not found in --dir", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    write = _open(db_path, readonly=False)
    try:
        try:
            wiped = wipe_legacy(write, manifest)
        except CutoverRefused as exc:
            print(str(exc), file=sys.stderr)
            if exc.mismatches:
                print(json.dumps(exc.mismatches), file=sys.stderr)
            return 1
        record_cutover(write, "wipe", wiped)
    finally:
        write.close()
    print(json.dumps({"ok": True, "wiped": wiped}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
