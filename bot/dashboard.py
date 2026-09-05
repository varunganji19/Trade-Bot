"""
FastAPI dashboard + JSON API — a tabbed single-page app (inline HTML/CSS/JS,
Chart.js via CDN; no build step).

Tabs (hash routing, ~4s polling):
  #overview   — equity curve, headline stats, engine controls, decision feed,
                per-strategy PnL bars
  #portfolio  — open positions (live marks, manual close) + trade history
  #watchlist  — full CRUD of what the bot trades (persisted data/watchlist.json;
                hot-reloads into a RUNNING engine's CONFIG)
  #account    — paper balance: deposits/withdrawals + type-to-confirm reset
  #chat       — the journal-aware chatbot

API: GET /  /api/stats /api/equity /api/trades /api/decisions /api/watchlist
     /api/account /api/account/transactions /api/chat /api/engine/status
     POST /api/chat {message}  /api/engine/start {interval}  /api/engine/stop
          /api/watchlist {kind,symbol,timeframe,display?}
          /api/account/deposit {amount}  /api/account/withdraw {amount}
          /api/account/reset {capital}
     DELETE /api/watchlist/{kind}/{symbol}/{timeframe}
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import traceback
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response as FastAPIResponse, FileResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from bot.chatbot import ChatBot
from bot.engine import TradingEngine
from bot.journal import Journal
from bot.strategies import STRATEGY_CLASSES
from config import (CONFIG, MarketSpec, VALID_KINDS,
                    VALID_TIMEFRAMES, MAX_WATCHLIST_SPECS,
                    apply_saved_watchlist, save_watchlist, infer_kind)

app = FastAPI(title="AI Trading Bot Dashboard", version="2.1")
# blocks DNS-rebinding pages from reaching the API (a rebind page becomes
# same-origin with 127.0.0.1 and gets full read/write otherwise) — the bot
# stays localhost-only
app.add_middleware(TrustedHostMiddleware,
                  allowed_hosts=["127.0.0.1", "localhost", "testserver"])


def _check_token(auth_header: str, token: str) -> bool:
    """Pure predicate for the optional bearer guard (unit-tested)."""
    if not token:
        return True
    import hmac
    return hmac.compare_digest(auth_header, f"Bearer {token}")


class _TokenGuard:   # pure ASGI middleware — no BaseHTTPMiddleware overhead
    """Optional shared-token auth, OFF by default. Set DASHBOARD_TOKEN to
    require `Authorization: Bearer <token>` on every request (page + API) —
    the belt-and-suspenders layer if the dashboard is ever deliberately
    exposed beyond loopback."""
    def __init__(self, asgi_app, token: str):
        self.app = asgi_app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not _check_token(
                next((v.decode() for k, v in scope.get("headers", [])
                      if k == b"authorization"), ""), self.token):
            resp = JSONResponse({"detail": "unauthorized"}, status_code=401)
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


_DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")
if _DASHBOARD_TOKEN:
    app.add_middleware(_TokenGuard, token=_DASHBOARD_TOKEN)

journal = Journal()
chatbot = ChatBot(journal)

_engine_lock = threading.Lock()
_wl_lock = threading.RLock()   # reentrant: endpoints hold it while calling _persist_and_install
_engine: TradingEngine | None = None
_engine_thread: threading.Thread | None = None
_last_engine_error: str | None = None   # survives engine teardown for /api/engine/status
_engine_interval: int = 60              # the running engine's cycle interval (stop persists it)

FOREX_RE = re.compile(r"^[A-Z]{6}=X$")
CRYPTO_RE = re.compile(r"^[A-Z]{2,10}/[A-Z]{2,10}$")

# legacy pre-timeframe journal rows carry no timeframe column value — backfill
# them to 1h (the engine's restore path has its own smarter spec-first rule;
# this is only the dashboard's read-side display default)
_LEGACY_TF = "1h"

# which strategy owns a timeframe — derived instead of hand-maintained: the
# badge must agree with what the orchestrator actually runs
# (preferred_timeframes is the enforcement point; a hand-kept literal lied
# for 5m/1d — vwap_scalper/connors_meanrev never traded those back then, the
# orchestrator just HOLDed, yet the UI badge claimed an owner)
STRATEGY_BY_TF = {tf: name for name, cls in STRATEGY_CLASSES.items()
                  for tf in cls.preferred_timeframes}

apply_saved_watchlist()  # data/watchlist.json → CONFIG.watchlist (creates file on first boot)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class EngineIn(BaseModel):
    # ge=5: interval=0 was a hot loop hammering the exchanges; negative killed
    # the loop thread silently (sleep() raised outside the try)
    interval: int = Field(default=60, ge=5, le=3600)


class WatchlistIn(BaseModel):
    kind: str
    symbol: str = Field(min_length=1, max_length=24)
    timeframe: str
    display: str | None = Field(default=None, max_length=64)


class AmountIn(BaseModel):
    # the le bound also rejects inf (gt=0 alone passed it) and values that
    # would blow up derived stats (return_pct -> inf)
    amount: float = Field(gt=0, le=1_000_000_000)


class ResetIn(BaseModel):
    capital: float = Field(gt=0, le=1_000_000_000)


class PositionCloseIn(BaseModel):
    symbol: str
    timeframe: str


class EmptyIn(BaseModel):
    """Body-required marker for POSTs that take no fields: a JSON body forces
    the CORS preflight that defeats form-encoded CSRF (same rule every other
    mutating endpoint already follows)."""


# --------------------------------------------------------------- engine state
def _engine_state_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(CONFIG.db_path)),
                        "engine_state.json")


def _write_engine_state(running: bool, interval: int):
    """Persist the operator's desired engine state so a dashboard restart can
    auto-resume it (a stop must win over a stale 'running' file). A failed
    write degrades to no-auto-resume; it must never break the endpoint."""
    try:
        # atomic: a torn state file would silently disable auto-resume
        tmp = _engine_state_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"desired": "running" if running else "stopped",
                       "interval": interval}, f)
        os.replace(tmp, _engine_state_path())
    except OSError:
        pass


def _spawn_engine(interval: int) -> dict:
    """Build + start the engine thread (shared by the API endpoint and the
    startup auto-resume). Returns the API response dict."""
    global _engine, _engine_thread, _engine_interval
    # build OUTSIDE _engine_lock: TradingEngine.__init__ loads the Kronos
    # model (seconds) and holding the lock froze every stats/status poll
    eng = TradingEngine(mode="paper", quiet=False, journal=journal)
    with _engine_lock:
        if _engine is not None:
            return {"status": "already_running", "cycles": _engine.cycles}
        # a previous thread may still be finishing its last cycle (stop only
        # clears the global); two engines writing one journal fork the account
        if _engine_thread is not None and _engine_thread.is_alive():
            return {"status": "stopping", "cycles": 0}
        _engine = eng
        _engine_interval = interval

    def _loop(eng_ref, interval):
        global _engine, _last_engine_error
        import time as _t
        while _get_engine() is eng_ref:
            try:
                eng_ref.run_cycle()
            except Exception as exc:
                # run_cycle already guards its own body, so reaching here means
                # the engine itself is broken: report it, clear the global so
                # the UI shows a stopped engine (never a green zombie), and stop
                eng_ref.last_error = f"{type(exc).__name__}: {exc}"
                _last_engine_error = eng_ref.last_error
                traceback.print_exc()
                eng_ref.cycles += 1        # count the failed cycle so the UI moves
                with _engine_lock:
                    if _engine is eng_ref:
                        _engine = None
                break
            _t.sleep(interval)
            if _get_engine() is not eng_ref:
                break

    _engine_thread = threading.Thread(target=_loop, args=(eng, interval), daemon=True)
    _engine_thread.start()
    return {"status": "started", "interval": interval}


def _get_engine() -> TradingEngine | None:
    global _engine
    with _engine_lock:
        return _engine


# ---------------------------------------------------------------------------
# helpers — shared by several endpoints
def _validate_spec(kind: str, symbol: str, timeframe: str) -> tuple[str, str, str]:
    """Normalize + validate a watchlist spec. Raises HTTPException(422) on bad input."""
    if kind not in VALID_KINDS:
        raise HTTPException(422, f"kind must be one of {list(VALID_KINDS)}")
    if timeframe not in VALID_TIMEFRAMES:
        raise HTTPException(422, f"timeframe must be one of {list(VALID_TIMEFRAMES)}")
    symbol = symbol.strip().upper()
    if kind == "crypto":
        if not CRYPTO_RE.match(symbol):
            raise HTTPException(422, "crypto symbol must be BASE/QUOTE, e.g. BTC/USDT")
    elif not FOREX_RE.match(symbol):
        raise HTTPException(422, "forex symbol must be XXXXXX=X, e.g. EURUSD=X")
    return kind, symbol, timeframe


def _persist_and_install(specs: list[MarketSpec]) -> None:
    """Save to data/watchlist.json and hot-install into CONFIG (in-place, so a
    running engine that shares the CONFIG singleton picks it up next cycle)."""
    if not save_watchlist(specs):
        raise HTTPException(500, "could not persist watchlist.json (disk error?) — "
                               "the change will not survive a restart")
    with _wl_lock:
        CONFIG.watchlist[:] = specs


def _mark_map(eng: TradingEngine, positions=None) -> dict[str, float]:
    """Best-effort live marks for the engine's open symbols, from the engine's
    own TTL-cached frames (cache hits are free; only stale symbols re-fetch).

    `positions` is a positions_snapshot() from the caller — never iterate the
    live dict (races the engine's mutations)."""
    from bot.data import MarketData
    marks: dict[str, float] = {}
    md: MarketData = eng.market_data
    if positions is None:
        positions = eng.broker.positions_snapshot()
    held = {p.symbol for p in positions}
    for spec in eng.cfg.watchlist:
        if spec.symbol in marks or spec.symbol not in held:
            continue
        try:
            df = md.latest(spec, limit=2)
            if df is not None and len(df):
                marks[spec.symbol] = float(df["close"].iloc[-1])
        except Exception:
            continue
    return marks


def _live_state(eng) -> tuple[list, dict, dict]:
    """(positions snapshot, marks, price_map) for a running engine — caller
    owns locking (api_stats/api_account read unlocked; _adjust_account must
    NOT use this, it snapshots inside eng.cycle_lock)."""
    positions = eng.broker.positions_snapshot()
    marks = _mark_map(eng, positions)
    price_map = {p.symbol: marks[p.symbol] for p in positions if p.symbol in marks}
    return positions, marks, price_map


def _position_dict(p, marks: dict[str, float] | None = None) -> dict:
    d = {"symbol": p.symbol, "timeframe": p.timeframe, "side": p.side, "qty": p.qty,
         "entry": p.entry_price, "stop": p.stop, "target": p.target,
         "strategy": p.strategy, "bars_held": p.bars_held, "trade_id": p.trade_id,
         "kind": infer_kind(p.symbol), "live": True}
    marks = marks or {}
    if p.symbol in marks:
        d["mark"] = marks[p.symbol]
        d["unrealized"] = eng_unrealized(p, marks[p.symbol])
    return d


def eng_unrealized(p, price: float) -> float:
    """Net of the deferred entry fee + estimated exit fee, matching the broker's
    realized PnL convention (PaperBroker.unrealized is gross)."""
    from bot.broker import PaperBroker
    gross = PaperBroker.unrealized(p, price)
    fee = (p.entry_fee or 0.0) + (price * p.qty) * CONFIG.costs.fee(infer_kind(p.symbol))
    return round(gross - fee, 2)


def _journal_position_dict(t: dict) -> dict:
    return {"symbol": t["symbol"], "timeframe": t.get("timeframe") or _LEGACY_TF,
            "side": t["side"], "qty": t["qty"], "entry": t["entry_price"],
            "stop": t["stop_price"], "target": t["target_price"],
            "strategy": t["strategy"], "bars_held": 0, "trade_id": t["id"],
            "opened_ts": t["opened_ts"],
            "kind": infer_kind(t["symbol"]), "live": False}


# ---------------------------------------------------------------------------
# pages
@app.get("/", response_class=HTMLResponse)
def index():
    return DASHBOARD_HTML


@app.get("/chart.umd.min.js",
         include_in_schema=False,
         response_class=FastAPIResponse)
def chart_js():
    """Vendored Chart.js 4.4.3 (MIT, see bot/chart.LICENSE) — served locally
    instead of from a CDN: no third-party same-origin script execution, and
    the dashboard works fully offline."""
    return FileResponse(os.path.join(os.path.dirname(__file__), "chart.umd.min.js"),
                        media_type="application/javascript")


# ---------------------------------------------------------------------------
# stats / history
@app.get("/api/stats")
def api_stats():
    stats = journal.stats()
    eng = _get_engine()
    stats["engine_running"] = eng is not None
    stats["cycles"] = eng.cycles if eng is not None else 0
    stats["watchlist_count"] = len(CONFIG.watchlist)
    if eng is not None:
        stats["llm_mode"] = eng.llm.provider if eng.llm.enabled else "quant"
        # live-state trio via the shared helper (marks come from the engine's
        # TTL-cached frames; iterating the live positions dict would race the
        # engine's open/close mutations -> "dict changed size" 500s)
        positions, marks, price_map = _live_state(eng)
        stats["open_positions"] = [_position_dict(p, marks) for p in positions]
        stats["broker_equity"] = round(eng.broker.equity(price_map), 2)
        stats["engine_error"] = eng.last_error
    else:
        stats["llm_mode"] = "quant"
        stats["open_positions"] = [_journal_position_dict(t) for t in journal.open_trades()]
    return stats


@app.get("/api/equity")
def api_equity():
    return journal.equity_curve(limit=3000)


@app.get("/api/trades")
def api_trades(limit: int = Query(default=100, ge=1, le=1000)):
    return journal.recent_trades(limit=limit)


@app.get("/api/decisions")
def api_decisions(limit: int = Query(default=40, ge=1, le=500)):
    return journal.recent_decisions(limit=limit)


# ---------------------------------------------------------------------------
# watchlist CRUD — the markets the bot trades
@app.get("/api/watchlist")
def api_watchlist_get():
    with _wl_lock:
        return [{"kind": s.kind, "symbol": s.symbol, "timeframe": s.timeframe,
                 "display": s.display, "strategy": STRATEGY_BY_TF.get(s.timeframe, "ensemble")}
                for s in CONFIG.watchlist]


@app.post("/api/watchlist", status_code=201)
def api_watchlist_add(body: WatchlistIn):
    kind, symbol, timeframe = _validate_spec(body.kind, body.symbol, body.timeframe)
    display = (body.display or "").strip()
    with _wl_lock:
        current = list(CONFIG.watchlist)
        if any(s.symbol == symbol and s.timeframe == timeframe for s in current):
            raise HTTPException(409, f"{symbol} {timeframe} is already on the watchlist")
        if len(current) >= MAX_WATCHLIST_SPECS:
            raise HTTPException(422, f"watchlist is full ({MAX_WATCHLIST_SPECS} specs max)")
        spec = MarketSpec(kind, symbol, timeframe, display)
        current.append(spec)
        _persist_and_install(current)
    return {"status": "added", "spec": spec.to_dict(), "count": len(current)}


@app.delete("/api/watchlist/{kind}/{symbol:path}/{timeframe}")
def api_watchlist_delete(kind: str, symbol: str, timeframe: str):
    # `:path` lets the crypto symbol's own slash live in the URL (BTC/USDT);
    # %2F-encoded symbols unquote to the same thing
    kind, symbol, timeframe = _validate_spec(kind, unquote(symbol), timeframe)
    with _wl_lock:
        current = list(CONFIG.watchlist)
        target = next((s for s in current
                       if s.symbol == symbol and s.timeframe == timeframe), None)
        if target is None:
            raise HTTPException(404, f"{symbol} {timeframe} is not on the watchlist")
        eng = _get_engine()
        if eng is not None and any(k == (symbol, timeframe) for k in eng.broker.positions):
            raise HTTPException(
                409, f"cannot remove {symbol} {timeframe}: an open position is held on it — "
                     f"close the position first (Portfolio tab)")
        # also guard the engine-off case: an OPEN journal trade on the spec
        # would restore on next start into a watchlist that no longer manages
        # it (stops never checked, marks never fetched — a zombie position)
        if any(t.get("symbol") == symbol and (t.get("timeframe") or _LEGACY_TF) == timeframe
               for t in journal.open_trades()):
            raise HTTPException(
                409, f"cannot remove {symbol} {timeframe}: an open journaled trade exists "
                     f"on it — close the position first (Portfolio tab)")
        current = [s for s in current if not (s.symbol == symbol and s.timeframe == timeframe)]
        _persist_and_install(current)
    return {"status": "removed", "symbol": symbol, "timeframe": timeframe, "count": len(current)}


# ---------------------------------------------------------------------------
# open positions + manual close
@app.post("/api/positions/close")
def api_position_close(body: PositionCloseIn):
    eng = _get_engine()
    if eng is None:
        raise HTTPException(409, "engine is not running — start it to close positions at live prices")
    symbol, timeframe = body.symbol.strip().upper(), body.timeframe
    key = (symbol, timeframe)
    if key not in eng.broker.positions:
        raise HTTPException(404, f"no open position on {symbol} {timeframe}")
    try:
        result = eng.close_manual(symbol, timeframe)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    return {"status": "closed", **result}


# ---------------------------------------------------------------------------
# account — paper balance management
@app.get("/api/account")
def api_account():
    last = journal.last_equity_point(mode="paper")
    eng = _get_engine()
    if eng is not None:
        _, _, price_map = _live_state(eng)   # only price_map is needed here
        cash = eng.broker.cash
        equity = eng.broker.equity(price_map)
    else:
        cash = last["cash"] if last else CONFIG.paper_capital
        equity = last["equity"] if last else CONFIG.paper_capital
    return {"capital": CONFIG.paper_capital, "cash": round(cash, 2), "equity": round(equity, 2),
            "mode": "paper", "engine_running": eng is not None,
            "last_equity": last,
            "unrealized": round(equity - cash, 2)}


def _reject_overdraft(direction: str, new_cash: float, base_cash: float,
                      amount: float) -> None:
    """Withdraw past the balance guard — identical 400 for the engine-on and
    engine-off paths (both branches compute the same inequality, so it's ONE
    rule, not two copies that could drift apart)."""
    if direction == "withdraw" and new_cash < -1e-9:
        raise HTTPException(400, f"insufficient cash: ${base_cash:,.2f} available, "
                                 f"-${amount:,.2f} requested")


def _adjust_account(amount: float, direction: str) -> dict:
    """Shared deposit/withdraw path: journal row + live broker patch.

    The broker patch runs under the ENGINE's cycle lock — patching cash while
    a cycle is mid-close used to overwrite the close proceeds with a stale
    read (lost update: journal and broker cash diverged permanently)."""
    delta = amount if direction == "deposit" else -amount
    kind = "deposit" if direction == "deposit" else "withdrawal"
    note = f"manual {kind}"
    eng = _get_engine()
    if eng is not None:
        # the whole read-compute-journal-patch sequence is serialized against
        # engine cycles; verify identity too — a fast stop/start must not
        # patch a NEW engine with numbers read from the old one
        with eng.cycle_lock:
            base_cash = eng.broker.cash
            new_cash = base_cash + delta
            _reject_overdraft(direction, new_cash, base_cash, amount)
            marks = _mark_map(eng)
            base_equity = eng.broker.equity(marks)
            new_equity = base_equity + delta
            journal.add_equity(round(new_equity, 2), round(new_cash, 2),
                                mode="paper", note=note)
            eng.broker.cash = new_cash
            # re-baseline the kill switch so a deposit doesn't read as a loss
            eng.risk.daily_start_equity = new_equity
    else:
        last = journal.last_equity_point(mode="paper")
        base_cash = last["cash"] if last else CONFIG.paper_capital
        base_equity = last["equity"] if last else CONFIG.paper_capital
        new_cash = base_cash + delta
        _reject_overdraft(direction, new_cash, base_cash, amount)
        new_equity = base_equity + delta
        journal.add_equity(round(new_equity, 2), round(new_cash, 2),
                           mode="paper", note=note)
    # typed ledger row for the Account tab's history (one call covers both
    # branches: same delta, same post-adjust cash/equity either way). kind is
    # the journal's own deposit/withdrawal vocab — pinned by
    # test_dashboard_api_smoke, don't "simplify" it
    journal.add_transaction(kind, amount, cash_after=round(new_cash, 2),
                            equity_after=round(new_equity, 2),
                            mode="paper", note=note)
    return {"status": kind, "amount": round(amount, 2),
            "cash": round(new_cash, 2), "equity": round(new_equity, 2)}


@app.post("/api/account/deposit")
def api_account_deposit(body: AmountIn):
    return _adjust_account(body.amount, "deposit")


@app.post("/api/account/withdraw")
def api_account_withdraw(body: AmountIn):
    return _adjust_account(body.amount, "withdraw")


@app.post("/api/account/reset")
def api_account_reset(body: ResetIn):
    # a reset while the engine trades would fork broker state from the journal
    api_engine_stop(EmptyIn())
    backup = f"{CONFIG.db_path.rsplit('.db', 1)[0]}.backup.{int(time.time())}.db"
    try:
        shutil.copy2(CONFIG.db_path, backup)
    except OSError as exc:
        # the wipe must NEVER proceed without the verified backup it promises
        raise HTTPException(500, f"reset aborted — backup failed: {exc}")
    if not os.path.exists(backup) or os.path.getsize(backup) == 0:
        os.path.exists(backup) and os.remove(backup)
        raise HTTPException(500, "reset aborted — backup file is empty")
    with journal._conn() as conn:
        for table in ("trades", "equity", "chat_log", "decisions", "transactions"):
            conn.execute(f"DELETE FROM {table}")
    journal.add_equity(round(body.capital, 2), round(body.capital, 2),
                       mode="paper", note="account reset")
    # the ledger restarts with the account: exactly one row, the fresh capital
    journal.add_transaction("reset", round(body.capital, 2),
                            cash_after=round(body.capital, 2),
                            equity_after=round(body.capital, 2),
                            mode="paper", note="account reset")
    return {"status": "reset", "capital": round(body.capital, 2), "backup": backup}


@app.get("/api/account/transactions")
def api_account_transactions(limit: int = Query(default=100, ge=1, le=1000)):
    # paper-mode ledger only (matches api_equity/api_trades conventions)
    return journal.recent_transactions(limit=limit, mode="paper")


# ---------------------------------------------------------------------------
# chatbot
@app.get("/api/chat")
def api_chat_history():
    with journal._conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT ts, role, content FROM chat_log ORDER BY id DESC LIMIT 50")]
    return list(reversed(rows))


@app.post("/api/chat")
def api_chat(msg: ChatIn):
    reply = chatbot.answer(msg.message)
    return {"reply": reply}


# ---------------------------------------------------------------------------
# engine control
@app.post("/api/engine/start")
def api_engine_start(body: EngineIn):
    # cheap guard BEFORE any construction: a repeat POST while running used to
    # pay a full TradingEngine build (torch/Kronos load, position restore) and
    # throw it away — an impatient double-click was a local CPU/memory spike
    existing = _get_engine()
    if existing is not None:
        return {"status": "already_running", "cycles": existing.cycles}
    result = _spawn_engine(body.interval)
    if result["status"] == "started":
        _write_engine_state(True, body.interval)
    return result


@app.post("/api/engine/stop")
def api_engine_stop(body: EmptyIn):
    """Quiesce: clear the global, then WAIT (bounded) for the in-flight cycle.
    Returning while a cycle still runs let a quick restart run two engines
    against one journal, and let a reset race stray writes into the wiped DB."""
    global _engine, _engine_thread, _engine_interval
    with _engine_lock:
        if _engine is None:
            _write_engine_state(False, CONFIG.live_interval_seconds)
            return {"status": "not_running"}
        _engine = None
    th = _engine_thread
    if th is not None and th is not threading.current_thread():
        th.join(timeout=300)
        if th.is_alive():
            return {"status": "stopping"}
    _write_engine_state(False, _engine_interval)
    return {"status": "stopped"}


@app.on_event("startup")
def _auto_resume_engine():
    """Restart the engine when the last session left it running (the operator's
    'the bot trades autonomously' expectation survives a dashboard restart).
    A manual stop persists desired=stopped, so it always wins. Skipped under
    pytest: tests swap CONFIG.db_path to temp dirs, but the real state file
    may exist with desired=running and must never spawn a live engine there."""
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    try:
        with open(_engine_state_path()) as f:
            state = json.load(f)
        if not isinstance(state, dict):
            return
        if state.get("desired") != "running":
            return
        interval = int(state.get("interval", CONFIG.live_interval_seconds))
    except (OSError, ValueError, TypeError, OverflowError):
        return
    interval = max(5, min(3600, interval))
    result = _spawn_engine(interval)
    if result["status"] == "started":
        print(f"[dashboard] engine auto-resumed (interval {interval}s)")


@app.get("/api/engine/status")
def api_engine_status():
    eng = _get_engine()
    th = _engine_thread
    if eng is None:
        return {"running": False, "cycles": 0, "llm": "quant", "positions": 0,
                "interval": CONFIG.live_interval_seconds,
                "alive": bool(th is not None and th.is_alive()),
                "last_error": _last_engine_error, "health_note": None}
    return {"running": True, "cycles": eng.cycles,
            "llm": eng.llm.provider if eng.llm.enabled else "quant",
            "positions": len(eng.broker.positions_snapshot()),
            "alive": bool(th is not None and th.is_alive()),
            "last_error": eng.last_error or _last_engine_error,
            # degraded-but-alive conditions (e.g. a held position behind a dead
            # feed) ride here — last_error is reserved for fatal engine errors
            "health_note": getattr(eng, "health_note", None)}


# ===========================================================================
# UI — module-level HTML constant (single-page app, no build tooling)
# ===========================================================================
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Algo Trading Bot — Dashboard</title>
<script src="/chart.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap');

:root {
  --color-secondary:#1E293B; --color-on-secondary:#FFFFFF;
  --color-accent:#22C55E; --color-on-accent:#0F172A;
  --color-background:#020617; --color-foreground:#F8FAFC;
  --color-card:#0E1223; --color-card-foreground:#F8FAFC;
  --color-muted:#1A1E2F; --color-muted-foreground:#94A3B8;
  --color-border:#334155; --color-destructive:#EF4444; --color-on-destructive:#000000;
  --color-ring:#FFFFFF;
  --color-pos:#22C55E; --color-neg:#EF4444; --color-blue:#38BDF8;
  --space-sm:0.25rem; --space-md:0.5rem; --space-lg:0.75rem;
  --space-xl:1rem; --space-2xl:1.5rem; --space-3xl:2rem;
  --shadow-lg:0 10px 15px rgba(0,0,0,0.1); --shadow-xl:0 20px 25px rgba(0,0,0,0.15);
  --font-ui:'Fira Sans',-apple-system,sans-serif;
  --font-mono:'Fira Code','SF Mono',monospace;
  --radius:8px; --radius-lg:12px;
  --trans:200ms ease;
}
* { box-sizing:border-box; margin:0; padding:0; }
html { scroll-behavior:smooth; }
body { background:var(--color-background); color:var(--color-foreground);
       font:14px/1.5 var(--font-ui); -webkit-font-smoothing:antialiased; }

/* ---------------------------------------------------------------- header */
.topbar { display:flex; justify-content:space-between; align-items:center; gap:var(--space-lg);
          padding:var(--space-lg) var(--space-xl); border-bottom:1px solid var(--color-border);
          background:var(--color-card); position:sticky; top:0; z-index:40;
          padding-left:max(1rem, env(safe-area-inset-left)); }
.brand { display:flex; align-items:center; gap:var(--space-md); min-width:0; }
.brand svg { color:var(--color-accent); flex-shrink:0; }
.brand h1 { font:600 15px/1.2 var(--font-mono); letter-spacing:.3px; white-space:nowrap; }
.brand .sub { color:var(--color-muted-foreground); font-size:11px; white-space:nowrap;
              overflow:hidden; text-overflow:ellipsis; }
.engine-pill { display:inline-flex; align-items:center; gap:6px; font-size:12px;
               color:var(--color-muted-foreground); padding:4px 10px;
               border:1px solid var(--color-border); border-radius:999px;
               white-space:nowrap; background:var(--color-muted); }
.dot { width:8px; height:8px; border-radius:50%; background:var(--color-muted-foreground);
       transition:background var(--trans); flex-shrink:0; }
.dot.on { background:var(--color-accent); box-shadow:0 0 10px rgba(34,197,94,.5); }
.dot.off { background:var(--color-neg); }

/* ---------------------------------------------------------------- tabs */
.tabs { display:flex; gap:2px; padding:0 var(--space-xl); border-bottom:1px solid var(--color-border);
        background:var(--color-card); overflow-x:auto; scrollbar-width:none;
        position:sticky; top:57px; z-index:39; }
.tabs::-webkit-scrollbar { display:none; }
.tab { appearance:none; background:transparent; border:none; border-bottom:2px solid transparent;
       color:var(--color-muted-foreground); font:500 13px/1 var(--font-ui);
       padding:12px 14px; cursor:pointer; transition:color var(--trans),border-color var(--trans);
       display:inline-flex; align-items:center; gap:7px; white-space:nowrap; min-height:44px; }
.tab svg { width:15px; height:15px; }
.tab:hover { color:var(--color-foreground); }
.tab.active { color:var(--color-accent); border-bottom-color:var(--color-accent); }
.tab:focus-visible, button:focus-visible, a:focus-visible, input:focus-visible,
select:focus-visible, .icon-btn:focus-visible { outline:2px solid var(--color-ring);
  outline-offset:2px; border-radius:4px; }
/* no fixed bar sits under <main> anymore (bottom nav removed) — ordinary
   bottom padding instead of the 96px strip reserved for it */
main { max-width:1400px; margin:0 auto; padding:var(--space-xl); padding-bottom:var(--space-3xl); }
.view { display:none; animation:fadeIn .2s ease; }
.view.active { display:block; }
@keyframes fadeIn { from { opacity:0; transform:translateY(4px); } to { opacity:1; transform:none; } }

/* ---------------------------------------------------------------- primitives */
.card { background:var(--color-card); border:1px solid var(--color-border);
        border-radius:var(--radius-lg); padding:var(--space-lg); }
.card + .card { margin-top:var(--space-lg); }
.card-head { display:flex; justify-content:space-between; align-items:center; gap:var(--space-md);
             margin-bottom:var(--space-lg); flex-wrap:wrap; }
.card-head h2 { display:flex; align-items:center; gap:8px; font:600 12px/1 var(--font-ui);
                text-transform:uppercase; letter-spacing:.8px; color:var(--color-muted-foreground); }
.card-head h2 svg { width:15px; height:15px; color:var(--color-accent); }
.card-head .hint { font-size:11px; color:var(--color-muted-foreground); }
.grid { display:grid; gap:var(--space-md); }

.btn { display:inline-flex; align-items:center; justify-content:center; gap:7px;
       min-height:44px; padding:0 16px; border-radius:var(--radius); border:1px solid transparent;
       font:600 13px/1 var(--font-ui); cursor:pointer; transition:all var(--trans);
       background:var(--color-accent); color:var(--color-on-accent); }
.btn:hover { opacity:.9; filter:brightness(1.08); }
.btn:disabled { opacity:.45; cursor:not-allowed; }
.btn svg { width:15px; height:15px; }
.btn-danger { background:var(--color-destructive); color:var(--color-on-destructive); }
.btn-danger:hover { background:#DC2626; }
.btn-secondary { background:transparent; color:var(--color-foreground);
                 border:1px solid var(--color-border); }
.btn-secondary:hover { border-color:var(--color-muted-foreground); background:var(--color-muted); }
.btn-ghost { background:transparent; color:var(--color-muted-foreground);
             border:1px solid var(--color-border); }
.btn-ghost:hover { color:var(--color-foreground); border-color:var(--color-muted-foreground); }

.input, select { min-height:44px; padding:8px 12px; background:var(--color-muted);
                 border:1px solid var(--color-border); border-radius:var(--radius);
                 color:var(--color-foreground); font:13px/1.4 var(--font-ui); width:100%;
                 transition:border-color var(--trans); cursor:pointer; }
.input { font-family:var(--font-mono); cursor:text; }
.input:hover, select:hover { border-color:var(--color-muted-foreground); }
.input:focus, select:focus { border-color:var(--color-accent); outline:none;
                             box-shadow:0 0 0 3px rgba(34,197,94,.15); }
label.fld { display:block; font-size:11px; text-transform:uppercase; letter-spacing:.5px;
            color:var(--color-muted-foreground); margin-bottom:4px; }

/* ---------------------------------------------------------------- tables */
.tbl-wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:12.5px; }
th, td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--color-border);
         white-space:nowrap; }
th { color:var(--color-muted-foreground); font:500 10.5px/1.2 var(--font-ui);
     text-transform:uppercase; letter-spacing:.7px; }
tbody tr { transition:background 150ms ease; }
tbody tr:hover { background:rgba(51,65,85,.18); }
tbody tr:last-child td { border-bottom:none; }
td.num, th.num { font-family:var(--font-mono); font-variant-numeric:tabular-nums;
                 text-align:right; }
.pos { color:var(--color-pos); } .neg { color:var(--color-neg); }
.mono { font-family:var(--font-mono); font-variant-numeric:tabular-nums; }
.empty { padding:var(--space-2xl) var(--space-md); text-align:center;
         color:var(--color-muted-foreground); font-size:13px; }

.tag { display:inline-block; padding:2px 8px; border-radius:4px; font:500 10.5px/1.4
       var(--font-mono); letter-spacing:.5px; }
.tag.long { background:rgba(34,197,94,.12); color:var(--color-pos); }
.tag.short { background:rgba(239,68,68,.12); color:var(--color-neg); }
.tag.hold { background:rgba(148,163,184,.12); color:var(--color-muted-foreground); }
.tag.close { background:rgba(56,189,248,.12); color:var(--color-blue); }
.tag.open { background:rgba(34,197,94,.10); color:var(--color-pos); }
.tag.tf { background:var(--color-muted); color:var(--color-muted-foreground); }
.icon-btn { background:transparent; border:1px solid var(--color-border); color:var(--color-neg);
            border-radius:6px; width:34px; height:34px; display:inline-flex; align-items:center;
            justify-content:center; cursor:pointer; transition:all var(--trans); }
.icon-btn:hover { border-color:var(--color-neg); background:rgba(239,68,68,.1); }
.icon-btn svg { width:14px; height:14px; }

/* loading skeleton */
.skeleton { position:relative; overflow:hidden; background:var(--color-muted);
            border-radius:4px; height:14px; }
.skeleton::after { content:''; position:absolute; inset:0;
                   background:linear-gradient(90deg,transparent,rgba(148,163,184,.12),transparent);
                   animation:shimmer 1.4s infinite; }
@keyframes shimmer { from { transform:translateX(-100%); } to { transform:translateX(100%); } }
.sk-row { display:flex; gap:var(--space-md); padding:9px 10px; align-items:center; }

/* ---------------------------------------------------------------- stat cards */
.stats-grid { display:grid; gap:var(--space-md); margin-bottom:var(--space-lg);
              grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); }
.stat { background:var(--color-card); border:1px solid var(--color-border);
        border-radius:var(--radius-lg); padding:var(--space-md) var(--space-lg); }
.stat .label { color:var(--color-muted-foreground); font:500 10px/1.3 var(--font-ui);
               text-transform:uppercase; letter-spacing:.8px; display:flex;
               align-items:center; gap:6px; }
.stat .label svg { width:13px; height:13px; }
.stat .value { font:600 19px/1.3 var(--font-mono); font-variant-numeric:tabular-nums;
               margin-top:3px; word-break:break-all; }
.stat .sub { font-size:10.5px; color:var(--color-muted-foreground); margin-top:1px; }

/* ---------------------------------------------------------------- equity panel */
/* flex column: .card-head takes its natural height, .chart-body absorbs the
   rest — with maintainAspectRatio:false Chart.js sizes the canvas to the
   wrapper, so it can no longer spill ~22px past the fixed card height onto
   the "Strategy P&L" card below */
.chart-card { display:flex; flex-direction:column; height:280px; }
.chart-body { flex:1 1 auto; min-height:0; position:relative; }
#equityChart { width:100%; height:100%; display:block; }

/* engine controls */
.engine-card .row { display:flex; gap:var(--space-md); flex-wrap:wrap; align-items:end; }
.engine-card .row > div { min-width:120px; }
.engine-card select { max-width:180px; }
.engine-state { display:flex; flex-direction:column; gap:2px; margin-left:auto; text-align:right; }
.engine-state .st { font:600 13px/1.2 var(--font-mono); }
.engine-state .sub { font-size:11px; color:var(--color-muted-foreground); }

/* ---------------------------------------------------------------- decisions feed */
.term { background:#05080F; border:1px solid var(--color-border); border-radius:var(--radius);
        font:12px/1.7 var(--font-mono); padding:var(--space-lg);
        max-height:520px; overflow-y:auto; }
.term-row { display:flex; gap:10px; padding:5px 0; border-bottom:1px dashed rgba(51,65,85,.5);
            align-items:baseline; }
.term-row:last-child { border-bottom:none; }
.term-ts { color:var(--color-muted-foreground); font-size:11px; flex-shrink:0; padding-top:2px; }
.term-body { min-width:0; }
.term-line { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.term-mkt { color:var(--color-foreground); font-weight:600; }
.term-meta { color:var(--color-muted-foreground); font-size:11px; }
.term-why { color:#A8B3C5; font-size:11.5px; margin-top:2px; word-break:break-word; }

/* strategy bars */
.strat-bars { display:flex; flex-direction:column; gap:10px; }
.sbar { display:grid; grid-template-columns:150px 1fr 90px; gap:10px; align-items:center;
        font-size:12px; }
.sbar .name { font-family:var(--font-mono); color:var(--color-muted-foreground);
              overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.sbar .track { height:18px; background:var(--color-muted); border-radius:4px;
               overflow:hidden; position:relative; }
.sbar .fill { position:absolute; top:0; bottom:0; transition:width .4s ease; border-radius:4px; }
.sbar .fill.pos { background:linear-gradient(90deg,#15803D,#22C55E); }
.sbar .fill.neg { background:linear-gradient(90deg,#B91C1C,#EF4444); right:0; }
.sbar .val { font-family:var(--font-mono); font-variant-numeric:tabular-nums;
             text-align:right; font-size:12px; }

/* ---------------------------------------------------------------- watchlist */
.wl-grid { display:grid; gap:var(--space-md); grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); }
.wl-item { display:flex; justify-content:space-between; align-items:center; gap:var(--space-md);
           background:var(--color-muted); border:1px solid var(--color-border);
           border-radius:var(--radius); padding:10px 12px; transition:border-color var(--trans); }
.wl-item:hover { border-color:var(--color-muted-foreground); }
.wl-item .sy { font:600 13px/1.3 var(--font-mono); }
.wl-item .meta { display:flex; gap:6px; margin-top:3px; flex-wrap:wrap; }
.wl-item .meta .tag { font-size:10px; }
.wl-item .disp { color:var(--color-muted-foreground); font-size:10.5px; margin-top:2px; }
/* add-market form: classed (not inline) so it can reflow below 900px/560px —
   the old 5-column inline grid squeezed the Kind select at 110px on narrow
   windows; 150px/120px fixed cols keep the selects readable and the wraps
   stack label+control pairs on phones */
.wl-form { display:grid; gap:12px; grid-template-columns:150px 1.2fr 120px 1fr auto;
           align-items:end; }
@media (max-width:900px) { .wl-form { grid-template-columns:1fr 1fr; }
  .wl-form button { grid-column:1 / -1; } }
@media (max-width:560px) { .wl-form { grid-template-columns:1fr; } }
.presets { display:flex; gap:var(--space-md); flex-wrap:wrap; }
.preset-btn { background:var(--color-muted); border:1px dashed var(--color-border);
              color:var(--color-muted-foreground); border-radius:var(--radius);
              padding:8px 14px; font:500 12px/1.3 var(--font-ui); cursor:pointer;
              transition:all var(--trans); min-height:44px; }
.preset-btn:hover { color:var(--color-accent); border-color:var(--color-accent); }
.preset-btn svg { width:13px; height:13px; vertical-align:-2px; margin-right:5px; }

/* ---------------------------------------------------------------- account */
.balance-card { text-align:center; padding:var(--space-2xl) var(--space-lg); }
.balance-label { color:var(--color-muted-foreground); font:500 11px/1 var(--font-ui);
                 text-transform:uppercase; letter-spacing:1px; }
.balance-value { font:700 clamp(28px,6vw,44px)/1.15 var(--font-mono);
                 font-variant-numeric:tabular-nums; margin:10px 0 4px; }
.balance-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr));
                gap:var(--space-md); margin-top:var(--space-lg); text-align:center; }
.balance-grid .card-in { background:var(--color-muted); border-radius:var(--radius);
                         padding:10px 8px; }
.balance-grid .k { color:var(--color-muted-foreground); font-size:10.5px;
                   text-transform:uppercase; letter-spacing:.6px; }
.balance-grid .v { font:600 15px/1.4 var(--font-mono); font-variant-numeric:tabular-nums;
                   margin-top:2px; }

/* ---------------------------------------------------------------- modal + toast */
.modal-overlay { position:fixed; inset:0; background:rgba(0,0,0,.6); backdrop-filter:blur(4px);
                 display:none; align-items:center; justify-content:center; z-index:100;
                 padding:var(--space-lg); }
.modal-overlay.open { display:flex; }
.modal { background:var(--color-card); border:1px solid var(--color-border);
         border-radius:16px; padding:var(--space-2xl); box-shadow:var(--shadow-xl);
         max-width:500px; width:100%; animation:popIn .2s ease; }
@keyframes popIn { from { opacity:0; transform:translateY(8px) scale(.97); }
                   to { opacity:1; transform:none; } }
.modal h3 { font:600 15px/1.3 var(--font-ui); margin-bottom:var(--space-md);
            display:flex; align-items:center; gap:8px; }
.modal h3 svg { width:17px; height:17px; color:var(--color-destructive); }
.modal p { color:var(--color-muted-foreground); font-size:13px; margin-bottom:var(--space-lg); }
.modal .modal-actions { display:flex; gap:var(--space-md); justify-content:flex-end;
                       margin-top:var(--space-lg); }
.modal input { margin-top:2px; }

#toasts { position:fixed; bottom:16px; right:16px; z-index:200; display:flex;
          flex-direction:column; gap:8px; max-width:min(92vw,380px); }
.toast { display:flex; align-items:flex-start; gap:10px; background:var(--color-card);
         border:1px solid var(--color-border); border-left:3px solid var(--color-accent);
         border-radius:var(--radius); padding:12px 14px; font-size:13px;
         box-shadow:var(--shadow-lg); animation:toastIn .25s ease; }
.toast.err { border-left-color:var(--color-neg); }
.toast .t-title { font-weight:600; }
.toast .t-msg { color:var(--color-muted-foreground); font-size:12px; margin-top:1px;
                word-break:break-word; }
.toast svg { width:16px; height:16px; flex-shrink:0; margin-top:1px;
             color:var(--color-accent); }
.toast.err svg { color:var(--color-neg); }
@keyframes toastIn { from { opacity:0; transform:translateX(16px); } to { opacity:1; transform:none; } }
.toast.out { opacity:0; transform:translateX(16px); transition:all .3s ease; }

/* ---------------------------------------------------------------- chat */
.chat-shell { display:flex; flex-direction:column; height:min(560px,70vh); }
.chatlog { flex:1; overflow-y:auto; padding:var(--space-md) 2px; display:flex;
           flex-direction:column; gap:var(--space-md); }
.msg { max-width:82%; padding:9px 12px; border-radius:12px; font-size:13px;
       white-space:pre-wrap; word-break:break-word; line-height:1.55;
       animation:fadeIn .18s ease; }
.msg.user { background:var(--color-secondary); align-self:flex-end;
            border-bottom-right-radius:4px; color:var(--color-on-secondary); }
.msg.bot { background:var(--color-muted); align-self:flex-start;
           border-bottom-left-radius:4px; color:var(--color-card-foreground);
           border:1px solid var(--color-border); }
.chat-quick { display:flex; gap:var(--space-sm); flex-wrap:wrap; margin-bottom:var(--space-md); }
.quick-chip { background:transparent; border:1px solid var(--color-border); color:var(--color-muted-foreground);
              border-radius:999px; padding:7px 13px; font:400 12px/1.3 var(--font-ui);
              cursor:pointer; transition:all var(--trans); }
.quick-chip:hover { color:var(--color-accent); border-color:var(--color-accent); }
.chatform { display:flex; gap:var(--space-md); margin-top:var(--space-md); }
.chatform input { flex:1; }
.typing { display:inline-flex; gap:4px; padding:10px 14px; }
.typing i { width:6px; height:6px; border-radius:50%; background:var(--color-muted-foreground);
            animation:blink 1.2s infinite; }
.typing i:nth-child(2) { animation-delay:.2s; } .typing i:nth-child(3) { animation-delay:.4s; }
@keyframes blink { 0%,80%,100% { opacity:.25; } 40% { opacity:1; } }

/* ---------------------------------------------------------------- responsive */
/* top tabs stay visible on phones (the .tabs bar is already overflow-x:auto
   with a hidden scrollbar, so 5 tabs scroll fine) — the bottom nav was
   removed, so mobile nav no longer hides the desktop one; main's bottom
   padding is ordinary again instead of clearing a fixed 110px bar */
@media (max-width:768px) {
  .topbar { flex-wrap:wrap; padding:var(--space-md) var(--space-lg); }
  .brand .sub { display:none; }
  main { padding:var(--space-lg); padding-bottom:var(--space-3xl); }
  .sbar { grid-template-columns:110px 1fr 76px; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration:.01ms !important; animation-iteration-count:1 !important;
                           transition-duration:.01ms !important; scroll-behavior:auto !important; }
  .skeleton::after { animation:none; }
}
</style>
</head>
<body>

<!-- SVG sprite: the tab/card icons below repeat 2-5× each — each <use> inherits
     fill/stroke from its own <svg>, the symbol carries only viewBox -->
<svg xmlns="http://www.w3.org/2000/svg" style="display:none">
  <symbol id="i-plus" viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></symbol>
  <symbol id="i-rotate" viewBox="0 0 24 24"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></symbol>
  <symbol id="i-chat" viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></symbol>
  <symbol id="i-alert" viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></symbol>
  <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></symbol>
  <symbol id="i-trend" viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></symbol>
</svg>

<header class="topbar">
  <div class="brand">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>
    <div>
      <h1>ALGO TRADING BOT</h1>
      <div class="sub">crypto + forex · paper trading · IST</div>
    </div>
  </div>
  <div class="engine-pill" role="status">
    <span class="dot" id="engineDot"></span>
    <span id="enginePillText">engine: checking…</span>
  </div>
</header>

<nav class="tabs" id="tabs" aria-label="Dashboard sections">
  <button class="tab active" data-view="overview" id="tab-overview">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/></svg>
    Overview</button>
  <button class="tab" data-view="portfolio" id="tab-portfolio">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>
    Portfolio</button>
  <button class="tab" data-view="watchlist" id="tab-watchlist">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-clock"/></svg>
    Watchlist</button>
  <button class="tab" data-view="account" id="tab-account">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 1v22M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
    Account</button>
  <button class="tab" data-view="chat" id="tab-chat">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-chat"/></svg>
    Chat</button>
</nav>

<!-- no bottom nav: it duplicated the top tabs and rendered at EVERY width
     (its min-width:769px hide rule sat above the base .bottom-nav rule, so the
     later display:grid always won) — the sticky, horizontally-scrollable
     .tabs bar is the nav at all sizes now -->

<main>
<!-- ============================================================ OVERVIEW -->
<section class="view active" id="view-overview">
  <div class="stats-grid" id="ovStats"></div>
  <div class="grid" style="grid-template-columns:2fr 1fr;margin-bottom:12px">
    <div class="card chart-card"><div class="card-head"><h2><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-trend"/></svg>Equity curve · mark-to-market</h2><span class="hint" id="eqRange"></span></div><div class="chart-body"><canvas id="equityChart"></canvas></div></div>
    <div class="card engine-card">
      <div class="card-head"><h2><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>Engine control</h2></div>
      <div class="row">
        <div>
          <label class="fld" for="intervalSel">Interval</label>
          <select id="intervalSel" aria-label="Engine cycle interval">
            <option value="30">30 s</option>
            <option value="60" selected>60 s</option>
            <option value="120">2 min</option>
            <option value="300">5 min</option>
            <option value="900">15 min</option>
          </select>
        </div>
        <button class="btn" id="btnStart">
          <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><polygon points="6 3 20 12 6 21 6 3"/></svg>
          Start</button>
        <button class="btn btn-secondary" id="btnStop" disabled>
          <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>
          Stop</button>
      </div>
      <div class="engine-state" style="margin-top:12px">
        <span class="st" id="engineStateText">stopped</span>
        <span class="sub" id="engineStateSub">0 cycles · watchlist 0 specs</span>
      </div>
    </div>
  </div>
  <div class="grid" style="grid-template-columns:1fr 1fr">
    <div class="card">
      <div class="card-head"><h2><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 20V6a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v14"/><polyline points="2 20 22 20"/><path d="M14 12v8"/><path d="M10 12v8"/></svg>Strategy P&amp;L</h2><span class="hint">closed trades</span></div>
      <div class="strat-bars" id="stratBars"><div class="sk-row"><div class="skeleton" style="width:60%"></div></div><div class="sk-row"><div class="skeleton" style="width:45%"></div></div></div>
    </div>
    <div class="card">
      <div class="card-head"><h2><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/></svg>Recent decisions</h2><span class="hint">every evaluation · HOLDs included</span></div>
      <div class="term" id="decisionFeed"><div class="sk-row"><div class="skeleton" style="width:85%"></div></div><div class="sk-row"><div class="skeleton" style="width:70%"></div></div><div class="sk-row"><div class="skeleton" style="width:78%"></div></div></div>
    </div>
  </div>
</section>

<!-- ============================================================ PORTFOLIO -->
<section class="view" id="view-portfolio">
  <div class="card">
    <div class="card-head">
      <h2><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M5 12l7 7 7-7"/></svg>Open positions</h2>
      <span class="hint" id="liveHint"></span>
    </div>
    <div class="tbl-wrap">
      <table id="posTable"><thead><tr>
        <th>Market</th><th>Side</th><th class="num">Qty</th><th class="num">Entry</th>
        <th class="num">Mark</th><th class="num">Stop</th><th class="num">Target</th>
        <th>Strategy</th><th class="num">Bars</th><th class="num">Unrealized</th><th></th>
      </tr></thead><tbody><tr><td colspan="11"><div class="sk-row"><div class="skeleton" style="width:90%"></div></div></td></tr></tbody></table>
    </div>
    <div class="empty" id="posEmpty" hidden>No open positions.</div>
  </div>
  <div class="card">
    <div class="card-head">
      <h2><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/><rect x="9" y="3" width="6" height="4" rx="1"/></svg>Trade history</h2>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <label class="fld" for="stratFilter" style="margin:0">Filter</label>
        <select id="stratFilter" style="min-height:36px;width:auto" aria-label="Filter trades by strategy">
          <option value="">all strategies</option>
        </select>
      </div>
    </div>
    <div class="tbl-wrap">
      <table id="tradeTable"><thead><tr>
        <th>Opened</th><th>Market</th><th>Side</th><th class="num">Qty</th>
        <th class="num">Entry</th><th class="num">Exit</th><th class="num">P&amp;L</th>
        <th>Strategy</th><th>Status</th><th>Exit reason</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <div class="empty" id="tradeEmpty" hidden>No trades yet — start the engine.</div>
  </div>
</section>

<!-- ============================================================ WATCHLIST -->
<section class="view" id="view-watchlist">
  <div class="card">
    <div class="card-head">
      <h2><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-clock"/></svg>Add a market</h2>
      <span class="hint" id="wlCount"></span>
    </div>
    <form id="wlForm" class="wl-form">
      <div><label class="fld" for="wlKind">Kind</label>
        <select id="wlKind" aria-label="Market kind">
          <option value="crypto">crypto</option>
          <option value="forex">forex</option>
        </select></div>
      <div><label class="fld" for="wlSymbol">Symbol</label>
        <input class="input" id="wlSymbol" placeholder="BTC/USDT" autocomplete="off" required></div>
      <div><label class="fld" for="wlTf">Timeframe</label>
        <select id="wlTf" aria-label="Timeframe">
          <option value="1h">1h</option><option value="15m">15m</option>
          <option value="4h">4h</option><option value="5m">5m</option><option value="1d">1d</option>
        </select></div>
      <div><label class="fld" for="wlDisplay">Display name (opt.)</label>
        <input class="input" id="wlDisplay" placeholder="Bitcoin" style="font-family:var(--font-ui)" autocomplete="off"></div>
      <button class="btn" type="submit">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><use href="#i-plus"/></svg>Add</button>
    </form>
    <div style="margin-top:14px">
      <label class="fld">Quick add presets</label>
      <div class="presets">
        <button class="preset-btn" type="button" data-preset="crypto-majors" title="BTC, ETH, SOL on 1h/15m/4h">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><use href="#i-plus"/></svg>Crypto majors (9 specs)</button>
        <button class="preset-btn" type="button" data-preset="forex-majors" title="EUR/USD, GBP/USD, USD/JPY on 1h">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><use href="#i-plus"/></svg>Forex majors (1h)</button>
        <button class="preset-btn" type="button" data-preset="reset-default" title="Restore the shipped default watchlist">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-rotate"/></svg>Default watchlist</button>
      </div>
    </div>
  </div>
  <div class="card">
    <div class="card-head">
      <h2><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>Traded markets</h2>
      <span class="hint">engine hot-reloads changes on its next cycle</span>
    </div>
    <div class="wl-grid" id="wlGrid"><div class="sk-row"><div class="skeleton" style="width:70%"></div></div></div>
    <div class="empty" id="wlEmpty" hidden>Watchlist is empty — add markets or use a preset.</div>
  </div>
  <div class="card">
    <div class="card-head"><h2>How ownership works</h2></div>
    <p style="color:var(--color-muted-foreground);font-size:12.5px;line-height:1.6">
      Each timeframe runs one specialized strategy: <span class="mono">1h → turtle_trend</span>,
      <span class="mono">15m/5m → vwap_scalper</span>, <span class="mono">4h/1d → connors_meanrev</span>.
      5m scalping is enabled by explicit decision but measured unprofitable
      (BACKTESTS.md) — pull it back by reverting the scalper's timeframes.
      One position per symbol across timeframes (risk rule). Max 12 specs.
    </p>
  </div>
</section>

<!-- ============================================================ ACCOUNT -->
<section class="view" id="view-account">
  <div class="card balance-card">
    <div class="balance-label">Paper account equity</div>
    <div class="balance-value" id="balanceBig">$0.00</div>
    <div style="color:var(--color-muted-foreground);font-size:12px" id="balanceMode">paper mode</div>
    <div class="balance-grid">
      <div class="card-in"><div class="k">Cash</div><div class="v mono" id="balCash">—</div></div>
      <div class="card-in"><div class="k">Unrealized</div><div class="v mono" id="balUnreal">—</div></div>
      <div class="card-in"><div class="k">Start capital</div><div class="v mono" id="balCapital">—</div></div>
      <div class="card-in"><div class="k">Last mark</div><div class="v mono" id="balLast" style="font-size:12px">—</div></div>
    </div>
  </div>
  <div class="grid" style="grid-template-columns:1fr 1fr">
    <div class="card">
      <div class="card-head"><h2><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>Deposit</h2></div>
      <form id="depositForm" style="display:flex;gap:10px;align-items:end;flex-wrap:wrap">
        <div style="flex:1;min-width:130px"><label class="fld" for="depositAmt">Amount ($)</label>
          <input class="input" id="depositAmt" inputmode="decimal" placeholder="500" autocomplete="off"></div>
        <button class="btn" type="submit" style="flex-shrink:0">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><use href="#i-plus"/></svg>Deposit</button>
      </form>
    </div>
    <div class="card">
      <div class="card-head"><h2><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/></svg>Withdraw</h2></div>
      <form id="withdrawForm" style="display:flex;gap:10px;align-items:end;flex-wrap:wrap">
        <div style="flex:1;min-width:130px"><label class="fld" for="withdrawAmt">Amount ($)</label>
          <input class="input" id="withdrawAmt" inputmode="decimal" placeholder="200" autocomplete="off"></div>
        <button class="btn btn-danger" type="submit" style="flex-shrink:0">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><use href="#i-plus"/></svg>Withdraw</button>
      </form>
    </div>
  </div>
  <div class="card">
    <div class="card-head"><h2><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12h4l3 8 4-16 3 8h4"/></svg>Deposit / withdrawal history</h2><span class="hint">account ledger · times in IST</span></div>
    <div class="tbl-wrap">
      <table id="txnTable"><thead><tr>
        <th>Time</th><th>Type</th><th class="num">Amount</th>
        <th class="num">Cash after</th><th class="num">Equity after</th><th>Note</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <div class="empty" id="txnEmpty" hidden>No deposits or withdrawals yet.</div>
  </div>
  <div class="card">
    <div class="card-head"><h2><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-alert"/></svg>Danger zone</h2></div>
    <p style="color:var(--color-muted-foreground);font-size:12.5px;margin-bottom:14px">
      Reset stops the engine, backs up the journal to <span class="mono">data/trading.backup.&lt;ts&gt;.db</span>,
      then wipes all trades, decisions, equity points and chat. The account restarts at a new
      start capital you choose. This cannot be undone (the backup file is the only copy).
    </p>
    <div style="display:flex;gap:10px;align-items:end;flex-wrap:wrap">
      <div style="flex:1;min-width:150px;max-width:280px"><label class="fld" for="resetCapital">New start capital ($)</label>
        <input class="input" id="resetCapital" inputmode="decimal" placeholder="10000" autocomplete="off"></div>
      <button class="btn btn-danger" type="button" id="btnResetOpen">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-rotate"/></svg>
        Reset account…</button>
    </div>
  </div>
</section>

<!-- ============================================================ CHAT -->
<section class="view" id="view-chat">
  <div class="card">
    <div class="card-head">
      <h2><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-chat"/></svg>Ask the bot</h2>
      <span class="hint">answers from the journal — it cannot invent trades</span>
    </div>
    <div class="chat-shell">
      <div class="chat-quick">
        <button class="quick-chip" data-q="how much did you earn?">how much did you earn?</button>
        <button class="quick-chip" data-q="which strategy is best?">best strategy?</button>
        <button class="quick-chip" data-q="explain the turtle strategy">explain the turtle strategy</button>
        <button class="quick-chip" data-q="what are the risk rules?">risk rules?</button>
        <button class="quick-chip engine-quick" data-engine="start">[ start engine ]</button>
        <button class="quick-chip engine-quick" data-engine="stop">[ stop engine ]</button>
      </div>
      <div class="chatlog" id="chatlog" aria-live="polite"></div>
      <form id="chatform" class="chatform">
        <input class="input" id="chatbox" type="text" autocomplete="off"
               placeholder="e.g. why did you buy BTC? which strategy is best?" aria-label="Message the bot">
        <button class="btn" type="submit" style="flex-shrink:0">Send</button>
      </form>
    </div>
  </div>
</section>
</main>

<!-- reset modal -->
<div class="modal-overlay" id="resetModal" role="dialog" aria-modal="true" aria-labelledby="resetTitle">
  <div class="modal">
    <h3 id="resetTitle"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-alert"/></svg>Reset the paper account?</h3>
    <p>This stops the engine, backs up the journal, deletes every trade, decision,
    equity point and chat message, then restarts the account.</p>
    <label class="fld" for="resetConfirm">Type RESET to confirm</label>
    <input class="input" id="resetConfirm" placeholder="RESET" autocomplete="off">
    <div class="modal-actions">
      <button class="btn btn-ghost" id="resetCancel">Cancel</button>
      <button class="btn btn-danger" id="resetGo" disabled>Reset everything</button>
    </div>
  </div>
</div>

<div id="toasts" aria-live="polite"></div>

<script>
'use strict';
/* ===================================================== tiny helpers */
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtNum = (v, d = 2) => Number(v).toLocaleString(undefined, {minimumFractionDigits: d, maximumFractionDigits: d});
const fmt$ = v => (v == null || isNaN(v)) ? '—' : '$' + fmtNum(v);
const sign = v => v > 0 ? '+' : '';
// sign() alone must stay ''-for-negatives: fmtPct does NOT wrap in Math.abs,
// so toFixed already emits the minus there ('--3.20%' if sign grew one). fmtPnl
// takes the abs path and prefixes its own '-'
const fmtPnl = v => (v == null || isNaN(v)) ? '—'
  : (v > 0 ? '+' : v < 0 ? '-' : '') + '$' + fmtNum(Math.abs(v));
const fmtPct = v => (v == null || isNaN(v)) ? '—' : sign(v) + Number(v).toFixed(2) + '%';
const isForex = s => String(s).includes('=');
const fmtPx = (v, s) => (v == null || isNaN(v) || !Number(v)) ? '—'
  : fmtNum(v, isForex(s) ? 5 : 2);
const fmtQty = v => (v == null || isNaN(v)) ? '—'
  : Number(v).toLocaleString(undefined, {maximumSignificantDigits: 5});
const posCls = v => Number(v) > 0 ? 'pos' : Number(v) < 0 ? 'neg' : '';
const tag = (cls, text) => '<span class="tag ' + esc(cls) + '">' + esc(text) + '</span>';
const sideTag = s => tag((s || '').toLowerCase(), String(s).toUpperCase());
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
/* journal timestamps are ISO-UTC; the UI reads IST (+05:30 fixed, no DST) —
   shift by 330min and read via getUTC* so the browser's own zone never leaks in.
   Keep in sync with _fmt_ts in bot/chatbot.py (same IST display contract). */
const fmtTs = ts => {
  const d = new Date(ts);
  if (ts == null || isNaN(d.getTime())) return String(ts || '');
  const ist = new Date(d.getTime() + 330 * 60000);
  const p = n => String(n).padStart(2, '0');
  return p(ist.getUTCMonth() + 1) + '-' + p(ist.getUTCDate()) + ' ' +
         p(ist.getUTCHours()) + ':' + p(ist.getUTCMinutes());
};

async function jget(u) { const r = await fetch(u); if (!r.ok) throw new Error('GET ' + u);
                         return r.json(); }
async function jreq(u, method, body) {
  const r = await fetch(u, {method, headers: {'Content-Type': 'application/json'},
                            body: body == null ? undefined : JSON.stringify(body)});
  let data = {};
  try { data = await r.json(); } catch (e) { /* non-JSON error body */ }
  if (!r.ok) {
    const msg = (data && data.detail) ? data.detail : (r.status + ' ' + r.statusText);
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    err.status = r.status;
    throw err;
  }
  return data;
}
const jpost = (u, b) => jreq(u, 'POST', b);
const jdel = u => jreq(u, 'DELETE');

/* ===================================================== toasts */
const ICON_OK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>';
const ICON_ERR = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>';
function toast(title, msg, ok = true) {
  const d = document.createElement('div');
  d.className = 'toast' + (ok ? '' : ' err');
  d.innerHTML = (ok ? ICON_OK : ICON_ERR) +
    '<div><div class="t-title">' + esc(title) + '</div>' +
    (msg ? '<div class="t-msg">' + esc(msg) + '</div>' : '') + '</div>';
  $('#toasts').appendChild(d);
  setTimeout(() => { d.classList.add('out'); setTimeout(() => d.remove(), 350); }, 4200);
}
const toastErr = (title, e) => toast(title, e && e.message ? e.message : String(e), false);

/* ===================================================== routing */
const VIEWS = ['overview', 'portfolio', 'watchlist', 'account', 'chat'];
function setView(name) {
  if (!VIEWS.includes(name)) name = 'overview';
  $$('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
  $$('#tabs .tab').forEach(t => t.classList.toggle('active', t.dataset.view === name));
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  document.title = 'Algo Bot — ' + name[0].toUpperCase() + name.slice(1);
  refreshVisible(name);
}
window.addEventListener('hashchange', () => setView(location.hash.slice(1) || 'overview'));
$('#tabs').addEventListener('click', e => { const t = e.target.closest('.tab'); if (t) setView(t.dataset.view); });
/* only #tabs is wired — the duplicated mobile nav is gone from the DOM, and a
   listener on a null element would throw at boot and kill this whole script */

/* first-load skeletons already in the DOM; data replaces them on first poll */
function refreshVisible(name) {
  if (name === 'overview') { refreshStats(); refreshEquity(); refreshDecisions(); }
  else if (name === 'portfolio') { refreshStats(); refreshTrades(); }
  else if (name === 'watchlist') refreshWatchlist();
  else if (name === 'account') { refreshAccount(); refreshTransactions(); }
  else if (name === 'chat' && !chatLoaded) loadChatHistory();
}

/* ===================================================== overview */
let equityChart = null;
function buildEquityChart() {
  equityChart = new Chart($('#equityChart'), {
    type: 'line',
    data: {labels: [], datasets: [{label: 'Equity', data: [], borderColor: '#22C55E',
      backgroundColor: 'rgba(34,197,94,.07)', fill: true, tension: .15, pointRadius: 0,
      borderWidth: 2}]},
    options: {responsive: true, maintainAspectRatio: false, animation: reduceMotion ? false : {duration: 250},
      plugins: {legend: {display: false}, tooltip: {backgroundColor: '#0E1223',
        borderColor: '#334155', borderWidth: 1, titleColor: '#F8FAFC', bodyColor: '#94A3B8',
        titleFont: {family: 'Fira Code'}, bodyFont: {family: 'Fira Code'},
        callbacks: {label: c => ' ' + fmt$(c.parsed.y)}}},
      scales: {x: {ticks: {maxTicksLimit: 8, color: '#94A3B8', font: {family: 'Fira Code', size: 10}},
                   grid: {color: 'rgba(51,65,85,.35)'}},
               y: {ticks: {color: '#94A3B8', font: {family: 'Fira Code', size: 10},
                           callback: v => '$' + v.toLocaleString()},
                   grid: {color: 'rgba(51,65,85,.35)'}}}}
  });
}

const STAT_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>';
async function refreshStats() {
  let s;
  try { s = await jget('/api/stats'); } catch (e) { return; }
  const cards = [
    ['Equity', fmt$(s.current_equity), s.return_pct > 0 ? 'pos' : s.return_pct < 0 ? 'neg' : '',
      'from ' + fmt$(s.start_equity)],
    ['Total P&L', fmtPnl(s.total_pnl), s.total_pnl > 0 ? 'pos' : s.total_pnl < 0 ? 'neg' : '',
      fmtPct(s.return_pct) + ' return'],
    ['Win rate', (s.win_rate ?? 0) + '%', '', s.closed_trades + ' closed trades'],
    ['Profit factor', s.profit_factor == null ? '∞' : s.profit_factor, '',
      s.profit_factor == null ? 'no losses yet' : 'gross win ÷ loss'],
    ['Max drawdown', fmtPct(s.max_drawdown_pct), 'neg', 'peak-to-trough'],
    ['Open positions', String((s.open_positions || []).length), '',
      s.engine_running ? 'live marks' : 'from journal'],
    ['Cycles', String(s.cycles ?? 0), '', s.engine_running ? ('llm: ' + (s.llm_mode || 'quant')) : 'engine stopped'],
    ['Watchlist', String(s.watchlist_count ?? 0), '', 'markets traded'],
  ];
  $('#ovStats').innerHTML = cards.map(c =>
    '<div class="stat"><div class="label">' + STAT_ICON + esc(c[0]) + '</div>' +
    '<div class="value ' + c[2] + '">' + esc(c[1]) + '</div>' +
    '<div class="sub">' + esc(c[3]) + '</div></div>').join('');

  $('#engineDot').className = 'dot ' + (s.engine_running ? 'on' : 'off');
  $('#enginePillText').textContent = 'engine: ' + (s.engine_running ? 'running' : 'stopped');
  $('#engineStateText').textContent = s.engine_running ? 'running' : 'stopped';
  $('#engineStateText').className = 'st ' + (s.engine_running ? 'pos' : 'neg');
  $('#engineStateSub').textContent = (s.cycles ?? 0) + ' cycles · watchlist ' +
    (s.watchlist_count ?? 0) + ' specs';
  $('#btnStart').disabled = !!s.engine_running;
  $('#btnStop').disabled = !s.engine_running;

  renderPositions(s);
  renderStratBars(s.by_strategy || {});
}

function renderStratBars(by) {
  const el = $('#stratBars');
  const entries = Object.entries(by);
  if (!entries.length) {
    el.innerHTML = '<div class="empty" style="padding:16px">No closed trades yet.</div>';
    return;
  }
  const maxAbs = Math.max(...entries.map(([, v]) => Math.abs(v.pnl || 0)), 1);
  el.innerHTML = entries.map(([name, v]) => {
    const pos = (v.pnl || 0) >= 0;
    const w = Math.max(2, Math.abs(v.pnl || 0) / maxAbs * 100);
    return '<div class="sbar" title="' + esc(name) + ': ' + v.trades + ' trades, ' + v.wins + ' wins">' +
      '<span class="name">' + esc(name) + '</span>' +
      '<span class="track"><span class="fill ' + (pos ? 'pos' : 'neg') +
      '" style="width:' + w.toFixed(1) + '%"></span></span>' +
      '<span class="val ' + (pos ? 'pos' : 'neg') + '">' + fmtPnl(v.pnl) + '</span></div>';
  }).join('');
}

async function refreshEquity() {
  if (!equityChart) return;   // offline: the boot banner already says so
  let eq;
  try { eq = await jget('/api/equity'); } catch (e) { return; }
  if (!eq.length) return;
  equityChart.data.labels = eq.map(p => fmtTs(p.ts));
  equityChart.data.datasets[0].data = eq.map(p => p.equity);
  equityChart.update(reduceMotion ? 'none' : undefined);
  $('#eqRange').textContent = fmtTs(eq[0].ts).slice(0, 5) + ' → ' +
    fmtTs(eq[eq.length - 1].ts).slice(0, 5) + ' · ' + eq.length + ' pts';
}

async function refreshDecisions() {
  let ds;
  try { ds = await jget('/api/decisions?limit=30'); } catch (e) { return; }
  const el = $('#decisionFeed');
  if (!ds.length) { el.innerHTML = '<div class="empty" style="padding:16px">No decisions journaled yet.</div>'; return; }
  el.innerHTML = ds.map(d => {
    const a = (d.action || '').toLowerCase();
    return '<div class="term-row">' +
      '<span class="term-ts">' + esc(fmtTs(d.ts)) + '</span>' +
      '<div class="term-body"><div class="term-line">' +
      '<span class="tag ' + esc(a === 'hold' ? 'hold' : a) + '">' + esc(d.action) + '</span>' +
      '<span class="term-mkt">' + esc(d.symbol) + ' <span class="tag tf">' + esc(d.timeframe) + '</span></span>' +
      '<span class="term-meta">regime ' + esc(d.regime || '—') + ' · conf ' +
        Math.round((d.confidence || 0) * 100) + '% · @ ' + fmtPx(d.price, d.symbol) + '</span>' +
      '</div><div class="term-why">' + esc(d.rationale || '') + '</div></div></div>';
  }).join('');
  el.scrollTop = 0;
}

/* engine controls */
async function startEngine() {
  const interval = parseInt($('#intervalSel').value, 10);
  try {
    const r = await jpost('/api/engine/start', {interval});
    toast('Engine ' + (r.status === 'started' ? 'started' : r.status),
          'cycle interval ' + interval + 's', r.status !== 'error');
    addMsg('[engine] started — interval ' + interval + 's', 'bot');
  } catch (e) { toastErr('Could not start engine', e); }
  refreshStats();
}
async function stopEngine() {
  try {
    const r = await jpost('/api/engine/stop', {});
    toast('Engine stopped', '', true);
    addMsg('[engine] stopped', 'bot');
  } catch (e) { toastErr('Could not stop engine', e); }
  refreshStats();
}
$('#btnStart').addEventListener('click', startEngine);
$('#btnStop').addEventListener('click', stopEngine);

/* ===================================================== portfolio */
function renderPositions(s) {
  const tbody = $('#posTable tbody');
  const ops = s.open_positions || [];
  $('#liveHint').textContent = s.engine_running
    ? 'engine running — live marks'
    : (ops.length ? 'engine stopped — entry prices from the journal; start the engine for live marks'
                   : 'engine stopped — start it for live marks');
  $('#posEmpty').hidden = ops.length > 0;
  if (!ops.length) { tbody.innerHTML = ''; return; }
  tbody.innerHTML = ops.map(p => {
    const live = s.engine_running && p.live !== false;
    return '<tr>' +
      '<td class="mono"><b>' + esc(p.symbol) + '</b> <span class="tag tf">' + esc(p.timeframe) + '</span></td>' +
      '<td>' + sideTag(p.side) + '</td>' +
      '<td class="num">' + fmtQty(p.qty) + '</td>' +
      '<td class="num">' + fmtPx(p.entry, p.symbol) + '</td>' +
      '<td class="num">' + fmtPx(p.mark, p.symbol) + '</td>' +
      '<td class="num">' + fmtPx(p.stop, p.symbol) + '</td>' +
      '<td class="num">' + fmtPx(p.target, p.symbol) + '</td>' +
      '<td class="mono" style="color:var(--color-blue)">' + esc(p.strategy) + '</td>' +
      '<td class="num">' + (p.bars_held ?? 0) + '</td>' +
      '<td class="num ' + posCls(p.unrealized) + '">' +
        (p.unrealized != null ? fmtPnl(p.unrealized) : (live ? '…' : '—')) + '</td>' +
      '<td>' + (live ? '<button class="icon-btn" title="Close position" aria-label="Close ' + esc(p.symbol) + ' ' + esc(p.timeframe) +
        '" data-close="' + esc(p.symbol) + '|' + esc(p.timeframe) + '">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>' : '') + '</td></tr>';
  }).join('');
}
$('#posTable').addEventListener('click', async e => {
  const btn = e.target.closest('[data-close]');
  if (!btn) return;
  const [symbol, timeframe] = btn.dataset.close.split('|');
  btn.disabled = true;
  try {
    const r = await jpost('/api/positions/close', {symbol, timeframe});
    toast('Position closed', symbol + ' ' + timeframe + ' @ ' + fmtPx(r.exit_price, symbol));
  } catch (err) {
    toastErr('Close failed', err);
    btn.disabled = false;
  }
  refreshStats();
});

async function refreshTrades() {
  let trades;
  try { trades = await jget('/api/trades?limit=1000'); } catch (e) { return; }
  const sel = $('#stratFilter');
  const current = sel.value;
  const strategies = [...new Set(trades.map(t => t.strategy))].sort();
  if (sel.options.length - 1 !== strategies.length ||
      [...sel.options].slice(1).map(o => o.value).join(',') !== strategies.join(',')) {
    sel.innerHTML = '<option value="">all strategies</option>' +
      strategies.map(s => '<option value="' + esc(s) + '">' + esc(s) + '</option>').join('');
    sel.value = current;
  }
  const filter = sel.value;
  const rows = filter ? trades.filter(t => t.strategy === filter) : trades;
  const tbody = $('#tradeTable tbody');
  $('#tradeEmpty').hidden = rows.length > 0;
  tbody.innerHTML = rows.map(t => '<tr>' +
    '<td class="mono" style="color:var(--color-muted-foreground)">' + esc(fmtTs(t.opened_ts)) + '</td>' +
    '<td class="mono"><b>' + esc(t.symbol) + '</b> <span class="tag tf">' + esc(t.timeframe || '') + '</span></td>' +
    '<td>' + sideTag(t.side) + '</td>' +
    '<td class="num">' + fmtQty(t.qty) + '</td>' +
    '<td class="num">' + fmtPx(t.entry_price, t.symbol) + '</td>' +
    '<td class="num">' + fmtPx(t.exit_price, t.symbol) + '</td>' +
    '<td class="num ' + posCls(t.pnl) + '">' + (t.status === 'CLOSED' ? fmtPnl(t.pnl) : '—') + '</td>' +
    '<td class="mono" style="color:var(--color-blue)">' + esc(t.strategy) + '</td>' +
    '<td><span class="tag ' + (t.status === 'OPEN' ? 'open' : 'hold') + '">' + esc(t.status) + '</span></td>' +
    '<td style="color:var(--color-muted-foreground)">' + esc(t.exit_reason || '—') + '</td></tr>').join('');
}
$('#stratFilter').addEventListener('change', refreshTrades);

/* ===================================================== watchlist */
async function refreshWatchlist() {
  let specs;
  try { specs = await jget('/api/watchlist'); } catch (e) { return; }
  $('#wlCount').textContent = specs.length + ' / 12 specs';
  $('#wlEmpty').hidden = specs.length > 0;
  const grid = $('#wlGrid');
  grid.innerHTML = specs.map(s =>
    '<div class="wl-item"><div style="min-width:0">' +
    '<div class="sy">' + esc(s.symbol) + ' <span class="tag tf">' + esc(s.timeframe) + '</span></div>' +
    '<div class="meta"><span class="tag ' + (s.kind === 'crypto' ? 'open' : 'close') + '">' + esc(s.kind) + '</span>' +
    '<span class="tag hold">' + esc(s.strategy) + '</span></div>' +
    (s.display && s.display !== s.symbol ? '<div class="disp">' + esc(s.display) + '</div>' : '') +
    '</div><button class="icon-btn" title="Remove from watchlist" aria-label="Remove ' + esc(s.symbol) + ' ' + esc(s.timeframe) +
    '" data-del="' + esc(s.kind) + '|' + esc(s.symbol) + '|' + esc(s.timeframe) + '">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button></div>').join('');
}
$('#wlGrid').addEventListener('click', async e => {
  const btn = e.target.closest('[data-del]');
  if (!btn) return;
  const [kind, symbol, timeframe] = btn.dataset.del.split('|');
  try {
    const r = await jdel('/api/watchlist/' + kind + '/' + encodeURIComponent(symbol) + '/' + timeframe);
    toast('Removed from watchlist', symbol + ' ' + timeframe + ' — ' + r.count + ' specs remain');
  } catch (err) { toastErr('Remove failed', err); }
  refreshWatchlist();
});

async function addSpecs(specs) {
  const errors = [];
  let added = 0;
  for (const s of specs) {
    try { await jpost('/api/watchlist', s); added++; }
    catch (err) { errors.push(s.symbol + ' ' + s.timeframe + ': ' + err.message); }
  }
  if (added) toast('Watchlist updated', added + ' market' + (added > 1 ? 's' : '') + ' added');
  errors.forEach(m => toast('Skipped', m, false));
  refreshWatchlist();
}

$('#wlForm').addEventListener('submit', e => {
  e.preventDefault();
  const kind = $('#wlKind').value, symbol = $('#wlSymbol').value.trim(),
        timeframe = $('#wlTf').value, display = $('#wlDisplay').value.trim();
  if (!symbol) { toast('Symbol required', 'e.g. BTC/USDT or EURUSD=X', false); return; }
  addSpecs([{kind, symbol, timeframe, display: display || null}]).then(() => {
    $('#wlSymbol').value = ''; $('#wlDisplay').value = ''; $('#wlSymbol').focus();
  });
});

const PRESETS = {
  'crypto-majors': () => ['BTC/USDT', 'ETH/USDT', 'SOL/USDT'].flatMap(s =>
    [{kind: 'crypto', symbol: s, timeframe: '1h'},
     {kind: 'crypto', symbol: s, timeframe: '15m'},
     {kind: 'crypto', symbol: s, timeframe: '4h'}]),
  'forex-majors': () => ['EURUSD=X', 'GBPUSD=X', 'USDJPY=X'].map(s =>
    ({kind: 'forex', symbol: s, timeframe: '1h'})),
};
$$('.preset-btn').forEach(b => b.addEventListener('click', () => {
  const p = b.dataset.preset;
  if (p === 'reset-default') {
    const defaults = [['crypto', 'BTC/USDT', '1h', 'Bitcoin'], ['crypto', 'ETH/USDT', '1h', 'Ethereum'],
      ['crypto', 'SOL/USDT', '1h', 'Solana'], ['crypto', 'BTC/USDT', '15m', 'Bitcoin (scalp)'],
      ['crypto', 'ETH/USDT', '15m', 'Ethereum (scalp)'], ['crypto', 'BTC/USDT', '4h', 'Bitcoin (mean-rev)'],
      ['crypto', 'ETH/USDT', '4h', 'Ethereum (mean-rev)'], ['forex', 'EURUSD=X', '1h', 'EUR/USD'],
      ['forex', 'GBPUSD=X', '1h', 'GBP/USD']];
    (async () => {
      try {   // clear then re-add the shipped default list
        let cur = [];
        try { cur = await jget('/api/watchlist'); } catch (e) {}
        for (const s of cur) {
          try { await jdel('/api/watchlist/' + s.kind + '/' + encodeURIComponent(s.symbol) + '/' + s.timeframe); }
          catch (err) { toastErr('Remove failed', err); }
        }
        await addSpecs(defaults.map(d => ({kind: d[0], symbol: d[1], timeframe: d[2], display: d[3]})));
      } catch (err) { toastErr('Preset failed', err); }
    })();
    return;
  }
  addSpecs(PRESETS[p]());
}));

/* ===================================================== account */
async function refreshAccount() {
  let a;
  try { a = await jget('/api/account'); } catch (e) { return; }
  const pnl = a.equity - a.capital;
  const big = $('#balanceBig');
  big.textContent = fmt$(a.equity);
  big.className = 'balance-value ' + posCls(pnl);
  $('#balanceMode').textContent = 'paper mode · ' +
    (a.engine_running ? 'engine running' : 'engine stopped') +
    (pnl ? ' · ' + fmtPnl(pnl) + ' (' + fmtPct(pnl / a.capital * 100) + ')' : '');
  $('#balCash').textContent = fmt$(a.cash);
  const u = $('#balUnreal');
  u.textContent = a.engine_running ? fmtPnl(a.unrealized) : '—';
  u.className = 'v mono ' + posCls(a.unrealized);
  $('#balCapital').textContent = fmt$(a.capital);
  $('#balLast').textContent = a.last_equity ? fmtTs(a.last_equity.ts) : '—';
}

/* deposit/withdrawal ledger — typed rows beat scraping add_equity notes */
async function refreshTransactions() {
  let txs;
  try { txs = await jget('/api/account/transactions'); } catch (e) { return; }
  $('#txnEmpty').hidden = txs.length > 0;
  const tagCls = {deposit: 'long', withdrawal: 'short', reset: 'close'};
  $('#txnTable tbody').innerHTML = txs.map(t => '<tr>' +
    '<td class="mono" style="color:var(--color-muted-foreground)">' + esc(fmtTs(t.ts)) + '</td>' +
    '<td><span class="tag ' + (tagCls[t.kind] || '') + '">' + esc(t.kind) + '</span></td>' +
    '<td class="num ' + (t.kind === 'withdrawal' ? 'neg' : 'pos') + '">' +
      fmtPnl(t.kind === 'withdrawal' ? -t.amount : t.amount) + '</td>' +
    '<td class="num">' + fmt$(t.cash_after) + '</td>' +
    '<td class="num">' + fmt$(t.equity_after) + '</td>' +
    '<td style="color:var(--color-muted-foreground)">' + esc(t.note || '') + '</td></tr>').join('');
}

function parseAmount(v) {
  const n = Number(String(v).replace(/[$,\\s]/g, ''));
  if (!isFinite(n) || isNaN(n) || n <= 0) return null;
  return Math.round(n * 100) / 100;
}
function submitAmount(url, raw, label) {
  const amount = parseAmount(raw);
  if (amount == null) { toast('Invalid amount', 'enter a positive number, e.g. 500', false); return Promise.resolve(false); }
  return jpost(url, {amount}).then(r => {
    toast(label + ' successful', (label === 'Deposit' ? '+' : '-') + fmt$(amount) +
      ' · cash now ' + fmt$(r.cash));
    refreshAccount();
    return true;
  }).catch(e => { toastErr(label + ' failed', e); return false; });
}
$('#depositForm').addEventListener('submit', async e => {
  e.preventDefault();
  const ok = await submitAmount('/api/account/deposit', $('#depositAmt').value, 'Deposit');
  if (ok) $('#depositAmt').value = '';
});
$('#withdrawForm').addEventListener('submit', async e => {
  e.preventDefault();
  const ok = await submitAmount('/api/account/withdraw', $('#withdrawAmt').value, 'Withdrawal');
  if (ok) $('#withdrawAmt').value = '';
});

/* reset modal */
const resetModal = $('#resetModal');
$('#btnResetOpen').addEventListener('click', () => {
  $('#resetConfirm').value = '';
  $('#resetGo').disabled = true;
  $('#resetCapital').value = $('#resetCapital').value || '10000';
  resetModal.classList.add('open');
  $('#resetConfirm').focus();
});
$('#resetCancel').addEventListener('click', () => resetModal.classList.remove('open'));
resetModal.addEventListener('click', e => { if (e.target === resetModal) resetModal.classList.remove('open'); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') resetModal.classList.remove('open'); });
$('#resetConfirm').addEventListener('input', e => {
  $('#resetGo').disabled = e.target.value.trim() !== 'RESET';
});
$('#resetGo').addEventListener('click', async () => {
  const capital = parseAmount($('#resetCapital').value);
  if (capital == null) { toast('Invalid capital', 'enter a positive number', false); return; }
  try {
    const r = await jpost('/api/account/reset', {capital});
    toast('Account reset', 'new capital ' + fmt$(r.capital) +
      (r.backup ? ' · backup ' + r.backup.split('/').pop() : ''));
    resetModal.classList.remove('open');
    $('#resetConfirm').value = '';
    chatLoaded = false;
    $('#chatlog').innerHTML = '';
    refreshAccount(); refreshStats(); refreshEquity();
  } catch (e) { toastErr('Reset failed', e); }
});

/* ===================================================== chat */
let chatLoaded = false;
function addMsg(text, role) {
  const log = $('#chatlog');
  const d = document.createElement('div');
  d.className = 'msg ' + (role === 'user' ? 'user' : 'bot');
  d.textContent = text;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}
async function loadChatHistory() {
  chatLoaded = true;
  let hist;
  try { hist = await jget('/api/chat'); } catch (e) { return; }
  hist.forEach(m => addMsg(m.content, m.role === 'user' ? 'user' : 'bot'));
  if (!hist.length) addMsg("Hi! I'm the bot's assistant. Ask me: 'how much did you earn?', " +
    "'which strategy is best?', 'why did you buy BTC?', 'explain the turtle strategy'…", 'bot');
  const log = $('#chatlog');
  log.scrollTop = log.scrollHeight;
}
$('#chatform').addEventListener('submit', async e => {
  e.preventDefault();
  const box = $('#chatbox');
  const msg = box.value.trim();
  if (!msg) return;
  box.value = '';
  addMsg(msg, 'user');
  const typing = document.createElement('div');
  typing.className = 'msg bot typing';
  typing.innerHTML = '<i></i><i></i><i></i>';
  $('#chatlog').appendChild(typing);
  $('#chatlog').scrollTop = 999999;
  try {
    const r = await jpost('/api/chat', {message: msg});
    typing.remove();
    addMsg(r.reply, 'bot');
  } catch (err) {
    typing.remove();
    addMsg('(network error) ' + err.message, 'bot');
  }
});
$('#chatlog').addEventListener('click', () => $('#chatbox').focus());
$$('.quick-chip').forEach(chip => chip.addEventListener('click', () => {
  if (chip.dataset.engine === 'start') { startEngine(); return; }
  if (chip.dataset.engine === 'stop') { stopEngine(); return; }
  const q = chip.dataset.q;
  $('#chatbox').value = q;
  $('#chatform').dispatchEvent(new Event('submit', {cancelable: true}));
}));

/* ===================================================== polling */
let pollTimer = null;
function poll() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    const name = document.querySelector('.view.active').id.replace('view-', '');
    refreshVisible(name);
  }, 4000);
}

/* ===================================================== boot
   Chart.js loads from a CDN: on venue/offline WiFi the SPA must still work.
   If the library is missing we show a notice and render everything else —
   the equity chart canvas just stays empty instead of killing routing,
   polling and every button listener with a ReferenceError. */
if (typeof Chart === 'undefined') {
  const banner = document.createElement('div');
  banner.style.cssText = 'padding:8px 14px;margin:10px 0;border-radius:8px;' +
    'background:#7F1D1D;color:#FEE2E2;font:12px "Fira Sans",sans-serif;';
  banner.textContent = 'Chart.js could not load (offline?) — the equity chart is ' +
    'disabled, everything else works normally.';
  const main = document.querySelector('main') || document.body;
  main.insertBefore(banner, main.firstChild);
} else {
  buildEquityChart();
}
setView(location.hash.slice(1) || 'overview');
poll();
</script>
</body>
</html>
"""
