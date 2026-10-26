"""Cutover archive/wipe and the deleted order-path surface.

Synthetic names only (AAA, BBB, XYZ). Temp SQLite files. No network.
"""
from __future__ import annotations

import ast
import csv
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentic.brokers.robinhood_mcp import ORDERS_HARD_DISABLED, RobinhoodMCPBroker
from agentic.config import Settings, load_config
from agentic.cutover import (
    LEGACY_TABLES,
    CutoverRefused,
    export_archive,
    live_counts,
    record_cutover,
    wipe_legacy,
)
from agentic.services.killswitch import KillSwitch
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app

OWNER_USER = "admin"
OWNER_PASS = "test-dashboard-password-ok"
VIEWER_USER = "viewer"
VIEWER_PASS = "test-viewer-password-ok"
CONTROL = "test-control-token-ok-long"

FORBIDDEN_MODULES = (
    "agentic.services.executor",
    "agentic.services.approval",
    "agentic.services.roll",
    "agentic.services.close_guard",
    "agentic.services.reconcile",
    "agentic.services.tax_reserve",
    "agentic.brokers.paper_broker",
    "agentic.brokers.live_guard",
    "agentic.brokers.alpaca_broker",
    "agentic.brokers.robinstocks_broker",
    "agentic.store.orders",
    "agentic.store.tax_reserve",
)

SRC = Path(__file__).resolve().parents[1] / "src" / "agentic"


def _seed(db: Database) -> None:
    """One synthetic row in every legacy table, plus rows wipe must leave alone."""
    conn = db.conn
    conn.execute(
        """INSERT INTO orders
           (id, decision_id, position_id, client_order_id, occ_symbol, side,
            order_type, quantity, limit_price, status, is_paper)
           VALUES ('o1', 'd1', 'p1', 'c1', 'AAA260101P00010000', 'sell',
                   'limit', 1, 1.25, 'filled', 1)"""
    )
    conn.execute(
        """INSERT INTO positions
           (id, occ_symbol, underlying, option_type, strategy, direction,
            quantity, strike, expiration, credit_received, status)
           VALUES ('p1', 'AAA260101P00010000', 'AAA', 'put', 'csp', 'short',
                   1, 10.0, '2026-01-01', 1.25, 'open')"""
    )
    conn.execute(
        """INSERT INTO decisions
           (id, position_id, rule_name, rule_type, reason, requires_approval,
            status, dedup_key, created_at)
           VALUES ('d1', 'p1', 'note', 'DTE', 'synthetic', 0, 'proposed',
                   'dedup-aaa', '2026-01-01T00:00:00+00:00')"""
    )
    conn.execute(
        """INSERT INTO entry_decisions
           (id, underlying, occ_symbol, strike, expiration, contracts, premium,
            rule_name, reason, status, dedup_key, created_at)
           VALUES ('e1', 'BBB', 'BBB260101P00020000', 20.0, '2026-01-01', 1, 0.4,
                   'screen', 'synthetic', 'proposed', 'entry-bbb',
                   '2026-01-01T00:00:00+00:00')"""
    )
    conn.execute(
        """INSERT INTO trade_journal
           (id, occ_symbol, underlying, kind, contracts, strike, dte, context,
            status, entered_at)
           VALUES ('j1', 'AAA260101P00010000', 'AAA', 'CSP', 1, 10.0, 30, '{}',
                   'open', '2026-01-01T00:00:00+00:00')"""
    )
    conn.execute(
        """INSERT INTO tax_reserve
           (id, created_at, period_start, period_end, net_realized, symbol, status)
           VALUES ('t1', '2026-01-01T00:00:00+00:00', '2025-12-01T00:00:00+00:00',
                   '2026-01-01T00:00:00+00:00', 12.5, 'XYZ', 'dry_run')"""
    )
    conn.execute(
        """INSERT INTO close_claims (position_id, client_order_id, claimed_at)
           VALUES ('p1', 'c1', '2026-01-01T00:00:00+00:00')"""
    )
    conn.execute(
        """INSERT INTO alerts
           (id, dedup_key, kind, underlying, occ_symbol, strike, expiration,
            contracts, created_at)
           VALUES ('a1', 'CSP:AAA:AAA260101P00010000', 'CSP', 'AAA',
                   'AAA260101P00010000', 10.0, '2026-01-01', 1,
                   '2026-01-01T00:00:00+00:00')"""
    )
    conn.execute(
        """INSERT INTO scorecard (alert_id, strike, expiration, contracts)
           VALUES ('a1', 10.0, '2026-01-01', 1)"""
    )
    conn.execute(
        """INSERT INTO scan_runs (id, scanned_at, status, alerts_sent)
           VALUES ('s1', '2026-01-01T00:00:00+00:00', 'ok', 0)"""
    )
    conn.execute(
        """INSERT INTO audit (ts, event_type, source, payload)
           VALUES ('2026-01-01T00:00:00+00:00', 'POLL', 'scanner', '{"keep": true}')"""
    )
    conn.commit()


def _snapshot(db: Database) -> dict[str, list[tuple]]:
    out = {}
    for table in (*LEGACY_TABLES, "alerts", "scan_runs", "scorecard", "audit"):
        rows = db.conn.execute(f"SELECT * FROM {table}").fetchall()
        out[table] = [tuple(row) for row in rows]
    return out


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_USER", OWNER_USER)
    monkeypatch.setenv("DASHBOARD_PASSWORD", OWNER_PASS)
    monkeypatch.setenv("CONTROL_TOKEN", CONTROL)
    monkeypatch.setenv("VIEWER_USER", VIEWER_USER)
    monkeypatch.setenv("VIEWER_PASSWORD", VIEWER_PASS)
    db = Database(tmp_path / "cutover.db")
    _seed(db)
    audit = AuditStore(db)
    deps = WebDeps(
        settings=Settings(broker="paper", market_data="paper"),
        signals=SignalStore(db),
        killswitch=KillSwitch(db, audit),
        approval_gate=None,
        audit=audit,
        positions=PositionStore(db),
        orders=None,
        decisions=DecisionStore(db),
    )
    return TestClient(create_app(deps)), db, audit


def test_export_writes_every_row_and_leaves_the_db(tmp_path):
    db = Database(tmp_path / "arch.db")
    _seed(db)
    before = _snapshot(db)
    manifest = export_archive(db, tmp_path / "archive")
    assert _snapshot(db) == before

    assert set(manifest["tables"]) == set(LEGACY_TABLES)
    for table in LEGACY_TABLES:
        info = manifest["tables"][table]
        assert info["row_count"] == 1
        assert info["files"] == [f"{table}.csv", f"{table}.json"]
        assert info["exported_at"]
        columns = [row[1] for row in db.conn.execute(f"PRAGMA table_info({table})")]
        with (tmp_path / "archive" / f"{table}.csv").open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            csv_rows = list(reader)
        assert reader.fieldnames == columns
        assert len(csv_rows) == 1
        assert set(csv_rows[0]) == set(columns)
        json_rows = json.loads((tmp_path / "archive" / f"{table}.json").read_text(encoding="utf-8"))
        assert len(json_rows) == 1
        assert set(json_rows[0]) == set(columns)
        for column in columns:
            assert str(json_rows[0][column] if json_rows[0][column] is not None else "") == csv_rows[0][column]

    on_disk = json.loads((tmp_path / "archive" / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk["tables"]["orders"]["row_count"] == live_counts(db)["orders"] == 1
    # The archive names the synthetic contract and nothing from the environment.
    blob = (tmp_path / "archive" / "orders.json").read_text(encoding="utf-8")
    assert "AAA260101P00010000" in blob
    assert "DASHBOARD_PASSWORD" not in blob
    assert "ROBINHOOD" not in blob


def test_wipe_refuses_without_a_matching_manifest(tmp_path):
    db = Database(tmp_path / "wipe.db")
    _seed(db)
    before = _snapshot(db)

    with pytest.raises(CutoverRefused):
        wipe_legacy(db, None)
    assert _snapshot(db) == before

    manifest = export_archive(db, tmp_path / "archive")
    manifest["tables"]["orders"]["row_count"] = 99
    with pytest.raises(CutoverRefused) as caught:
        wipe_legacy(db, manifest)
    assert "orders" in caught.value.mismatches
    assert _snapshot(db) == before

    manifest["tables"]["orders"]["row_count"] = 1
    wiped = wipe_legacy(db, manifest)
    assert wiped == {table: 1 for table in LEGACY_TABLES}
    assert live_counts(db) == {table: 0 for table in LEGACY_TABLES}
    # Schema remains so a later export can still see the columns.
    assert db.conn.execute("SELECT COUNT(*) FROM pragma_table_info('orders')").fetchone()[0] > 0
    kept = _snapshot(db)
    assert len(kept["alerts"]) == 1
    assert len(kept["scan_runs"]) == 1
    assert len(kept["scorecard"]) == 1
    assert len(kept["audit"]) == 1
    assert kept["orders"] == []


def test_cutover_audit_records_counts_only(tmp_path):
    db = Database(tmp_path / "audit.db")
    _seed(db)
    record_cutover(db, "export", live_counts(db))
    row = AuditStore(db).recent(1)[0]
    assert row["source"] == "cutover"
    assert row["payload"]["cutover"] == "export"
    assert row["payload"]["counts"]["orders"] == 1
    blob = json.dumps(row["payload"])
    assert "AAA" not in blob
    assert "BBB" not in blob
    assert "XYZ" not in blob


def test_legacy_endpoints_are_owner_only(tmp_path, monkeypatch):
    client, db, _audit = _app(tmp_path, monkeypatch)
    assert client.get("/control/legacy-archive", auth=None).status_code == 401
    assert client.post("/control/legacy-wipe", json={}, auth=None).status_code == 401
    viewer = (VIEWER_USER, VIEWER_PASS)
    assert client.get("/control/legacy-archive", auth=viewer).status_code == 403
    assert client.post("/control/legacy-wipe", json={}, auth=viewer).status_code == 403

    owner = (OWNER_USER, OWNER_PASS)
    exported = client.get("/control/legacy-archive", auth=owner)
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        names = set(archive.namelist())
        assert "manifest.json" in names
        manifest = json.loads(archive.read("manifest.json"))
        orders = json.loads(archive.read("orders.json"))
    assert manifest["tables"]["orders"]["row_count"] == 1
    assert orders[0]["occ_symbol"] == "AAA260101P00010000"
    # Export did not empty the table.
    assert live_counts(db)["orders"] == 1

    bad = json.loads(json.dumps(manifest))
    bad["tables"]["positions"]["row_count"] = 0
    refused = client.post("/control/legacy-wipe", json=bad, auth=owner)
    assert refused.status_code == 409
    assert live_counts(db)["positions"] == 1

    wiped = client.post("/control/legacy-wipe", json=manifest, auth=owner)
    assert wiped.status_code == 200
    assert wiped.json()["ok"] is True
    assert live_counts(db) == {table: 0 for table in LEGACY_TABLES}
    assert db.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM scorecard").fetchone()[0] == 1
    payloads = [json.dumps(row["payload"]) for row in AuditStore(db).recent(10)]
    assert any('"cutover": "wipe"' in blob or '"cutover":"wipe"' in blob.replace(" ", "") for blob in payloads)
    assert all("AAA260101P00010000" not in blob for blob in payloads)


def test_cli_export_and_wipe_on_a_temp_file(tmp_path):
    from agentic.cutover import main

    db_path = tmp_path / "cli.db"
    db = Database(db_path)
    _seed(db)
    db.conn.close()
    archive = tmp_path / "out"
    assert main(["export", "--dir", str(archive), "--db", str(db_path)]) == 0
    assert (archive / "manifest.json").is_file()
    assert main(["wipe", "--dir", str(archive), "--db", str(db_path)]) == 0
    check = Database(db_path)
    assert live_counts(check) == {table: 0 for table in LEGACY_TABLES}
    assert check.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1


def _imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_import_graph_has_no_order_path_and_old_config_starts(tmp_path, monkeypatch):
    forbidden_files = {
        "executor.py",
        "approval.py",
        "roll.py",
        "close_guard.py",
        "paper_broker.py",
        "live_guard.py",
        "alpaca_broker.py",
        "robinstocks_broker.py",
        "tax_reserve.py",
    }
    present = {path.name for path in SRC.rglob("*.py")}
    assert forbidden_files.isdisjoint(present)
    assert not (SRC / "store" / "orders.py").exists()

    imported: set[str] = set()
    write_defs: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported |= _imports(tree)
        if path.name != "robinhood_mcp.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name
                if name.startswith(("submit_", "review_")) or name in {
                    "cancel_order",
                    "_place_and_confirm",
                    "_resolve_placed_order",
                    "_require_live",
                    "_ref_id",
                }:
                    write_defs.append(name)
    assert write_defs == []
    for banned in FORBIDDEN_MODULES:
        assert banned not in imported
        assert not any(name == banned or name.startswith(banned + ".") for name in imported)

    assert ORDERS_HARD_DISABLED is True
    assert RobinhoodMCPBroker is not None

    secret = tmp_path / "old.yaml"
    secret.write_text(
        "mode: paper\n"
        "i_understand_live_trading: true\n"
        "broker: paper\n"
        "market_data: paper\n"
        "tax_reserve:\n"
        "  enabled: true\n"
        "  pct: 0.2\n"
        "  symbol: XYZ\n"
        "risk:\n"
        "  loss_breaker_enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("agentic.config.OVERLAY_PATH", tmp_path / "overlay.yaml")
    settings = load_config(secret)
    assert settings.broker == "paper"
    assert not hasattr(settings, "mode")
    assert not hasattr(settings, "i_understand_live_trading")
    assert not hasattr(settings, "tax_reserve")

    db = Database(tmp_path / "boot.db")
    audit = AuditStore(db)
    deps = WebDeps(
        settings=settings,
        signals=SignalStore(db),
        killswitch=KillSwitch(db, audit),
        approval_gate=None,
        audit=audit,
        positions=PositionStore(db),
        orders=None,
        decisions=DecisionStore(db),
    )
    health = TestClient(create_app(deps)).get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["orders_hard_disabled"] is True
    assert body["real_orders"] == "HARD DISABLED in code"
