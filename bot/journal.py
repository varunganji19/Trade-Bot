"""
SQLite trading journal — the single source of truth for the dashboard and chatbot.

Tables:
  decisions  — every evaluation the bot makes (even HOLDs, with full reasoning)
  trades     — position lifecycle with strategy attribution
  equity     — mark-to-market equity curve points
  cash_events — ordered, per-book cash effects for restart reconciliation
  chat_log   — dashboard chatbot conversation
  transactions — deposit/withdrawal/reset ledger (Account tab history)
  book_owner — which process currently owns each book (cross-process lease)

Concurrency: the dashboard's API threads and the live engine thread write through
ONE Journal instance per process, but a second process (CLI engine, seeding) can
exist too — so every connection enables WAL (readers never block the writer) and
a generous busy timeout, and a module-level lock serializes writers across ALL
Journal instances of this process. Serialized writes are not the same as a
single owner, though: an account's balance is also held in an engine's memory
between checkpoints, so trading a book is leased through the book_owner table
(see BookOwnedError) and only the lease-holder may checkpoint or adjust it.
"""
from __future__ import annotations

import json
import math
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


def _is_locked_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _with_busy_retry(fn, retries: int = 3, base_s: float = 0.05):
    """Retry SQLITE_BUSY/LOCKED with backoff (two processes can contend: CLI
    engine + dashboard). Raises the last OperationalError so the dashboard
    can map it to 503."""
    last = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not _is_locked_error(exc) or attempt >= retries:
                raise
            last = exc
            time.sleep(base_s * (2 ** attempt))
    raise last  # pragma: no cover


def _retry_busy(fn):
    """Decorator: retry SQLITE_BUSY/LOCKED 3x with backoff, then re-raise
    (dashboard maps the survivor to HTTP 503)."""
    import functools

    @functools.wraps(fn)
    def wrap(*a, **k):
        return _with_busy_retry(lambda: fn(*a, **k))
    return wrap

class BookOwnedError(Exception):
    """Another live process owns this book's account state.

    The process lock and SQLite's writer lock make individual writes safe, but
    neither stops a standalone `main.py run` engine and the dashboard from each
    believing they own a book: the engine holds cash in memory for a whole
    cycle, so a dashboard deposit committed in between is silently erased by
    the engine's next checkpoint (its stale broker cash becomes the new
    anchor), leaving the transactions ledger claiming a deposit the equity
    curve never received. Ownership is a row in the database, not a variable
    in one process, so it is visible to every process sharing the file.
    """


# A lease whose owner is gone must not block the book forever, and a lease
# whose owner is alive must never be stolen. On the same host the owning pid
# settles both: it is checked directly and the heartbeat is not consulted at
# all, because an engine's cycle interval is operator-controlled up to an
# hour and a laptop can sleep — a live engine whose last heartbeat is old is
# still the owner. Across hosts (never a supported setup for one SQLite file,
# but not a crash either) no pid can be probed, so only a silent heartbeat
# can expire a lease.
_LEASE_TTL_S = 900.0

# `owner_token` has three meanings. A token string asserts "this lease is
# mine"; NO_OWNER asserts "no process owns this book"; None skips the check
# for writers that are not part of a trading session (seeding, migrations).
NO_OWNER = "\x00no-owner"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, owned by another user
    except (OverflowError, ValueError, OSError):
        return True   # cannot tell: assume live rather than steal the book
    return True


def _lease_is_live(row, now: float) -> bool:
    if row is None:
        return False
    if row["host"] == _HOST:
        return _pid_alive(int(row["pid"]))
    return (now - float(row["heartbeat"])) <= _LEASE_TTL_S


# the engine used to define its own byte-identical utc_now() — one shared clock
_now = utc_now
_HOST = os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", "?")

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
    last_error: str | None = None  # last quarantine/migration warning, via stats()

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or CONFIG.db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = _PROCESS_LOCK
        self.last_error = None
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
            # keep a READ-ONLY copy next to the quarantine for forensics, then
            # surface last_error (dashboard stats exposes it — never silent).
            try:
                import shutil
                shutil.copy2(self.db_path, quarantine + ".ro-copy.db")
            except OSError:
                pass
            msg = (f"trading db was corrupt ({exc}) — quarantined to "
                   f"{os.path.basename(quarantine)}(+wal/shm), kept read-only "
                   f"copy {os.path.basename(quarantine)}.ro-copy.db, starting fresh")
            print(f"[journal] {msg}")
            self.last_error = msg
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
            if "decision_bar_ts" not in cols:
                conn.execute("ALTER TABLE trades ADD COLUMN decision_bar_ts REAL")
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

    # ------------------------------------------------------------ ownership
    @staticmethod
    def _owner_row(conn, mode):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                            " AND name='book_owner'").fetchone():
            return None
        return conn.execute("SELECT * FROM book_owner WHERE mode=?", (mode,)).fetchone()

    def _require_owner(self, conn, mode: str, token: str | None):
        """Fail the caller's transaction unless it may write this book.

        See NO_OWNER for the three meanings of `token`. Called INSIDE the
        caller's transaction, so the check and the write commit or roll back
        together — a lease cannot change underneath them.
        """
        if token is None:
            return
        row = self._owner_row(conn, mode)
        live = _lease_is_live(row, time.time())
        if token != NO_OWNER:
            if not live or row["token"] != token:
                raise BookOwnedError(
                    f"the {mode} book's lease was lost (another process took it "
                    f"over, or it expired) — this engine must stop")
            return
        if live:
            raise BookOwnedError(
                f"the {mode} book is owned by pid {row['pid']} on {row['host']} "
                f"since {row['started_ts']} — stop that engine first")

    @_retry_busy
    def claim_book(self, mode: str = "paper") -> str:
        """Take this book's lease, returning the token its writes must carry.

        Raises BookOwnedError when another live process holds it. A lease whose
        owner died is taken over (see _lease_is_live).
        """
        token = f"{_HOST}:{os.getpid()}:{time.time_ns()}"
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_owner(conn, mode, NO_OWNER)
            conn.execute(
                "INSERT INTO book_owner (mode, token, pid, host, started_ts, heartbeat)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(mode) DO UPDATE SET token=excluded.token, pid=excluded.pid,"
                " host=excluded.host, started_ts=excluded.started_ts, heartbeat=excluded.heartbeat",
                (mode, token, os.getpid(), _HOST, _now(), time.time()))
        return token

    @_retry_busy
    def heartbeat_book(self, mode: str, token: str) -> bool:
        """Refresh the lease. False means it is no longer ours — stop writing."""
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("UPDATE book_owner SET heartbeat=? WHERE mode=? AND token=?",
                               (time.time(), mode, token))
            return bool(cur.rowcount)

    @_retry_busy
    def release_book(self, mode: str, token: str) -> None:
        """Release our lease. Releasing a lease we no longer hold is a no-op."""
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM book_owner WHERE mode=? AND token=?", (mode, token))

    def book_owner(self, mode: str = "paper") -> dict | None:
        """The live owner of this book, or None. Read-only (status displays)."""
        with self._conn() as conn:
            row = self._owner_row(conn, mode)
        return dict(row) if _lease_is_live(row, time.time()) else None

    # ---------------------------------------------------------------- writes
    @_retry_busy
    def reset_account(self, capital: float, backup_path: str, mode: str = "paper"):
        """Back up a consistent SQLite snapshot, then atomically reset one book.

        Reserve the writer before taking the backup so another process cannot
        insert rows between the backup and deletion. The backup uses a separate
        reader: SQLite's backup API cannot run on our active write transaction.
        Global chat history belongs to no single book and is retained. An
        engine owning this book in ANY process refuses the reset: a wipe under
        a live engine forks its in-memory broker from the journal.
        """
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_owner(conn, mode, NO_OWNER)
            with self._conn() as source:
                dest = sqlite3.connect(backup_path)
                try:
                    source.backup(dest)
                    if dest.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise sqlite3.DatabaseError("reset backup failed integrity check")
                finally:
                    dest.close()
            for table in ("trades", "equity", "decisions", "transactions", "cash_events"):
                conn.execute(f"DELETE FROM {table} WHERE mode=?", (mode,))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_state'").fetchone():
                conn.execute("DELETE FROM risk_state WHERE mode=?", (mode,))
            ts = _now()
            self._insert_equity(conn, capital, capital, mode, "account reset", ts)
            conn.execute(
                "INSERT INTO transactions (ts, kind, amount, cash_after, equity_after, mode, note)"
                " VALUES (?,?,?,?,?,?,?)",
                (ts, "reset", capital, capital, capital, mode, "account reset"))

    @_retry_busy
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

    @_retry_busy
    def open_trade(self, symbol: str, side: str, qty: float, entry_price: float,
                   stop: float | None, target: float | None, strategy: str,
                   rationale: str, mode: str = "paper", opened_ts: str | None = None,
                   timeframe: str = "1h", entry_fee: float | None = None,
                   pending_fill: bool = False, decision_bar_ts: float | None = None) -> int:
        """`stop` (when given) is recorded as BOTH stop_price and
        initial_stop_price: at open they are the same level, and the initial
        copy is never overwritten by later trails (R-multiple ground truth).
        `entry_fee` settles an already-filled imported entry. Live engine
        intents use pending_fill=True until record_fill atomically confirms
        the position and its cash effect."""
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO trades (symbol, side, qty, entry_price, stop_price,"
                " initial_stop_price, target_price, strategy, status, opened_ts,"
                " rationale_open, mode, timeframe, entry_fee, decision_bar_ts)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (symbol, side, qty, entry_price, stop, stop, target, strategy,
                 "PENDING" if pending_fill else "OPEN",
                 _iso(opened_ts), rationale, mode, timeframe, entry_fee, decision_bar_ts))
            if entry_fee is not None and not pending_fill:
                self._cash_event(conn, mode, "entry", -entry_fee, cur.lastrowid)
            return cur.lastrowid

    @_retry_busy
    def record_fill(self, trade_id: int, entry_price: float, stop: float | None = None,
                    target: float | None = None, entry_fee: float | None = None,
                    initial_stop: float | None = None,
                    decision_bar_ts: float | None = None):
        """Post-fill correction of the row the engine opens BEFORE the broker
        fill (journal-first, self-healing). Sets the fill-derived entry/stop/
        target AND their initial-risk snapshots: `initial_stop_price` latches
        the fill-derived stop exactly once (never on later calls) and
        `entry_fee` latches the entry leg's fee. Re-calls only refresh
        stop_price/target_price — the trailing path."""
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
            if row is None or row["status"] not in ("OPEN", "PENDING"):
                raise ValueError("fill requires an open or pending trade")
            fee = entry_fee if entry_fee is not None else row["entry_fee"]
            if row["status"] == "PENDING" and fee is None:
                raise ValueError("a pending fill requires its settled entry fee")
            if fee is not None:
                self._cash_event(conn, row["mode"], "entry", -fee, trade_id)
            conn.execute("UPDATE trades SET entry_price=?, entry_fee=COALESCE(?,entry_fee),"
                         " status='OPEN', decision_bar_ts=COALESCE(?,decision_bar_ts) WHERE id=?",
                         (entry_price, entry_fee, decision_bar_ts, trade_id))
            if stop is not None:
                conn.execute(
                    "UPDATE trades SET stop_price=?,"
                    " initial_stop_price=COALESCE(initial_stop_price, ?) WHERE id=?",
                    (stop, initial_stop if initial_stop is not None else stop, trade_id))
            if target is not None:
                conn.execute("UPDATE trades SET target_price=? WHERE id=?",
                             (target, trade_id))

    @_retry_busy
    def close_trade(self, trade_id: int, exit_price: float, pnl: float, pnl_pct: float,
                    fees: float, exit_reason: str, rationale_close: str = "",
                    closed_ts: str | None = None, equity: float | None = None,
                    cash: float | None = None, mode: str = "paper",
                    entry_fee: float | None = None,
                    realized_cash_delta: float | None = None,
                    owner_token: str | None = None):
        """Close a trade. When equity/cash are given, the cycle-end equity point
        is written IN THE SAME transaction — a crash between the two used to
        drop the exit proceeds from the account (CLOSED trade, pre-exit cash
        as the restart anchor).

        `entry_fee` (the entry leg's fee) and `realized_cash_delta` (the exact
        broker cash effect of THIS close event) are written when supplied so
        closed_cash_delta_since can reconcile crash windows exactly instead
        of guessing which fees the anchor already reflects."""
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
            if row is None or row["status"] not in ("OPEN", "CLOSED"):
                raise ValueError("close requires an open trade")
            if row["status"] == "CLOSED":
                return  # committed retry: never settle the cash twice
            trade_mode = row["mode"]
            # guard the book this row actually belongs to, not the caller's
            # `mode` argument — a wrong argument would check one book's lease
            # and write the other's
            self._require_owner(conn, trade_mode, owner_token)
            recorded_fee = row["entry_fee"]
            if recorded_fee is None:
                recorded_fee = entry_fee if entry_fee is not None else fees / 2.0
            cash_delta = realized_cash_delta if realized_cash_delta is not None else pnl + recorded_fee
            self._cash_event(conn, trade_mode, "close", cash_delta, trade_id)
            conn.execute(
                "UPDATE trades SET status='CLOSED', exit_price=?, pnl=?, pnl_pct=?, fees=?,"
                " exit_reason=?, rationale_close=?, closed_ts=?,"
                " entry_fee=COALESCE(entry_fee, ?), realized_cash_delta=? WHERE id=?",
                (exit_price, pnl, pnl_pct, fees, exit_reason, rationale_close,
                 _iso(closed_ts), entry_fee, realized_cash_delta, trade_id))
            if equity is not None and cash is not None:
                self._insert_equity(conn, equity, cash, trade_mode, "", _now())

    @_retry_busy
    def record_round_trip(self, *, symbol: str, side: str, qty: float, price: float,
                          pnl: float, pnl_pct: float, fees: float, cash_delta: float,
                          strategy: str,
                          rationale: str, rationale_close: str, exit_reason: str,
                          cash_after: float, equity_after: float,
                          transaction: tuple[str, float, str], mode: str = "paper",
                          timeframe: str = "1m", ts: str | None = None,
                          owner_token: str | None = None) -> int:
        """Write a position that opens and closes in one event.

        The cash-settled triangular arb is flat by construction, so it never
        has a restorable position. Writing it as open-then-close left a window
        where process death stranded an OPEN row for a symbol that is in no
        watchlist: nothing could ever mark or close it, and every later
        restart restored the ghost into the broker. One transaction writes the
        CLOSED row, its single cash event, the Account-tab row and the equity
        anchor together, and the caller moves the broker only after it commits.
        """
        ts = _iso(ts)
        kind, amount, note = transaction
        # `pnl` is the display number (rounded for the trade row); `cash_delta`
        # is the exact effect on cash, so the ledger a restart replays and the
        # broker's own movement are the same value to the last digit.
        realized = float(cash_delta)
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # this writes an equity anchor, so it needs the same lease the
            # cycle checkpoint does
            self._require_owner(conn, mode, owner_token)
            cur = conn.execute(
                "INSERT INTO trades (symbol, side, qty, entry_price, exit_price, strategy,"
                " status, opened_ts, closed_ts, rationale_open, rationale_close, exit_reason,"
                " pnl, pnl_pct, fees, mode, timeframe, entry_fee, realized_cash_delta)"
                " VALUES (?,?,?,?,?,?,'CLOSED',?,?,?,?,?,?,?,?,?,?,0.0,?)",
                (symbol, side, qty, price, price, strategy, ts, ts, rationale,
                 rationale_close, exit_reason, pnl, pnl_pct, fees, mode, timeframe,
                 realized))
            trade_id = cur.lastrowid
            self._cash_event(conn, mode, "close", realized, trade_id)
            conn.execute(
                "INSERT INTO transactions (ts, kind, amount, cash_after, equity_after, mode, note)"
                " VALUES (?,?,?,?,?,?,?)",
                (ts, kind, amount, cash_after, equity_after, mode, note))
            self._insert_equity(conn, equity_after, cash_after, mode, "", ts)
            return trade_id

    @_retry_busy
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

    @_retry_busy
    def abort_trade(self, trade_id: int):
        """Mark a just-opened trade ABORTED when the broker fill failed after
        the INSERT — without this the OPEN row lingers and a restart restores
        a ghost position for a trade that never existed in the broker."""
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE trades SET status='ABORTED',"
                         " rationale_close='aborted: broker fill failed'"
                         " WHERE id=? AND status IN ('OPEN','PENDING')", (trade_id,))

    @staticmethod
    def _cash_event(conn, mode, kind, amount, trade_id=None):
        if not math.isfinite(amount):
            raise ValueError("cash event amount must be finite")
        if trade_id is not None:
            prior = conn.execute("SELECT amount FROM cash_events WHERE trade_id=? AND kind=?",
                                 (trade_id, kind)).fetchone()
            if prior is not None:
                if not math.isclose(prior["amount"], amount, rel_tol=0, abs_tol=1e-12):
                    raise ValueError("a settled cash event cannot be changed")
                return
        conn.execute("INSERT INTO cash_events (ts,mode,kind,amount,trade_id) VALUES (?,?,?,?,?)",
                     (_now(), mode, kind, amount, trade_id))

    @staticmethod
    def _insert_equity(conn, equity, cash, mode, note, ts):
        cursor = conn.execute("SELECT COALESCE(MAX(id),0) FROM cash_events WHERE mode=?",
                              (mode,)).fetchone()[0]
        conn.execute("INSERT INTO equity (ts,equity,cash,mode,note,cash_event_id) VALUES (?,?,?,?,?,?)",
                     (ts, equity, cash, mode, note, cursor))

    @_retry_busy
    def add_equity(self, equity: float, cash: float, mode: str = "paper", note: str = "",
                   ts: str | None = None, owner_token: str | None = None):
        """Checkpoint the book's cash/equity anchor.

        An engine passes its lease: this anchor is written from cash it has
        held in memory for a whole cycle, so writing it after losing the book
        would silently erase whatever the new owner committed meanwhile. The
        check runs in this transaction, so the lease cannot lapse between.
        """
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_owner(conn, mode, owner_token)
            self._insert_equity(conn, equity, cash, mode, note, _iso(ts))

    def _recover_cash(self, conn, initial_cash, mode):
        # Live checkpoints are ordered by commit id, not their wall clock.
        # Clock rollback and multiple events within one second are harmless.
        anchor = conn.execute("SELECT * FROM equity WHERE mode=? ORDER BY id DESC LIMIT 1",
                              (mode,)).fetchone()
        cash = float(anchor["cash"]) if anchor else float(initial_cash)
        cursor = anchor["cash_event_id"] if anchor else None
        cash += conn.execute("SELECT COALESCE(SUM(amount),0) FROM cash_events WHERE mode=? AND id>?",
                             (mode, cursor or 0)).fetchone()[0]
        if cursor is None:
            # No order can be inferred retrospectively for same-second legacy
            # writes. Retain their strict timestamp boundary, then switch to
            # exact event cursors on the next checkpoint. Never replay a leg
            # which already has an event in the new ledger.
            ts = anchor["ts"] if anchor else ""
            cash -= conn.execute(
                "SELECT COALESCE(SUM(COALESCE(entry_fee,fees/2.0,0)),0) FROM trades t"
                " WHERE mode=? AND status IN ('OPEN','CLOSED') AND opened_ts>?"
                " AND NOT EXISTS(SELECT 1 FROM cash_events e WHERE e.trade_id=t.id AND e.kind='entry')",
                (mode, ts)).fetchone()[0]
            cash += conn.execute(
                "SELECT COALESCE(SUM(COALESCE(realized_cash_delta,pnl+COALESCE(entry_fee,fees/2.0,0),0)),0)"
                " FROM trades t WHERE mode=? AND status='CLOSED' AND closed_ts>?"
                " AND NOT EXISTS(SELECT 1 FROM cash_events e WHERE e.trade_id=t.id AND e.kind='close')",
                (mode, ts)).fetchone()[0]
        return cash

    @_retry_busy
    def recover_cash(self, initial_cash: float, mode: str = "paper") -> float:
        """Recover settled cash exactly once from the last checkpoint cursor."""
        with self._conn() as conn:
            conn.execute("BEGIN")
            return self._recover_cash(conn, initial_cash, mode)

    def adjust_account(self, amount: float, kind: str, mode: str = "paper", *,
                       base_cash: float | None = None, base_equity: float | None = None,
                       initial_cash: float | None = None, on_adjust=None,
                       owner_token: str | None = NO_OWNER) -> dict:
        """Commit cash event, checkpoint and typed ledger under one writer lock.

        Engine-on callers supply a broker snapshot while holding cycle_lock.
        Engine-off callers recover their balance inside BEGIN IMMEDIATE, so
        concurrent dashboard adjustments cannot overwrite each other.
        on_adjust(delta, conn) may persist risk baselines in this transaction.
        `owner_token` is the calling engine's lease; without one, an engine
        owning this book in any process refuses the adjustment rather than
        letting its next checkpoint overwrite the new balance.

        Only the callback-free form retries a busy database. `on_adjust`
        mutates the caller's in-memory risk baselines before this transaction
        commits, so a blind retry of a COMMIT that lost the writer race would
        apply the delta to those baselines twice while the database, correctly
        rolled back, recorded it once. The caller retries that form itself.
        """
        if on_adjust is None:
            return _with_busy_retry(lambda: self._adjust_account_once(
                amount, kind, mode, base_cash=base_cash, base_equity=base_equity,
                initial_cash=initial_cash, on_adjust=None, owner_token=owner_token))
        return self._adjust_account_once(
            amount, kind, mode, base_cash=base_cash, base_equity=base_equity,
            initial_cash=initial_cash, on_adjust=on_adjust, owner_token=owner_token)

    def _adjust_account_once(self, amount: float, kind: str, mode: str = "paper", *,
                             base_cash: float | None = None, base_equity: float | None = None,
                             initial_cash: float | None = None, on_adjust=None,
                             owner_token: str | None = NO_OWNER) -> dict:
        """One attempt at adjust_account; see it for the contract."""
        if kind not in ("deposit", "withdrawal") or not math.isfinite(amount) or amount <= 0:
            raise ValueError("a deposit or withdrawal requires a finite positive amount")
        delta = amount if kind == "deposit" else -amount
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_owner(conn, mode, owner_token)
            if base_cash is None:
                base_cash = self._recover_cash(conn, CONFIG.paper_capital if initial_cash is None else initial_cash, mode)
                anchor = conn.execute("SELECT equity,cash FROM equity WHERE mode=? ORDER BY id DESC LIMIT 1",
                                      (mode,)).fetchone()
                base_equity = base_cash + (anchor["equity"] - anchor["cash"] if anchor else 0)
            if base_equity is None or not math.isfinite(base_cash) or not math.isfinite(base_equity):
                raise ValueError("account balance must be finite")
            cash, equity = base_cash + delta, base_equity + delta
            if not math.isfinite(cash) or not math.isfinite(equity):
                raise ValueError("adjusted account balance must be finite")
            if kind == "withdrawal" and cash < -1e-9:
                raise ValueError(f"insufficient cash: ${base_cash:,.2f} available, -${amount:,.2f} requested")
            ts, note = _now(), f"manual {kind}"
            self._cash_event(conn, mode, kind, delta)
            self._insert_equity(conn, equity, cash, mode, note, ts)
            conn.execute(
                "INSERT INTO transactions (ts,kind,amount,cash_after,equity_after,mode,note) VALUES (?,?,?,?,?,?,?)",
                (ts, kind, amount, cash, equity, mode, note))
            if on_adjust is not None:
                on_adjust(delta, conn)
            elif conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_state'").fetchone():
                row = conn.execute("SELECT state_json FROM risk_state WHERE mode=?", (mode,)).fetchone()
                if row is not None:
                    try:
                        state = json.loads(row["state_json"])
                        daily, peak = state["daily_start_equity"], state["peak_equity"]
                        if (state.get("version") != 1 or isinstance(peak, bool)
                                or not isinstance(peak, (int, float)) or not math.isfinite(peak)
                                or (daily is not None and (isinstance(daily, bool)
                                    or not isinstance(daily, (int, float)) or not math.isfinite(daily)))):
                            raise ValueError("invalid risk baseline")
                        state["daily_start_equity"] = daily + delta if daily is not None else None
                        state["peak_equity"] = max(0.0, peak + delta)
                        payload = json.dumps(state, allow_nan=False)
                    except (ValueError, TypeError, KeyError) as exc:
                        raise ValueError("cannot adjust an invalid risk checkpoint") from exc
                    conn.execute("UPDATE risk_state SET state_json=? WHERE mode=?", (payload, mode))
            return {"cash": cash, "equity": equity}

    @_retry_busy
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

    @_retry_busy
    def log_chat(self, role: str, content: str):
        with self._lock, self._conn() as conn:
            conn.execute("INSERT INTO chat_log (ts, role, content) VALUES (?,?,?)",
                         (_now(), role, content))

    # ---------------------------------------------------------------- reads
    @_retry_busy
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

    @_retry_busy
    def recent_trades(self, limit: int = 100, mode: str | None = None,
                      since_id: int | None = None) -> list:
        q, params = "SELECT * FROM trades", []
        if mode:
            q += " WHERE mode=?"
            params.append(mode)
        if since_id is not None:
            q += (" AND id>?" if mode else " WHERE id>?")
            params.append(since_id)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, params)]

    @_retry_busy
    def trade_mode_counts(self) -> dict[str, int]:
        """Closed+open trade counts per mode ('paper' vs 'demo'): the dashboard
        badges seeded demo rows instead of silently presenting them as the
        bot's own paper record."""
        with self._conn() as conn:
            return {r["mode"]: r["n"] for r in conn.execute(
                "SELECT mode, COUNT(*) AS n FROM trades GROUP BY mode")}

    @_retry_busy
    def recent_transactions(self, limit: int = 100, mode: str | None = None) -> list:
        q, params = "SELECT * FROM transactions", []
        if mode:
            q += " WHERE mode=?"
            params.append(mode)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, params)]

    @_retry_busy
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

    @_retry_busy
    def recent_decisions(self, limit: int = 60, mode: str | None = None,
                         since_id: int | None = None) -> list:
        """mode filters like the other read paths: the dashboard feed and the
        chatbot read the bot's OWN paper decisions first and only fall back to
        every row on a demo-only journal (seed-demo writes mode='demo')."""
        q, params = "SELECT * FROM decisions", []
        if mode:
            q += " WHERE mode=?"
            params.append(mode)
        if since_id is not None:
            q += (" AND id>?" if mode else " WHERE id>?")
            params.append(since_id)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, params)]

    @_retry_busy
    def equity_curve(self, limit: int = 2000, mode: str | None = None,
                     since_id: int | None = None) -> list:
        q, params = "SELECT id, ts, equity, cash FROM equity", []
        if mode:
            q += " WHERE mode=?"
            params.append(mode)
        if since_id is not None:
            q += (" AND id>?" if mode else " WHERE id>?")
            params.append(since_id)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute(q, params)]
        rows = list(reversed(rows))
        # sort chronologically by timestamp: seeded history is written
        # market-by-market, so insertion order is not time order
        rows.sort(key=lambda r: (r["ts"],))
        return rows

    @_retry_busy
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

    @_retry_busy
    def closed_cash_delta_since(self, ts: str, mode: str | None = None) -> float:
        """Legacy timestamp-query compatibility; live recovery uses recover_cash.

        Net cash effect of trades CLOSED strictly after `ts` — the
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

    @_retry_busy
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
        out = {
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
        if getattr(self, "last_error", None):
            out["journal_error"] = self.last_error
        return out
