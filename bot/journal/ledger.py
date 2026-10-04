"""Journal ledger: book ownership (the cross-process lease), cash events,
equity checkpoints, cash recovery, deposits/withdrawals/reset, and the
consistency check that reconciles a book's cash with its history."""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time

import bot.journal as base   # shared helpers, looked up at call time (tests patch base._now)
from config import CONFIG

class LedgerMixin:
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
        live = base._lease_is_live(row, time.time())
        if token != base.NO_OWNER:
            if not live or row["token"] != token:
                raise base.BookOwnedError(
                    f"the {mode} book's lease was lost (another process took it "
                    f"over, or it expired) — this engine must stop")
            return
        if live:
            raise base.BookOwnedError(
                f"the {mode} book is owned by pid {row['pid']} on {row['host']} "
                f"since {row['started_ts']} — stop that engine first")

    @base._retry_busy
    def claim_book(self, mode: str = "paper") -> str:
        """Take this book's lease, returning the token its writes must carry.

        Raises BookOwnedError when another live process holds it. A lease whose
        owner died is taken over (see _lease_is_live).
        """
        token = f"{base._HOST}:{os.getpid()}:{time.time_ns()}"
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_owner(conn, mode, base.NO_OWNER)
            conn.execute(
                "INSERT INTO book_owner (mode, token, pid, host, started_ts, heartbeat)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(mode) DO UPDATE SET token=excluded.token, pid=excluded.pid,"
                " host=excluded.host, started_ts=excluded.started_ts, heartbeat=excluded.heartbeat",
                (mode, token, os.getpid(), base._HOST, base._now(), time.time()))
        return token

    @base._retry_busy
    def heartbeat_book(self, mode: str, token: str) -> bool:
        """Refresh the lease. False means it is no longer ours — stop writing."""
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("UPDATE book_owner SET heartbeat=? WHERE mode=? AND token=?",
                               (time.time(), mode, token))
            return bool(cur.rowcount)

    @base._retry_busy
    def release_book(self, mode: str, token: str) -> None:
        """Release our lease. Releasing a lease we no longer hold is a no-op."""
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM book_owner WHERE mode=? AND token=?", (mode, token))

    def book_owner(self, mode: str = "paper") -> dict | None:
        """The live owner of this book, or None. Read-only (status displays)."""
        with self._conn() as conn:
            row = self._owner_row(conn, mode)
        return dict(row) if base._lease_is_live(row, time.time()) else None

    # ---------------------------------------------------------------- writes
    @base._retry_busy
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
            self._require_owner(conn, mode, base.NO_OWNER)
            if mode == "testnet" and (
                    conn.execute("SELECT 1 FROM trades WHERE mode='testnet' AND status IN ('OPEN','PENDING') LIMIT 1").fetchone()
                    or conn.execute("SELECT 1 FROM testnet_dust WHERE qty>0 LIMIT 1").fetchone()
                    or conn.execute("SELECT 1 FROM testnet_execution_orders WHERE state NOT IN ('SETTLED','REJECTED') LIMIT 1").fetchone()):
                raise ValueError("cannot reset testnet holdings or unresolved exchange executions")
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
            if mode == "testnet":
                conn.execute("DELETE FROM testnet_execution_orders")
                conn.execute("DELETE FROM testnet_dust")
                # A reset intentionally discards old history; never re-import
                # its compatibility export into the fresh journal book.
                conn.execute("INSERT OR REPLACE INTO testnet_metadata(key,value) VALUES ('legacy_imported','1')")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_state'").fetchone():
                conn.execute("DELETE FROM risk_state WHERE mode=?", (mode,))
            ts = base._now()
            self._insert_equity(conn, capital, capital, mode, "account reset", ts)
            conn.execute(
                "INSERT INTO transactions (ts, kind, amount, cash_after, equity_after, mode, note)"
                " VALUES (?,?,?,?,?,?,?)",
                (ts, "reset", capital, capital, capital, mode, "account reset"))

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
                     (base._now(), mode, kind, amount, trade_id))

    @staticmethod
    def _insert_equity(conn, equity, cash, mode, note, ts):
        cursor = conn.execute("SELECT COALESCE(MAX(id),0) FROM cash_events WHERE mode=?",
                              (mode,)).fetchone()[0]
        conn.execute("INSERT INTO equity (ts,equity,cash,mode,note,cash_event_id) VALUES (?,?,?,?,?,?)",
                     (ts, equity, cash, mode, note, cursor))

    @base._retry_busy
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
            self._insert_equity(conn, equity, cash, mode, note, base._iso(ts))

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

    @base._retry_busy
    def recover_cash(self, initial_cash: float, mode: str = "paper") -> float:
        """Recover settled cash exactly once from the last checkpoint cursor."""
        with self._conn() as conn:
            conn.execute("BEGIN")
            return self._recover_cash(conn, initial_cash, mode)

    def adjust_account(self, amount: float, kind: str, mode: str = "paper", *,
                       base_cash: float | None = None, base_equity: float | None = None,
                       initial_cash: float | None = None, on_adjust=None,
                       owner_token: str | None = base.NO_OWNER) -> dict:
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
            return base._with_busy_retry(lambda: self._adjust_account_once(
                amount, kind, mode, base_cash=base_cash, base_equity=base_equity,
                initial_cash=initial_cash, on_adjust=None, owner_token=owner_token))
        return self._adjust_account_once(
            amount, kind, mode, base_cash=base_cash, base_equity=base_equity,
            initial_cash=initial_cash, on_adjust=on_adjust, owner_token=owner_token)

    def _adjust_account_once(self, amount: float, kind: str, mode: str = "paper", *,
                             base_cash: float | None = None, base_equity: float | None = None,
                             initial_cash: float | None = None, on_adjust=None,
                             owner_token: str | None = base.NO_OWNER) -> dict:
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
            ts, note = base._now(), f"manual {kind}"
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

    @base._retry_busy
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
                (base._iso(ts), kind, amount, cash_after, equity_after, mode, note))

    @base._retry_busy
    def ledger_check(self, mode: str = "paper") -> dict:
        """Does the book's cash reconcile with its own history?

        The paper broker settles like a margin account: cash moves only by
        entry fees, realized P&L and external flows, and equity is cash plus
        unrealized P&L. So, independent of any market price,

            journal cash == starting capital + net deposits
                            + realized P&L of closed trades
                            - entry fees of still-open trades

        and if cash reconciles, equity does too. A gap means the history and
        the balance disagree — the kind of legacy mismatch that once showed
        +6.9% equity beside -$195 realized P&L with nothing flagging it.
        Starting capital is the last account reset, else the configured
        paper capital."""
        with self._conn() as conn:
            reset = conn.execute(
                "SELECT amount FROM transactions WHERE mode=? AND kind='reset'"
                " ORDER BY id DESC LIMIT 1", (mode,)).fetchone()
            start = float(reset["amount"]) if reset else float(CONFIG.paper_capital)
            flows = conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM cash_events"
                " WHERE mode=? AND kind IN ('deposit','withdrawal')", (mode,)).fetchone()[0]
            realized, n_closed = conn.execute(
                "SELECT COALESCE(SUM(COALESCE(pnl,0)),0), COUNT(*) FROM trades"
                " WHERE mode=? AND status='CLOSED'", (mode,)).fetchone()
            open_fees = conn.execute(
                "SELECT COALESCE(SUM(COALESCE(entry_fee,0)),0) FROM trades"
                " WHERE mode=? AND status='OPEN'", (mode,)).fetchone()[0]
            has_anchor = conn.execute("SELECT 1 FROM equity WHERE mode=? LIMIT 1",
                                      (mode,)).fetchone() is not None
            cash = self._recover_cash(conn, start, mode)
            execution_delta = None
            if mode == "testnet":
                # Recalculate settlement effects from immutable order snapshots,
                # independently of cash events; partial exits count while OPEN.
                execution_delta = 0.0
                for o in conn.execute("SELECT o.snapshot,o.leg,t.entry_price FROM testnet_execution_orders o"
                                      " JOIN trades t ON t.id=o.trade_id WHERE o.state='SETTLED'"):
                    s = json.loads(o["snapshot"])
                    execution_delta += (-s["fee_quote"] if o["leg"] == "entry" else
                                        (s["average"]-o["entry_price"])*(s["filled"]+s["base_fee"])-s["fee_quote"])
                legacy_delta = conn.execute(
                    "SELECT COALESCE(SUM(CASE WHEN t.status='CLOSED' THEN COALESCE(t.pnl,0)"
                    " WHEN t.status='OPEN' THEN -COALESCE(t.entry_fee,0) ELSE 0 END),0)"
                    " FROM trades t WHERE t.mode='testnet' AND NOT EXISTS"
                    " (SELECT 1 FROM testnet_execution_orders o WHERE o.trade_id=t.id AND o.state='SETTLED')").fetchone()[0]
        expected = start + flows + (execution_delta+legacy_delta if execution_delta is not None else realized-open_fees)
        gap = cash - expected
        # per-trade P&L is stored to the cent, so rounding alone can drift by
        # half a cent a trade; a dollar of slack covers float noise
        tolerance = 1.0 + 0.005 * n_closed
        return {"ok": (not has_anchor) or abs(gap) <= tolerance,
                "cash": round(cash, 2), "expected_cash": round(expected, 2),
                "gap": round(gap, 2), "tolerance": round(tolerance, 2),
                "start_capital": round(start, 2), "net_deposits": round(flows, 2),
                "realized_pnl": round(realized, 2), "open_entry_fees": round(open_fees, 2),
                "closed_trades": n_closed, "start_source": "reset" if reset else "config"}

    @base._retry_busy
    def closed_cash_delta_since(self, ts: str, mode: str | None = None) -> float:
        """Legacy timestamp-query compatibility; live recovery uses recover_cash.

        Net cash effect of trades CLOSED strictly after `ts` — the
        reconciliation term for a crash between a trade's close and the
        cycle-end equity write. Idempotent: once the engine journals a fresh
        equity point, the window moves past them.

        Anchor-aware arithmetic (pnl + fees would be gross, refunding every
        fee and overstating recovered cash by both legs):
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
