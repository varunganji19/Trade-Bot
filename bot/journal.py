"""
SQLite trading journal — the single source of truth for the dashboard and chatbot.

Tables:
  decisions  — every evaluation the bot makes (even HOLDs, with full reasoning)
  trades     — position lifecycle with strategy attribution
  equity     — mark-to-market equity curve points
  chat_log   — dashboard chatbot conversation
  transactions — deposit/withdrawal/reset ledger (Account tab history)

Concurrency: the dashboard's API threads and the live engine thread write through
ONE Journal instance per process, but a second process (CLI engine, seeding) can
exist too — so every connection enables WAL (readers never block the writer) and
a generous busy timeout, and a module-level lock serializes writers across ALL
Journal instances of this process.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from config import CONFIG, utc_now

# One lock per PROCESS (not per Journal instance): the dashboard and its engine
# thread each construct a Journal, and per-instance locks would not serialize
# writes between them.
_PROCESS_LOCK = threading.RLock()

# the engine used to define its own byte-identical utc_now() — one shared clock
_now = utc_now

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
    target_price REAL,
    strategy TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    opened_ts TEXT NOT NULL,
    closed_ts TEXT,
    pnl REAL,
    pnl_pct REAL,
    fees REAL,
    entry_fee REAL,
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
    note TEXT
);
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
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts, id);
"""

# real-file-corruption signatures only — NOT lock/busy/disk-full (a merely
# locked or full-disk db must never be quarantined, only a torn file)
_DB_CORRUPT_SIGNATURES = ("not a database", "malformed")


def _iso(ts: str | None) -> str:
    """Canonical journal timestamp (ISO-UTC, 'T' separator). seed_demo used to
    write pandas' space-separated str(Timestamp) — the two formats sorted
    differently within a day and corrupted ts-ordered reads (the restart cash
    anchor could pick a stale point). Normalize every ts ON WRITE, and
    _migrate normalizes legacy rows ON BOOT. An unparseable value is stamped
    with the current time instead of passing through: garbage sorts AFTER
    every ISO string, so closed_cash_delta_since would re-count that trade's
    PnL into broker cash on EVERY restart."""
    if not ts:
        return _now()
    from config import parse_utc
    dt = parse_utc(str(ts))
    return dt.isoformat(timespec="seconds") if dt else _now()


def _db_corrupt(exc: sqlite3.DatabaseError) -> bool:
    """True only for real file corruption — NOT for lock/busy/disk-full
    (quarantining a merely-locked or full disk db would destroy the journal)."""
    msg = str(exc).lower()
    return any(s in msg for s in _DB_CORRUPT_SIGNATURES)


class Journal:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or CONFIG.db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = _PROCESS_LOCK
        try:
            with self._conn() as conn:
                conn.executescript(_SCHEMA)
                self._migrate(conn)
        except sqlite3.DatabaseError as exc:
            # the journal is the single source of truth, but a corrupt file
            # must not brick the whole app at import time (uvicorn dies, no UI
            # to explain) — quarantine like every OTHER stateful artifact and
            # start fresh. Locked/busy/disk-full errors re-raise (see _db_corrupt).
            if not _db_corrupt(exc):
                raise
            stamp = int(time.time())
            quarantine = f"{self.db_path}.corrupt.{stamp}"
            print(f"[journal] trading db is corrupt ({exc}) — quarantined to "
                  f"{os.path.basename(quarantine)}(+wal/shm), starting a fresh journal")
            try:
                os.replace(self.db_path, quarantine)
            finally:
                for suffix in ("-wal", "-shm"):
                    side = self.db_path + suffix
                    if os.path.exists(side):
                        try:
                            os.replace(side, quarantine + suffix)
                        except OSError:
                            pass
            with self._conn() as conn:
                conn.executescript(_SCHEMA)
                self._migrate(conn)

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
            # backfills re-run until they stick (crash between ALTER and UPDATE)
            conn.execute("UPDATE trades SET initial_stop_price = stop_price"
                         " WHERE initial_stop_price IS NULL AND stop_price IS NOT NULL")
            conn.execute("UPDATE trades SET entry_fee = fees / 2.0"
                         " WHERE entry_fee IS NULL AND fees IS NOT NULL")
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
                    fixed = _iso(row["v"])
                    if fixed != row["v"]:
                        conn.execute(f"UPDATE {table} SET {col}=? WHERE id=?",
                                     (fixed, row["id"]))
            conn.commit()
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if "duplicate column" not in str(exc).lower():
                raise

    @contextmanager
    def _conn(self):
        """Connection with the sqlite3 commit/rollback semantics callers rely
        on, PLUS a guaranteed close — the bare `with self._conn()` committed
        but never closed, so every 4s dashboard poll leaked a connection to
        the GC's discretion."""
        # timeout: SQLite's default 5s becomes a dropped cycle under dashboard
        # read load; WAL readers never block the writer either way.
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            with conn:   # commit on success / rollback on exception
                yield conn
        finally:
            conn.close()

    # ---------------------------------------------------------------- writes
    def add_decision(self, symbol: str, timeframe: str, decision, mode: str = "paper") -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO decisions (ts, symbol, timeframe, action, confidence, price, regime,"
                " stop_distance, target_rr, strategy_signals, sentiment, rationale, mode)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_now(), symbol, timeframe, decision.action, float(decision.confidence),
                 float(decision.price), decision.regime, decision.stop_distance, decision.target_rr,
                 json.dumps(decision.strategy_signals, default=str),
                 json.dumps(decision.sentiment, default=str),
                 decision.rationale, mode))
            return cur.lastrowid

    def open_trade(self, symbol: str, side: str, qty: float, entry_price: float,
                   stop: float | None, target: float | None, strategy: str,
                   rationale: str, mode: str = "paper", opened_ts: str | None = None,
                   timeframe: str = "1h", entry_fee: float | None = None) -> int:
        """`stop` (when given) is recorded as BOTH stop_price and
        initial_stop_price: at open they are the same level, and the initial
        copy is never overwritten by later trails (R-multiple ground truth).
        `entry_fee` records the entry leg's fee for anchor-aware cash
        reconciliation (see closed_cash_delta_since)."""
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO trades (symbol, side, qty, entry_price, stop_price,"
                " initial_stop_price, target_price, strategy, status, opened_ts,"
                " rationale_open, mode, timeframe, entry_fee)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (symbol, side, qty, entry_price, stop, stop, target, strategy, "OPEN",
                 _iso(opened_ts), rationale, mode, timeframe, entry_fee))
            return cur.lastrowid

    def record_fill(self, trade_id: int, entry_price: float, stop: float | None = None,
                    target: float | None = None, entry_fee: float | None = None,
                    initial_stop: float | None = None):
        """Post-fill correction of the row the engine opens BEFORE the broker
        fill (journal-first, self-healing). Sets the fill-derived entry/stop/
        target AND their initial-risk snapshots: `initial_stop_price` latches
        the fill-derived stop exactly once (never on later calls) and
        `entry_fee` latches the entry leg's fee. Re-calls only refresh
        stop_price/target_price — the trailing path."""
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE trades SET entry_price=?, entry_fee=? WHERE id=?",
                         (entry_price, entry_fee, trade_id))
            if stop is not None:
                conn.execute(
                    "UPDATE trades SET stop_price=?,"
                    " initial_stop_price=COALESCE(initial_stop_price, ?) WHERE id=?",
                    (stop, initial_stop if initial_stop is not None else stop, trade_id))
            if target is not None:
                conn.execute("UPDATE trades SET target_price=? WHERE id=?",
                             (target, trade_id))

    def close_trade(self, trade_id: int, exit_price: float, pnl: float, pnl_pct: float,
                    fees: float, exit_reason: str, rationale_close: str = "",
                    closed_ts: str | None = None, equity: float | None = None,
                    cash: float | None = None, mode: str = "paper",
                    entry_fee: float | None = None,
                    realized_cash_delta: float | None = None):
        """Close a trade. When equity/cash are given, the cycle-end equity point
        is written IN THE SAME transaction — a crash between the two used to
        drop the exit proceeds from the account (CLOSED trade, pre-exit cash
        as the restart anchor).

        `entry_fee` (the entry leg's fee) and `realized_cash_delta` (the exact
        broker cash effect of THIS close event) are written when supplied so
        closed_cash_delta_since can reconcile crash windows exactly instead
        of guessing which fees the anchor already reflects."""
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE trades SET status='CLOSED', exit_price=?, pnl=?, pnl_pct=?, fees=?,"
                " exit_reason=?, rationale_close=?, closed_ts=?,"
                " entry_fee=COALESCE(entry_fee, ?), realized_cash_delta=? WHERE id=?",
                (exit_price, pnl, pnl_pct, fees, exit_reason, rationale_close,
                 _iso(closed_ts), entry_fee, realized_cash_delta, trade_id))
            if equity is not None and cash is not None:
                conn.execute(
                    "INSERT INTO equity (ts, equity, cash, mode, note) VALUES (?,?,?,?,?)",
                    (_now(), equity, cash, mode, ""))

    def update_trade_stops(self, trade_id: int, stop: float | None = None, target: float | None = None,
                           entry_price: float | None = None):
        """Trail stop/target (and legacy entry-price correction). Only the
        LIVE levels move: initial_stop_price is never touched here — the
        initial risk must survive every trail for R-multiple math."""
        if stop is None and target is None and entry_price is None:
            return
        with self._lock, self._conn() as conn:
            if entry_price is not None:
                conn.execute("UPDATE trades SET entry_price=? WHERE id=?",
                             (entry_price, trade_id))
            if stop is not None:
                conn.execute("UPDATE trades SET stop_price=? WHERE id=?", (stop, trade_id))
            if target is not None:
                conn.execute("UPDATE trades SET target_price=? WHERE id=?", (target, trade_id))

    def abort_trade(self, trade_id: int):
        """Mark a just-opened trade ABORTED when the broker fill failed after
        the INSERT — without this the OPEN row lingers and a restart restores
        a ghost position for a trade that never existed in the broker."""
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE trades SET status='ABORTED',"
                         " rationale_close='aborted: broker fill failed'"
                         " WHERE id=? AND status='OPEN'", (trade_id,))

    def add_equity(self, equity: float, cash: float, mode: str = "paper", note: str = "",
                   ts: str | None = None):
        with self._lock, self._conn() as conn:
            conn.execute("INSERT INTO equity (ts, equity, cash, mode, note) VALUES (?,?,?,?,?)",
                         (_iso(ts), equity, cash, mode, note))

    def add_transaction(self, kind: str, amount: float, cash_after: float | None = None,
                        equity_after: float | None = None, mode: str = "paper",
                        note: str = "", ts: str | None = None):
        """Account ledger row for deposits/withdrawals/resets. Written next to the
        add_equity note rows, but structured (typed kind, exact amount) so the
        Account tab can show a real history instead of reverse-engineering notes."""
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO transactions (ts, kind, amount, cash_after, equity_after, mode, note)"
                " VALUES (?,?,?,?,?,?,?)",
                (_iso(ts), kind, amount, cash_after, equity_after, mode, note))

    def log_chat(self, role: str, content: str):
        with self._lock, self._conn() as conn:
            conn.execute("INSERT INTO chat_log (ts, role, content) VALUES (?,?,?)",
                         (_now(), role, content))

    # ---------------------------------------------------------------- reads
    def open_trades(self, mode: str | None = None) -> list:
        """OPEN rows. mode filter matters: two engine books (standard 'paper'
        and 'hft') share this DB, and each engine's restart-restore must
        rebuild ONLY its own positions — an unfiltered restore would pull the
        other book's open trades into the wrong broker."""
        q, params = "SELECT * FROM trades WHERE status='OPEN'", []
        if mode:
            q += " AND mode=?"
            params.append(mode)
        q += " ORDER BY id"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, params)]

    def recent_trades(self, limit: int = 100, mode: str | None = None) -> list:
        q, params = "SELECT * FROM trades", []
        if mode:
            q += " WHERE mode=?"
            params.append(mode)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, params)]

    def trade_mode_counts(self) -> dict[str, int]:
        """Closed+open trade counts per mode ('paper' vs 'demo'): the dashboard
        badges seeded demo rows instead of silently presenting them as the
        bot's own paper record."""
        with self._conn() as conn:
            return {r["mode"]: r["n"] for r in conn.execute(
                "SELECT mode, COUNT(*) AS n FROM trades GROUP BY mode")}

    def recent_transactions(self, limit: int = 100, mode: str | None = None) -> list:
        q, params = "SELECT * FROM transactions", []
        if mode:
            q += " WHERE mode=?"
            params.append(mode)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, params)]

    def deposits_net(self, mode: str | None = None) -> float:
        """Net deposits (deposit − withdrawals) in the typed account ledger —
        the reconciliation between the equity walk (includes deposits) and the
        trades' own P&L (excludes them). A reset row is 0 by definition (the
        ledger restarts with the account; reset wipes all rows anyway)."""
        q = ("SELECT COALESCE(SUM(CASE kind WHEN 'deposit' THEN amount"
             " WHEN 'withdrawal' THEN -amount ELSE 0 END), 0) FROM transactions")
        params: list = []
        if mode:
            q += " WHERE mode=?"
            params.append(mode)
        with self._conn() as conn:
            return float(conn.execute(q, params).fetchone()[0])

    def recent_decisions(self, limit: int = 60, mode: str | None = None) -> list:
        """mode filters like the other read paths: the dashboard feed and the
        chatbot read the bot's OWN paper decisions first and only fall back to
        every row on a demo-only journal (seed-demo writes mode='demo')."""
        q, params = "SELECT * FROM decisions", []
        if mode:
            q += " WHERE mode=?"
            params.append(mode)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, params)]

    def equity_curve(self, limit: int = 2000, mode: str | None = None) -> list:
        q, params = "SELECT ts, equity, cash FROM equity", []
        if mode:
            q += " WHERE mode=?"
            params.append(mode)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute(q, params)]
        rows = list(reversed(rows))
        # sort chronologically by timestamp: seeded history is written
        # market-by-market, so insertion order is not time order
        rows.sort(key=lambda r: (r["ts"],))
        return rows

    def last_equity_point(self, mode: str | None = None) -> dict | None:
        """The most recent equity row (the restart anchor for broker cash).

        Ordered by timestamp, not insertion id — the journal's own
        equity_curve() sorts by ts because seeded rows are written
        market-by-market, so the last-inserted row is not the latest point.
        """
        q, params = "SELECT id, ts, equity, cash, mode FROM equity", []
        if mode:
            q += " WHERE mode=?"
            params.append(mode)
        q += " ORDER BY ts DESC, id DESC LIMIT 1"
        with self._conn() as conn:
            row = conn.execute(q, params).fetchone()
        return dict(row) if row else None

    def closed_cash_delta_since(self, ts: str, mode: str | None = None) -> float:
        """Exact net cash effect of trades CLOSED strictly after `ts` — the
        reconciliation term for a crash between a trade's close and the
        cycle-end equity write. Idempotent: once the engine journals a fresh
        equity point, the window moves past them.

        Anchor-aware arithmetic (the old query was pnl+fees = gross, which
        refunded every fee and overstated recovered cash by both legs):
          - anchor BETWEEN entry and close: the anchor cash still owes the
            entry fee + entry slippage effect but the broker only charged the
            entry fee at open (already in the anchor). The close event then
            adds gross - exit_fee. So the window delta = gross - exit_fee
            = pnl + entry_fee.
          - entry ALSO after the anchor (outage window with skipped equity
            writes): the whole trade is unreflected -> the window delta is
            pnl - entry_fee (the entry fee was charged after the anchor).
            With realized_cash_delta recorded (the exact close-event delta) we
            take the exact value and subtract the entry fee charged inside the
            window.
        Legacy rows (no entry_fee/realized_cash_delta) approximate entry_fee
        as fees/2 — right by symmetry, documented in _migrate."""
        q = ("SELECT COALESCE(SUM(CASE"
             " WHEN opened_ts <= ? THEN"
             "   COALESCE(realized_cash_delta, COALESCE(pnl,0) + COALESCE(entry_fee, COALESCE(fees,0)/2.0))"
             " ELSE"
             "   COALESCE(realized_cash_delta, COALESCE(pnl,0) + COALESCE(entry_fee, COALESCE(fees,0)/2.0))"
             "   - COALESCE(entry_fee, COALESCE(fees,0)/2.0)"
             " END), 0)"
             " FROM trades WHERE status='CLOSED' AND closed_ts > ?")
        params: list = [ts, ts]
        if mode:
            q += " AND mode=?"
            params.append(mode)
        with self._conn() as conn:
            return float(conn.execute(q, params).fetchone()[0])

    def stats(self, mode: str | None = None) -> dict:
        # SQL aggregates — the old Python-side full-table scan read every
        # rationale TEXT blob on each 4s dashboard poll. mode is a bound
        # parameter everywhere (the old f-string built invalid SQL:
        # "WHERE status='CLOSED' WHERE mode=..." — a crash on any mode filter).
        mode_sql, mode_args = (" AND mode=?", (mode,)) if mode else ("", ())
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n,"
                " COALESCE(SUM(CASE WHEN COALESCE(pnl,0) > 0 THEN 1 ELSE 0 END), 0) AS wins,"
                " COALESCE(SUM(CASE WHEN COALESCE(pnl,0) > 0 THEN COALESCE(pnl,0) ELSE 0 END), 0) AS gross_win,"
                " COALESCE(SUM(CASE WHEN COALESCE(pnl,0) <= 0 THEN ABS(COALESCE(pnl,0)) ELSE 0 END), 0) AS gross_loss,"
                " COALESCE(SUM(COALESCE(pnl,0)), 0) AS total"
                f" FROM trades WHERE status='CLOSED'{mode_sql}", mode_args).fetchone()
            n_open = conn.execute(
                f"SELECT COUNT(*) FROM trades WHERE status='OPEN'{mode_sql}",
                mode_args).fetchone()[0]
            by_strategy = {r["strategy"]: {"trades": r["n"], "wins": r["wins"],
                                            "pnl": round(r["pnl"] or 0.0, 2)}
                           for r in conn.execute(
                               "SELECT strategy, COUNT(*) AS n,"
                               " SUM(CASE WHEN COALESCE(pnl,0) > 0 THEN 1 ELSE 0 END) AS wins,"
                               " SUM(COALESCE(pnl,0)) AS pnl"
                               f" FROM trades WHERE status='CLOSED'{mode_sql} GROUP BY strategy",
                               mode_args)}
            eq_q, eq_args = "SELECT equity FROM equity", []
            if mode:
                eq_q += " WHERE mode=?"
                eq_args.append(mode)
            # ORDER BY ts: seeded rows are inserted market-by-market, so id
            # order scrambles the peak-to-trough walk (drawdown, start/end).
            # The scan is bounded (stats() runs on every 4s dashboard poll; a
            # year of sub-minute equity points would otherwise read ~500k rows
            # each time) — 200k rows covers years of realistic runs identically.
            eq = [r[0] for r in conn.execute(eq_q + " ORDER BY ts, id LIMIT 200000",
                                             eq_args)]

        n, wins = row["n"], row["wins"]
        losses = n - wins
        gross_win = row["gross_win"] or 0.0
        gross_loss = row["gross_loss"] or 0.0
        total = row["total"] or 0.0

        max_dd, peak = 0.0, float("-inf")
        for e in eq:
            peak = max(peak, e)
            max_dd = min(max_dd, (e - peak) / peak if peak > 0 else 0.0)

        start_eq = eq[0] if eq else CONFIG.paper_capital
        end_eq = eq[-1] if eq else start_eq
        return {
            "total_pnl": round(total, 2),
            "closed_trades": n,
            "open_trades": n_open,
            "win_rate": round(wins / n * 100, 1) if n else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "avg_win": round(gross_win / wins, 2) if wins else 0.0,
            "avg_loss": round(-gross_loss / losses, 2) if losses else 0.0,
            "max_drawdown_pct": round(max_dd * 100, 2),
            "start_equity": start_eq,
            "current_equity": round(end_eq, 2),
            "return_pct": round((end_eq / start_eq - 1) * 100, 2) if start_eq else 0.0,
            "by_strategy": by_strategy,
        }
