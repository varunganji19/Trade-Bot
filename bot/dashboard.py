"""
FastAPI dashboard + JSON API — a single-page app served from bot/static/
(index.html + app.css + app.js). Chart.js is VENDORED at bot/chart.umd.min.js
and no web fonts are fetched, so the page makes no outbound requests at all.

Tabs (hash routing, ~4s polling):
  #overview   — equity curve, headline stats, engine controls, decision feed,
                per-strategy PnL bars
  #portfolio  — open positions (live marks, manual close) + trade history
  #hft        — the SEPARATE high-frequency paper book: its own engine
                controls, equity curve, ALL HFT trades in one place, decision
                feed for the fast book (mode='hft')
  #watchlist  — full CRUD of what the bot trades (persisted data/watchlist.json;
                hot-reloads into a RUNNING engine's CONFIG)
  #lab        — Strategy Lab: pick ANY crypto/forex pair (aliases
                normalized), apply the strategies registered for it,
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
import threading
import time
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response as FastAPIResponse, FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from bot.chatbot import ChatBot
from bot.engine import TradingEngine
from bot.journal import NO_OWNER, BookOwnedError, Journal
from bot.strategies import STRATEGY_CLASSES
from config import (CONFIG, MarketSpec, VALID_KINDS,
                    VALID_TIMEFRAMES, apply_saved_watchlist, save_watchlist, infer_kind)


@asynccontextmanager
async def _lifespan(_app):
    # replaces the deprecated @app.on_event("startup") hook (which emitted
    # deprecation warnings on every boot and test run); the body lives below
    # the handlers it calls and resolves at startup time
    # a failed auto-resume must never abort uvicorn's startup: without the UI
    # there is nothing left to explain the failure with
    if os.environ.get("ALGO_NO_AUTO_RESUME", "") not in ("", "0", "false"):
        print("[dashboard] auto-resume disabled via ALGO_NO_AUTO_RESUME — "
              "start the engines from the dashboard when you want them trading")
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
    _EXEMPT_GET = frozenset({"/", "/chart.umd.min.js", "/app.css", "/app.js"})

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
# Several strategies may share a timeframe (1h: turtle, ts_momentum,
# fx_regime_meanrev), so the badge lists all of them, never an arbitrary one.
STRATEGIES_BY_TF: dict[str, list[str]] = {}
for _name, _cls in sorted(STRATEGY_CLASSES.items()):
    if getattr(_cls, "book", "standard") == "standard":
        for _tf in _cls.preferred_timeframes:
            STRATEGIES_BY_TF.setdefault(_tf, []).append(_name)

apply_saved_watchlist()  # data/watchlist.json → CONFIG.watchlist (creates file on first boot)


from bot.api.models import (AmountIn, ChatIn, EmptyIn, EngineIn,  # noqa: E402,F401
                            HftEngineIn, PauseIn, PositionCloseIn, ResetIn,
                            WatchlistIn)


# --------------------------------------------------------------- engine state
def _engine_state_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(CONFIG.db_path)),
                        "engine_state.json")


def _write_state_file(path: str, running: bool, interval: int) -> bool:
    """Persist a book's desired engine state so a dashboard restart can
    auto-resume it (a stop must win over a stale 'running' file). Returns
    False (and logs loudly) when the write fails so endpoints can surface it
    instead of silently losing auto-resume."""
    try:
        # atomic: a torn state file would silently disable auto-resume
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"desired": "running" if running else "stopped",
                       "interval": interval}, f)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        print(f"[dashboard] FAILED to persist {os.path.basename(path)} (desired="
              f"{'running' if running else 'stopped'}): {exc}")
        traceback.print_exc()
        return False


def _read_resume_interval(path: str, default: int, floor: int) -> int | None:
    """The interval to auto-resume a book with, or None when it must stay
    stopped: auto-resume disabled, the last session stopped it, or the state
    file is missing/unreadable. Skipped under pytest: tests swap
    CONFIG.db_path to temp dirs, but the real state file may say 'running'
    and must never spawn a live engine there."""
    if "PYTEST_CURRENT_TEST" in os.environ:
        return None
    if os.environ.get("ALGO_NO_AUTO_RESUME", "") not in ("", "0", "false"):
        return None
    try:
        with open(path) as f:
            state = json.load(f)
        if not isinstance(state, dict) or state.get("desired") != "running":
            return None
        return max(floor, min(3600, int(state.get("interval", default))))
    except (OSError, ValueError, TypeError, OverflowError):
        return None


def _state_warning(result: dict, ok: bool, what: str) -> dict:
    if not ok:
        result["state_warning"] = (f"{what} but desired-state persist failed — "
                                   f"auto-resume may be stale")
    return result


def _write_engine_state(running: bool, interval: int) -> bool:
    return _write_state_file(_engine_state_path(), running, interval)


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
    return _write_state_file(_hft_state_path(), running, interval)


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



def _is_busy_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


# ---------------------------------------------------------------------------
# per-poll payload helpers shared by both books' stats endpoints
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


def _ledger_payload(mode: str) -> dict:
    try:
        return journal.ledger_check(mode)
    except Exception as exc:          # a broken check must not break the poll
        return {"ok": True, "error": f"{type(exc).__name__}: {exc}"}


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
        return {"voting": [], "voters": [], "silent": [], "registered": 0, "gate": {},
                "error": f"{type(exc).__name__}: {exc}"}




# ---------------------------------------------------------------------------
# engine control
def _join_in_background(th: threading.Thread, done, interval: int):
    """Bounded join off the request path; persists stopped state on completion."""

    def _wait():
        th.join(timeout=300)
        if not th.is_alive():
            done(interval)

    t = threading.Thread(target=_wait, daemon=True)
    t.start()


def _auto_resume_engine():
    """Restart the engine when the last session left it running (the operator's
    'the bot trades autonomously' expectation survives a dashboard restart).
    A manual stop persists desired=stopped, so it always wins. Skipped under
    pytest: tests swap CONFIG.db_path to temp dirs, but the real state file
    may exist with desired=running and must never spawn a live engine there.
    ALGO_NO_AUTO_RESUME=1 disables the resume entirely (a rehearsal/demo
    machine that must NOT start trading on boot)."""
    global _AUTO_RESUMED_AT_BOOT
    interval = _read_resume_interval(_engine_state_path(),
                                     CONFIG.live_interval_seconds, floor=5)
    if interval is None:
        return
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
    interval = _read_resume_interval(_hft_state_path(),
                                     CONFIG.hft.live_interval_seconds, floor=1)
    if interval is None:
        return
    result = _spawn_hft_engine(interval)
    if result["status"] == "owned":
        print(f"[dashboard] fast book NOT auto-resumed — {result['detail']}")
        return
    if result["status"] == "started":
        _HFT_AUTO_RESUMED_AT_BOOT = True
        print(f"[dashboard] fast book auto-resumed (interval {interval}s)")


# ---------------------------------------------------------------------------
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


# Routers live in bot/api/ and read the shared state above from this module
# at call time. Imported last because they import this module.
from bot.api import account as _account_api, evidence as _evidence_api  # noqa: E402
from bot.api import fast as _fast_api, lab as _lab_api, standard as _standard_api  # noqa: E402

app.include_router(_evidence_api.router)
app.include_router(_account_api.router)
app.include_router(_lab_api.router)
app.include_router(_fast_api.router)
app.include_router(_standard_api.router)
# names other code and the tests reach through this module
from bot.api.account import _adjust_account, api_account_reset  # noqa: E402,F401
from bot.api.standard import api_engine_stop  # noqa: E402,F401
