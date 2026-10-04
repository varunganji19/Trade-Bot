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


# one shared clock for every journal timestamp
_now = utc_now
_HOST = os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", "?")


# real-file-corruption signatures only — NOT lock/busy/disk-full (a merely
# locked or full-disk db must never be quarantined, only a torn file)
_DB_CORRUPT_SIGNATURES = ("not a database", "malformed")


def _iso(ts: str | None) -> str:
    """Canonical journal timestamp (ISO-UTC, 'T' separator). pandas'
    space-separated str(Timestamp) sorts differently within a day and would
    corrupt ts-ordered reads (the restart cash anchor could pick a stale
    point). Normalize every ts ON WRITE, and
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


from bot.journal.ledger import LedgerMixin  # noqa: E402  (helpers above first)
from bot.journal.reads import ReadsMixin  # noqa: E402
from bot.journal.schema import _SCHEMA, SchemaMixin  # noqa: E402
from bot.journal.trades import TradesMixin  # noqa: E402
from bot.journal.executions import ExecutionsMixin  # noqa: E402


class Journal(LedgerMixin, TradesMixin, ExecutionsMixin, ReadsMixin, SchemaMixin):
    """The trading journal. Its methods are grouped by concern: schema.py
    (tables, migrations), ledger.py (ownership, cash, equity, reconciliation),
    trades.py (decisions and the trade lifecycle) and reads.py (queries and
    statistics)."""
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

