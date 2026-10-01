"""Journal trades: decisions, the trade lifecycle (open, fill, stop updates,
close, abort) and the chat log."""
from __future__ import annotations

import json

import bot.journal as base   # shared helpers, looked up at call time (tests patch base._now)

class TradesMixin:
    @base._retry_busy
    def add_decision(self, symbol: str, timeframe: str, decision, mode: str = "paper") -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO decisions (ts, symbol, timeframe, action, confidence, price, regime,"
                " stop_distance, target_rr, strategy_signals, sentiment, rationale, mode)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (base._now(), symbol, timeframe, decision.action, float(decision.confidence),
                 float(decision.price), decision.regime, decision.stop_distance, decision.target_rr,
                 json.dumps(decision.strategy_signals, default=str),
                 json.dumps(decision.sentiment, default=str),
                 decision.rationale, mode))
            return cur.lastrowid

    @base._retry_busy
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
                 base._iso(opened_ts), rationale, mode, timeframe, entry_fee, decision_bar_ts))
            if entry_fee is not None and not pending_fill:
                self._cash_event(conn, mode, "entry", -entry_fee, cur.lastrowid)
            return cur.lastrowid

    @base._retry_busy
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

    @base._retry_busy
    def close_trade(self, trade_id: int, exit_price: float, pnl: float, pnl_pct: float,
                    fees: float, exit_reason: str, rationale_close: str = "",
                    closed_ts: str | None = None, equity: float | None = None,
                    cash: float | None = None, mode: str = "paper",
                    entry_fee: float | None = None,
                    realized_cash_delta: float | None = None,
                    owner_token: str | None = None):
        """Close a trade. When equity/cash are given, the cycle-end equity point
        is written IN THE SAME transaction, so a crash between the two cannot
        drop the exit proceeds from the account (a CLOSED trade with pre-exit
        cash as the restart anchor).

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
                 base._iso(closed_ts), entry_fee, realized_cash_delta, trade_id))
            if equity is not None and cash is not None:
                self._insert_equity(conn, equity, cash, trade_mode, "", base._now())

    @base._retry_busy
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
        ts = base._iso(ts)
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

    @base._retry_busy
    def update_trade_stops(self, trade_id: int, stop: float | None = None, target: float | None = None,
                           entry_price: float | None = None,
                           stop_effective_bar_ts: float | None = None):
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
                conn.execute("UPDATE trades SET stop_price=?, stop_effective_bar_ts=? WHERE id=?",
                             (stop, stop_effective_bar_ts, trade_id))
            if target is not None:
                conn.execute("UPDATE trades SET target_price=? WHERE id=?", (target, trade_id))

    @base._retry_busy
    def abort_trade(self, trade_id: int):
        """Mark a just-opened trade ABORTED when the broker fill failed after
        the INSERT — without this the OPEN row lingers and a restart restores
        a ghost position for a trade that never existed in the broker."""
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE trades SET status='ABORTED',"
                         " rationale_close='aborted: broker fill failed'"
                         " WHERE id=? AND status IN ('OPEN','PENDING')", (trade_id,))

    @base._retry_busy
    def log_chat(self, role: str, content: str):
        with self._lock, self._conn() as conn:
            conn.execute("INSERT INTO chat_log (ts, role, content) VALUES (?,?,?)",
                         (base._now(), role, content))
