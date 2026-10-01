"""Paper account and chatbot API: balance, deposits and withdrawals, the
type-to-confirm reset (backup first), the transaction ledger, and the
journal-aware chat. Shared state is read from bot.dashboard at call time."""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from fastapi import APIRouter, HTTPException, Query

from bot import dashboard as core
from bot.api.models import AmountIn, ChatIn, EmptyIn, ResetIn
from bot.journal import BookOwnedError
from config import CONFIG

router = APIRouter()


# ---------------------------------------------------------------------------
# account — paper balance management
@router.get("/api/account")
def api_account():
    last = core.journal.last_equity_point(mode="paper")
    eng = core._get_engine()
    if eng is not None:
        _, _, price_map = core._live_state(eng)   # only price_map is needed here
        cash = eng.broker.cash
        equity = eng.broker.equity(price_map)
    else:
        cash = last["cash"] if last else CONFIG.paper_capital
        equity = last["equity"] if last else CONFIG.paper_capital
    return {"capital": CONFIG.paper_capital, "cash": round(cash, 2), "equity": round(equity, 2),
            "mode": "paper", "engine_running": eng is not None,
            "last_equity": last,
            "unrealized": round(equity - cash, 2)}


def _adjust_account(amount: float, direction: str) -> dict:
    """Atomically adjust the account ledger and broker under lifecycle/cycle locks."""
    kind = "deposit" if direction == "deposit" else "withdrawal"
    eng = core._get_engine()
    try:
        if eng is not None:
            try:
                marks = core._mark_map(eng)
            except Exception:
                marks = {}
            with eng.cycle_lock, core._engine_lock:
                if core._engine is not eng:
                    raise HTTPException(409, "engine restarted mid-adjust — retry")
                risk = eng.risk
                fields = ("daily_start_equity", "peak_equity", "_saved_state", "persistence_error")
                snapshot = {name: getattr(risk, name) for name in fields if hasattr(risk, name)}
                try:
                    result = core.journal.adjust_account(
                        amount, kind, mode="paper", base_cash=eng.broker.cash,
                        base_equity=eng.broker.equity(marks),
                        owner_token=eng.book_token,
                        on_adjust=lambda delta, conn: risk.adjust_cash_flow(delta, conn=conn))
                except Exception:
                    for name, value in snapshot.items():
                        setattr(risk, name, value)
                    raise
                eng.broker.cash = result["cash"]
        else:
            # Reserve the lifecycle lock as well as SQLite's writer. An engine
            # starting or still finishing a cycle owns the account until done.
            with core._engine_lock:
                if (core._engine is not None or core._engine_starting
                        or (core._engine_thread is not None and core._engine_thread.is_alive())):
                    raise HTTPException(409, "engine is starting or stopping — retry once settled")
                result = core.journal.adjust_account(amount, kind, mode="paper")
    except BookOwnedError as exc:
        # another process (a standalone CLI engine) owns this account
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except sqlite3.OperationalError as exc:
        if core._is_busy_error(exc):
            raise HTTPException(503, "journal is busy — retry shortly") from exc
        raise
    return {"status": kind, "amount": round(amount, 2),
            "cash": round(result["cash"], 2), "equity": round(result["equity"], 2)}


@router.post("/api/account/deposit")
def api_account_deposit(body: AmountIn):
    return _adjust_account(body.amount, "deposit")


@router.post("/api/account/withdraw")
def api_account_withdraw(body: AmountIn):
    return _adjust_account(body.amount, "withdraw")


def _prune_reset_backups(keep: str, keep_n: int = 5):
    """Keep only the newest `keep_n` reset backups (each is a full db copy —
    ~20MB weekly resets would grow to ~1GB/year with no pruning). Never touches
    the just-written `keep` path; failures are silent (pruning is best-effort)."""
    try:
        import glob
        pattern = f"{CONFIG.db_path.rsplit('.db', 1)[0]}.backup.*.db"
        backups = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
        for old in backups[keep_n:]:
            if os.path.abspath(old) != os.path.abspath(keep):
                os.remove(old)
    except OSError:
        pass


@router.post("/api/account/reset")
def api_account_reset(body: ResetIn):
    # a reset while the engine trades would fork broker state from the journal
    stop_status = core.api_engine_stop(EmptyIn())["status"]
    if stop_status in ("stopping", "starting"):
        # the engine thread outlived the bounded join: a wipe now could race
        # its in-flight close_trade/add_equity writes into the fresh DB
        raise HTTPException(409, "engine is still stopping — retry the reset once "
                                 "its status shows stopped")
    # Hold the lifecycle lock through reset: no new paper engine may restore
    # the old account while we replace it. Recheck the thread independently of
    # _engine, which stop clears BEFORE its last in-flight cycle finishes.
    with core._engine_lock:
        if (core._engine is not None or core._engine_starting
                or (core._engine_thread is not None and core._engine_thread.is_alive())):
            raise HTTPException(409, "engine is starting or stopping — retry once stopped")
        backup = f"{core.journal.db_path.rsplit('.db', 1)[0]}.backup.{time.time_ns()}.db"
        try:
            core.journal.reset_account(round(body.capital, 2), backup, mode="paper")
        except BookOwnedError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (OSError, sqlite3.Error) as exc:
            if core._is_busy_error(exc):
                raise HTTPException(503, "journal is busy — retry shortly") from exc
            raise HTTPException(500, f"reset aborted — backup/reset failed: {exc}") from exc
    _prune_reset_backups(backup)
    return {"status": "reset", "capital": round(body.capital, 2), "backup": backup}


@router.get("/api/account/transactions")
def api_account_transactions(limit: int = Query(default=100, ge=1, le=1000)):
    # paper-mode ledger only (matches api_equity/api_trades conventions)
    return core.journal.recent_transactions(limit=limit, mode="paper")


# ---------------------------------------------------------------------------
# chatbot
@router.get("/api/chat")
def api_chat_history():
    try:
        with core.journal._conn() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT ts, role, content FROM chat_log ORDER BY id DESC LIMIT 50")]
    except Exception as exc:
        if core._is_busy_error(exc):
            raise HTTPException(503, "journal is busy — retry shortly")
        raise
    return list(reversed(rows))


# minimal per-IP token-bucket for /api/chat (synchronous answer kept; the
# bucket only throttles abuse — full async/background chat is out of scope).
_CHAT_BUCKET: dict[str, list[float]] = {}
_CHAT_BUCKET_LOCK = threading.Lock()
_CHAT_RATE = 10  # msgs
_CHAT_WINDOW = 60.0  # per 60s per IP


def _chat_rate_ok(ip: str) -> bool:
    now = time.monotonic()
    with _CHAT_BUCKET_LOCK:
        hits = [t for t in _CHAT_BUCKET.get(ip, []) if now - t < _CHAT_WINDOW]
        if len(hits) >= _CHAT_RATE:
            _CHAT_BUCKET[ip] = hits
            return False
        hits.append(now)
        _CHAT_BUCKET[ip] = hits
        return True


@router.post("/api/chat")
def api_chat(msg: ChatIn, request: object = None):
    # per-IP throttle (request optional so in-process calls keep working)
    ip = "local"
    try:
        client = getattr(request, "client", None)
        if client is not None:
            ip = getattr(client, "host", "local") or "local"
    except Exception:
        ip = "local"
    if not _chat_rate_ok(ip):
        raise HTTPException(429, "chat rate limit — wait a minute and retry")
    try:
        reply = core.chatbot.answer(msg.message)
    except Exception as exc:
        if core._is_busy_error(exc):
            raise HTTPException(503, "journal is busy — retry shortly")
        raise
    return {"reply": reply}
