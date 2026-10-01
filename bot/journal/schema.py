"""Journal schema: the tables, and the idempotent migrations that add columns
introduced after the first release."""
from __future__ import annotations

import sqlite3

import bot.journal as base   # shared helpers, looked up at call time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    action TEXT NOT NULL,
    confidence REAL NOT NULL,
    price REAL NOT NULL,
    regime TEXT,
    stop_distance REAL,
    target_rr REAL,
    strategy_signals TEXT,
    sentiment TEXT,
    rationale TEXT,
    mode TEXT NOT NULL DEFAULT 'paper'
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    stop_price REAL,
    initial_stop_price REAL,
    stop_effective_bar_ts REAL,
    target_price REAL,
    strategy TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    opened_ts TEXT NOT NULL,
    closed_ts TEXT,
    pnl REAL,
    pnl_pct REAL,
    fees REAL,
    entry_fee REAL,
    decision_bar_ts REAL,
    realized_cash_delta REAL,
    exit_reason TEXT,
    rationale_open TEXT,
    rationale_close TEXT,
    mode TEXT NOT NULL DEFAULT 'paper',
    timeframe TEXT NOT NULL DEFAULT '1h'
);
CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    mode TEXT NOT NULL DEFAULT 'paper',
    note TEXT,
    cash_event_id INTEGER
);
CREATE TABLE IF NOT EXISTS cash_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount REAL NOT NULL,
    trade_id INTEGER,
    UNIQUE(trade_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_cash_events_mode_id ON cash_events(mode, id);
CREATE TABLE IF NOT EXISTS chat_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount REAL NOT NULL,
    cash_after REAL,
    equity_after REAL,
    mode TEXT NOT NULL DEFAULT 'paper',
    note TEXT
);
CREATE TABLE IF NOT EXISTS book_owner (
    mode TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    pid INTEGER NOT NULL,
    host TEXT NOT NULL,
    started_ts TEXT NOT NULL,
    heartbeat REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts, id);
"""


class SchemaMixin:
    def _migrate(self, conn: sqlite3.Connection):
        """Add columns introduced after the first release. Idempotent.

        Wrapped in BEGIN IMMEDIATE so two processes starting simultaneously
        can't race the ALTER; a duplicate-column error from the loser of that
        race is success, not failure. The backfill re-runs on every boot —
        a crash between ALTER and backfill must not leave NULLs forever.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
            if "timeframe" not in cols:
                # NOT NULL DEFAULT keeps migrated DBs under the same constraint
                # the fresh schema declares
                conn.execute("ALTER TABLE trades ADD COLUMN timeframe TEXT NOT NULL DEFAULT '1h'")
            # --- 2026-09 audit columns -------------------------------------
            # initial_stop_price: R-multiples must divide by the INITIAL stop,
            # never the trailed one (behavior_profile read exploded ±20R values
            # and false stop-breach counts because stop_price is overwritten by
            # every trail). Legacy rows backfill from their final stop_price —
            # the best available approximation for rows whose trail history is
            # gone; only BE-trailed legacy rows stay distorted and are
            # documented as such.
            if "initial_stop_price" not in cols:
                conn.execute("ALTER TABLE trades ADD COLUMN initial_stop_price REAL")
            # entry_fee: the entry leg's taker fee, so cash reconciliation can
            # distinguish anchor-relative windows (see closed_cash_delta_since)
            # instead of guessing which fees the anchor already reflects.
            if "entry_fee" not in cols:
                conn.execute("ALTER TABLE trades ADD COLUMN entry_fee REAL")
            # realized_cash_delta: exact broker cash effect of the CLOSE event
            # (gross - exit_fee), recorded at close time — the reconciliation
            # ground truth for crash windows.
            if "realized_cash_delta" not in cols:
                conn.execute("ALTER TABLE trades ADD COLUMN realized_cash_delta REAL")
            if "decision_bar_ts" not in cols:
                conn.execute("ALTER TABLE trades ADD COLUMN decision_bar_ts REAL")
            if "stop_effective_bar_ts" not in cols:
                conn.execute("ALTER TABLE trades ADD COLUMN stop_effective_bar_ts REAL")
            eq_cols = {r[1] for r in conn.execute("PRAGMA table_info(equity)")}
            if "cash_event_id" not in eq_cols:
                # NULL explicitly means a legacy timestamp anchor. New writes
                # always carry a cursor, including zero on an empty ledger.
                conn.execute("ALTER TABLE equity ADD COLUMN cash_event_id INTEGER")
            if conn.execute("SELECT 1 FROM equity WHERE cash_event_id IS NULL LIMIT 1").fetchone():
                self.last_error = ("legacy cash anchors use approximate timestamp recovery; "
                                   "same-second historical ordering cannot be reconstructed")
            # backfills re-run until they stick (crash between ALTER and UPDATE).
            # APPROXIMATE: legacy rows lose trail history — warn loudly with the
            # count so R-multiples on old rows are read as estimates.
            cur1 = conn.execute("UPDATE trades SET initial_stop_price = stop_price"
                                " WHERE initial_stop_price IS NULL AND stop_price IS NOT NULL")
            n1 = cur1.rowcount if cur1.rowcount and cur1.rowcount > 0 else 0
            cur2 = conn.execute("UPDATE trades SET entry_fee = fees / 2.0"
                                " WHERE entry_fee IS NULL AND fees IS NOT NULL")
            n2 = cur2.rowcount if cur2.rowcount and cur2.rowcount > 0 else 0
            if n1 or n2:
                msg = (f"[journal] backfilled {n1} initial_stop_price + {n2} entry_fee "
                       f"rows from approximations (stop_price / fees/2) — legacy "
                       f"R-multiples are estimates")
                print(msg)
                self.last_error = (self.last_error + "; " + msg) if self.last_error else msg
            # strategy-specialized backfill removed: the column is NOT NULL
            # DEFAULT '1h' (ALTER fills existing rows with the default), so
            # NULLs cannot exist — the catch-all below is the only safety net
            conn.execute("UPDATE trades SET timeframe='1h' WHERE timeframe IS NULL")
            # normalize legacy space-separated timestamps (pandas str(Timestamp))
            # to canonical ISO so ts-string ordering is correct everywhere
            for table, col in (("equity", "ts"), ("trades", "opened_ts"),
                               ("trades", "closed_ts"), ("decisions", "ts"),
                               ("transactions", "ts"), ("chat_log", "ts")):
                for row in conn.execute(
                        f"SELECT id, {col} AS v FROM {table} WHERE {col} LIKE '% %'").fetchall():
                    fixed = base._iso(row["v"])
                    if fixed != row["v"]:
                        conn.execute(f"UPDATE {table} SET {col}=? WHERE id=?",
                                     (fixed, row["id"]))
            conn.commit()
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if "duplicate column" not in str(exc).lower():
                raise
