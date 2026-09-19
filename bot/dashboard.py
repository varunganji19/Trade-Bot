"""
FastAPI dashboard + JSON API — a tabbed single-page app (inline HTML/CSS/JS;
Chart.js is VENDORED locally at bot/chart.umd.min.js, so all JS/CSS work with
no network; the only outbound fetch is the Google Fonts stylesheet for
typography, which silently falls back to system fonts offline).

Tabs (hash routing, ~4s polling):
  #overview   — equity curve, headline stats, engine controls, decision feed,
                per-strategy PnL bars
  #portfolio  — open positions (live marks, manual close) + trade history
  #hft        — the SEPARATE high-frequency paper book: its own engine
                controls, equity curve, ALL HFT trades in one place, decision
                feed for the fast book (mode='hft')
  #watchlist  — full CRUD of what the bot trades (persisted data/watchlist.json;
                hot-reloads into a RUNNING engine's CONFIG)
  #lab        — Strategy Lab: pick ANY stock/pair (crypto, forex, NSE —
                aliases normalized), apply the strategies registered for it,
                backtest on real data — in BOTH books (standard + HFT);
                async runs with status polling, comparison mode, artifacts
                in data/results/lab_*.json
  #evidence   — the honesty layer, rendered: Kronos rolling IC vs its promotion
                hurdle, purged-CV path distribution, PBO/Deflated-Sharpe/MinTRL
                verdict cards, shadow adherence, pinned-data manifest (reads
                the generated artifacts in data/results + data/kronos_ic.json)
  #account    — paper balance: deposits/withdrawals + type-to-confirm reset
  #chat       — the journal-aware chatbot

API: GET /  /api/stats /api/equity /api/trades /api/decisions /api/evidence
     /api/positions (open positions) /api/watchlist /api/account
     /api/account/transactions /api/chat /api/engine/status
     time, never both; see config.MARKET_MODE)
     POST /api/chat {message}  /api/engine/start {interval}  /api/engine/stop
          /api/watchlist {kind,symbol,timeframe,display?}
          /api/account/deposit {amount}  /api/account/withdraw {amount}
          /api/account/reset {capital}
          /api/trading/pause {note?}  /api/trading/resume {}   (manual halt)
     DELETE /api/watchlist/{kind}/{symbol}/{timeframe}
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import traceback
from contextlib import asynccontextmanager
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response as FastAPIResponse, FileResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from bot.chatbot import ChatBot
from bot.engine import TradingEngine
from bot.journal import NO_OWNER, BookOwnedError, Journal
from bot.pause import is_paused, set_paused
from bot.strategies import STRATEGY_CLASSES
from config import (CONFIG, MarketSpec, VALID_KINDS,
                    VALID_TIMEFRAMES, MAX_WATCHLIST_SPECS,
                    apply_saved_watchlist, save_watchlist, db_dir, infer_kind)


@asynccontextmanager
async def _lifespan(_app):
    # replaces the deprecated @app.on_event("startup") hook (which emitted
    # deprecation warnings on every boot and test run); the body lives below
    # the handlers it calls and resolves at startup time
    # a failed auto-resume must never abort uvicorn's startup: without the UI
    # there is nothing left to explain the failure with
    for resume in (_auto_resume_engine, _auto_resume_hft_engine):
        try:
            resume()
        except Exception:
            print(f"[dashboard] {resume.__name__} failed — the dashboard is up, "
                  f"start the engine from the top bar")
            traceback.print_exc()
    yield


app = FastAPI(title="AI Trading Bot Dashboard", version="2.1", lifespan=_lifespan)
# blocks DNS-rebinding pages from reaching the API (a rebind page becomes
# same-origin with 127.0.0.1 and gets full read/write otherwise) — the bot
# stays localhost-only
app.add_middleware(TrustedHostMiddleware,
                  allowed_hosts=["127.0.0.1", "localhost"])


def _check_token(auth_header: str, token: str) -> bool:
    """Pure predicate for the optional bearer guard (unit-tested)."""
    if not token:
        return True
    import hmac
    # bytes, not str: str compare_digest raises TypeError on non-ASCII input,
    # turning a wrong-header probe into a 500 on every API route
    return hmac.compare_digest(auth_header.encode("utf-8"),
                               f"Bearer {token}".encode("utf-8"))


class _TokenGuard:   # pure ASGI middleware — no BaseHTTPMiddleware overhead
    """Optional shared-token auth, OFF by default. Set DASHBOARD_TOKEN to
    require `Authorization: Bearer <token>` on every API request — the
    belt-and-suspenders layer if the dashboard is ever deliberately exposed
    beyond loopback.

    The HTML page shell and the vendored chart.js are EXEMPT: browsers cannot
    send headers on navigation, and guarding GET / made the dashboard literally
    unopenable when the feature was on (a 401 JSON page, no UI at all). The
    shell has no secrets — every number comes from the guarded /api/* routes,
    and the SPA attaches the bearer token from localStorage on its fetches
    (prompting for it once when the API answers 401)."""
    # shell only — every data route stays behind the token
    _EXEMPT_GET = frozenset({"/", "/chart.umd.min.js", "/dashboard.css",
                             "/app.css", "/app.js"})

    def __init__(self, asgi_app, token: str):
        self.app = asgi_app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope.get("method") == "GET" and scope.get("path") in self._EXEMPT_GET:
                await self.app(scope, receive, send)
                return
            if not _check_token(
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
_engine_interval: int = CONFIG.live_interval_seconds   # chosen cadence (persisted across stops)
_AUTO_RESUMED_AT_BOOT = False           # the UI's first poll toasts it once (no silent surprise)

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
# STANDARD-book strategies only: this map badges the standard watchlist, and
# both books trade 5m since the fast book left 1m — without the book filter
# a 5m standard spec would be badged with a fast-book strategy that never
# votes on it.
STRATEGY_BY_TF = {tf: name for name, cls in STRATEGY_CLASSES.items()
                  if getattr(cls, "book", "standard") == "standard"
                  for tf in cls.preferred_timeframes}

apply_saved_watchlist()  # data/watchlist.json → CONFIG.watchlist (creates file on first boot)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class EngineIn(BaseModel):
    # ge=5: interval=0 was a hot loop hammering the exchanges; negative killed
    # the loop thread silently (sleep() raised outside the try)
    interval: int = Field(default=60, ge=5, le=3600)


class HftEngineIn(BaseModel):
    """The HFT book's interval floor is 1s (paper trading: the whole point is
    minimal bar-close -> decision -> fill latency). The standard engine keeps
    its ge=5 floor."""
    interval: int = Field(default=2, ge=1, le=3600)


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


class PauseIn(BaseModel):
    """Manual pause/resume body: optional human note. The body-less variant
    is a plain {} like every other mutating POST (the JSON content type forces
    the CORS preflight that defeats form-encoded CSRF)."""
    note: str = Field(default="", max_length=200)


class EmptyIn(BaseModel):
    """Body-required marker for POSTs that take no fields: a JSON body forces
    the CORS preflight that defeats form-encoded CSRF (same rule every other
    mutating endpoint already follows)."""


# --------------------------------------------------------------- engine state
def _engine_state_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(CONFIG.db_path)),
                        "engine_state.json")


def _write_engine_state(running: bool, interval: int) -> bool:
    """Persist the operator's desired engine state so a dashboard restart can
    auto-resume it (a stop must win over a stale 'running' file). Returns
    False (and logs loudly) when the write fails so endpoints can surface it
    instead of silently losing auto-resume."""
    try:
        # atomic: a torn state file would silently disable auto-resume
        tmp = _engine_state_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"desired": "running" if running else "stopped",
                       "interval": interval}, f)
        os.replace(tmp, _engine_state_path())
        return True
    except OSError as exc:
        print(f"[dashboard] FAILED to persist engine_state (desired="
              f"{'running' if running else 'stopped'}): {exc}")
        traceback.print_exc()
        return False


_engine_starting = False  # placeholder claimed under lock before the build


def _release_book(eng, mode: str) -> None:
    """Retire an engine: stop its background workers and drop its
    cross-process lease. Every call site is discarding the engine, and a
    stopped book must not leave a Kronos worker forecasting into its ledger
    (a stop/start used to leave one running per start)."""
    try:
        eng.shutdown()
    except Exception:
        pass
    token, eng.book_token = eng.book_token, None
    if token is None:
        return
    try:
        journal.release_book(mode, token)
    except Exception:
        pass   # the lease expires on its own (see Journal._lease_is_live)


def _spawn_engine(interval: int) -> dict:
    """Build + start the engine thread (shared by the API endpoint and the
    startup auto-resume). Returns the API response dict."""
    global _engine, _engine_thread, _engine_interval, _engine_starting
    # check-and-set under lock FIRST: a double-POST used to build two engines
    # (torch/Kronos probe each) before discovering the race under the lock.
    with _engine_lock:
        if _engine is not None:
            return {"status": "already_running", "cycles": _engine.cycles}
        if _engine_starting:
            return {"status": "starting", "cycles": 0}
        if _engine_thread is not None and _engine_thread.is_alive():
            return {"status": "stopping", "cycles": 0}
        _engine_starting = True
    # build OUTSIDE _engine_lock: TradingEngine.__init__ probes the Kronos stack
    # (imports, no weight load — that happens lazily in the first engine cycle)
    # and holding the lock froze every stats/status poll
    try:
        eng = TradingEngine(mode="paper", quiet=False, journal=journal)
        # Take the book's cross-process lease BEFORE publishing the engine: a
        # standalone `main.py run` in another process owns the same account,
        # and two engines on one book fork it (see Journal.claim_book).
        eng.book_token = journal.claim_book("paper")
    except BookOwnedError as exc:
        with _engine_lock:
            _engine_starting = False
        return {"status": "owned", "cycles": 0, "detail": str(exc)}
    except Exception:
        with _engine_lock:
            _engine_starting = False
        raise
    with _engine_lock:
        if _engine is not None:
            _engine_starting = False
            _release_book(eng, "paper")
            return {"status": "already_running", "cycles": _engine.cycles}
        # a previous thread may still be finishing its last cycle (stop only
        # clears the global); two engines writing one journal fork the account
        if _engine_thread is not None and _engine_thread.is_alive():
            _engine_starting = False
            _release_book(eng, "paper")
            return {"status": "stopping", "cycles": 0}
        _engine = eng
        _engine_interval = interval

    def _loop(eng_ref, interval):
        global _engine, _last_engine_error
        try:
            _engine_cycles(eng_ref, interval)
        finally:
            _release_book(eng_ref, "paper")

    def _engine_cycles(eng_ref, interval):
        global _engine, _last_engine_error   # interval: start value only (see below)
        while _get_engine() is eng_ref:
            cycle_t0 = time.monotonic()
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
            # sleep the REMAINDER of the interval from cycle START (a 2-minute
            # Kronos cycle at interval=60 used to land one decision burst every
            # ~2.5 min), and wake the SECOND the identity check flips so a stop
            # is near-instant instead of stranding the UI for up to interval-300s.
            # The interval is re-read from the module global EVERY cycle so
            # /api/engine/interval can retune a RUNNING engine (the captured
            # argument made the cadence unchangeable without a stop/start).
            remaining = max(0.0, _engine_interval - (time.monotonic() - cycle_t0))
            deadline = time.monotonic() + remaining
            while time.monotonic() < deadline:
                if _get_engine() is not eng_ref:
                    return
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            if _get_engine() is not eng_ref:
                break

    with _engine_lock:
        try:
            _engine_thread = threading.Thread(target=_loop, args=(eng, interval), daemon=True)
            _engine_thread.start()
        except Exception:
            _engine = None
            _release_book(eng, "paper")
            raise
        finally:
            _engine_starting = False
    return {"status": "started", "interval": interval}


def _get_engine() -> TradingEngine | None:
    global _engine
    with _engine_lock:
        return _engine


# ------------------------------------------------------- HFT engine (mode=hft)
# The high-frequency book's engine: same TradingEngine class on the HFT
# config (bot/hft.py), a SEPARATE broker/cash/risk state, and journal rows
# tagged mode='hft' — the two books never share positions or equity.
_hft_lock = threading.Lock()
_hft_engine: TradingEngine | None = None
_hft_thread: threading.Thread | None = None
_last_hft_error: str | None = None
_hft_interval: int = CONFIG.hft.live_interval_seconds
_HFT_AUTO_RESUMED_AT_BOOT = False


def _hft_state_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(CONFIG.db_path)),
                        "hft_engine_state.json")


def _write_hft_state(running: bool, interval: int) -> bool:
    try:
        tmp = _hft_state_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"desired": "running" if running else "stopped",
                       "interval": interval}, f)
        os.replace(tmp, _hft_state_path())
        return True
    except OSError as exc:
        print(f"[dashboard] FAILED to persist hft_engine_state: {exc}")
        traceback.print_exc()
        return False


_hft_starting = False


def _spawn_hft_engine(interval: int) -> dict:
    """Build + start the HFT engine thread (mirrors _spawn_engine)."""
    global _hft_engine, _hft_thread, _hft_interval, _hft_starting
    from bot.hft import build_hft_engine
    with _hft_lock:
        if _hft_engine is not None:
            return {"status": "already_running", "cycles": _hft_engine.cycles}
        if _hft_starting:
            return {"status": "starting", "cycles": 0}
        if _hft_thread is not None and _hft_thread.is_alive():
            return {"status": "stopping", "cycles": 0}
        _hft_starting = True
    try:
        eng = build_hft_engine(journal=journal, quiet=False)
        eng.book_token = journal.claim_book("hft")   # see _spawn_engine
    except BookOwnedError as exc:
        with _hft_lock:
            _hft_starting = False
        return {"status": "owned", "cycles": 0, "detail": str(exc)}
    except Exception:
        with _hft_lock:
            _hft_starting = False
        raise
    with _hft_lock:
        _hft_starting = False
        if _hft_engine is not None:
            _release_book(eng, "hft")
            return {"status": "already_running", "cycles": _hft_engine.cycles}
        if _hft_thread is not None and _hft_thread.is_alive():
            _release_book(eng, "hft")
            return {"status": "stopping", "cycles": 0}
        _hft_engine = eng
        _hft_interval = interval

    def _hft_loop(eng_ref, interval):
        try:
            _hft_cycles(eng_ref, interval)
        finally:
            _release_book(eng_ref, "hft")

    def _hft_cycles(eng_ref, interval):
        global _hft_engine, _last_hft_error
        while _get_hft_engine() is eng_ref:
            cycle_t0 = time.monotonic()
            try:
                eng_ref.run_cycle()
            except Exception as exc:
                eng_ref.last_error = f"{type(exc).__name__}: {exc}"
                _last_hft_error = eng_ref.last_error
                traceback.print_exc()
                eng_ref.cycles += 1
                with _hft_lock:
                    if _hft_engine is eng_ref:
                        _hft_engine = None
                break
            # re-read each cycle: /api/hft/engine/interval retunes a RUNNING
            # book without a stop/start (see the standard loop)
            remaining = max(0.0, _hft_interval - (time.monotonic() - cycle_t0))
            deadline = time.monotonic() + remaining
            while time.monotonic() < deadline:
                if _get_hft_engine() is not eng_ref:
                    return
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            if _get_hft_engine() is not eng_ref:
                break

    with _hft_lock:
        try:
            _hft_thread = threading.Thread(target=_hft_loop, args=(eng, interval), daemon=True)
            _hft_thread.start()
        except Exception:
            # the loop's finally never runs if the thread never starts, so the
            # lease would be held by this live pid for the process's lifetime,
            # making the book unstartable and unresettable
            _hft_engine = None
            _release_book(eng, "hft")
            raise
    return {"status": "started", "interval": interval}


def _get_hft_engine() -> TradingEngine | None:
    global _hft_engine
    with _hft_lock:
        return _hft_engine


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
    # no-store: the shell carries no secrets but must never be served stale
    # from a cache after an auth-gated deploy (token in localStorage flow).
    with open(static_path("index.html")) as fh:
        return HTMLResponse(content=fh.read(),
                            headers={"Cache-Control": "no-store"})


@app.get("/app.css", include_in_schema=False)
def app_css():
    # no-store like the shell: a cached stylesheet against a redeployed page
    # is exactly the "why does the UI look wrong" report nobody can reproduce
    return FileResponse(static_path("app.css"), media_type="text/css",
                        headers={"Cache-Control": "no-store"})


@app.get("/app.js", include_in_schema=False)
def app_js():
    return FileResponse(static_path("app.js"), media_type="application/javascript",
                        headers={"Cache-Control": "no-store"})


@app.get("/chart.umd.min.js",
         include_in_schema=False,
         response_class=FastAPIResponse)
def chart_js():
    """Vendored Chart.js 4.4.3 (MIT, see bot/chart.LICENSE) — served locally
    instead of from a CDN: no third-party same-origin script execution, and
    the dashboard works fully offline."""
    return FileResponse(os.path.join(os.path.dirname(__file__), "chart.umd.min.js"),
                        media_type="application/javascript")


@app.get("/dashboard.css", include_in_schema=False)
def dashboard_css():
    return FileResponse(os.path.join(os.path.dirname(__file__), "dashboard.css"),
                        media_type="text/css", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# stats / history
@app.get("/api/stats")
def api_stats():
    # the headline cards are the bot's OWN paper record when one exists; a
    # demo-only journal (fresh seed-demo) still shows so the demo works — the
    # overview demo note labels what's seeded. The paper record needs BOTH a
    # paper trade and a paper equity point: trades without an equity walk
    # (only hand-producible) would put a nonzero total_pnl beside a
    # capital-equals-equity headline — the same contradiction the demo fix
    # targeted
    modes = journal.trade_mode_counts()
    if modes.get("paper") and journal.last_equity_point(mode="paper") is not None:
        stats = journal.stats(mode="paper")
    else:
        stats = journal.stats()
    # seeded demo rows (seed-demo backtest replays) are labeled mode='demo' —
    # surface the split so the UI can badge them instead of passing them off
    # as the bot's own paper record
    stats["trade_modes"] = modes
    # boot-resume notice: the engine started by auto-resume (not by the
    # operator's click) — the UI toasts it once so trading never silently begins
    stats["auto_resumed"] = _AUTO_RESUMED_AT_BOOT
    eng = _get_engine()
    stats["engine_running"] = eng is not None
    stats["engine_state"] = ("running" if eng is not None else "starting" if _engine_starting
                             else "stopping" if _engine_thread is not None
                             and _engine_thread.is_alive() else "stopped")
    stats["entries_halted"] = bool(eng is not None and eng.risk.halted)
    stats["vetoes"] = _veto_payload(eng)
    stats["strategies"] = _voting_payload("standard")
    stats["cycles"] = eng.cycles if eng is not None else 0
    # the chosen cadence rides the SAME poll the Interval select follows
    # (/api/stats, not /api/engine/status — the UI polls this one), so a
    # change made from another tab or session shows up within a tick
    stats["interval"] = _engine_interval
    stats["watchlist_count"] = len(CONFIG.watchlist)
    # manual pause rides the same poll as health_note (the banner + button
    # must flip within one 4s tick, without a second request). Reported for a
    # stopped engine too — the flag file outlives any single engine run.
    paused, pause_note = is_paused()
    if eng is not None:
        paused = paused or bool(getattr(eng.risk, "paused", False))
    stats["paused"] = paused
    stats["paused_note"] = pause_note
    if eng is not None:
        stats["llm_mode"] = eng.llm.provider if eng.llm.enabled else "quant"
        # live-state trio via the shared helper (marks come from the engine's
        # TTL-cached frames; iterating the live positions dict would race the
        # engine's open/close mutations -> "dict changed size" 500s)
        positions, marks, price_map = _live_state(eng)
        stats["open_positions"] = [_position_dict(p, marks) for p in positions]
        stats["broker_equity"] = round(eng.broker.equity(price_map), 2)
        stats["engine_error"] = eng.last_error
        # degraded-but-alive conditions (e.g. a held position behind a dead
        # feed) ride here — the UI turns the pill amber and shows a banner
        stats["health_note"] = getattr(eng, "health_note", None)
    else:
        stats["llm_mode"] = "quant"
        stats["open_positions"] = [_journal_position_dict(t) for t in journal.open_trades()]
        stats["health_note"] = None
    return stats


def _is_busy_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _downsample(rows: list, cap: int = 500) -> list:
    if len(rows) <= cap:
        return rows
    step = len(rows) / cap
    return [rows[int(i * step)] for i in range(cap)]


@app.get("/api/equity")
def api_equity(limit: int = Query(default=500, ge=1, le=3000),
               since_id: int | None = Query(default=None, ge=0)):
    # the account curve is the paper record; NEVER fall back to all modes
    # (a paper-empty book must read empty, labeled demo_only — mixing demo
    # rows into the paper curve overstated the account). Empty returns an
    # envelope so the UI can badge demo_only; non-empty stays a bare list
    # (the chart's eq.map contract).
    try:
        rows = journal.equity_curve(limit=min(limit, 3000), mode="paper",
                                    since_id=since_id)
    except Exception as exc:
        if _is_busy_error(exc):
            raise HTTPException(503, "journal is busy — retry shortly")
        raise
    if not rows:
        return {"rows": [], "demo_only": True}
    # downsample to <=500 points for the chart (a year of sub-minute points
    # used to ship 3000 rows on every 4s poll); paged reads skip downsampling
    if since_id is None:
        rows = _downsample(rows, 500)
    return rows


@app.get("/api/trades")
def api_trades(limit: int = Query(default=100, ge=1, le=1000),
               since_id: int | None = Query(default=None, ge=0)):
    try:
        return journal.recent_trades(limit=limit, since_id=since_id)
    except Exception as exc:
        if _is_busy_error(exc):
            raise HTTPException(503, "journal is busy — retry shortly")
        raise


@app.get("/api/decisions")
def api_decisions(limit: int = Query(default=40, ge=1, le=500),
                  since_id: int | None = Query(default=None, ge=0)):
    # paper feed first; a demo-only journal (fresh seed-demo) still renders —
    # demo rows are then badged in the terminal (they are backtest replays)
    try:
        rows = journal.recent_decisions(limit=limit, mode="paper",
                                        since_id=since_id)
        if not rows and since_id is None:
            rows = journal.recent_decisions(limit=limit)
    except Exception as exc:
        if _is_busy_error(exc):
            raise HTTPException(503, "journal is busy — retry shortly")
        raise
    return rows


# ---------------------------------------------------------------------------
# evidence — the generated artifacts behind every honesty claim, read-only
def _results_dir() -> str:
    return os.path.join(os.path.dirname(CONFIG.db_path), "results")


def _ic_of(records: list, cfg) -> float | None:
    """Rolling rank-IC over `records` — the same window promoted() uses."""
    from bot.kronos_signal import KronosICTracker
    tr = KronosICTracker.__new__(KronosICTracker)
    tr.records, tr.half_life = list(records), cfg.ic_half_life
    return tr.ic()


def _veto_payload(eng) -> dict:
    """Why the book is not entering, as counts rather than log lines.

    This is the telemetry whose absence let the fast book refuse 100% of its
    entries for a week while the UI showed a healthy engine (see
    TradingEngine.veto_counts)."""
    if eng is None:
        return {"attempts": 0, "approved": 0, "by_reason": []}
    counts = dict(getattr(eng, "veto_counts", {}) or {})
    return {
        "attempts": getattr(eng, "entry_attempts", 0),
        "approved": getattr(eng, "entries_approved", 0),
        "by_reason": [{"reason": k, "count": v} for k, v in
                      sorted(counts.items(), key=lambda kv: -kv[1])],
    }


def _voting_payload(book: str) -> dict:
    """Which strategies can actually trade this book, and why the rest cannot.

    "The engine is running" and "the engine has anything to trade with" are
    different claims. The first promotion run left the fast book with ONE
    voter (two demoted on their record, one a candidate) — a book that cannot
    trade must not look identical to a quiet market."""
    try:
        from bot.promotion import voting_strategies
        return voting_strategies(book)
    except Exception as exc:
        return {"voting": [], "silent": [], "registered": 0,
                "error": f"{type(exc).__name__}: {exc}"}


def _evidence_kronos() -> dict:
    """The Kronos IC ledger as a series: rolling rank-IC (same math as
    promoted()'s gate) computed over the persisted records, so the UI can draw
    the model's evidence curve against its own promotion hurdle."""
    try:
        import pandas as pd
        from bot.kronos_signal import KronosConfig, KronosICTracker
        cfg = KronosConfig()
        # per-BOOK ledgers (the two engines used to share one file and
        # overwrite each other): read whichever exist, plus the legacy
        # single-file ledger, so the evidence curve keeps its history
        paths = [os.path.join(db_dir(), f"kronos_ic_{m}.json")
                 for m in ("paper", "hft")] + [cfg.track_file]
        recs, pending = [], 0
        for path in paths:
            if not os.path.exists(path):
                continue
            tr = KronosICTracker(path, half_life=cfg.ic_half_life)
            recs.extend(tr.records)
            pending += len(tr._pending)
        win = max(10, int(2 * cfg.ic_half_life))
        series = []
        for i in range(10, len(recs) + 1):
            sub = recs[max(0, i - win):i]
            scores = pd.Series([r[0] for r in sub])
            rets = pd.Series([r[1] for r in sub])
            c = scores.corr(rets, method="spearman")
            if c == c:
                series.append({"i": i, "ic": round(float(c), 4)})
        return {"n": len(recs), "pending": pending, "ic": _ic_of(recs, cfg),
                "hurdle": cfg.ic_hurdle, "demote_below": cfg.demote_below,
                "min_observations": cfg.min_observations, "series": series,
                "note": "records resolved before 2026-09 predate per-market keying"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _evidence_validations(limit: int = 20) -> list:
    """Newest first (by file mtime): the dropdown's default '0' used to be the
    OLDEST file by name sort, so a stale report answered as if current.
    Capped to the newest `limit` (artifact blowup: each report is parsed on
    every uncached poll)."""
    out = []
    rdir = _results_dir()
    if os.path.isdir(rdir):
        files = [f for f in os.listdir(rdir)
                 if f.startswith("validation_") and f.endswith(".json")]
        files.sort(key=lambda f: os.path.getmtime(os.path.join(rdir, f)), reverse=True)
        for f in files[:max(1, limit)]:
            try:
                with open(os.path.join(rdir, f)) as fh:
                    r = json.load(fh)
                r["_file"] = f
                out.append(r)
            except Exception:
                continue
    return out


def _evidence_shadow() -> dict | None:
    path = os.path.join(_results_dir(), "shadow_report.json")
    if os.path.exists(path):
        try:
            with open(path) as fh:
                return json.load(fh)
        except Exception:
            return None
    return None


def _evidence_manifest() -> dict:
    path = os.path.join(os.path.dirname(CONFIG.db_path), "manifest.json")
    if os.path.exists(path):
        try:
            with open(path) as fh:
                return json.load(fh)
        except Exception:
            return {}
    return {}


_EVIDENCE_CACHE: dict = {"key": None, "payload": None, "ts": 0.0}


def _evidence_cache_key() -> tuple | None:
    """Cache key: (mtime, size) of every file the payload reads. Any new
    validation report, ledger write or manifest change flips it."""
    try:
        paths = [os.path.join(os.path.dirname(CONFIG.db_path), "kronos_ic.json"),
                 os.path.join(os.path.dirname(CONFIG.db_path), "manifest.json"),
                 os.path.join(_results_dir(), "shadow_report.json")]
        rdir = _results_dir()
        if os.path.isdir(rdir):
            paths += [os.path.join(rdir, f) for f in os.listdir(rdir)
                      if f.endswith(".json")]
        return tuple(sorted((p, os.path.getmtime(p), os.path.getsize(p))
                           for p in paths if os.path.exists(p)))
    except OSError:
        return None


@app.get("/api/evidence")
def api_evidence():
    """Everything the Evidence tab renders, in one read-only payload: the
    Kronos IC ledger, generated validation reports, the shadow report, and
    the pinned-data manifest. No computation on trade data — these are the
    artifacts `main.py validate` / `main.py shadow` / fetch_history wrote.

    Cached by artifact (mtime,size): the rolling-IC series costs ~0.7s at the
    ledger cap and the tab used to recompute it on every 4s poll while open —
    12% of a core for numbers that only change when an artifact is rewritten."""
    key = _evidence_cache_key()
    now = time.time()
    if key is not None and _EVIDENCE_CACHE["key"] == key and now - _EVIDENCE_CACHE["ts"] < 60:
        return _EVIDENCE_CACHE["payload"]
    payload = {"kronos": _evidence_kronos(),
               "validations": _evidence_validations(),
               "shadow": _evidence_shadow(),
               "manifest": _evidence_manifest()}
    _EVIDENCE_CACHE.update({"key": key, "payload": payload, "ts": now})
    return payload


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
    # USD-only book: the india kind (and its INR accounting) was removed on
    # 2026-09-19, so the guard is now a straight kind check
    allowed = {"crypto", "forex"}
    if kind not in allowed:
        raise HTTPException(409, f"kind {kind!r} is not tradable "
                                 f"(allowed: {sorted(allowed)})")
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
@app.get("/api/positions")
def api_positions():
    """Alias for the open-positions block of /api/stats — the name an operator
    (or a curl sanity check on stage) guesses first; it used to 404."""
    eng = _get_engine()
    if eng is not None:
        positions, marks, _ = _live_state(eng)
        return {"live": True, "positions": [_position_dict(p, marks) for p in positions]}
    return {"live": False,
            "positions": [_journal_position_dict(t) for t in journal.open_trades()]}


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
    """Atomically adjust the account ledger and broker under lifecycle/cycle locks."""
    kind = "deposit" if direction == "deposit" else "withdrawal"
    eng = _get_engine()
    try:
        if eng is not None:
            try:
                marks = _mark_map(eng)
            except Exception:
                marks = {}
            with eng.cycle_lock, _engine_lock:
                if _engine is not eng:
                    raise HTTPException(409, "engine restarted mid-adjust — retry")
                risk = eng.risk
                fields = ("daily_start_equity", "peak_equity", "_saved_state", "persistence_error")
                snapshot = {name: getattr(risk, name) for name in fields if hasattr(risk, name)}
                try:
                    result = journal.adjust_account(
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
            with _engine_lock:
                if (_engine is not None or _engine_starting
                        or (_engine_thread is not None and _engine_thread.is_alive())):
                    raise HTTPException(409, "engine is starting or stopping — retry once settled")
                result = journal.adjust_account(amount, kind, mode="paper")
    except BookOwnedError as exc:
        # another process (a standalone CLI engine) owns this account
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except sqlite3.OperationalError as exc:
        if _is_busy_error(exc):
            raise HTTPException(503, "journal is busy — retry shortly") from exc
        raise
    return {"status": kind, "amount": round(amount, 2),
            "cash": round(result["cash"], 2), "equity": round(result["equity"], 2)}


@app.post("/api/account/deposit")
def api_account_deposit(body: AmountIn):
    return _adjust_account(body.amount, "deposit")


@app.post("/api/account/withdraw")
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


@app.post("/api/account/reset")
def api_account_reset(body: ResetIn):
    # a reset while the engine trades would fork broker state from the journal
    stop_status = api_engine_stop(EmptyIn())["status"]
    if stop_status in ("stopping", "starting"):
        # the engine thread outlived the bounded join: a wipe now could race
        # its in-flight close_trade/add_equity writes into the fresh DB
        raise HTTPException(409, "engine is still stopping — retry the reset once "
                                 "its status shows stopped")
    # Hold the lifecycle lock through reset: no new paper engine may restore
    # the old account while we replace it. Recheck the thread independently of
    # _engine, which stop clears BEFORE its last in-flight cycle finishes.
    with _engine_lock:
        if (_engine is not None or _engine_starting
                or (_engine_thread is not None and _engine_thread.is_alive())):
            raise HTTPException(409, "engine is starting or stopping — retry once stopped")
        backup = f"{journal.db_path.rsplit('.db', 1)[0]}.backup.{time.time_ns()}.db"
        try:
            journal.reset_account(round(body.capital, 2), backup, mode="paper")
        except BookOwnedError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (OSError, sqlite3.Error) as exc:
            if _is_busy_error(exc):
                raise HTTPException(503, "journal is busy — retry shortly") from exc
            raise HTTPException(500, f"reset aborted — backup/reset failed: {exc}") from exc
    _prune_reset_backups(backup)
    return {"status": "reset", "capital": round(body.capital, 2), "backup": backup}


@app.get("/api/account/transactions")
def api_account_transactions(limit: int = Query(default=100, ge=1, le=1000)):
    # paper-mode ledger only (matches api_equity/api_trades conventions)
    return journal.recent_transactions(limit=limit, mode="paper")


# ---------------------------------------------------------------------------
# chatbot
@app.get("/api/chat")
def api_chat_history():
    try:
        with journal._conn() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT ts, role, content FROM chat_log ORDER BY id DESC LIMIT 50")]
    except Exception as exc:
        if _is_busy_error(exc):
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


@app.post("/api/chat")
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
        reply = chatbot.answer(msg.message)
    except Exception as exc:
        if _is_busy_error(exc):
            raise HTTPException(503, "journal is busy — retry shortly")
        raise
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
    if result["status"] == "owned":
        raise HTTPException(409, result["detail"])
    if result["status"] == "started":
        ok = _write_engine_state(True, body.interval)
        if not ok:
            result["state_warning"] = ("engine started but desired-state "
                                       "persist failed — auto-resume disabled")
    return result


def _join_in_background(th: threading.Thread, done, interval: int, hft: bool = False):
    """Bounded join off the request path; persists stopped state on completion."""

    def _wait():
        th.join(timeout=300)
        if not th.is_alive():
            done(interval)

    t = threading.Thread(target=_wait, daemon=True)
    t.start()


@app.post("/api/engine/stop")
def api_engine_stop(body: EmptyIn):
    """Quiesce: clear the global and return immediately; the bounded join runs
    in a background thread (the endpoint used to block up to 300s, hanging
    the UI and the reset flow). The desired=stopped state is persisted
    SYNCHRONOUSLY before returning so an immediate state-file read (and a
    dashboard restart) sees the stop even while the join is still in flight."""
    global _engine, _engine_thread, _engine_interval
    with _engine_lock:
        if _engine is None:
            ok = _write_engine_state(False, CONFIG.live_interval_seconds)
            status = "starting" if _engine_starting else (
                "stopping" if _engine_thread is not None and _engine_thread.is_alive()
                else "not_running")
            result: dict = {"status": status}
            if not ok:
                result["state_warning"] = ("engine stopped but desired-state "
                                           "persist failed — auto-resume may be stale")
            return result
        _engine = None
        stop_interval = _engine_interval
    th = _engine_thread
    if th is not None and th is not threading.current_thread() and th.is_alive():
        ok = _write_engine_state(False, stop_interval)
        _join_in_background(th, lambda iv: _write_engine_state(False, iv),
                            stop_interval)
        result = {"status": "stopping"}
        if not ok:
            result["state_warning"] = ("engine stopped but desired-state "
                                       "persist failed — auto-resume may be stale")
        return result
    ok = _write_engine_state(False, stop_interval)
    result = {"status": "stopped"}
    if not ok:
        result["state_warning"] = ("engine stopped but desired-state "
                                   "persist failed — auto-resume may be stale")
    return result


def _auto_resume_engine():
    """Restart the engine when the last session left it running (the operator's
    'the bot trades autonomously' expectation survives a dashboard restart).
    A manual stop persists desired=stopped, so it always wins. Skipped under
    pytest: tests swap CONFIG.db_path to temp dirs, but the real state file
    may exist with desired=running and must never spawn a live engine there.
    ALGO_NO_AUTO_RESUME=1 disables the resume entirely (a rehearsal/demo
    machine that must NOT start trading on boot)."""
    global _AUTO_RESUMED_AT_BOOT
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    if os.environ.get("ALGO_NO_AUTO_RESUME", "") not in ("", "0", "false"):
        print("[dashboard] auto-resume disabled via ALGO_NO_AUTO_RESUME — "
              "start the engine from the top bar when you want it trading")
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
    if result["status"] == "owned":
        # a standalone CLI engine already trades this book — resuming here
        # would give one account two owners
        print(f"[dashboard] engine NOT auto-resumed — {result['detail']}")
        return
    if result["status"] == "started":
        _AUTO_RESUMED_AT_BOOT = True
        print(f"[dashboard] engine auto-resumed (interval {interval}s) — stop it "
              f"from the top bar, or set ALGO_NO_AUTO_RESUME=1 before boot")


def _auto_resume_hft_engine():
    """Same contract as the standard engine's auto-resume, for the HFT book:
    a stopped book stays stopped, a running one resumes with a toast."""
    global _HFT_AUTO_RESUMED_AT_BOOT
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    if os.environ.get("ALGO_NO_AUTO_RESUME", "") not in ("", "0", "false"):
        return
    try:
        with open(_hft_state_path()) as f:
            state = json.load(f)
        if not isinstance(state, dict) or state.get("desired") != "running":
            return
        interval = int(state.get("interval", CONFIG.hft.live_interval_seconds))
    except (OSError, ValueError, TypeError, OverflowError):
        return
    interval = max(1, min(3600, interval))
    result = _spawn_hft_engine(interval)
    if result["status"] == "owned":
        print(f"[dashboard] HFT book NOT auto-resumed — {result['detail']}")
        return
    if result["status"] == "started":
        _HFT_AUTO_RESUMED_AT_BOOT = True
        print(f"[dashboard] HFT book auto-resumed (interval {interval}s)")


@app.post("/api/engine/interval")
def api_engine_interval(body: EngineIn):
    """Retune the cycle cadence — for a RUNNING engine too.

    The interval used to be captured by the loop thread at start, so the
    dashboard's Interval select was disabled while the engine ran and the
    only way to change cadence was stop -> start (which re-probes Kronos and
    re-claims the book lease). Both loops now re-read their module global
    every cycle, so this takes effect on the next wake."""
    global _engine_interval
    _engine_interval = body.interval
    running = _get_engine() is not None
    # persist so auto-resume comes back on the NEW cadence (a stop writes the
    # then-current value, so the two never disagree)
    _write_engine_state(running, body.interval)
    return {"status": "ok", "interval": body.interval, "running": running}


@app.post("/api/hft/engine/interval")
def api_hft_engine_interval(body: HftEngineIn):
    """Retune the HFT book's cadence (1s floor), running or stopped."""
    global _hft_interval
    _hft_interval = body.interval
    running = _get_hft_engine() is not None
    _write_hft_state(running, body.interval)
    return {"status": "ok", "interval": body.interval, "running": running}


@app.get("/api/engine/status")
def api_engine_status():
    # paused = the operator's manual halt. It is shown for a STOPPED engine
    # too: the flag is a file (outlives the engine) and a fresh start must
    # visibly carry the pause, never silently resume trading.
    paused, _ = is_paused()
    eng = _get_engine()
    th = _engine_thread
    if eng is not None:
        paused = paused or bool(getattr(eng.risk, "paused", False))
        return {"running": True, "cycles": eng.cycles,
                "llm": eng.llm.provider if eng.llm.enabled else "quant",
                "positions": len(eng.broker.positions_snapshot()),
                "interval": _engine_interval,
                "alive": bool(th is not None and th.is_alive()),
                "last_error": eng.last_error or _last_engine_error,
                # degraded-but-alive conditions (e.g. a held position behind a dead
                # feed) ride here — last_error is reserved for fatal engine errors
                "health_note": getattr(eng, "health_note", None),
                # the ACTIVE market universe rides the same poll so the UI's
                # mode badge stays live (reported for a stopped engine too:
                # the mode is a file that outlives any engine run)
                "paused": paused,
                }
    return {"running": False, "cycles": 0, "llm": "quant", "positions": 0,
            # the operator's CHOSEN cadence, not the config default: reporting
            # the default snapped the UI's Interval select back after every
            # change made while the engine was stopped
            "interval": _engine_interval,
            "alive": bool(th is not None and th.is_alive()),
            "last_error": _last_engine_error, "health_note": None,
            "paused": paused,
            }


# ------------------------------------------------------------- HFT book API
# The high-frequency paper book (mode='hft'): separate engine thread, account,
# universe (1m crypto + forex), and the ONE PLACE all HFT trades live. Every
# read filters mode='hft' — the standard book's pages never show these rows.
@app.get("/api/hft/stats")
def api_hft_stats():
    h = CONFIG.hft
    stats = journal.stats(mode="hft")
    eng = _get_hft_engine()
    positions = []
    if eng is not None:
        positions, marks, price_map = _live_state(eng)
        stats["engine_running"] = True
        stats["cycles"] = eng.cycles
        stats["broker_equity"] = round(eng.broker.equity(price_map), 2)
        stats["last_error"] = eng.last_error
        stats["health_note"] = getattr(eng, "health_note", None)
    else:
        marks = {}
        stats["engine_running"] = False
        stats["cycles"] = 0
        stats["last_error"] = _last_hft_error
    stats["positions"] = [_position_dict(p, marks) for p in positions]
    stats["vetoes"] = _veto_payload(eng)
    stats["strategies"] = _voting_payload("fast")
    stats["capital"] = h.paper_capital
    from bot.hft import hft_fee_tier
    stats["fee_tier"] = hft_fee_tier()
    stats["interval"] = _hft_interval     # chosen cadence, running or not
    stats["auto_resumed"] = _HFT_AUTO_RESUMED_AT_BOOT
    stats["paused"], _ = is_paused()
    return stats


@app.get("/api/hft/equity")
def api_hft_equity():
    return journal.equity_curve(limit=2000, mode="hft")


@app.get("/api/hft/trades")
def api_hft_trades(limit: int = Query(default=1000, ge=1, le=1000)):
    """ALL high-frequency trades in one place (mode='hft' rows, newest first)."""
    return journal.recent_trades(limit=limit, mode="hft")


@app.get("/api/hft/decisions")
def api_hft_decisions(limit: int = Query(default=50, ge=1, le=200)):
    return journal.recent_decisions(limit=limit, mode="hft")


@app.get("/api/hft/candles")
def api_hft_candles(symbol: str = Query(default=""),
                    limit: int = Query(default=180, ge=20, le=1000)):
    """Recent 1m candles + EMA20 for ONE market on the HFT book.

    The HFT page only ever plotted the equity curve, which is a flat line
    until the book fills its first trade — there was no way to see whether
    the market itself was moving. This serves the same bars the engine
    decides on (shared MarketData cache), so the chart and the decision feed
    can never disagree."""
    from bot.data import MarketData
    from bot.hft import HFT_WATCHLIST
    specs = {sp.symbol: sp for sp in HFT_WATCHLIST}
    sym = symbol or next(iter(specs))
    spec = specs.get(sym)
    if spec is None:
        raise HTTPException(404, f"{sym} is not on the HFT watchlist")
    eng = _get_hft_engine()
    md = eng.market_data if eng is not None else MarketData(ttl_seconds=2.0)
    try:
        df = md.latest(spec)
    except Exception as exc:
        raise HTTPException(503, f"{sym}: {type(exc).__name__}: {exc}")
    if df is None or not len(df):
        return {"symbol": sym, "markets": list(specs), "bars": []}
    df = df.tail(limit)
    ema = df["close"].ewm(span=20, adjust=False).mean()
    bars = [{"ts": str(ts), "close": float(c), "ema20": float(e)}
            for ts, c, e in zip(df.index, df["close"], ema)]
    first, last = bars[0]["close"], bars[-1]["close"]
    return {"symbol": sym, "markets": list(specs), "bars": bars,
            "change_pct": round((last / first - 1.0) * 100.0, 3) if first else 0.0}


@app.post("/api/hft/engine/start")
def api_hft_engine_start(body: HftEngineIn):
    if not CONFIG.hft.enabled:
        raise HTTPException(409, "HFT book disabled via HFT_ENABLED=0")
    existing = _get_hft_engine()
    if existing is not None:
        return {"status": "already_running", "cycles": existing.cycles}
    result = _spawn_hft_engine(body.interval)
    if result["status"] == "owned":
        raise HTTPException(409, result["detail"])
    if result["status"] == "started":
        ok = _write_hft_state(True, body.interval)
        if not ok:
            result["state_warning"] = ("HFT engine started but desired-state "
                                       "persist failed — auto-resume disabled")
    return result


@app.post("/api/hft/engine/stop")
def api_hft_engine_stop(body: EmptyIn):
    global _hft_engine, _hft_thread, _hft_interval
    with _hft_lock:
        if _hft_engine is None:
            ok = _write_hft_state(False, CONFIG.hft.live_interval_seconds)
            result: dict = {"status": "not_running"}
            if not ok:
                result["state_warning"] = ("HFT engine stopped but desired-state "
                                           "persist failed — auto-resume may be stale")
            return result
        _hft_engine = None
        stop_interval = _hft_interval
    th = _hft_thread
    if th is not None and th is not threading.current_thread() and th.is_alive():
        ok = _write_hft_state(False, stop_interval)
        _join_in_background(th, lambda iv: _write_hft_state(False, iv),
                            stop_interval, hft=True)
        result = {"status": "stopping"}
        if not ok:
            result["state_warning"] = ("HFT engine stopped but desired-state "
                                       "persist failed — auto-resume may be stale")
        return result
    ok = _write_hft_state(False, stop_interval)
    result = {"status": "stopped"}
    if not ok:
        result["state_warning"] = ("HFT engine stopped but desired-state "
                                   "persist failed — auto-resume may be stale")
    return result


@app.get("/api/hft/engine/status")
def api_hft_engine_status():
    eng = _get_hft_engine()
    th = _hft_thread
    if eng is not None:
        return {"running": True, "cycles": eng.cycles,
                "positions": len(eng.broker.positions_snapshot()),
                "interval": _hft_interval,
                "alive": bool(th is not None and th.is_alive()),
                "last_error": eng.last_error or _last_hft_error,
                "health_note": getattr(eng, "health_note", None),
                "paused": is_paused()[0]}
    return {"running": False, "cycles": 0, "positions": 0,
            "interval": CONFIG.hft.live_interval_seconds,
            "alive": bool(th is not None and th.is_alive()),
            "last_error": _last_hft_error, "health_note": None,
            "paused": is_paused()[0]}


# ------------------------------------------------------------- Strategy Lab
# Pick any stock/pair, apply the strategies registered for it, backtest —
# in BOTH books (standard forex+crypto+NSE and the HFT book). Pure backtest:
# own broker/risk per run, zero journal writes, zero engine interference.
class LabIn(BaseModel):
    book: str = Field(default="standard", max_length=16)
    kind: str = Field(default="crypto", max_length=16)
    symbol: str = Field(min_length=1, max_length=24)
    timeframe: str = Field(default="1h", max_length=8)
    strategy: str = Field(default="ensemble", max_length=32)
    days: int = Field(default=0, ge=0, le=3650)
    start: str | None = Field(default=None, max_length=10)
    end: str | None = Field(default=None, max_length=10)
    fee_tier: str | None = Field(default=None, max_length=8)


@app.post("/api/lab/run")
def api_lab_run(body: LabIn):
    from bot import lab
    try:
        return lab.start_lab_run(body.model_dump())
    except lab.LabError as exc:
        raise HTTPException(422, str(exc))


@app.get("/api/lab/status")
def api_lab_status():
    from bot import lab
    return lab.lab_status()


@app.get("/api/lab/meta")
def api_lab_meta():
    """One source of truth for the Lab form: suggestions, the timeframes
    offered per book+kind, and the strategies REGISTERED per book+timeframe
    (derived from the registry, never hand-maintained)."""
    from bot import lab
    books = ("standard", "hft")
    kinds = ("crypto", "forex")
    tfs_all = sorted(set(lab._STANDARD_TFS) | set(lab._HFT_TFS))
    return {
        "suggestions": lab.SUGGESTIONS,
        "timeframes": {b: {k: lab.timeframes_for(b, k) for k in kinds} for b in books},
        "strategies": {b: {tf: lab.strategies_for(b, tf) for tf in tfs_all} for b in books},
        "days_default": {b: {k: {tf: lab.default_days(k, tf) for tf in tfs_all}
                             for k in kinds} for b in books},
        "days_cap": {b: {k: {tf: lab.days_cap(k, tf) for tf in tfs_all}
                         for k in kinds} for b in books},
    }


@app.post("/api/trading/pause")
def api_trading_pause(body: PauseIn):
    """Set the manual pause. The file write persists the halt for a future
    engine/dashboard restart; when an engine IS live its risk.paused is set
    in the same call so the halt takes effect before the next cycle (a new
    entry could otherwise slip in during the interval). Entries only — open
    positions keep their stops/targets/exits; nothing is force-closed."""
    if not set_paused(True, body.note):
        raise HTTPException(500, "could not write the pause flag (disk error?) — "
                               "trading is NOT paused")
    eng = _get_engine()
    if eng is not None:
        eng.risk.paused = True
    return {"status": "paused",
            "note": body.note,
            "semantics": "blocks new entries only — open positions are still "
                         "managed (stops, targets, strategy exits). Nothing is "
                         "force-closed."}


@app.post("/api/trading/resume")
def api_trading_resume(body: EmptyIn):
    """Clear the manual pause (new entries allowed again; every other risk
    gate still applies). Resuming WRITES paused:false rather than deleting
    the flag — a visible record beats an absence that reads as 'never paused'."""
    if not set_paused(False):
        raise HTTPException(500, "could not write the pause flag (disk error?) — "
                               "trading is still paused")
    eng = _get_engine()
    if eng is not None:
        eng.risk.paused = False
    return {"status": "resumed"}


# ---------------------------------------------------------------------------
def _open_position_dicts(eng: TradingEngine | None) -> list[dict]:
    """Every open position the switch could orphan, from BOTH holders:
    the live engine's broker (engine-on case) and the journal's OPEN rows
    (engine-off case — a stale engine process could still hold rows the
    dashboard would otherwise miss). Live and journal views are merged and
    de-duplicated on (symbol, timeframe): a position the engine holds is
    also a journal row; only a journal row the engine lost (crash window)
    shows as journal-only."""
    live: dict[tuple[str, str], dict] = {}
    if eng is not None:
        # positions_snapshot + the caller's engine identity check keep this
        # race-free against the engine's own open/close mutations
        for p in eng.broker.positions_snapshot():
            live[(p.symbol, p.timeframe)] = {
                "symbol": p.symbol, "timeframe": p.timeframe, "side": p.side,
                "qty": p.qty, "entry": p.entry_price, "held_in": "engine",
                "kind": infer_kind(p.symbol)}
    journal_only: dict[tuple[str, str], dict] = {}
    for t in journal.open_trades():
        tf = t.get("timeframe") or _LEGACY_TF
        key = (t["symbol"], tf)
        if key in live:
            continue
        journal_only[key] = {"symbol": t["symbol"], "timeframe": tf,
                             "side": t["side"], "qty": t["qty"],
                             "entry": t["entry_price"], "held_in": "journal",
                             "kind": infer_kind(t["symbol"])}
    return list(live.values()) + list(journal_only.values())


def _close_all_open_positions(eng: TradingEngine | None) -> list[dict]:
    """Force-close every open paper position at its last mark. Returns the
    per-position close results.

    LIVE ENGINE: each position goes through eng.close_manual — the engine's
    OWN sanctioned close path (broker fill with fees/slippage + journal row
    + risk cooldown, under cycle_lock, identical to the Portfolio tab's
    close button and to an engine exit).

    NO ENGINE but OPEN JOURNAL ROWS (stale engine process / crash window):
    the rows are closed DIRECTLY in the journal at their entry price — the
    last mark the engine-off dashboard has (no data feed is running to
    produce a fresher one). This is the same restore-anchor discipline the
    engine itself uses on restart (journal.last_equity_point /
    closed_cash_delta_since): the next engine start restores cash from the
    journal's equity points and reconciles trades closed after the anchor,
    so the close's cash effect is picked up exactly once — closing at entry
    marks a round-trip P&L of just the fee estimate, the conservative floor
    for a price we cannot honestly know offline."""
    closed = []
    if eng is not None:
        for key in sorted({(p.symbol, p.timeframe)
                           for p in eng.broker.positions_snapshot()}):
            try:
                result = eng.close_manual(key[0], key[1])
                closed.append({**result, "symbol": key[0], "timeframe": key[1],
                               "path": "engine close_manual"})
            except (KeyError, RuntimeError) as exc:
                raise HTTPException(
                    503, f"could not close {key[0]} {key[1]} ({exc}) — the "
                         f"market is NOT switched; retry, or close it from "
                         f"the Portfolio tab first")
        return closed
    rows = journal.open_trades()
    # "no engine" means no engine in THIS process. A standalone CLI engine in
    # another process still holds these positions in its broker, and closing
    # its rows underneath it would fork the two. Checked up front for a clean
    # message, and again inside each close's own transaction (NO_OWNER below)
    # so an engine claiming the book in between cannot slip through.
    for owned in {t.get("mode") or "paper" for t in rows}:
        owner = journal.book_owner(owned)
        if owner is not None:
            raise HTTPException(
                409, f"the {owned} book is owned by pid {owner['pid']} on "
                     f"{owner['host']} — the market is NOT switched; stop that "
                     f"engine first")
    for t in rows:
        tf = t.get("timeframe") or _LEGACY_TF
        entry = float(t["entry_price"])
        qty = float(t["qty"])
        kind = infer_kind(t["symbol"])
        rate = CONFIG.costs.fee(kind, side=None)   # conservative max per leg
        notional = entry * qty
        fees = round(2 * notional * rate, 4)       # entry + exit leg estimate
        direction = 1.0 if t["side"] == "long" else -1.0
        # exit at the entry mark: zero gross P&L, the full fee cost — the
        # honest floor when no live feed is available to price the exit
        pnl = round(-fees, 2)
        pnl_pct = round(-fees / notional * 100.0 * direction, 3) if notional else 0.0
        journal.close_trade(trade_id=t["id"], exit_price=entry, pnl=pnl,
                            pnl_pct=pnl_pct, fees=fees,
                            exit_reason="market switch (no live marks)",
                            rationale_close="closed by the market switch: no "
                                            "engine was running to fetch a "
                                            "live mark",
                            mode=t.get("mode") or "paper",
                            entry_fee=round(notional * rate, 6),
                            owner_token=NO_OWNER)
        closed.append({"symbol": t["symbol"], "timeframe": tf,
                       "exit_price": entry, "path": "journal close at entry mark"})
    return closed


# The page itself lives in bot/static/ (index.html + app.css + app.js).
# It was a 2,700-line triple-quoted string in this module until 2026-09-19:
# HTML, CSS and JavaScript with no syntax highlighting, no linting and no way
# to diff a UI change apart from an API change. Every UI bug in this repo's
# history was written in that string. The files are served below.
_STATIC = os.path.join(os.path.dirname(__file__), "static")


def static_path(name: str) -> str:
    """Path to a served asset. Tests read the SAME files the app serves, so a
    UI invariant cannot pass against a stale copy."""
    return os.path.join(_STATIC, name)
