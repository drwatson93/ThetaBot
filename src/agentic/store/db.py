"""SQLite connection + schema management.

A single ``Database`` wraps one sqlite3 connection in WAL mode. The schema is applied
idempotently on open via ``CREATE TABLE IF NOT EXISTS``. The repositories
(``PositionStore``, ``AuditStore``, ...) take a ``Database`` and own their tables.

Concurrency note: the monitor/reconcile/web tasks all run in one asyncio process, so a
single connection with a short busy-timeout is sufficient. DB calls are quick and
synchronous; if they ever get heavy, wrap them in ``asyncio.to_thread``.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("agentic.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id              TEXT PRIMARY KEY,
    broker_position_id TEXT,
    option_id       TEXT,                   -- broker option-instrument UUID (RH order leg key)
    occ_symbol      TEXT NOT NULL,
    underlying      TEXT NOT NULL,
    option_type     TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    direction       TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    strike          REAL NOT NULL,
    expiration      TEXT NOT NULL,           -- ISO date
    credit_received REAL NOT NULL,
    open_avg_price  REAL,
    current_bid     REAL,
    current_ask     REAL,
    current_mark    REAL,
    delta           REAL,
    iv              REAL,
    peak_profit_pct REAL NOT NULL DEFAULT 0,   -- MFE: high-water mark of profit captured (trailing exits)
    trough_profit_pct REAL NOT NULL DEFAULT 0, -- MAE: low-water mark of profit captured (worst drawdown)
    is_paper        INTEGER,                    -- 1 paper / 0 real / NULL unknown (stamped at sync
                                                -- from the active broker; drives real-only views)
    status          TEXT NOT NULL,
    opened_at       TEXT,
    last_synced_at  TEXT
);
-- NON-unique: one row per OPEN EPISODE of a contract. Re-selling the same OCC after a close
-- inserts a NEW row so the closed trade's P&L survives (stats would otherwise lose it).
CREATE INDEX IF NOT EXISTS idx_positions_occ ON positions(occ_symbol, status);

CREATE TABLE IF NOT EXISTS decisions (
    id              TEXT PRIMARY KEY,
    position_id     TEXT NOT NULL,
    rule_name       TEXT NOT NULL,
    rule_type       TEXT NOT NULL,
    reason          TEXT NOT NULL,
    requires_approval INTEGER NOT NULL,
    status          TEXT NOT NULL,
    dedup_key       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    decided_at      TEXT,
    expires_at      TEXT,
    order_type      TEXT,
    limit_price     REAL,
    bid             REAL,
    ask             REAL,
    mid             REAL,
    time_in_force   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_dedup ON decisions(dedup_key);

CREATE TABLE IF NOT EXISTS orders (
    id              TEXT PRIMARY KEY,
    decision_id     TEXT NOT NULL,
    position_id     TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    broker_order_id TEXT,
    option_id       TEXT,                   -- broker option-instrument UUID (RH order leg key)
    occ_symbol      TEXT NOT NULL,
    side            TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    limit_price     REAL NOT NULL,
    filled_qty      INTEGER NOT NULL DEFAULT 0,
    avg_fill_price  REAL,
    status          TEXT NOT NULL,
    is_paper        INTEGER NOT NULL,
    submitted_at    TEXT,
    last_status_at  TEXT,
    bid             REAL,                   -- NBBO at the moment the limit was priced
    ask             REAL,
    mid             REAL,
    time_in_force   TEXT                    -- e.g. gfd; null on rows written before this existed
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_client ON orders(client_order_id);

-- One owner of a position's close. Removed when that close is rejected or cancelled
-- so a later exit can retry. Live/filled orders are also unique per position
-- (idx_orders_one_live_close, created in migrate).
CREATE TABLE IF NOT EXISTS close_claims (
    position_id     TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    decision_id     TEXT,
    claimed_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entry_decisions (
    id              TEXT PRIMARY KEY,
    underlying      TEXT NOT NULL,
    occ_symbol      TEXT NOT NULL,
    option_id       TEXT,
    strike          REAL NOT NULL,
    expiration      TEXT NOT NULL,           -- ISO date
    contracts       INTEGER NOT NULL,
    premium         REAL NOT NULL,
    rule_name       TEXT NOT NULL,
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL,
    dedup_key       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    decided_at      TEXT,
    order_type      TEXT,
    limit_price     REAL,
    bid             REAL,
    ask             REAL,
    mid             REAL,
    time_in_force   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entry_decisions_dedup ON entry_decisions(dedup_key);

CREATE TABLE IF NOT EXISTS trade_journal (
    id              TEXT PRIMARY KEY,
    entry_decision_id TEXT,
    occ_symbol      TEXT NOT NULL,
    underlying      TEXT NOT NULL,
    kind            TEXT NOT NULL,           -- CSP | CC
    contracts       INTEGER NOT NULL,
    strike          REAL NOT NULL,
    dte             INTEGER NOT NULL,
    delta           REAL,
    iv              REAL,
    premium         REAL,
    spread_pct      REAL,
    open_interest   INTEGER,
    volume          INTEGER,
    annualized_ror  REAL,
    underlying_price REAL,
    context         TEXT NOT NULL,           -- JSON: extensible future signals
    status          TEXT NOT NULL,           -- open | win | loss | expired | assigned | called_away
    realized_pnl    REAL,
    close_price     REAL,
    days_held       INTEGER,
    exit_reason     TEXT,
    entered_at      TEXT NOT NULL,
    closed_at       TEXT,
    order_type      TEXT,                    -- entry order (null on pre-snapshot rows)
    limit_price     REAL,
    bid             REAL,
    ask             REAL,
    mid             REAL,
    time_in_force   TEXT,
    close_order_type TEXT,                   -- close/BTC order (null until a close fills)
    close_limit_price REAL,
    close_bid       REAL,
    close_ask       REAL,
    close_mid       REAL,
    close_time_in_force TEXT
);
CREATE INDEX IF NOT EXISTS idx_journal_occ_status ON trade_journal(occ_symbol, status);

-- Every screened entry candidate + its disposition, one row per (scan, contract). Captures the
-- NEGATIVE examples (screened but not entered) with the exact gate that rejected them, so the
-- model can be refined on why candidates lost — not just on the trades that filled.
CREATE TABLE IF NOT EXISTS entry_candidates (
    id              TEXT PRIMARY KEY,
    scan_id         TEXT NOT NULL,            -- groups all candidates from one scan cycle
    scanned_at      TEXT NOT NULL,
    underlying      TEXT NOT NULL,
    occ_symbol      TEXT NOT NULL,
    kind            TEXT NOT NULL,            -- CSP | CC
    strike          REAL NOT NULL,
    expiration      TEXT NOT NULL,            -- ISO date
    dte             INTEGER NOT NULL,
    delta           REAL,
    iv              REAL,
    premium         REAL,
    annualized_ror  REAL,
    open_interest   INTEGER,
    volume          INTEGER,
    score           REAL,
    approved        INTEGER NOT NULL,         -- 1 = sized + submitted this scan, 0 = rejected
    contracts       INTEGER,                  -- sized size when approved
    reason          TEXT NOT NULL,            -- 'approved' or the gate that rejected it
    scanned_date    TEXT NOT NULL             -- ISO date, for cheap day-level dedup/pruning
);
CREATE INDEX IF NOT EXISTS idx_entry_candidates_scan ON entry_candidates(scanned_at);
CREATE INDEX IF NOT EXISTS idx_entry_candidates_occ ON entry_candidates(occ_symbol, scanned_at);

CREATE TABLE IF NOT EXISTS setup_events (
    id          TEXT PRIMARY KEY,
    symbol      TEXT NOT NULL,
    label       TEXT NOT NULL,            -- entry/setups.py composite label that fired
    bar_date    TEXT NOT NULL,            -- ISO date of the COMPLETED bar it fired on
    source      TEXT NOT NULL,            -- bot_daily | tv_daily | tv_intraday | replay
    fired_at    TEXT NOT NULL,
    fire_price  REAL NOT NULL,
    features    TEXT NOT NULL,            -- JSON snapshot of the read's features at fire time
    ret_5d      REAL,                     -- forward returns vs fire_price (fractions)
    ret_10d     REAL,
    mae_10d     REAL,                     -- worst excursion over 10 bars (<= 0): the put-seller's question
    mfe_10d     REAL,                     -- best excursion over 10 bars (>= 0)
    resolved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_setup_events_dedup ON setup_events(symbol, label, bar_date, source);
CREATE INDEX IF NOT EXISTS idx_setup_events_pending ON setup_events(resolved_at, symbol);

CREATE TABLE IF NOT EXISTS briefs (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,            -- markdown as rendered by the dashboard
    has_ai      INTEGER NOT NULL DEFAULT 0,  -- 1 when the Claude tactical read was included
    meta        TEXT NOT NULL DEFAULT '{}'   -- JSON: watchlist size, open positions, etc.
);
CREATE INDEX IF NOT EXISTS idx_briefs_created ON briefs(created_at);

CREATE TABLE IF NOT EXISTS tax_reserve (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    period_start    TEXT NOT NULL,            -- previous sweep instant (UTC ISO)
    period_end      TEXT NOT NULL,            -- this sweep instant (UTC ISO); one row per period
    net_realized    REAL NOT NULL,            -- realized P&L closed in the period
    carry_in        REAL NOT NULL DEFAULT 0,  -- loss carried into the period (<= 0)
    carry_out       REAL NOT NULL DEFAULT 0,  -- balance carried out (loss, or a too-small gain)
    amount_due      REAL NOT NULL DEFAULT 0,  -- pct * net when positive
    symbol          TEXT NOT NULL,
    dollar_amount   REAL,
    shares          REAL,
    fill_price      REAL,
    broker_order_id TEXT,
    ref_id          TEXT,
    status          TEXT NOT NULL,            -- filled | skipped | dry_run | failed
    error           TEXT,
    meta            TEXT NOT NULL DEFAULT '{}',
    order_type      TEXT,
    limit_price     REAL,
    bid             REAL,
    ask             REAL,
    mid             REAL,
    time_in_force   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tax_reserve_period ON tax_reserve(period_end);

CREATE TABLE IF NOT EXISTS iv_history (
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,               -- ISO date
    atm_iv      REAL NOT NULL,
    PRIMARY KEY (symbol, date)
);

-- One row per play the scanner has alerted. dedup_key is kind:underlying:occ
-- with no date, so a play alerts at most once.
CREATE TABLE IF NOT EXISTS alerts (
    id           TEXT PRIMARY KEY,
    dedup_key    TEXT NOT NULL,
    kind         TEXT NOT NULL,             -- CSP | CC
    underlying   TEXT NOT NULL,
    occ_symbol   TEXT NOT NULL,
    strike       REAL NOT NULL,
    expiration   TEXT NOT NULL,             -- ISO date
    contracts    INTEGER NOT NULL,
    premium      REAL,                      -- recommended mid, per share
    bid          REAL,
    ask          REAL,
    created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_dedup ON alerts(dedup_key);

-- One row per scan that reached the health gate. status is ok or skipped.
CREATE TABLE IF NOT EXISTS scan_runs (
    id           TEXT PRIMARY KEY,
    scanned_at   TEXT NOT NULL,
    status       TEXT NOT NULL,             -- ok | skipped
    reason       TEXT,                      -- why a skipped run sent nothing
    alerts_sent  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS signals (
    id              TEXT PRIMARY KEY,
    raw             TEXT NOT NULL,
    dedup_key       TEXT NOT NULL,
    token_ok        INTEGER NOT NULL,
    action          TEXT NOT NULL,
    match_field     TEXT,
    match_value     TEXT,
    status          TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    ttl_expires_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_dedup ON signals(dedup_key);

-- Latest TradingView indicator snapshot per symbol (entry-context channel; NOT a close signal).
CREATE TABLE IF NOT EXISTS tv_indicators (
    symbol      TEXT PRIMARY KEY,
    raw         TEXT NOT NULL,               -- JSON: the full TradingView alert payload
    received_at TEXT NOT NULL
);

-- AI trade-analyst verdicts (advisory), one per reviewed entry candidate.
CREATE TABLE IF NOT EXISTS ai_reviews (
    id             TEXT PRIMARY KEY,
    decision_id    TEXT,
    occ_symbol     TEXT NOT NULL,
    underlying     TEXT NOT NULL,
    recommendation TEXT NOT NULL,            -- take | caution | skip
    confidence     REAL,
    rationale      TEXT,
    flags          TEXT,                     -- JSON list
    regime_label   TEXT,
    move_class     TEXT,                     -- systemic | idiosyncratic | neutral | unknown
    model          TEXT,
    verdict        TEXT NOT NULL,            -- full JSON verdict
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_reviews_created ON ai_reviews(created_at);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    source      TEXT NOT NULL,
    position_id TEXT,
    decision_id TEXT,
    order_id    TEXT,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);

-- Recent news / catalyst items per symbol (entry-context channel; advisory, never a trade-picker).
-- Fed by the Alpaca (Benzinga) pull and a webhook push (curated X / other feeds).
CREATE TABLE IF NOT EXISTS news_items (
    dedup_key   TEXT PRIMARY KEY,           -- source:id:symbol, so re-ingest is idempotent
    symbol      TEXT NOT NULL,
    headline    TEXT NOT NULL,
    source      TEXT,                        -- benzinga | x | ...
    url         TEXT,
    created_at  TEXT,                        -- article/post timestamp (ISO), for recency
    received_at TEXT NOT NULL                -- when we stored it
);
CREATE INDEX IF NOT EXISTS idx_news_symbol_created ON news_items(symbol, created_at);

-- single-row control table for the kill switch / global pause + alerts mode
CREATE TABLE IF NOT EXISTS control (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    paused  INTEGER NOT NULL DEFAULT 0,
    reason  TEXT,
    updated_at TEXT,
    alerts_mode TEXT NOT NULL DEFAULT 'instant',
    alerts_last_sent_at TEXT,
    alerts_last_status TEXT,
    alerts_last_error TEXT
);
INSERT OR IGNORE INTO control (id, paused) VALUES (1, 0);
"""

_ONE_LIVE_CLOSE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_one_live_close
   ON orders(position_id)
   WHERE side = 'BUY_TO_CLOSE'
     AND status IN ('PENDING', 'SUBMITTED', 'PARTIAL', 'FILLED')
"""

_LIVE_CLOSE_RANK = {"FILLED": 0, "PARTIAL": 1, "SUBMITTED": 2, "PENDING": 3}


class _SerializedConnection:
    """One sqlite connection shared by monitor threads.

    A write opens an implicit transaction and holds the lock until commit or
    rollback, so two threads cannot interleave ``execute`` and ``commit`` on
    the same connection. Reads release the lock as soon as the statement
    finishes.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()
        self._held = threading.local()

    def _enter(self) -> None:
        self._lock.acquire()
        self._held.depth = getattr(self._held, "depth", 0) + 1

    def _leave_all(self) -> None:
        depth = getattr(self._held, "depth", 0)
        self._held.depth = 0
        for _ in range(depth):
            self._lock.release()

    def execute(self, *args, **kwargs):
        self._enter()
        already = self._conn.in_transaction
        try:
            cursor = self._conn.execute(*args, **kwargs)
        except BaseException:
            if not already:
                self._leave_all()
            raise
        if not self._conn.in_transaction:
            self._leave_all()
        return cursor

    def executemany(self, *args, **kwargs):
        self._enter()
        already = self._conn.in_transaction
        try:
            cursor = self._conn.executemany(*args, **kwargs)
        except BaseException:
            if not already:
                self._leave_all()
            raise
        if not self._conn.in_transaction:
            self._leave_all()
        return cursor

    def executescript(self, script: str):
        self._enter()
        try:
            return self._conn.executescript(script)
        finally:
            self._leave_all()

    def commit(self) -> None:
        if getattr(self._held, "depth", 0) == 0:
            self._enter()
        try:
            self._conn.commit()
        finally:
            self._leave_all()

    def rollback(self) -> None:
        if getattr(self._held, "depth", 0) == 0:
            self._enter()
        try:
            self._conn.rollback()
        finally:
            self._leave_all()

    def close(self) -> None:
        self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _live_close_rank(row) -> tuple:
    """Prefer a fill, then a broker-accepted order, then the earliest submit."""
    status_rank = _LIVE_CLOSE_RANK.get(row["status"], 9)
    has_broker = 0 if row["broker_order_id"] else 1
    submitted = row["submitted_at"] or "9999"
    return (status_rank, has_broker, submitted, row["id"])


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(str(self.path), check_same_thread=False)
        raw.row_factory = sqlite3.Row
        self.conn = _SerializedConnection(raw)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.execute("PRAGMA busy_timeout=5000;")
        self.migrate()

    def migrate(self) -> None:
        self.conn.executescript(SCHEMA)
        # Idempotent column adds for DBs created before option_id existed (CREATE TABLE
        # IF NOT EXISTS won't alter an existing table). ADD COLUMN raises if it's already
        # there — swallow that and move on.
        for table in ("positions", "orders"):
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN option_id TEXT")
            except sqlite3.OperationalError:
                pass  # column already present
        try:
            self.conn.execute(
                "ALTER TABLE positions ADD COLUMN peak_profit_pct REAL NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass  # column already present
        try:
            self.conn.execute(
                "ALTER TABLE positions ADD COLUMN trough_profit_pct REAL NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass  # column already present
        try:
            self.conn.execute("ALTER TABLE positions ADD COLUMN is_paper INTEGER")
        except sqlite3.OperationalError:
            pass  # column already present
        # Quote/limit snapshot on every order path (additive; existing rows stay NULL).
        for table, col, decl in (
            ("orders", "bid", "REAL"),
            ("orders", "ask", "REAL"),
            ("orders", "mid", "REAL"),
            ("orders", "time_in_force", "TEXT"),
            ("trade_journal", "order_type", "TEXT"),
            ("trade_journal", "limit_price", "REAL"),
            ("trade_journal", "bid", "REAL"),
            ("trade_journal", "ask", "REAL"),
            ("trade_journal", "mid", "REAL"),
            ("trade_journal", "time_in_force", "TEXT"),
            ("trade_journal", "close_order_type", "TEXT"),
            ("trade_journal", "close_limit_price", "REAL"),
            ("trade_journal", "close_bid", "REAL"),
            ("trade_journal", "close_ask", "REAL"),
            ("trade_journal", "close_mid", "REAL"),
            ("trade_journal", "close_time_in_force", "TEXT"),
            ("entry_decisions", "order_type", "TEXT"),
            ("entry_decisions", "limit_price", "REAL"),
            ("entry_decisions", "bid", "REAL"),
            ("entry_decisions", "ask", "REAL"),
            ("entry_decisions", "mid", "REAL"),
            ("entry_decisions", "time_in_force", "TEXT"),
            ("decisions", "order_type", "TEXT"),
            ("decisions", "limit_price", "REAL"),
            ("decisions", "bid", "REAL"),
            ("decisions", "ask", "REAL"),
            ("decisions", "mid", "REAL"),
            ("decisions", "time_in_force", "TEXT"),
            ("tax_reserve", "order_type", "TEXT"),
            ("tax_reserve", "limit_price", "REAL"),
            ("tax_reserve", "bid", "REAL"),
            ("tax_reserve", "ask", "REAL"),
            ("tax_reserve", "mid", "REAL"),
            ("tax_reserve", "time_in_force", "TEXT"),
            ("control", "alerts_mode", "TEXT NOT NULL DEFAULT 'instant'"),
            ("control", "alerts_last_sent_at", "TEXT"),
            ("control", "alerts_last_status", "TEXT"),
            ("control", "alerts_last_error", "TEXT"),
        ):
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # column already present
        # Replace the old UNIQUE(occ_symbol) index (which overwrote a closed trade when the same
        # contract was re-sold) with a non-unique one, so each open episode gets its own row.
        try:
            row = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_positions_occ'"
            ).fetchone()
            if row and row["sql"] and "UNIQUE" in row["sql"].upper():
                self.conn.execute("DROP INDEX idx_positions_occ")
                self.conn.execute(
                    "CREATE INDEX idx_positions_occ ON positions(occ_symbol, status)"
                )
        except sqlite3.OperationalError:
            pass
        # One live or filled buy-to-close per position. REJECTED/CANCELLED stay out of
        # the index so a failed close can be retried. A partial fill that later cancels
        # keeps its filled_qty on that terminal row; the replacement is the one live row.
        # Duplicate live rows (index cannot be created) are quarantined, then the index
        # is created.
        self._ensure_one_live_close_index()
        self.conn.commit()

    def _ensure_one_live_close_index(self) -> None:
        try:
            self.conn.execute(_ONE_LIVE_CLOSE_INDEX)
            return
        except sqlite3.OperationalError as exc:
            log.warning("one-live-close index not created: %s", exc)
            return
        except sqlite3.IntegrityError as exc:
            log.warning("one-live-close index blocked by duplicate rows: %s", exc)
        self._rollback_quietly()
        quarantined = self._quarantine_duplicate_live_closes()
        log.warning(
            "quarantined %d duplicate live close(s) so the one-live-close index can be created: %s",
            len(quarantined), quarantined,
        )
        try:
            self.conn.execute(_ONE_LIVE_CLOSE_INDEX)
        except sqlite3.Error as exc:
            log.error("one-live-close index still not created after quarantine: %s", exc)

    def _rollback_quietly(self) -> None:
        try:
            self.conn.rollback()
        except sqlite3.Error:
            pass

    def _quarantine_duplicate_live_closes(self) -> list[str]:
        """Keep one live close per position. Extra rows leave the unique index as CANCELLED.

        filled_qty is left as reported so a partial fill stays reserved. The kept row is
        the fill if there is one, otherwise the order the broker has accepted.
        """
        rows = self.conn.execute(
            """SELECT id, position_id, client_order_id, decision_id, status,
                      broker_order_id, submitted_at
               FROM orders
               WHERE side = 'BUY_TO_CLOSE'
                 AND status IN ('PENDING', 'SUBMITTED', 'PARTIAL', 'FILLED')
               ORDER BY rowid"""
        ).fetchall()
        groups: dict[str, list] = {}
        for row in rows:
            groups.setdefault(row["position_id"], []).append(row)
        quarantined: list[str] = []
        now = datetime.now(timezone.utc).isoformat()
        for position_id, group in groups.items():
            if len(group) < 2:
                continue
            winner = min(group, key=_live_close_rank)
            for row in group:
                if row["id"] == winner["id"]:
                    continue
                self.conn.execute(
                    "UPDATE orders SET status = 'CANCELLED', last_status_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                self.conn.execute(
                    """UPDATE close_claims
                       SET client_order_id = ?, decision_id = ?
                       WHERE position_id = ? AND client_order_id = ?""",
                    (winner["client_order_id"], winner["decision_id"],
                     position_id, row["client_order_id"]),
                )
                quarantined.append(row["client_order_id"])
        self.conn.commit()
        return quarantined

    def close(self) -> None:
        self.conn.close()
