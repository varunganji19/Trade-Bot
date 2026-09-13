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
                feed incl. the TRI-ETH triangular-arb monitor (mode='hft')
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
     /api/market/mode (the ACTIVE market universe: forex | india — one at a
     time, never both; see config.MARKET_MODE)
     POST /api/chat {message}  /api/engine/start {interval}  /api/engine/stop
          /api/watchlist {kind,symbol,timeframe,display?}
          /api/account/deposit {amount}  /api/account/withdraw {amount}
          /api/account/reset {capital}
          /api/trading/pause {note?}  /api/trading/resume {}   (manual halt)
          /api/market/mode {mode,confirm_close_positions?}     (forex ↔ india)
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
from contextlib import asynccontextmanager
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response as FastAPIResponse, FileResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from bot.chatbot import ChatBot
from bot.engine import TradingEngine
from bot.journal import Journal
from bot.pause import is_paused, set_paused
from bot.strategies import STRATEGY_CLASSES
import config as config_mod
from config import (CONFIG, MarketSpec, VALID_KINDS,
                    VALID_TIMEFRAMES, MAX_WATCHLIST_SPECS,
                    apply_saved_watchlist, save_watchlist, infer_kind)


@asynccontextmanager
async def _lifespan(_app):
    # replaces the deprecated @app.on_event("startup") hook (which emitted
    # deprecation warnings on every boot and test run); the body lives below
    # the handlers it calls and resolves at startup time
    _auto_resume_engine()
    _auto_resume_hft_engine()
    yield


app = FastAPI(title="AI Trading Bot Dashboard", version="2.1", lifespan=_lifespan)
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
    _EXEMPT_GET = frozenset({"/", "/chart.umd.min.js"})

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
_engine_interval: int = 60              # the running engine's cycle interval (stop persists it)
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


class PauseIn(BaseModel):
    """Manual pause/resume body: optional human note. The body-less variant
    is a plain {} like every other mutating POST (the JSON content type forces
    the CORS preflight that defeats form-encoded CSRF)."""
    note: str = Field(default="", max_length=200)


class EmptyIn(BaseModel):
    """Body-required marker for POSTs that take no fields: a JSON body forces
    the CORS preflight that defeats form-encoded CSRF (same rule every other
    mutating endpoint already follows)."""


class MarketModeIn(BaseModel):
    """Market-universe switch body. `mode` is the target universe; the
    optional confirm flag is the ONLY way past the orphan guard (an
    explicit, second-click "yes, close my open paper positions" — the UI
    never sends it on the first click)."""
    mode: str
    confirm_close_positions: bool = False


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
    # build OUTSIDE _engine_lock: TradingEngine.__init__ probes the Kronos stack
    # (imports, no weight load — that happens lazily in the first engine cycle)
    # and holding the lock froze every stats/status poll
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
            cycle_t0 = _t.monotonic()
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
            # is near-instant instead of stranding the UI for up to interval-300s
            remaining = max(0.0, interval - (_t.monotonic() - cycle_t0))
            deadline = _t.monotonic() + remaining
            while _t.monotonic() < deadline:
                if _get_engine() is not eng_ref:
                    return
                _t.sleep(min(1.0, max(0.0, deadline - _t.monotonic())))
            if _get_engine() is not eng_ref:
                break

    _engine_thread = threading.Thread(target=_loop, args=(eng, interval), daemon=True)
    _engine_thread.start()
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
_hft_interval: int = 20
_HFT_AUTO_RESUMED_AT_BOOT = False


def _hft_state_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(CONFIG.db_path)),
                        "hft_engine_state.json")


def _write_hft_state(running: bool, interval: int):
    try:
        tmp = _hft_state_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"desired": "running" if running else "stopped",
                       "interval": interval}, f)
        os.replace(tmp, _hft_state_path())
    except OSError:
        pass


def _spawn_hft_engine(interval: int) -> dict:
    """Build + start the HFT engine thread (mirrors _spawn_engine)."""
    global _hft_engine, _hft_thread, _hft_interval
    from bot.hft import build_hft_engine
    eng = build_hft_engine(journal=journal, quiet=False)
    with _hft_lock:
        if _hft_engine is not None:
            return {"status": "already_running", "cycles": _hft_engine.cycles}
        if _hft_thread is not None and _hft_thread.is_alive():
            return {"status": "stopping", "cycles": 0}
        _hft_engine = eng
        _hft_interval = interval

    def _hft_loop(eng_ref, interval):
        global _hft_engine, _last_hft_error
        import time as _t
        while _get_hft_engine() is eng_ref:
            cycle_t0 = _t.monotonic()
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
            remaining = max(0.0, interval - (_t.monotonic() - cycle_t0))
            deadline = _t.monotonic() + remaining
            while _t.monotonic() < deadline:
                if _get_hft_engine() is not eng_ref:
                    return
                _t.sleep(min(1.0, max(0.0, deadline - _t.monotonic())))
            if _get_hft_engine() is not eng_ref:
                break

    _hft_thread = threading.Thread(target=_hft_loop, args=(eng, interval), daemon=True)
    _hft_thread.start()
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
    stats["cycles"] = eng.cycles if eng is not None else 0
    stats["watchlist_count"] = len(CONFIG.watchlist)
    # manual pause rides the same poll as health_note (the banner + button
    # must flip within one 4s tick, without a second request). Reported for a
    # stopped engine too — the flag file outlives any single engine run.
    paused, pause_note = is_paused()
    if eng is not None:
        paused = paused or bool(getattr(eng.risk, "paused", False))
    stats["paused"] = paused
    stats["paused_note"] = pause_note
    # the active market universe rides the same poll as paused (W1's pattern):
    # the mode badge/banner must flip within one 4s tick, without a second
    # request. Reported for a stopped engine too — the mode file outlives any
    # single engine run, so a restart can never silently flip markets.
    stats["market_mode"] = config_mod.get_market_mode()
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


@app.get("/api/equity")
def api_equity():
    # the account curve is the paper record; a demo-only journal (fresh
    # seed-demo) falls back to all rows so the chart still renders, labeled
    # by the overview demo note
    rows = journal.equity_curve(limit=3000, mode="paper")
    if not rows:
        rows = journal.equity_curve(limit=3000)
    return rows


@app.get("/api/trades")
def api_trades(limit: int = Query(default=100, ge=1, le=1000)):
    return journal.recent_trades(limit=limit)


@app.get("/api/decisions")
def api_decisions(limit: int = Query(default=40, ge=1, le=500)):
    # paper feed first; a demo-only journal (fresh seed-demo) still renders —
    # demo rows are then badged in the terminal (they are backtest replays)
    rows = journal.recent_decisions(limit=limit, mode="paper")
    if not rows:
        rows = journal.recent_decisions(limit=limit)
    return rows


# ---------------------------------------------------------------------------
# evidence — the generated artifacts behind every honesty claim, read-only
def _results_dir() -> str:
    return os.path.join(os.path.dirname(CONFIG.db_path), "results")


def _evidence_kronos() -> dict:
    """The Kronos IC ledger as a series: rolling rank-IC (same math as
    promoted()'s gate) computed over the persisted records, so the UI can draw
    the model's evidence curve against its own promotion hurdle."""
    try:
        import pandas as pd
        from bot.kronos_signal import KronosConfig, KronosICTracker
        cfg = KronosConfig()
        tr = KronosICTracker(cfg.track_file, half_life=cfg.ic_half_life)
        recs = tr.records
        win = max(10, int(2 * cfg.ic_half_life))
        series = []
        for i in range(10, len(recs) + 1):
            sub = recs[max(0, i - win):i]
            scores = pd.Series([r[0] for r in sub])
            rets = pd.Series([r[1] for r in sub])
            c = scores.corr(rets, method="spearman")
            if c == c:
                series.append({"i": i, "ic": round(float(c), 4)})
        return {"n": len(recs), "pending": len(tr._pending), "ic": tr.ic(),
                "hurdle": cfg.ic_hurdle, "demote_below": cfg.demote_below,
                "min_observations": cfg.min_observations, "series": series,
                "note": "records resolved before 2026-09 predate per-market keying"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _evidence_validations() -> list:
    """Newest first (by file mtime): the dropdown's default '0' used to be the
    OLDEST file by name sort, so a stale report answered as if current."""
    out = []
    rdir = _results_dir()
    if os.path.isdir(rdir):
        files = [f for f in os.listdir(rdir)
                 if f.startswith("validation_") and f.endswith(".json")]
        files.sort(key=lambda f: os.path.getmtime(os.path.join(rdir, f)), reverse=True)
        for f in files:
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
    if stop_status == "stopping":
        # the engine thread outlived the bounded join: a wipe now could race
        # its in-flight close_trade/add_equity writes into the fresh DB
        raise HTTPException(409, "engine is still stopping — retry the reset once "
                                 "its status shows stopped")
    # WAL checkpoint BEFORE the copy: copy2 of the main db file alone can miss
    # everything still living in the -wal (proven in testing: the copy was
    # missing even the schema). TRUNCATE checkpoints and resets the WAL, so the
    # backup is a complete, self-contained database.
    try:
        with journal._conn() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass   # best-effort; the copy below still runs either way
    # name with microseconds: two resets in the same second used to overwrite
    # the first backup (int(time.time()) collides on scripted double-clicks)
    backup = f"{CONFIG.db_path.rsplit('.db', 1)[0]}.backup.{time.time():.6f}.db"
    try:
        shutil.copy2(CONFIG.db_path, backup)
    except OSError as exc:
        # the wipe must NEVER proceed without the verified backup it promises
        raise HTTPException(500, f"reset aborted — backup failed: {exc}")
    if not os.path.exists(backup) or os.path.getsize(backup) == 0:
        os.path.exists(backup) and os.remove(backup)
        raise HTTPException(500, "reset aborted — backup file is empty")
    _prune_reset_backups(backup)
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
              "start the engine from the UI when you want it trading")
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
        _AUTO_RESUMED_AT_BOOT = True
        print(f"[dashboard] engine auto-resumed (interval {interval}s) — stop it "
              f"from the Overview tab, or set ALGO_NO_AUTO_RESUME=1 before boot")


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
    interval = max(5, min(3600, interval))
    result = _spawn_hft_engine(interval)
    if result["status"] == "started":
        _HFT_AUTO_RESUMED_AT_BOOT = True
        print(f"[dashboard] HFT book auto-resumed (interval {interval}s)")


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
                "interval": CONFIG.live_interval_seconds,
                "alive": bool(th is not None and th.is_alive()),
                "last_error": eng.last_error or _last_engine_error,
                # degraded-but-alive conditions (e.g. a held position behind a dead
                # feed) ride here — last_error is reserved for fatal engine errors
                "health_note": getattr(eng, "health_note", None),
                # the ACTIVE market universe rides the same poll so the UI's
                # mode badge stays live (reported for a stopped engine too:
                # the mode is a file that outlives any engine run)
                "paused": paused,
                "market_mode": config_mod.get_market_mode()}
    return {"running": False, "cycles": 0, "llm": "quant", "positions": 0,
            "interval": CONFIG.live_interval_seconds,
            "alive": bool(th is not None and th.is_alive()),
            "last_error": _last_engine_error, "health_note": None,
            "paused": paused,
            "market_mode": config_mod.get_market_mode()}


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
    stats["capital"] = h.paper_capital
    from bot.hft import hft_fee_tier
    stats["fee_tier"] = hft_fee_tier()
    stats["interval"] = _hft_interval if eng is not None else h.live_interval_seconds
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


@app.post("/api/hft/engine/start")
def api_hft_engine_start(body: EngineIn):
    if not CONFIG.hft.enabled:
        raise HTTPException(409, "HFT book disabled via HFT_ENABLED=0")
    existing = _get_hft_engine()
    if existing is not None:
        return {"status": "already_running", "cycles": existing.cycles}
    result = _spawn_hft_engine(body.interval)
    if result["status"] == "started":
        _write_hft_state(True, body.interval)
    return result


@app.post("/api/hft/engine/stop")
def api_hft_engine_stop(body: EmptyIn):
    global _hft_engine, _hft_thread, _hft_interval
    with _hft_lock:
        if _hft_engine is None:
            _write_hft_state(False, CONFIG.hft.live_interval_seconds)
            return {"status": "not_running"}
        _hft_engine = None
    th = _hft_thread
    if th is not None and th is not threading.current_thread():
        th.join(timeout=300)
        if th.is_alive():
            return {"status": "stopping"}
    _write_hft_state(False, _hft_interval)
    return {"status": "stopped"}


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
    kinds = ("crypto", "forex", "india")
    tfs_all = sorted(set(lab._STANDARD_TFS) | set(lab._HFT_TFS))
    return {
        "suggestions": lab.SUGGESTIONS,
        "timeframes": {b: {k: lab.timeframes_for(b, k) for k in kinds} for b in books},
        "strategies": {b: {tf: lab.strategies_for(b, tf) for tf in tfs_all} for b in books},
        "days_default": {b: {k: {tf: lab.default_days(b, k, tf) for tf in tfs_all}
                             for k in kinds} for b in books},
        "days_cap": {b: {k: {tf: lab.days_cap(b, k, tf) for tf in tfs_all}
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
# market mode — the forex ↔ india universe toggle (ONE active book at a time;
# never both: single-currency accounting, USD vs INR books cannot mix)
_MARKET_MODES = ("forex", "india")


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
    for t in journal.open_trades():
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
                            entry_fee=round(notional * rate, 6))
        closed.append({"symbol": t["symbol"], "timeframe": tf,
                       "exit_price": entry, "path": "journal close at entry mark"})
    return closed


@app.get("/api/market/mode")
def api_market_mode_get():
    """The ACTIVE market universe: 'forex' (crypto + forex, the historical
    default) or 'india' (NSE cash equities + the Nifty 50 index). One is
    active at a time — never both (single-currency accounting)."""
    mode = config_mod.get_market_mode()
    return {"mode": mode,
            "specs": [s.to_dict() for s in config_mod.active_specs(mode)]}


@app.post("/api/market/mode")
def api_market_mode_set(body: MarketModeIn):
    """Switch the active market universe and rewrite data/watchlist.json in
    lockstep (the mode is the single source of truth — see config.py).

    THE ORPHAN GUARD: switching while paper positions are open would drop
    their markets' feeds out of the watchlist — unpriced, unmanaged zombie
    books. Both holders are checked (live engine positions AND the
    journal's OPEN rows — a stale engine process could still hold rows).
    With open positions the switch is refused with 409 +
    requires_confirm:true; ONLY an explicit confirm_close_positions=true
    proceeds, and then every position is force-closed FIRST (at its last
    mark, via the engine's own close path when one is live), and only then
    does the mode flip. If anything fails mid-close the mode is NOT
    switched — a half-closed book is recoverable, an orphaned one is not."""
    mode = body.mode.strip().lower()
    if mode not in _MARKET_MODES:
        raise HTTPException(422, f"mode must be one of {list(_MARKET_MODES)} "
                                 f"(got {body.mode!r})")
    if mode == config_mod.get_market_mode():
        specs = config_mod.active_specs(mode)
        return {"status": "unchanged", "mode": mode, "closed": 0,
                "specs": [s.to_dict() for s in specs]}
    eng = _get_engine()
    open_positions = _open_position_dicts(eng)
    if open_positions and not body.confirm_close_positions:
        raise HTTPException(
            409,
            {"detail": f"{len(open_positions)} open paper position(s) would be "
                       f"orphaned by the switch — confirm to close them at "
                       f"their last prices, or close them yourself first",
             "open_positions": open_positions,
             "requires_confirm": True})
    closed = []
    if open_positions:
        # verify the engine identity too: a stop racing this handler could
        # have cleared it between _get_engine() and the close (a fast
        # stop/start must never patch a NEW engine with the OLD one's rows)
        if eng is not None and _get_engine() is not eng:
            raise HTTPException(409, "engine changed state mid-switch — retry")
        closed = _close_all_open_positions(eng)
        # the close must have actually emptied the books (a concurrent entry
        # could have slipped in under the cycle lock we do NOT hold here)
        if _open_position_dicts(_get_engine()):
            raise HTTPException(
                409, "positions were opened while the switch was closing — "
                     "the market is NOT switched; retry the switch")
    if not config_mod.set_market_mode(mode):
        raise HTTPException(500, f"could not persist market mode {mode!r} "
                                 "(disk error?) — the market is NOT switched")
    # hot-install the new universe into CONFIG so a RUNNING engine's next
    # cycle trades the new book (apply_saved_watchlist mutates
    # CONFIG.watchlist in place — the same rule the watchlist CRUD uses)
    with _wl_lock:
        config_mod.apply_saved_watchlist()
    return {"status": "switched", "mode": mode, "closed": len(closed),
            "closes": closed,
            "specs": [s.to_dict() for s in config_mod.active_specs(mode)]}


# ===========================================================================
# UI — module-level HTML constant (single-page app, no build tooling)
# ===========================================================================
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#F6F8FB">
<title>Algo Trading Bot — Dashboard</title>
<script>
/* theme boot — runs before first paint so a saved theme never flashes.
   light (white-blue/green) is the default; first-time visitors follow the
   OS preference. values: light · dark (grayish) · black (AMOLED). */
(function(){var t='light';
try{t=localStorage.getItem('algo-theme')||t;
if(t!=='light'&&t!=='dark'&&t!=='black')
  t=window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';
}catch(e){t='light';}
document.documentElement.setAttribute('data-theme',t);})();
</script>
<script src="/chart.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap');

/* ============================================================= themes
   three full palettes, swapped by [data-theme] on <html> (set before
   first paint by the boot script in <head>):
     light (default) — white-blue/green: blue = interactive chrome
       (tabs, buttons, focus), green = money-in/success/engine, red =
       money-out/danger. Deposit keeps .btn-success green and withdraw
       keeps .btn-danger red in ALL themes.
     dark  — grayish slate finish. black — AMOLED true #000.
   Components below only ever consume tokens. */
:root {
  color-scheme: light;
  --color-background:#F6F8FB; --color-foreground:#0F172A;
  --color-card:#FFFFFF; --color-card-foreground:#0F172A;
  --color-muted:#EDF1F6; --color-muted-foreground:#5B6B80;
  --color-border:#DFE5EC;
  --color-primary:#2563EB; --color-on-primary:#FFFFFF; --color-primary-hover:#1D4ED8;
  --color-accent:#15803D; --color-on-accent:#FFFFFF; --color-accent-hover:#166534;
  --color-secondary:#2563EB; --color-on-secondary:#FFFFFF;
  --color-destructive:#DC2626; --color-on-destructive:#FFFFFF;
  --color-destructive-hover:#B91C1C;
  --color-pos:#15803D; --color-neg:#DC2626; --color-blue:#2563EB;
  --color-ring:#2563EB;
  --ring-soft:rgba(37,99,235,.16);
  --pos-soft:rgba(21,128,61,.11); --neg-soft:rgba(220,38,38,.10);
  --blue-soft:rgba(37,99,235,.11); --hold-soft:rgba(91,107,128,.12);
  /* amber = caution states (manual pause banner): distinct from red=danger
     (loss/damage) and green=pos — the pause is a deliberate operator state,
     not an error */
  --amber:#B45309; --amber-soft:rgba(245,158,11,.15);
  --row-hover:rgba(37,99,235,.045);
  --hover-border:rgba(37,99,235,.38);
  --header-bg:rgba(255,255,255,.86);
  --overlay:rgba(15,23,42,.45);
  --glow-pos:rgba(21,128,61,.30);
  --shimmer:rgba(91,107,128,.16);
  --shadow-sm:0 1px 2px rgba(15,23,42,.05);
  --shadow-md:0 1px 2px rgba(15,23,42,.05),0 4px 14px -4px rgba(15,23,42,.10);
  --shadow-lg:0 12px 28px -8px rgba(15,23,42,.16);
  --shadow-xl:0 24px 56px -16px rgba(15,23,42,.24);
  --hero-grad:radial-gradient(90% 140% at 88% -20%,rgba(37,99,235,.09),transparent 55%),
              radial-gradient(90% 140% at 8% -30%,rgba(21,128,61,.11),transparent 52%);
  --grad-pos:linear-gradient(90deg,#15803D,#22C55E);
  --grad-neg:linear-gradient(90deg,#B91C1C,#EF4444);
  --chart-line:#16A34A; --chart-fill:rgba(22,163,74,.10); --chart-grid:rgba(15,23,42,.08);
  --scrollbar:rgba(91,107,128,.38); --scrollbar-hover:rgba(91,107,128,.60);
  --brand-grad:linear-gradient(135deg,#22C55E,#2563EB);
  /* the terminal feed stays a dark code-block in every theme (deliberate) */
  --term-bg:#05080F; --term-border:#1B2432; --term-line:rgba(51,65,85,.5);
  --term-fg:#E7EDF5; --term-muted:#7C8AA0; --term-dim:#A8B3C5;
  /* shared metrics — identical across themes */
  --space-sm:0.25rem; --space-md:0.5rem; --space-lg:0.75rem;
  --space-xl:1rem; --space-2xl:1.5rem; --space-3xl:2rem;
  --font-ui:'Fira Sans',-apple-system,sans-serif;
  --font-mono:'Fira Code','SF Mono',monospace;
  --radius:8px; --radius-lg:12px;
  --trans:200ms ease;
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --color-background:#0F1218; --color-foreground:#E8ECF3;
  --color-card:#161A22; --color-card-foreground:#E8ECF3;
  --color-muted:#1D222C; --color-muted-foreground:#98A2B3;
  --color-border:#2A3140;
  --color-primary:#2563EB; --color-on-primary:#FFFFFF; --color-primary-hover:#3B82F6;
  --color-accent:#22C55E; --color-on-accent:#052E16; --color-accent-hover:#4ADE80;
  --color-secondary:#1D4ED8; --color-on-secondary:#FFFFFF;
  --color-destructive:#DC2626; --color-on-destructive:#FFFFFF;
  --color-destructive-hover:#EF4444;
  --color-pos:#4ADE80; --color-neg:#F87171; --color-blue:#60A5FA;
  --color-ring:#60A5FA;
  --ring-soft:rgba(96,165,250,.22);
  --pos-soft:rgba(74,222,128,.13); --neg-soft:rgba(248,113,113,.13);
  --blue-soft:rgba(96,165,250,.13); --hold-soft:rgba(152,162,179,.14);
  --amber:#F59E0B; --amber-soft:rgba(245,158,11,.16);
  --row-hover:rgba(148,163,184,.07);
  --hover-border:rgba(96,165,250,.45);
  --header-bg:rgba(19,23,31,.86);
  --overlay:rgba(2,6,16,.62);
  --glow-pos:rgba(34,197,94,.45);
  --shimmer:rgba(152,162,179,.10);
  --shadow-sm:0 1px 2px rgba(0,0,0,.35);
  --shadow-md:0 1px 2px rgba(0,0,0,.35),0 4px 16px -4px rgba(0,0,0,.45);
  --shadow-lg:0 12px 28px -8px rgba(0,0,0,.55);
  --shadow-xl:0 24px 56px -16px rgba(0,0,0,.65);
  --hero-grad:radial-gradient(90% 140% at 88% -20%,rgba(37,99,235,.16),transparent 55%),
              radial-gradient(90% 140% at 8% -30%,rgba(34,197,94,.12),transparent 52%);
  --grad-pos:linear-gradient(90deg,#16A34A,#4ADE80);
  --grad-neg:linear-gradient(90deg,#DC2626,#F87171);
  --chart-line:#4ADE80; --chart-fill:rgba(74,222,128,.09); --chart-grid:rgba(148,163,184,.13);
  --scrollbar:rgba(152,162,179,.30); --scrollbar-hover:rgba(152,162,179,.50);
}
:root[data-theme="black"] {
  color-scheme: dark;
  --color-background:#000000; --color-foreground:#F4F5F7;
  --color-card:#0B0B0D; --color-card-foreground:#F4F5F7;
  --color-muted:#151518; --color-muted-foreground:#A1A6B0;
  --color-border:#26262C;
  --color-primary:#2563EB; --color-on-primary:#FFFFFF; --color-primary-hover:#3B82F6;
  --color-accent:#22C55E; --color-on-accent:#052E16; --color-accent-hover:#4ADE80;
  --color-secondary:#1D4ED8; --color-on-secondary:#FFFFFF;
  --color-destructive:#DC2626; --color-on-destructive:#FFFFFF;
  --color-destructive-hover:#EF4444;
  --color-pos:#4ADE80; --color-neg:#F87171; --color-blue:#60A5FA;
  --color-ring:#60A5FA;
  --ring-soft:rgba(96,165,250,.20);
  --pos-soft:rgba(74,222,128,.13); --neg-soft:rgba(248,113,113,.13);
  --blue-soft:rgba(96,165,250,.13); --hold-soft:rgba(161,166,176,.14);
  --amber:#F59E0B; --amber-soft:rgba(245,158,11,.14);
  --row-hover:rgba(255,255,255,.05);
  --hover-border:rgba(96,165,250,.42);
  --header-bg:rgba(0,0,0,.84);
  --overlay:rgba(0,0,0,.72);
  --glow-pos:rgba(34,197,94,.50);
  --shimmer:rgba(161,166,176,.10);
  --shadow-sm:0 1px 2px rgba(0,0,0,.6);
  --shadow-md:0 1px 2px rgba(0,0,0,.6),0 4px 16px -4px rgba(0,0,0,.7);
  --shadow-lg:0 12px 28px -8px rgba(0,0,0,.8);
  --shadow-xl:0 24px 56px -16px rgba(0,0,0,.9);
  --hero-grad:radial-gradient(90% 140% at 88% -20%,rgba(37,99,235,.13),transparent 55%),
              radial-gradient(90% 140% at 8% -30%,rgba(34,197,94,.10),transparent 52%);
  --grad-pos:linear-gradient(90deg,#16A34A,#4ADE80);
  --grad-neg:linear-gradient(90deg,#DC2626,#F87171);
  --chart-line:#4ADE80; --chart-fill:rgba(74,222,128,.08); --chart-grid:rgba(255,255,255,.09);
  --scrollbar:rgba(161,166,176,.28); --scrollbar-hover:rgba(161,166,176,.48);
}
* { box-sizing:border-box; margin:0; padding:0; }
html { scroll-behavior:smooth; scroll-padding-top:118px; scrollbar-gutter:stable; }
body { background:var(--color-background); color:var(--color-foreground);
       font:14px/1.5 var(--font-ui); -webkit-font-smoothing:antialiased; }
::selection { background:var(--ring-soft); }
/* slim theme-aware scrollbars everywhere (firefox via scrollbar-*,
   webkit below) — part of the smooth-scroll feel */
* { scrollbar-width:thin; scrollbar-color:var(--scrollbar) transparent; }
::-webkit-scrollbar { width:10px; height:10px; }
::-webkit-scrollbar-track { background:transparent; }
::-webkit-scrollbar-thumb { background:var(--scrollbar); border-radius:8px;
                            border:3px solid transparent; background-clip:content-box; }
::-webkit-scrollbar-thumb:hover { background-color:var(--scrollbar-hover); }

/* ---------------------------------------------------------------- header */
.topbar { display:flex; justify-content:space-between; align-items:center; gap:var(--space-lg);
          padding:var(--space-lg) var(--space-xl); border-bottom:1px solid var(--color-border);
          background:var(--header-bg); backdrop-filter:blur(10px);
          -webkit-backdrop-filter:blur(10px);
          position:sticky; top:0; z-index:40;
          padding-left:max(1rem, env(safe-area-inset-left)); }
.brand { display:flex; align-items:center; gap:var(--space-md); min-width:0; }
/* gradient logo chip — the one fixed-brand-color element in every theme */
.logo-chip { width:32px; height:32px; border-radius:9px; background:var(--brand-grad);
             display:grid; place-items:center; color:#fff; flex-shrink:0;
             box-shadow:0 2px 10px rgba(37,99,235,.30); }
.logo-chip svg { width:17px; height:17px; }
.brand h1 { font:600 15px/1.2 var(--font-mono); letter-spacing:.3px; white-space:nowrap; }
.brand .sub { color:var(--color-muted-foreground); font-size:11px; white-space:nowrap;
              overflow:hidden; text-overflow:ellipsis; }
.topbar-right { display:flex; align-items:center; gap:10px; }
.engine-pill { display:inline-flex; align-items:center; gap:6px; font-size:12px;
               color:var(--color-muted-foreground); padding:4px 10px;
               border:1px solid var(--color-border); border-radius:999px;
               white-space:nowrap; background:var(--color-muted); }
.dot { width:8px; height:8px; border-radius:50%; background:var(--color-muted-foreground);
       transition:background var(--trans); flex-shrink:0; }
.dot.on { background:var(--color-pos); box-shadow:0 0 10px var(--glow-pos); }
.dot.off { background:var(--color-neg); }

/* theme switch — 3-state segmented control (light · dark · AMOLED black) */
.theme-switch { display:inline-flex; gap:2px; padding:3px;
                border:1px solid var(--color-border); border-radius:999px;
                background:var(--color-muted); }
.theme-switch button { width:32px; height:26px; border-radius:999px; border:none;
                       background:transparent; color:var(--color-muted-foreground);
                       cursor:pointer; display:grid; place-items:center;
                       transition:background var(--trans),color var(--trans); }
.theme-switch button:hover { color:var(--color-foreground); }
.theme-switch button[aria-pressed="true"] { background:var(--color-card);
  color:var(--color-primary); box-shadow:var(--shadow-sm); }
.theme-switch svg { width:14px; height:14px; }

/* amber degraded-engine pill (health_note: position behind a dead feed) */
.engine-pill.warn .dot { background:var(--color-neg); }
.health-banner { display:none; align-items:flex-start; gap:10px;
                 background:var(--neg-soft); border:1px solid var(--color-neg);
                 color:var(--color-neg); border-radius:var(--radius);
                 padding:12px 14px; font-size:13px; margin-bottom:12px; }
.health-banner.show { display:flex; }
.health-banner svg { width:17px; height:17px; flex-shrink:0; margin-top:1px; }
.health-banner .h-dismiss { margin-left:auto; background:transparent;
                            border:1px solid var(--color-neg); border-radius:6px;
                            color:var(--color-neg); cursor:pointer; padding:4px 10px;
                            font:600 11px/1.4 var(--font-ui); flex-shrink:0; }
.health-banner .h-dismiss:hover { background:rgba(220,38,38,.14); }

/* amber manual-pause banner: the operator paused the bot — a deliberate
   state, so amber (caution) rather than the red degraded-engine banner;
   never dismissible while the pause is actually active (unlike health_note
   the operator ends it via Resume, not via a dismiss button) */
.pause-banner { display:none; align-items:flex-start; gap:10px;
                background:var(--amber-soft); border:1px solid var(--amber);
                color:var(--amber); border-radius:var(--radius);
                padding:12px 14px; font-size:13px; margin-bottom:12px; }
.pause-banner.show { display:flex; }
.pause-banner svg { width:17px; height:17px; flex-shrink:0; margin-top:1px; }
.pause-banner .p-body b { font-weight:700; }

/* market-mode banner + toggle: which universe the bot trades is a FIRST-CLASS
   visible state (like the pause banner) — a restart must never silently flip
   markets on the operator. Blue (interactive chrome), not amber (caution:
   trading India is a choice, not a fault) and not green/red (money in/out). */
.market-banner { display:none; align-items:flex-start; gap:10px;
                 background:var(--blue-soft); border:1px solid var(--color-blue);
                 color:var(--color-blue); border-radius:var(--radius);
                 padding:12px 14px; font-size:13px; margin-bottom:12px; }
.market-banner.show { display:flex; }
.market-banner svg { width:17px; height:17px; flex-shrink:0; margin-top:1px; }
.market-banner b { font-weight:700; }
.market-banner .m-sub { display:block; font-size:11.5px; opacity:.85; margin-top:1px; }
/* the two-state switch: ON = Forex (crypto + forex universe), OFF = India
   (NSE). A switch (not two buttons) because the state is binary and
   exclusive — one book at a time, never both. */
.mkt-row { display:flex; align-items:center; gap:10px; margin-top:12px;
           border-top:1px dashed var(--color-border); padding-top:10px;
           flex-wrap:wrap; }
.mkt-row .lbl { font:500 11px/1.3 var(--font-ui); text-transform:uppercase;
                letter-spacing:.5px; color:var(--color-muted-foreground); }
.mkt-switch { position:relative; display:inline-flex; align-items:center;
              border:1px solid var(--color-border); border-radius:999px;
              background:var(--color-muted); padding:2px; cursor:pointer;
              min-height:34px; }
.mkt-switch button { appearance:none; border:none; cursor:pointer;
                      border-radius:999px; padding:7px 14px;
                      font:600 12px/1.3 var(--font-ui); color:var(--color-muted-foreground);
                      background:transparent;
                      transition:background var(--trans),color var(--trans),
                                 box-shadow var(--trans); }
.mkt-switch button[aria-pressed="true"] { background:var(--color-card);
  color:var(--color-primary); box-shadow:var(--shadow-sm); }
.mkt-switch .sep { width:1px; align-self:stretch; margin:4px 2px;
                   background:var(--color-border); }
.mkt-row .hint { font-size:11.5px; color:var(--color-muted-foreground); }

/* plain-language risk-controls explanation beside the engine buttons — the
   non-technical reader is the audience (what each halt does AND does not do) */
.risk-controls { margin-top:12px; border-top:1px dashed var(--color-border);
                 padding-top:10px; font-size:12px; line-height:1.55;
                 color:var(--color-muted-foreground); }
.risk-controls b { color:var(--color-foreground); font-weight:600; }
.risk-controls .rc-row { margin-top:6px; }
.btn-warn { background:var(--amber); color:#1F1300; }
.btn-warn:hover { filter:brightness(.94); }

/* ---------------------------------------------------------------- tabs */
.tabs { display:flex; gap:2px; padding:0 var(--space-xl); border-bottom:1px solid var(--color-border);
        background:var(--header-bg); backdrop-filter:blur(10px);
        -webkit-backdrop-filter:blur(10px);
        overflow-x:auto; scrollbar-width:none;
        position:sticky; top:57px; z-index:39; }
.tabs::-webkit-scrollbar { display:none; }
.tab { appearance:none; background:transparent; border:none; border-bottom:2px solid transparent;
       color:var(--color-muted-foreground); font:500 13px/1 var(--font-ui);
       padding:12px 14px; cursor:pointer; border-radius:6px 6px 0 0;
       transition:color var(--trans),border-color var(--trans),background var(--trans);
       display:inline-flex; align-items:center; gap:7px; white-space:nowrap; min-height:44px; }
.tab svg { width:15px; height:15px; }
.tab:hover { color:var(--color-foreground); background:var(--color-muted); }
.tab.active { color:var(--color-primary); border-bottom-color:var(--color-primary);
              background:transparent; }
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
        border-radius:var(--radius-lg); padding:var(--space-lg);
        box-shadow:var(--shadow-sm); }
.card + .card { margin-top:var(--space-lg); }
.card-head { display:flex; justify-content:space-between; align-items:center; gap:var(--space-md);
             margin-bottom:var(--space-lg); flex-wrap:wrap; }
.card-head h2 { display:flex; align-items:center; gap:8px; font:600 12px/1 var(--font-ui);
                text-transform:uppercase; letter-spacing:.8px; color:var(--color-muted-foreground); }
.card-head h2 svg { width:15px; height:15px; color:var(--color-primary); }
.card-head .hint { font-size:11px; color:var(--color-muted-foreground); }
.grid { display:grid; gap:var(--space-md); }

.btn { display:inline-flex; align-items:center; justify-content:center; gap:7px;
       min-height:44px; padding:0 16px; border-radius:var(--radius); border:1px solid transparent;
       font:600 13px/1 var(--font-ui); cursor:pointer; box-shadow:var(--shadow-sm);
       transition:background var(--trans),color var(--trans),border-color var(--trans),
                  box-shadow var(--trans),transform var(--trans),opacity var(--trans);
       background:var(--color-primary); color:var(--color-on-primary); }
.btn:hover { background:var(--color-primary-hover); box-shadow:var(--shadow-md);
             transform:translateY(-1px); }
.btn:active { transform:translateY(0); box-shadow:var(--shadow-sm); }
.btn:disabled { opacity:.45; cursor:not-allowed; transform:none; box-shadow:none; }
.btn svg { width:15px; height:15px; }
/* money semantics stay fixed across every theme: green in · red out */
.btn-success { background:var(--color-accent); color:var(--color-on-accent); }
.btn-success:hover { background:var(--color-accent-hover); }
.btn-danger { background:var(--color-destructive); color:var(--color-on-destructive); }
.btn-danger:hover { background:var(--color-destructive-hover); }
.btn-secondary { background:var(--color-card); color:var(--color-foreground);
                 border:1px solid var(--color-border); box-shadow:none; }
.btn-secondary:hover { border-color:var(--color-muted-foreground); background:var(--color-muted);
                       box-shadow:var(--shadow-sm); transform:translateY(-1px); }
.btn-ghost { background:transparent; color:var(--color-muted-foreground);
             border:1px solid var(--color-border); box-shadow:none; }
.btn-ghost:hover { color:var(--color-foreground); border-color:var(--color-muted-foreground);
                   background:var(--color-muted); transform:none; }

.input, select { min-height:44px; padding:8px 12px; background:var(--color-card);
                 border:1px solid var(--color-border); border-radius:var(--radius);
                 color:var(--color-foreground); font:13px/1.4 var(--font-ui); width:100%;
                 transition:border-color var(--trans),box-shadow var(--trans); cursor:pointer; }
.input { font-family:var(--font-mono); cursor:text; }
.input:hover, select:hover { border-color:var(--color-muted-foreground); }
.input:focus, select:focus { border-color:var(--color-primary); outline:none;
                             box-shadow:0 0 0 3px var(--ring-soft); }
.input::placeholder { color:var(--color-muted-foreground); opacity:.7; }
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
tbody tr:hover { background:var(--row-hover); }
tbody tr:last-child td { border-bottom:none; }
td.num, th.num { font-family:var(--font-mono); font-variant-numeric:tabular-nums;
                 text-align:right; }
.pos { color:var(--color-pos); } .neg { color:var(--color-neg); }
.mono { font-family:var(--font-mono); font-variant-numeric:tabular-nums; }
.empty { padding:var(--space-2xl) var(--space-md); text-align:center;
         color:var(--color-muted-foreground); font-size:13px; }

.tag { display:inline-block; padding:2px 8px; border-radius:4px; font:500 10.5px/1.4
       var(--font-mono); letter-spacing:.5px; }
.tag.long { background:var(--pos-soft); color:var(--color-pos); }
.tag.short { background:var(--neg-soft); color:var(--color-neg); }
.tag.hold { background:var(--hold-soft); color:var(--color-muted-foreground); }
.tag.close { background:var(--blue-soft); color:var(--color-blue); }
.tag.open { background:var(--pos-soft); color:var(--color-pos); }
.tag.tf { background:var(--color-muted); color:var(--color-muted-foreground); }
.tag.demo { background:var(--color-muted); color:var(--color-muted-foreground);
            border:1px dashed var(--color-muted-foreground); font-style:italic; }
.icon-btn { background:transparent; border:1px solid var(--color-border); color:var(--color-neg);
            border-radius:6px; width:34px; height:34px; display:inline-flex; align-items:center;
            justify-content:center; cursor:pointer; transition:all var(--trans); }
.icon-btn:hover { border-color:var(--color-neg); background:var(--neg-soft); }
.icon-btn svg { width:14px; height:14px; }

/* loading skeleton */
.skeleton { position:relative; overflow:hidden; background:var(--color-muted);
            border-radius:4px; height:14px; }
.skeleton::after { content:''; position:absolute; inset:0;
                   background:linear-gradient(90deg,transparent,var(--shimmer),transparent);
                   animation:shimmer 1.4s infinite; }
@keyframes shimmer { from { transform:translateX(-100%); } to { transform:translateX(100%); } }
.sk-row { display:flex; gap:var(--space-md); padding:9px 10px; align-items:center; }

/* ---------------------------------------------------------------- stat cards */
.stats-grid { display:grid; gap:var(--space-md); margin-bottom:var(--space-lg);
              grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); }
.stat { background:var(--color-card); border:1px solid var(--color-border);
        border-radius:var(--radius-lg); padding:var(--space-md) var(--space-lg);
        box-shadow:var(--shadow-sm);
        transition:border-color var(--trans),box-shadow var(--trans),transform var(--trans); }
.stat:hover { border-color:var(--hover-border); box-shadow:var(--shadow-md);
              transform:translateY(-2px); }
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
.term { background:var(--term-bg); border:1px solid var(--term-border);
        border-radius:var(--radius);
        font:12px/1.7 var(--font-mono); padding:var(--space-lg);
        max-height:520px; overflow-y:auto; }
.term-row { display:flex; gap:10px; padding:5px 0; border-bottom:1px dashed var(--term-line);
            align-items:baseline; }
.term-row:last-child { border-bottom:none; }
.term-ts { color:var(--term-muted); font-size:11px; flex-shrink:0; padding-top:2px; }
.term-body { min-width:0; }
.term-line { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.term-mkt { color:var(--term-fg); font-weight:600; }
.term-meta { color:var(--term-muted); font-size:11px; }
.term-why { color:var(--term-dim); font-size:11.5px; margin-top:2px; word-break:break-word; }

/* strategy bars */
.strat-bars { display:flex; flex-direction:column; gap:10px; }
.sbar { display:grid; grid-template-columns:150px 1fr 90px; gap:10px; align-items:center;
        font-size:12px; }
.sbar .name { font-family:var(--font-mono); color:var(--color-muted-foreground);
              overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.sbar .track { height:18px; background:var(--color-muted); border-radius:4px;
               overflow:hidden; position:relative; }
.sbar .fill { position:absolute; top:0; bottom:0; transition:width .4s ease; border-radius:4px; }
.sbar .fill.pos { background:var(--grad-pos); }
.sbar .fill.neg { background:var(--grad-neg); right:0; }
.sbar .val { font-family:var(--font-mono); font-variant-numeric:tabular-nums;
             text-align:right; font-size:12px; }

/* ---------------------------------------------------------------- watchlist */
.wl-grid { display:grid; gap:var(--space-md); grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); }
.wl-item { display:flex; justify-content:space-between; align-items:center; gap:var(--space-md);
           background:var(--color-muted); border:1px solid var(--color-border);
           border-radius:var(--radius); padding:10px 12px;
           transition:border-color var(--trans),box-shadow var(--trans); }
.wl-item:hover { border-color:var(--hover-border); box-shadow:var(--shadow-sm); }
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
.preset-btn { background:var(--color-card); border:1px dashed var(--color-border);
              color:var(--color-muted-foreground); border-radius:var(--radius);
              padding:8px 14px; font:500 12px/1.3 var(--font-ui); cursor:pointer;
              transition:all var(--trans); min-height:44px; }
.preset-btn:hover { color:var(--color-primary); border-color:var(--hover-border);
                    background:var(--blue-soft); }
.preset-btn svg { width:13px; height:13px; vertical-align:-2px; margin-right:5px; }

/* ---------------------------------------------------------------- account */
.balance-card { text-align:center; padding:var(--space-2xl) var(--space-lg);
                background-color:var(--color-card); background-image:var(--hero-grad); }
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
.modal-overlay { position:fixed; inset:0; background:var(--overlay); backdrop-filter:blur(4px);
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
         border:1px solid var(--color-border); border-left:3px solid var(--color-pos);
         border-radius:var(--radius); padding:12px 14px; font-size:13px;
         box-shadow:var(--shadow-lg); animation:toastIn .25s ease; }
.toast.err { border-left-color:var(--color-neg); }
.toast .t-title { font-weight:600; }
.toast .t-msg { color:var(--color-muted-foreground); font-size:12px; margin-top:1px;
                word-break:break-word; }
.toast svg { width:16px; height:16px; flex-shrink:0; margin-top:1px;
             color:var(--color-pos); }
.toast.err svg { color:var(--color-neg); }
@keyframes toastIn { from { opacity:0; transform:translateX(16px); } to { opacity:1; transform:none; } }
.toast.out { opacity:0; transform:translateX(16px); transition:all .3s ease; }

/* offline fallback for Chart.js (styled here, not inline, so it themes) */
.chart-offline { padding:8px 14px; margin:10px 0; border-radius:8px; font-size:12px;
                 background:var(--neg-soft); border:1px solid var(--color-neg);
                 color:var(--color-neg); }

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
.quick-chip:hover { color:var(--color-primary); border-color:var(--hover-border);
                    background:var(--blue-soft); }
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
    <span class="logo-chip" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>
    </span>
    <div>
      <h1>ALGO TRADING BOT</h1>
      <div class="sub" id="brandSub">paper trading · IST</div>
    </div>
  </div>
  <div class="topbar-right">
    <div class="engine-pill" role="status" title="">
      <span class="dot" id="engineDot"></span>
      <span id="enginePillText">engine: checking…</span>
    </div>
    <div class="theme-switch" role="group" aria-label="Color theme">
      <button type="button" data-theme="light" aria-pressed="false" title="Light" aria-label="Light theme">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>
      </button>
      <button type="button" data-theme="dark" aria-pressed="false" title="Dark (gray)" aria-label="Dark theme">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
      </button>
      <button type="button" data-theme="black" aria-pressed="false" title="Black (AMOLED)" aria-label="AMOLED black theme">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" stroke="none"/></svg>
      </button>
    </div>
  </div>
</header>

<nav class="tabs" id="tabs" aria-label="Dashboard sections">
  <button class="tab active" data-view="overview" id="tab-overview">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/></svg>
    Overview</button>
  <button class="tab" data-view="portfolio" id="tab-portfolio">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>
    Portfolio</button>
  <button class="tab" data-view="hft" id="tab-hft">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
    HFT</button>
  <button class="tab" data-view="watchlist" id="tab-watchlist">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-clock"/></svg>
    Watchlist</button>
  <button class="tab" data-view="lab" id="tab-lab">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 2v7.5L4.5 19a2 2 0 0 0 1.7 3h11.6a2 2 0 0 0 1.7-3L14 9.5V2"/><line x1="8.5" y1="2" x2="15.5" y2="2"/><line x1="7" y1="16" x2="17" y2="16"/></svg>
    Lab</button>
  <button class="tab" data-view="evidence" id="tab-evidence">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="10"/></svg>
    Evidence</button>
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
<div class="pause-banner" id="pauseBanner" role="status">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>
  <div class="p-body"><b>Trading paused (manual).</b> Blocks new entries only — open positions are still managed (stops, targets, strategy exits). Nothing is force-closed.<span id="pauseNoteBox"></span> <span style="opacity:.85">Resume from the Overview tab when you want new entries again.</span></div>
</div>
<div class="market-banner" id="marketBanner" role="status">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
  <div class="m-body"><b id="marketBannerText">Market: —</b><span class="m-sub" id="marketBannerSub"></span></div>
</div>
<div class="health-banner" id="healthBanner" role="alert">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-alert"/></svg>
  <div><b>Engine degraded:</b> <span id="healthMsg"></span></div>
  <button class="h-dismiss" id="healthDismiss" type="button" aria-label="Dismiss">dismiss</button>
</div>
<!-- ============================================================ OVERVIEW -->
<section class="view active" id="view-overview">
  <div class="stats-grid" id="ovStats"></div>
  <div class="hint" id="ovDemoNote" style="margin:-6px 0 10px"></div>
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
        <button class="btn btn-warn" id="btnPause" title="Blocks new entries only — open positions are still managed (stops, targets, strategy exits). Nothing is force-closed.">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>
          <span id="pauseLabel">Pause trading</span></button>
      </div>
      <div class="engine-state" style="margin-top:12px">
        <span class="st" id="engineStateText">stopped</span>
        <span class="sub" id="engineStateSub">0 cycles · watchlist 0 specs</span>
      </div>
      <div class="risk-controls">
        <b>Risk controls</b> — what they do and don't do
        <div class="rc-row"><b>Daily kill switch (automatic):</b> after a −3% day it blocks new entries for the rest of the UTC day. It resets by itself at the next UTC day. It never force-closes positions — their stops, targets and strategy exits keep running.</div>
        <div class="rc-row"><b>Pause trading (manual):</b> stays until you press Resume. Blocks new entries only — open positions are still managed (stops, targets, strategy exits). Nothing is force-closed.</div>
      </div>
      <div class="mkt-row">
        <span class="lbl" id="mktLabel">Market</span>
        <div class="mkt-switch" role="group" aria-label="Active market: on = Forex, off = India">
          <button type="button" id="mktForex" aria-pressed="false">Forex (crypto + forex)</button>
          <span class="sep" aria-hidden="true"></span>
          <button type="button" id="mktIndia" aria-pressed="false">India (NSE)</button>
        </div>
        <span class="hint" id="mktHint">One market at a time. Your market choice is remembered — restarting the dashboard keeps the same market.</span>
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
    <p class="hint" id="tradeModeNote" style="margin-top:10px">Trades from previous market modes remain in the journal history; the bot only opens new positions in the active market.</p>
  </div>
</section>

<!-- ================================================================ HFT -->
<section class="view" id="view-hft">
  <div class="card">
    <div class="card-head">
      <h2><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>High-frequency book <span class="badge">1m · paper</span></h2>
      <span class="hint" id="hftFeeHint"></span>
    </div>
    <p class="hint" style="margin:0 0 10px">A SECOND paper account trading 1-minute bars (crypto + forex) with its own capital, risk dials and fee tier — the standard book above is untouched. HFT strategies: <b>hft_market_maker</b> (Avellaneda–Stoikov-inspired maker quotes), <b>hft_exhaustion_fade</b> (volume-spike reversion, maker entry), <b>hft_micro_breakout</b> (2R micro-range breakout, taker) + the <b>TRI-ETH</b> triangular-arb monitor. Research + fee math: <code>HFT.md</code>.</p>
    <div class="stat-grid" id="hftStats"></div>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px" id="hftEngineControls">
      <button class="btn primary" id="hftStartBtn">Start HFT engine</button>
      <button class="btn danger" id="hftStopBtn" disabled>Stop</button>
      <span class="engine-pill"><span class="dot" id="hftEngineDot"></span><span id="hftPillText">hft: checking…</span></span>
    </div>
    <p class="hint" id="hftEngineNote" style="margin:8px 0 0" hidden></p>
  </div>
  <div class="card">
    <div class="card-head">
      <h2>Equity curve</h2>
      <span class="hint">mode='hft' journal rows</span>
    </div>
    <div class="chart-wrap"><canvas id="hftEquityChart" aria-label="HFT equity curve" role="img"></canvas></div>
    <div class="empty" id="hftEquityEmpty" hidden>No HFT equity points yet — start the HFT engine.</div>
  </div>
  <div class="card">
    <div class="card-head">
      <h2>All high-frequency trades</h2>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <label class="fld" for="hftStratFilter" style="margin:0">Filter</label>
        <select id="hftStratFilter" style="min-height:36px;width:auto" aria-label="Filter HFT trades by strategy">
          <option value="">all strategies</option>
        </select>
      </div>
    </div>
    <div class="tbl-wrap">
      <table id="hftTradeTable"><thead><tr>
        <th>Opened</th><th>Market</th><th>Side</th><th class="num">Qty</th>
        <th class="num">Entry</th><th class="num">Exit</th><th class="num">P&amp;L</th>
        <th>Strategy</th><th>Status</th><th>Exit reason</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <div class="empty" id="hftTradeEmpty" hidden>No HFT trades yet — start the HFT engine or run `python3 main.py hft-backtest`.</div>
  </div>
  <div class="card">
    <div class="card-head">
      <h2>Decision feed</h2>
      <span class="hint">every HFT evaluation, HOLDs included</span>
    </div>
    <div id="hftDecisions" class="decision-feed"></div>
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

<!-- ================================================================ LAB -->
<section class="view" id="view-lab">
  <div class="card">
    <div class="card-head">
      <h2><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 2v7.5L4.5 19a2 2 0 0 0 1.7 3h11.6a2 2 0 0 0 1.7-3L14 9.5V2"/><line x1="8.5" y1="2" x2="15.5" y2="2"/><line x1="7" y1="16" x2="17" y2="16"/></svg>Strategy Lab <span class="badge">pick · apply · backtest</span></h2>
      <span class="hint">any stock/pair, both books</span>
    </div>
    <p class="hint" style="margin:0 0 12px">Pick any stock or pair, apply every strategy registered for it, and backtest on real data with full costs. <b>Standard</b> = the forex+crypto+NSE book's strategies and kind-aware cost stacks; <b>HFT</b> = the high-frequency book's 1m strategies and fee tiers. Pure backtest — your open paper positions are never touched.</p>
    <div style="display:grid;gap:12px" id="labForm">
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:end">
        <label class="fld">Book
          <select id="labBook">
            <option value="standard">Standard (forex + crypto + NSE)</option>
            <option value="hft">HFT (high-frequency, 1m)</option>
          </select>
        </label>
        <label class="fld">Market
          <select id="labKind">
            <option value="crypto">Crypto</option>
            <option value="forex">Forex</option>
            <option value="india">NSE India</option>
          </select>
        </label>
        <label class="fld" style="flex:1;min-width:200px">Symbol
          <input id="labSymbol" type="text" placeholder="BTC/USDT" maxlength="24" autocomplete="off">
        </label>
      </div>
      <div id="labChips" style="display:flex;gap:6px;flex-wrap:wrap"></div>
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:end">
        <label class="fld">Timeframe
          <select id="labTimeframe"></select>
        </label>
        <label class="fld">Strategy
          <select id="labStrategy"></select>
        </label>
        <label class="fld">History
          <select id="labDays"></select>
        </label>
        <label class="fld" id="labFeeWrap" hidden>Fee tier
          <select id="labFee">
            <option value="perp">perp (maker 2bp / taker 5bp)</option>
            <option value="spot">spot (10bp / 10bp)</option>
          </select>
        </label>
        <button class="btn primary" id="labRunBtn">Backtest</button>
      </div>
      <div class="hint" id="labNote"></div>
    </div>
    <div class="empty" id="labStatus" hidden></div>
  </div>
  <div class="card" id="labResultCard" hidden>
    <div class="card-head">
      <h2 id="labResultTitle">Result</h2>
      <span class="hint" id="labResultMeta"></span>
    </div>
    <div class="stat-grid" id="labStats"></div>
    <div class="card-head" style="margin-top:8px"><h2>Equity curve</h2><span class="hint" id="labCurveHint"></span></div>
    <div class="chart-wrap"><canvas id="labEquityChart" aria-label="Lab equity curve" role="img"></canvas></div>
    <div class="card-head" style="margin-top:8px"><h2>Exit reasons</h2></div>
    <div id="labExits" style="display:flex;gap:6px;flex-wrap:wrap"></div>
  </div>
  <div class="card" id="labCompareCard" hidden>
    <div class="card-head">
      <h2>Strategy comparison</h2>
      <span class="hint">every registered strategy on the same frame — best total P&amp;L drives the charts above</span>
    </div>
    <div class="tbl-wrap">
      <table id="labCompareTable"><thead><tr>
        <th>Strategy</th><th class="num">Return</th><th class="num">P&amp;L</th>
        <th class="num">Trades</th><th class="num">Win rate</th><th class="num">PF</th>
        <th class="num">Max DD</th><th class="num">Sharpe</th><th class="num">Fees</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>
  <div class="card" id="labTradesCard" hidden>
    <div class="card-head"><h2>Trades</h2><span class="hint" id="labTradesHint"></span></div>
    <div class="tbl-wrap">
      <table id="labTradeTable"><thead><tr>
        <th>Opened</th><th>Side</th><th class="num">Qty</th><th class="num">Entry</th>
        <th class="num">Exit</th><th class="num">P&amp;L</th><th>Exit reason</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>
</section>

<!-- ============================================================ EVIDENCE -->
<section class="view" id="view-evidence">
  <div class="stats-grid" id="evCards"></div>
  <div class="hint" id="evHint" style="margin:-6px 0 10px">The honesty layer, rendered: every card and chart here is a GENERATED artifact (<code>make validate</code>, <code>python3 main.py shadow</code>, the fetch manifest) — never hand-edited. Empty cards mean the command hasn't been run on this machine yet.</div>
  <div class="grid" style="grid-template-columns:1fr 1fr;margin-bottom:12px">
    <div class="card chart-card">
      <div class="card-head"><h2>Kronos rolling rank-IC vs its promotion hurdle</h2><span class="hint" id="krMeta"></span></div>
      <div class="chart-body"><canvas id="kronosChart"></canvas></div>
      <div class="empty" id="krEmpty" hidden>no resolved forecasts yet — the ledger fills as the engine (or <code>main.py kronos</code>) forecasts and the horizons resolve</div>
    </div>
    <div class="card chart-card">
      <div class="card-head"><h2>Purged-CV out-of-sample path distribution</h2>
        <select class="input" id="evReportSel" style="max-width:300px;min-height:34px"></select></div>
      <div class="chart-body"><canvas id="cvChart"></canvas></div>
      <div class="empty" id="cvEmpty" hidden>no validation reports on this machine — run <code>make validate</code></div>
    </div>
  </div>
  <div class="card" style="margin-bottom:12px">
    <div class="card-head"><h2>Shadow Account — did the bot follow its own rules?</h2></div>
    <div id="evShadow"><div class="empty">loading…</div></div>
  </div>
  <div class="card">
    <div class="card-head"><h2>Pinned data manifest — fetch provenance</h2></div>
    <div id="evManifest"><div class="empty">loading…</div></div>
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
        <button class="btn btn-success" type="submit" style="flex-shrink:0">
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

<!-- market-switch confirmation modal: shown ONLY when the switch would close
     open paper positions (the orphan guard's 409). Plain language, both what
     it does and what it does NOT do. -->
<div class="modal-overlay" id="marketModal" role="dialog" aria-modal="true" aria-labelledby="marketTitle">
  <div class="modal">
    <h3 id="marketTitle"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-alert"/></svg>Switch markets and close open positions?</h3>
    <p id="marketModalMsg"></p>
    <div class="modal-actions">
      <button class="btn btn-ghost" id="marketCancel">Cancel</button>
      <button class="btn" id="marketGo">Switch &amp; close positions</button>
    </div>
  </div>
</div>

<div id="toasts" aria-live="polite"></div>

<!-- token gate: shown by askForToken() when the API answers 401 with no
     stored token (DASHBOARD_TOKEN mode) -->
<div class="modal-overlay" id="tokenGate" role="dialog" aria-modal="true" aria-labelledby="tokenTitle">
  <div class="modal">
    <h3 id="tokenTitle"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>Token required</h3>
    <p>This dashboard requires a bearer token. Paste it once — it is stored in
    this browser only and attached to every API call.</p>
    <label class="fld" for="tokenInput">Bearer token</label>
    <input class="input" id="tokenInput" placeholder="the DASHBOARD_TOKEN value" autocomplete="off">
    <div class="modal-actions">
      <button class="btn btn-ghost" id="tokenCancel">Cancel</button>
      <button class="btn" id="tokenSave">Save &amp; reload</button>
    </div>
  </div>
</div>

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
// takes the abs path and prefixes its own '-'.
// z() kills negative zero and float noise: -0.0 showed as "-0.00%" and a
// 1e-16 re-mark showed as "-$0.00" (a red flag on an empty minus)
const z = v => (v === 0 || Math.abs(v) < 0.005) ? 0 : v;
const fmtPnl = v => (v == null || isNaN(v)) ? '—'
  : (v > 0.005 ? '+' : v < -0.005 ? '-' : '') + '$' + fmtNum(Math.abs(v));
const fmtPct = v => (v == null || isNaN(v)) ? '—' : sign(v) + z(v).toFixed(2) + '%';
const isForex = s => String(s).includes('=');
const fmtPx = (v, s) => (v == null || isNaN(v) || !Number(v)) ? '—'
  : fmtNum(v, isForex(s) ? 5 : 2);
const fmtQty = v => (v == null || isNaN(v)) ? '—'
  : Number(v).toLocaleString(undefined, {maximumSignificantDigits: 5});
const posCls = v => z(v) > 0 ? 'pos' : z(v) < 0 ? 'neg' : '';
const tag = (cls, text) => '<span class="tag ' + esc(cls) + '">' + esc(text) + '</span>';
const sideTag = s => tag((s || '').toLowerCase(), String(s).toUpperCase());
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
/* read a CSS custom property off :root — lets the Chart.js canvas follow
   the active theme (light/dark/black) without rebuilding it */
const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
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

/* optional bearer token (DASHBOARD_TOKEN): stored once, attached to every
   fetch. A normal browser navigation cannot send headers, so the page shell
   is served unguarded (no secrets in it) and the SPA supplies the header. */
const _tok = () => { try { return localStorage.getItem('algo-token') || ''; }
                     catch (e) { return ''; } };
function askForToken() {
  /* the gate: shown once when the API answers 401 and no token is stored */
  const gate = $('#tokenGate');
  if (gate.classList.contains('open')) return;
  $('#tokenInput').value = _tok();
  gate.classList.add('open');
  $('#tokenInput').focus();
}
$('#tokenSave').addEventListener('click', () => {
  const v = $('#tokenInput').value.trim();
  try { v ? localStorage.setItem('algo-token', v) : localStorage.removeItem('algo-token'); }
  catch (e) { /* private mode: token just won't persist */ }
  $('#tokenGate').classList.remove('open');
  location.reload();   // re-boot the pollers with the header attached
});
$('#tokenGate').addEventListener('click', e => {
  if (e.target === e.currentTarget) e.currentTarget.classList.remove('open');
});

async function jget(u) {
  const t = _tok();
  const r = await fetch(u, t ? {headers: {Authorization: 'Bearer ' + t}} : undefined);
  if (r.status === 401) { askForToken(); throw new Error('token required (401)'); }
  if (!r.ok) throw new Error('GET ' + u);
  return r.json();
}
async function jreq(u, method, body) {
  const h = {'Content-Type': 'application/json'};
  const t = _tok();
  if (t) h.Authorization = 'Bearer ' + t;
  const r = await fetch(u, {method, headers: h,
                            body: body == null ? undefined : JSON.stringify(body)});
  if (r.status === 401) { askForToken(); throw new Error('token required (401)'); }
  let data = {};
  try { data = await r.json(); } catch (e) { /* non-JSON error body */ }
  if (!r.ok) {
    const msg = (data && data.detail) ? data.detail : (r.status + ' ' + r.statusText);
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    err.status = r.status;
    err.data = data;   // full error body (the market-switch 409 carries
                       // open_positions + requires_confirm the caller reads)
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
/* the degraded-engine banner: dismissed by the operator stays dismissed until
   the note CHANGES (a new condition re-shows it) */
let healthDismissed = false, healthDismissedMsg = '';
let autoResumeToasted = false;
$('#healthDismiss').addEventListener('click', () => {
  healthDismissed = true;
  healthDismissedMsg = $('#healthMsg').textContent;
  $('#healthBanner').classList.remove('show');
  $('.engine-pill').classList.remove('warn');
});

$('#tokenCancel').addEventListener('click', () => $('#tokenGate').classList.remove('open'));

/* ===================================================== routing */
const VIEWS = ['overview', 'portfolio', 'hft', 'watchlist', 'lab', 'evidence', 'account', 'chat'];
function setView(name) {
  if (!VIEWS.includes(name)) name = 'overview';
  $$('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
  $$('#tabs .tab').forEach(t => t.classList.toggle('active', t.dataset.view === name));
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  document.title = 'Algo Bot — ' + name[0].toUpperCase() + name.slice(1);
  refreshVisible(name);
  /* smooth ride back to the top when swapping views (auto under reduced motion) */
  if (reduceMotion) window.scrollTo(0, 0);
  else window.scrollTo({top: 0, behavior: 'smooth'});
}
window.addEventListener('hashchange', () => setView(location.hash.slice(1) || 'overview'));
$('#tabs').addEventListener('click', e => { const t = e.target.closest('.tab'); if (t) setView(t.dataset.view); });
/* only #tabs is wired — the duplicated mobile nav is gone from the DOM, and a
   listener on a null element would throw at boot and kill this whole script */

/* first-load skeletons already in the DOM; data replaces them on first poll */
/* one-shot view flags — declared before setView() can run them at boot */
const chatLoaded = {v: false}, evLoaded = {v: false};
function refreshVisible(name) {
  if (name === 'overview') { refreshStats(); refreshEquity(); refreshDecisions(); }
  else if (name === 'portfolio') { refreshStats(); refreshTrades(); }
  else if (name === 'hft') { refreshHft(); }
  else if (name === 'watchlist') refreshWatchlist();
  else if (name === 'lab') { refreshLab(); }
  /* evidence loads ONCE per tab entry, not on every 4s poll: the payload is
     generated artifacts (slow to change) and rebuilding both charts each tick
     churned ~0.7s CPU while the tab was merely open */
  else if (name === 'evidence' && !evLoaded.v) refreshEvidence();
  else if (name === 'account') { refreshAccount(); refreshTransactions(); }
  else if (name === 'chat' && !chatLoaded.v) loadChatHistory();
}

/* ===================================================== theme
   data-theme is already on <html> (set pre-paint in <head>) — this
   section only wires the switcher, mirrors state onto the buttons,
   updates the mobile browser chrome color and recolors the chart. */
const THEME_KEY = 'algo-theme';
const THEMES = ['light', 'dark', 'black'];
function applyChartTheme() {
  for (const chart of [equityChart, hftChart, labChart]) {
    if (!chart) continue;
    const ds = chart.data.datasets[0];
    ds.borderColor = cssVar('--chart-line');
    ds.backgroundColor = cssVar('--chart-fill');
    const tt = chart.options.plugins.tooltip;
    tt.backgroundColor = cssVar('--color-card');
    tt.borderColor = cssVar('--color-border');
    tt.titleColor = cssVar('--color-foreground');
    tt.bodyColor = cssVar('--color-muted-foreground');
    const tick = cssVar('--color-muted-foreground'), grid = cssVar('--chart-grid');
    ['x', 'y'].forEach(ax => {
      chart.options.scales[ax].ticks.color = tick;
      chart.options.scales[ax].grid.color = grid;
    });
    chart.update('none');
  }
}
function applyTheme(t, persist) {
  if (!THEMES.includes(t)) t = 'light';
  document.documentElement.setAttribute('data-theme', t);
  if (persist) { try { localStorage.setItem(THEME_KEY, t); } catch (e) { /* private mode */ } }
  $$('.theme-switch button').forEach(b =>
    b.setAttribute('aria-pressed', String(b.dataset.theme === t)));
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', cssVar('--color-background'));
  applyChartTheme();
}
$$('.theme-switch button').forEach(b =>
  b.addEventListener('click', () => applyTheme(b.dataset.theme, true)));

/* ===================================================== overview */
let equityChart = null;
function buildEquityChart() {
  /* colors come from the live theme's CSS variables (see applyChartTheme) */
  equityChart = new Chart($('#equityChart'), {
    type: 'line',
    data: {labels: [], datasets: [{label: 'Equity', data: [],
      borderColor: cssVar('--chart-line'), backgroundColor: cssVar('--chart-fill'),
      fill: true, tension: .15, pointRadius: 0, borderWidth: 2}]},
    options: {responsive: true, maintainAspectRatio: false, animation: reduceMotion ? false : {duration: 250},
      plugins: {legend: {display: false}, tooltip: {backgroundColor: cssVar('--color-card'),
        borderColor: cssVar('--color-border'), borderWidth: 1,
        titleColor: cssVar('--color-foreground'), bodyColor: cssVar('--color-muted-foreground'),
        titleFont: {family: 'Fira Code'}, bodyFont: {family: 'Fira Code'},
        callbacks: {label: c => ' ' + fmt$(c.parsed.y)}}},
      scales: {x: {ticks: {maxTicksLimit: 8, color: cssVar('--color-muted-foreground'),
                           font: {family: 'Fira Code', size: 10}},
                   grid: {color: cssVar('--chart-grid')}},
               y: {ticks: {color: cssVar('--color-muted-foreground'),
                           font: {family: 'Fira Code', size: 10},
                           callback: v => '$' + v.toLocaleString()},
                   grid: {color: cssVar('--chart-grid')}}}}
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

  const demoN = (s.trade_modes || {}).demo || 0;
  const paperN = (s.trade_modes || {}).paper || 0;
  $('#ovDemoNote').innerHTML = demoN
    ? '<span class="tag demo">demo</span> ' + demoN + ' seeded backtest-replay trades (mode=demo): ' +
      (paperN
        ? 'badged in the history and excluded from these headline stats, the equity curve, chatbot and shadow answers'
        : 'no paper trades yet — showing the demo record until the engine trades') +
      '. python3 main.py shadow --include-demo audits the replay rows.'
    : '';

  $('#engineDot').className = 'dot ' + (s.engine_running ? 'on' : 'off');
  $('#enginePillText').textContent = 'engine: ' + (s.engine_running ? (s.health_note ? 'degraded' : 'running') : 'stopped');
  /* health_note: degraded-but-alive (e.g. an open position behind a dead feed).
     It rode only in /api/engine/status before — nothing rendered it, so the
     pill stayed green on stage while a position sat unguarded. */
  const hb = $('#healthBanner');
  if (s.health_note && !healthDismissed) {
    hb.classList.add('show');
    $('#healthMsg').textContent = s.health_note;
    $('.engine-pill').classList.add('warn');
    $('.engine-pill').title = s.health_note;
  } else {
    hb.classList.remove('show');
    $('.engine-pill').classList.remove('warn');
    $('.engine-pill').title = '';
  }
  if (s.health_note && s.health_note !== healthDismissedMsg) healthDismissed = false;
  if (s.auto_resumed && !autoResumeToasted) {
    autoResumeToasted = true;
    toast('Engine auto-resumed', 'the last session left it running — it is paper-trading now (stop it from this tab)');
  }
  $('#engineStateText').textContent = s.engine_running ? 'running' : 'stopped';
  $('#engineStateText').className = 'st ' + (s.engine_running ? 'pos' : 'neg');
  $('#engineStateSub').textContent = (s.cycles ?? 0) + ' cycles · watchlist ' +
    (s.watchlist_count ?? 0) + ' specs';
  $('#btnStart').disabled = !!s.engine_running;
  $('#btnStop').disabled = !s.engine_running;

  /* manual pause: banner + button flip. Reported for a stopped engine too —
     the flag is a file that outlives any engine run, and a pause must never
     be lost by stopping/starting the engine. */
  const pb = $('#pauseBanner');
  if (s.paused) {
    pb.classList.add('show');
    $('#pauseNoteBox').textContent = s.paused_note ? ' Note: ' + s.paused_note + '.' : '';
    $('#pauseLabel').textContent = 'Resume trading';
  } else {
    pb.classList.remove('show');
    $('#pauseLabel').textContent = 'Pause trading';
  }

  /* market mode rides the same poll (W1's paused pattern): the banner +
     toggle must follow the PERSISTED mode within one 4s tick — a restart
     can never silently flip markets on the operator. */
  applyMarketMode(s.market_mode === 'india' ? 'india' : 'forex');

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
      '<span class="term-mkt">' + esc(d.symbol) + ' <span class="tag tf">' + esc(d.timeframe) + '</span>' +
      (d.mode === 'demo' ? ' <span class="tag demo">demo</span>' : '') + '</span>' +
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

/* manual pause/resume — one button that follows the flag's state. The copy
   must state the semantics every time it fires: entries-only, nothing is
   force-closed (an operator pausing in a panic must know what they did NOT
   just do to their open positions). */
async function togglePause() {
  const paused = $('#pauseBanner').classList.contains('show');
  if (!paused) {
    try {
      const r = await jpost('/api/trading/pause', {note: ''});
      toast('Trading paused', r.semantics || 'blocks new entries only');
      addMsg('[engine] trading paused — new entries blocked, open positions still managed', 'bot');
    } catch (e) { toastErr('Could not pause', e); }
  } else {
    try {
      await jpost('/api/trading/resume', {});
      toast('Trading resumed', 'new entries are allowed again (all other risk gates still apply)');
      addMsg('[engine] trading resumed — new entries allowed again', 'bot');
    } catch (e) { toastErr('Could not resume', e); }
  }
  refreshStats();
}
$('#btnPause').addEventListener('click', togglePause);

/* ===================================================== HFT book
   The separate high-frequency paper account: its own stats poll, equity
   chart, ALL-trades history table and decision feed — mode='hft' rows only,
   so the standard book's pages above never mix in HFT records. */
let hftChart = null;
let hftAutoResumeToasted = false;
let hftDefaultInterval = 20;
function buildHftEquityChart() {
  hftChart = new Chart($('#hftEquityChart'), {
    type: 'line',
    data: {labels: [], datasets: [{label: 'HFT equity', data: [],
      borderColor: cssVar('--chart-line'), backgroundColor: cssVar('--chart-fill'),
      fill: true, tension: .15, pointRadius: 0, borderWidth: 2}]},
    options: {responsive: true, maintainAspectRatio: false, animation: reduceMotion ? false : {duration: 250},
      plugins: {legend: {display: false}, tooltip: {backgroundColor: cssVar('--color-card'),
        borderColor: cssVar('--color-border'), borderWidth: 1,
        titleColor: cssVar('--color-foreground'), bodyColor: cssVar('--color-muted-foreground'),
        titleFont: {family: 'Fira Code'}, bodyFont: {family: 'Fira Code'},
        callbacks: {label: c => ' ' + fmt$(c.parsed.y)}}},
      scales: {x: {ticks: {maxTicksLimit: 8, color: cssVar('--color-muted-foreground'),
                           font: {family: 'Fira Code', size: 10}},
                   grid: {color: cssVar('--chart-grid')}},
               y: {ticks: {color: cssVar('--color-muted-foreground'),
                           font: {family: 'Fira Code', size: 10},
                           callback: v => '$' + v.toLocaleString()},
                   grid: {color: cssVar('--chart-grid')}}}}
  });
}

async function refreshHft() {
  let s;
  try { s = await jget('/api/hft/stats'); } catch (e) { return; }
  hftDefaultInterval = s.interval || hftDefaultInterval;
  $('#hftFeeHint').textContent = 'fee tier: ' + (s.fee_tier || 'perp') +
    ' · capital ' + fmt$(s.capital) + ' · 1m bars';
  const cards = [
    ['Equity', fmt$(s.broker_equity ?? s.current_equity ?? s.capital),
      (s.broker_equity ?? 0) > s.capital ? 'pos' : (s.broker_equity ?? 0) < s.capital ? 'neg' : '',
      'HFT book · start ' + fmt$(s.capital)],
    ['Total P&L', fmtPnl(s.total_pnl), s.total_pnl > 0 ? 'pos' : s.total_pnl < 0 ? 'neg' : '',
      fmtPct(s.return_pct) + ' return'],
    ['Closed trades', String(s.closed_trades ?? 0), '', 'win rate ' + (s.win_rate ?? 0) + '%'],
    ['Profit factor', s.profit_factor == null ? '∞' : s.profit_factor, '',
      s.profit_factor == null ? 'no losses yet' : 'gross win ÷ loss'],
    ['Max drawdown', fmtPct(s.max_drawdown_pct), 'neg', 'peak-to-trough'],
    ['Open positions', String((s.positions || []).length), '',
      s.engine_running ? 'live marks' : 'from journal'],
    ['Cycles', String(s.cycles ?? 0), '', s.engine_running ? 'running' : 'engine stopped'],
    ['Total fees', fmt$(s.total_fees ?? 0), 'neg', 'the HFT cost autopsy'],
  ];
  $('#hftStats').innerHTML = cards.map(c =>
    '<div class="stat"><div class="label">' + STAT_ICON + esc(c[0]) + '</div>' +
    '<div class="value ' + c[2] + '">' + esc(c[1]) + '</div>' +
    '<div class="sub">' + esc(c[3]) + '</div></div>').join('');

  $('#hftEngineDot').className = 'dot ' + (s.engine_running ? 'on' : 'off');
  $('#hftPillText').textContent = 'hft: ' +
    (s.engine_running ? (s.health_note ? 'degraded' : 'running') : 'stopped');
  $('#hftStartBtn').disabled = !!s.engine_running;
  $('#hftStopBtn').disabled = !s.engine_running;
  const note = $('#hftEngineNote');
  if (s.last_error) { note.hidden = false; note.textContent = 'last error: ' + s.last_error; }
  else if (s.health_note) { note.hidden = false; note.textContent = s.health_note; }
  else note.hidden = true;
  if (s.auto_resumed && !hftAutoResumeToasted) {
    hftAutoResumeToasted = true;
    toast('HFT book auto-resumed', 'the last session left it running (stop it from this tab)');
  }

  if (hftChart) {
    let eq;
    try { eq = await jget('/api/hft/equity'); } catch (e) { eq = []; }
    $('#hftEquityEmpty').hidden = eq.length > 0;
    if (eq.length) {
      hftChart.data.labels = eq.map(p => fmtTs(p.ts));
      hftChart.data.datasets[0].data = eq.map(p => p.equity);
      hftChart.update(reduceMotion ? 'none' : undefined);
    }
  }

  /* ALL HFT trades — the one place for the high-frequency history */
  let trades;
  try { trades = await jget('/api/hft/trades?limit=1000'); } catch (e) { trades = []; }
  const sel = $('#hftStratFilter');
  const current = sel.value;
  const strategies = [...new Set(trades.map(t => t.strategy))].sort();
  if (sel.options.length - 1 !== strategies.length ||
      [...sel.options].slice(1).map(o => o.value).join(',') !== strategies.join(',')) {
    sel.innerHTML = '<option value="">all strategies</option>' +
      strategies.map(x => '<option value="' + esc(x) + '">' + esc(x) + '</option>').join('');
    sel.value = current;
  }
  const filter = sel.value;
  const rows = filter ? trades.filter(t => t.strategy === filter) : trades;
  const tbody = $('#hftTradeTable tbody');
  $('#hftTradeEmpty').hidden = rows.length > 0;
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

  /* decision feed (HOLDs included — TRI-ETH monitor rows land here too) */
  let ds;
  try { ds = await jget('/api/hft/decisions?limit=30'); } catch (e) { ds = []; }
  const el = $('#hftDecisions');
  if (!ds.length) { el.innerHTML = '<div class="empty" style="padding:16px">No HFT decisions yet.</div>'; }
  else {
    el.innerHTML = ds.map(d => {
      const a = (d.action || '').toLowerCase();
      return '<div class="term-row">' +
        '<span class="term-ts">' + esc(fmtTs(d.ts)) + '</span>' +
        '<div class="term-body"><div class="term-line">' +
        '<span class="tag ' + esc(a === 'hold' ? 'hold' : a) + '">' + esc(d.action) + '</span>' +
        '<span class="term-mkt">' + esc(d.symbol) + ' <span class="tag tf">' + esc(d.timeframe) + '</span>' +
        (d.symbol === 'TRI-ETH' ? ' <span class="tag demo">arb</span>' : '') + '</span>' +
        '<span class="term-meta">regime ' + esc(d.regime || '—') + ' · conf ' +
          Math.round((d.confidence || 0) * 100) + '% · @ ' + fmtPx(d.price, d.symbol) + '</span>' +
        '</div><div class="term-why">' + esc(d.rationale || '') + '</div></div></div>';
    }).join('');
    el.scrollTop = 0;
  }
}
$('#hftStratFilter').addEventListener('change', refreshHft);

async function startHftEngine() {
  try {
    const r = await jpost('/api/hft/engine/start', {interval: hftDefaultInterval});
    toast('HFT engine ' + (r.status === 'started' ? 'started' : r.status),
          'cycle interval ' + hftDefaultInterval + 's · 1m bars', r.status !== 'error');
    addMsg('[hft] engine started — interval ' + hftDefaultInterval + 's', 'bot');
  } catch (e) { toastErr('Could not start HFT engine', e); }
  refreshHft();
}
async function stopHftEngine() {
  try {
    await jpost('/api/hft/engine/stop', {});
    toast('HFT engine stopped', '', true);
    addMsg('[hft] engine stopped', 'bot');
  } catch (e) { toastErr('Could not stop HFT engine', e); }
  refreshHft();
}
$('#hftStartBtn').addEventListener('click', startHftEngine);
$('#hftStopBtn').addEventListener('click', stopHftEngine);

/* ===================================================== Strategy Lab
   Pick any stock/pair -> apply the strategies registered for it -> backtest
   on real data. Works for BOTH books (standard + HFT) via the book toggle.
   The form options are derived from the server's registry (one source of
   truth), never hand-maintained in JS. */
let labChart = null;
let labMeta = null;
let labPollTimer = null;

function buildLabEquityChart() {
  labChart = new Chart($('#labEquityChart'), {
    type: 'line',
    data: {labels: [], datasets: [{label: 'Equity', data: [],
      borderColor: cssVar('--chart-line'), backgroundColor: cssVar('--chart-fill'),
      fill: true, tension: .15, pointRadius: 0, borderWidth: 2}]},
    options: {responsive: true, maintainAspectRatio: false, animation: reduceMotion ? false : {duration: 250},
      plugins: {legend: {display: false}, tooltip: {backgroundColor: cssVar('--color-card'),
        borderColor: cssVar('--color-border'), borderWidth: 1,
        titleColor: cssVar('--color-foreground'), bodyColor: cssVar('--color-muted-foreground'),
        titleFont: {family: 'Fira Code'}, bodyFont: {family: 'Fira Code'},
        callbacks: {label: c => ' ' + fmt$(c.parsed.y)}}},
      scales: {x: {ticks: {maxTicksLimit: 8, color: cssVar('--color-muted-foreground'),
                           font: {family: 'Fira Code', size: 10}},
                   grid: {color: cssVar('--chart-grid')}},
               y: {ticks: {color: cssVar('--color-muted-foreground'),
                           font: {family: 'Fira Code', size: 10},
                           callback: v => '$' + v.toLocaleString()},
                   grid: {color: cssVar('--chart-grid')}}}}
  });
}

async function refreshLab() {
  if (!labMeta) {
    try { labMeta = await jget('/api/lab/meta'); } catch (e) { return; }
    labFormRefresh();
  }
  /* while a run is in flight, poll its status (the form's 4s poll is too
     slow for a 10s backtest — poll fast ONLY while running) */
  const st = await jget('/api/lab/status').catch(() => null);
  if (st) labRenderStatus(st);
}

function labFormRefresh() {
  if (!labMeta) return;
  const book = $('#labBook').value, kind = $('#labKind').value;
  const tfs = (labMeta.timeframes[book] || {})[kind] || [];
  const sym = $('#labSymbol').value;
  const tfPrev = $('#labTimeframe').value;
  $('#labTimeframe').innerHTML = tfs.map(t => '<option value="' + t + '">' + t + '</option>').join('');
  $('#labTimeframe').value = tfs.includes(tfPrev) ? tfPrev : tfs[0];
  labStrategyRefresh();
  labDaysRefresh();
  $('#labFeeWrap').hidden = book !== 'hft';
  /* suggestion chips for the picked market */
  const chips = (labMeta.suggestions || {})[kind] || [];
  $('#labChips').innerHTML = chips.map(s =>
    '<button type="button" class="tag tf" style="cursor:pointer;border:1px solid var(--color-border)"' +
    ' data-sym="' + esc(s) + '">' + esc(s) + '</button>').join('');
  if (!sym) $('#labSymbol').value = chips[0] || '';
  $('#labSymbol').placeholder = kind === 'crypto' ? 'BTC/USDT'
    : kind === 'forex' ? 'EURUSD' : 'RELIANCE or ^NSEI';
}

function labStrategyRefresh() {
  const book = $('#labBook').value, tf = $('#labTimeframe').value;
  const strats = (labMeta.strategies[book] || {})[tf] || [];
  const prev = $('#labStrategy').value;
  $('#labStrategy').innerHTML = strats.map(s =>
    '<option value="' + esc(s) + '">' + (s === 'all' ? 'ALL strategies (comparison)' : esc(s)) + '</option>').join('');
  if (strats.includes(prev)) $('#labStrategy').value = prev;
  const cap = ((labMeta.days_cap[book] || {})[$('#labKind').value] || {})[tf];
  const def = ((labMeta.days_default[book] || {})[$('#labKind').value] || {})[tf];
  const note = [];
  if (cap != null) note.push('history cap ' + cap + 'd');
  if (tf === '1m' && $('#labKind').value !== 'crypto') note.push('yfinance caps 1m at 7d');
  $('#labNote').textContent = note.join(' · ');
}

function labDaysRefresh() {
  const book = $('#labBook').value, kind = $('#labKind').value, tf = $('#labTimeframe').value;
  const cap = ((labMeta.days_cap[book] || {})[kind] || {})[tf] || 365;
  const def = ((labMeta.days_default[book] || {})[kind] || {})[tf] || 180;
  const opts = [1, 3, 7, 14, 30, 60, 90, 180, 365, 730, 1825, 3650].filter(d => d <= cap);
  if (!opts.includes(def)) opts.push(def);
  opts.sort((a, b) => a - b);
  const prev = parseInt($('#labDays').value, 10);
  $('#labDays').innerHTML = opts.map(d =>
    '<option value="' + d + '"' + (d === (prev || def) ? ' selected' : '') + '>' + d + ' days</option>').join('');
}

async function labRun() {
  const body = {
    book: $('#labBook').value, kind: $('#labKind').value,
    symbol: $('#labSymbol').value.trim(), timeframe: $('#labTimeframe').value,
    strategy: $('#labStrategy').value,
    days: parseInt($('#labDays').value, 10) || 0,
    fee_tier: $('#labBook').value === 'hft' ? $('#labFee').value : null,
  };
  if (!body.symbol) { toastErr('Pick a symbol first', 'type one or tap a suggestion chip'); return; }
  $('#labRunBtn').disabled = true;
  try {
    await jpost('/api/lab/run', body);
    if (labPollTimer) clearInterval(labPollTimer);
    labPollTimer = setInterval(async () => {
      const st = await jget('/api/lab/status').catch(() => null);
      if (!st) return;
      labRenderStatus(st);
      if (st.status !== 'running') {
        clearInterval(labPollTimer); labPollTimer = null;
        $('#labRunBtn').disabled = false;
      }
    }, 1500);
  } catch (e) {
    $('#labRunBtn').disabled = false;
    toastErr('Lab refused the run', e);
  }
  labRenderStatus(await jget('/api/lab/status').catch(() => ({})));
}

function labRenderStatus(st) {
  const el = $('#labStatus');
  el.hidden = false;
  if (st.status === 'running') {
    el.textContent = '⏳ ' + (st.note || 'running…');
  } else if (st.status === 'error') {
    el.textContent = '✗ ' + (st.error || 'run failed');
  } else if (st.status === 'done') {
    el.hidden = true;
    labRenderResult(st.result);
  }
}

function labRenderResult(r) {
  if (!r) return;
  const s = r.spec;
  $('#labResultCard').hidden = false;
  $('#labResultTitle').textContent = s.symbol + ' ' + s.timeframe + ' · ' + r.stats.strategy;
  $('#labResultMeta').textContent = '[' + s.book + (s.fee_tier ? ' · ' + s.fee_tier : '') +
    '] ' + r.bars + ' bars · ' + r.window.first.slice(0, 10) + ' → ' + r.window.last.slice(0, 10) +
    ' · taker RT ~' + r.taker_round_trip_bps + 'bp';
  const st = r.stats;
  const cards = [
    ['Return', fmtPct(st.return_pct), st.return_pct > 0 ? 'pos' : st.return_pct < 0 ? 'neg' : '', 'full costs'],
    ['Total P&L', fmtPnl(st.total_pnl), st.total_pnl > 0 ? 'pos' : st.total_pnl < 0 ? 'neg' : '', 'on ' + fmt$(10000) + ' basis'],
    ['Trades', String(st.trades), '', 'win rate ' + st.win_rate_pct + '%'],
    ['Profit factor', st.profit_factor == null ? '∞' : st.profit_factor, '', 'gross win ÷ loss'],
    ['Max drawdown', fmtPct(st.max_drawdown_pct), 'neg', 'peak-to-trough'],
    ['Sharpe', st.sharpe == null ? '—' : st.sharpe, '', 'annualized'],
    ['Fees paid', fmt$(st.fees), 'neg', 'the cost autopsy'],
  ];
  $('#labStats').innerHTML = cards.map(c =>
    '<div class="stat"><div class="label">' + STAT_ICON + esc(c[0]) + '</div>' +
    '<div class="value ' + c[2] + '">' + esc(c[1]) + '</div>' +
    '<div class="sub">' + esc(c[3]) + '</div></div>').join('');
  $('#labCurveHint').textContent = r.stats.strategy + ' · ' + r.equity_curve.length + ' pts';
  if (labChart) {
    labChart.data.labels = r.equity_curve.map(p => fmtTs(p.ts));
    labChart.data.datasets[0].data = r.equity_curve.map(p => p.equity);
    labChart.update(reduceMotion ? 'none' : undefined);
  }
  $('#labExits').innerHTML = Object.entries(r.exit_reasons || {}).map(([k, v]) =>
    '<span class="tag tf">' + esc(k) + ' × ' + v + '</span>').join('') ||
    '<span class="hint">no exits</span>';
  /* comparison table (the "apply strategies" plural view) */
  const cmp = r.comparison;
  $('#labCompareCard').hidden = !cmp;
  if (cmp) {
    const sorted = [...cmp].sort((a, b) => b.total_pnl - a.total_pnl);
    $('#labCompareTable tbody').innerHTML = sorted.map(c =>
      '<tr><td class="mono" style="color:var(--color-blue)">' + esc(c.strategy) + '</td>' +
      '<td class="num ' + (c.return_pct > 0 ? 'pos' : c.return_pct < 0 ? 'neg' : '') + '">' + fmtPct(c.return_pct) + '</td>' +
      '<td class="num ' + (c.total_pnl > 0 ? 'pos' : c.total_pnl < 0 ? 'neg' : '') + '">' + fmtPnl(c.total_pnl) + '</td>' +
      '<td class="num">' + c.trades + '</td>' +
      '<td class="num">' + c.win_rate_pct + '%</td>' +
      '<td class="num">' + esc(c.profit_factor == null ? '∞' : c.profit_factor) + '</td>' +
      '<td class="num neg">' + fmtPct(c.max_drawdown_pct) + '</td>' +
      '<td class="num">' + esc(c.sharpe == null ? '—' : c.sharpe) + '</td>' +
      '<td class="num">' + fmt$(c.fees) + '</td></tr>').join('');
  }
  /* trades of the best strategy (last 200) */
  const trades = r.trades || [];
  $('#labTradesCard').hidden = trades.length === 0;
  $('#labTradesHint').textContent = trades.length + ' most recent (of ' + st.trades + ')';
  $('#labTradeTable tbody').innerHTML = trades.slice().reverse().map(t =>
    '<tr><td class="mono" style="color:var(--color-muted-foreground)">' + esc(fmtTs(t.entry_ts)) + '</td>' +
    '<td>' + sideTag(t.side) + '</td>' +
    '<td class="num">' + fmtQty(t.qty) + '</td>' +
    '<td class="num">' + fmtPx(t.entry_price, t.symbol) + '</td>' +
    '<td class="num">' + fmtPx(t.exit_price, t.symbol) + '</td>' +
    '<td class="num ' + posCls(t.pnl) + '">' + fmtPnl(t.pnl) + '</td>' +
    '<td style="color:var(--color-muted-foreground)">' + esc(t.exit_reason || '—') + '</td></tr>').join('');
  toast('Lab run complete', r.stats.strategy + ': ' + fmtPct(r.stats.return_pct) +
        ' over ' + st.trades + ' trades', r.stats.return_pct >= 0);
}

$('#labBook').addEventListener('change', labFormRefresh);
$('#labKind').addEventListener('change', () => { labFormRefresh(); });
$('#labTimeframe').addEventListener('change', labStrategyRefresh);
$('#labChips').addEventListener('click', e => {
  const b = e.target.closest('[data-sym]');
  if (b) { $('#labSymbol').value = b.dataset.sym; }
});
$('#labRunBtn').addEventListener('click', labRun);

/* ===================================================== market toggle
   One market at a time: ON = Forex (crypto + forex universe), OFF = India
   (NSE). The switch POSTs /api/market/mode; the orphan guard on the server
   answers 409 + requires_confirm when open paper positions exist — the
   first click NEVER closes anything, it only opens this dialog, and only
   the dialog's confirm button sends confirm_close_positions=true. */
let marketMode = null;        // 'forex' | 'india' — the last state the UI rendered
let marketPending = null;     // target mode while the confirm dialog is open
let marketBusy = false;       // in-flight guard: a double-click must not double-POST
const MARKET_NAMES = {forex: 'the Forex paper universe (crypto + forex)',
                      india: 'the India (NSE) paper universe'};

function applyMarketMode(mode) {
  if (mode === marketMode) return;
  marketMode = mode;
  $('#mktForex').setAttribute('aria-pressed', String(mode === 'forex'));
  $('#mktIndia').setAttribute('aria-pressed', String(mode === 'india'));
  $('#marketBanner').classList.add('show');
  $('#marketBannerText').textContent = mode === 'india'
    ? 'Market: India (NSE) — Nifty 50 and NSE shares'
    : 'Market: Forex — crypto and forex pairs';
  $('#marketBannerSub').textContent =
    'One market at a time. Your market choice is remembered — restarting the dashboard keeps the same market.';
  /* every surface that names the markets follows the mode (the brand line
     used to say "crypto + forex" statically — wrong in India mode) */
  $('#brandSub').textContent = (mode === 'india' ? 'India (NSE)' : 'crypto + forex')
    + ' · paper trading · IST';
}

function marketSwitchedToast(mode, closed) {
  toast('Market switched', 'Now trading ' + (MARKET_NAMES[mode] || mode) +
    (closed ? ' — ' + closed + ' open position' + (closed > 1 ? 's' : '') +
      ' closed at their last prices' : ''));
  addMsg('[engine] market switched to ' + mode + ' — now trading ' +
    (MARKET_NAMES[mode] || mode), 'bot');
}

async function doMarketSwitch(mode, confirmClose) {
  if (marketBusy) return false;
  marketBusy = true;
  try {
    const r = await jpost('/api/market/mode',
                          {mode, confirm_close_positions: !!confirmClose});
    applyMarketMode(mode);
    if (r.status === 'switched') marketSwitchedToast(mode, r.closed);
    else toast('Market unchanged', 'already on ' + (MARKET_NAMES[mode] || mode));
    refreshWatchlist(); refreshStats(); refreshDecisions();
    return true;
  } catch (e) {
    if (e.status === 409 && e.data && e.data.requires_confirm) {
      // the orphan guard refused the first click — show the confirmation
      // dialog (plain language, both what it does and does not do). NOTE:
      // FastAPI wraps the structured 409 body inside "detail", so the
      // payload the guard built lives at e.data.detail.
      const body = e.data.detail && e.data.detail.requires_confirm
        ? e.data.detail : e.data;
      const n = (body.open_positions || []).length;
      $('#marketModalMsg').textContent =
        'You have ' + n + ' open paper position' + (n === 1 ? '' : 's') + '. ' +
        'Switching markets will close them at their last prices so none are left orphaned. ' +
        'Blocks nothing else — this only changes which markets the bot watches.';
      marketPending = mode;
      $('#marketModal').classList.add('open');
      return false;
    }
    toastErr('Could not switch market', e);
    return false;
  } finally {
    marketBusy = false;
  }
}

$('#mktForex').addEventListener('click', () => {
  if (marketMode !== 'forex') doMarketSwitch('forex', false);
});
$('#mktIndia').addEventListener('click', () => {
  if (marketMode !== 'india') doMarketSwitch('india', false);
});
$('#marketCancel').addEventListener('click', () => {
  $('#marketModal').classList.remove('open');
  marketPending = null;
});
$('#marketModal').addEventListener('click', e => {
  if (e.target === e.currentTarget) e.currentTarget.classList.remove('open');
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') $('#marketModal').classList.remove('open');
});
$('#marketGo').addEventListener('click', async () => {
  const mode = marketPending;
  $('#marketModal').classList.remove('open');
  marketPending = null;
  if (mode) await doMarketSwitch(mode, true);   // explicit confirm: close + switch
});

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
    '<td class="mono"><b>' + esc(t.symbol) + '</b> <span class="tag tf">' + esc(t.timeframe || '') + '</span>' +
      (t.mode === 'demo' ? ' <span class="tag demo">demo</span>' : '') + '</td>' +
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

/* ===================================================== evidence */
/* read-only render of the GENERATED artifacts: data/results/*.json
   (validate/shadow), data/kronos_ic.json, data/manifest.json — the tab
   computes nothing from trade data, it presents what the CLI wrote */
let kronosChart = null, cvChart = null;
let EV_REPORTS = [];

async function refreshEvidence() {
  evLoaded.v = true;
  let ev;
  try { ev = await jget('/api/evidence'); } catch (e) { return; }

  /* --- Kronos rolling IC vs its own hurdle --- */
  const k = ev.kronos || {};
  const S = k.series || [];
  const labels = S.map(p => p.i);
  if (kronosChart) { kronosChart.destroy(); kronosChart = null; }
  $('#krMeta').textContent = k.error ? ('ledger unavailable: ' + k.error)
    : (k.n ? k.n + ' resolved forecasts · pending ' + (k.pending ?? 0) +
        ' · rolling IC ' + (k.ic ?? '—') : '');
  $('#krEmpty').hidden = labels.length > 0;
  if (labels.length) {
    kronosChart = new Chart($('#kronosChart'), {
      type: 'line',
      data: { labels, datasets: [
        { label: 'rolling IC', data: S.map(p => p.ic),
          borderColor: cssVar('--color-blue'), pointRadius: 0, borderWidth: 1.5, tension: 0.25 },
        { label: 'promotion hurdle ' + (k.hurdle ?? 0.02), data: labels.map(() => k.hurdle ?? 0.02),
          borderColor: cssVar('--color-pos'), borderDash: [6, 4], pointRadius: 0, borderWidth: 1 },
        { label: 'demotion floor ' + (k.demote_below ?? 0), data: labels.map(() => k.demote_below ?? 0),
          borderColor: cssVar('--color-neg'), borderDash: [4, 4], pointRadius: 0, borderWidth: 1 }]},
      options: { responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { labels: { color: cssVar('--color-muted-foreground'),
                                       boxWidth: 10, font: { size: 10 } } } },
        scales: {
          x: { ticks: { color: cssVar('--color-muted-foreground'), maxTicksLimit: 8,
                        font: { size: 10 } }, grid: { color: cssVar('--chart-grid') } },
          y: { ticks: { color: cssVar('--color-muted-foreground'), font: { size: 10 } },
               grid: { color: cssVar('--chart-grid') } } } }
    });
  }

  /* --- validation reports: selector + verdict cards + path chart --- */
  EV_REPORTS = ev.validations || [];
  const sel = $('#evReportSel');
  const cur = sel.value;
  sel.innerHTML = '<option value="">— no report selected —</option>' +
    EV_REPORTS.map((r, i) => '<option value="' + i + '">' +
      esc((r.symbol || '?') + ' ' + (r.timeframe || '') + ' · ' +
          (r.strategy || '?') + ' · ' +
          (r.start ? r.start + '→' + (r.end || 'now') : (r.days || '?') + 'd')) + '</option>').join('');
  sel.value = (cur !== '' && Number(cur) < EV_REPORTS.length) ? cur
                                                             : (EV_REPORTS.length ? '0' : '');
  renderEvidenceCards();
  renderEvidencePaths();

  /* --- shadow adherence --- */
  const sh = ev.shadow;
  if (!sh || !sh.profile) {
    $('#evShadow').innerHTML = '<div class="empty">no shadow report yet — run ' +
      '<code>python3 main.py shadow</code> (writes data/results/shadow_report.json)</div>';
  } else {
    const rows = Object.entries(sh.symbols || {}).map(([name, s]) =>
      '<tr><td class="mono"><b>' + esc(name) + '</b></td>' +
      '<td class="num">' + esc(s.adherence_pct) + '%</td>' +
      '<td class="num">' + esc(s.on_rule) + '</td>' +
      '<td class="num">' + esc(s.late) + '</td>' +
      '<td class="num ' + (s.rule_breaks ? 'neg' : '') + '">' + esc(s.rule_breaks) + '</td>' +
      '<td class="num">' + esc(s.unknown) + '</td></tr>').join('');
    $('#evShadow').innerHTML = '<table><thead><tr><th>market</th><th class="num">on-rule %</th>' +
      '<th class="num">on-rule</th><th class="num">late</th><th class="num">rule breaks</th>' +
      '<th class="num">unknown</th></tr></thead><tbody>' + (rows ||
        '<tr><td colspan="6" class="empty">no auditable trades in the report</td></tr>') +
      '</tbody></table><div class="hint" style="padding:8px 12px">profile over ' +
      esc(sh.profile.n_trades) + ' closed trades · win rate ' + esc(sh.profile.win_rate_pct) +
      '% · blew through stop: ' + esc(sh.profile.n_blew_through_stop) +
      ' · disposition gap ' + esc(sh.profile.disposition_gap_hours) + 'h</div>';
  }

  /* --- pinned-data manifest --- */
  const man = ev.manifest || {};
  const keys = Object.keys(man);
  $('#evManifest').innerHTML = keys.length
    ? '<table><thead><tr><th>market</th><th class="num">bars</th><th>window</th>' +
      '<th>source</th><th>sha256</th><th>fetched</th></tr></thead><tbody>' +
      keys.map(kk => { const m = man[kk];
        return '<tr><td class="mono"><b>' + esc(kk) + '</b></td>' +
          '<td class="num">' + esc(m.bars) + '</td>' +
          '<td class="mono">' + esc(String(m.first_ts).slice(0, 10) + ' → ' +
                                    String(m.last_ts).slice(0, 10)) + '</td>' +
          '<td>' + esc(m.source) + '</td>' +
          '<td class="mono">' + esc(String(m.sha256).slice(0, 12)) + '…</td>' +
          '<td class="mono">' + esc(String(m.fetched_at).slice(0, 10)) + '</td></tr>';
      }).join('') + '</tbody></table>' +
      '<div class="hint" style="padding:8px 12px">fetch with <code>--start/--end</code> for ' +
      'byte-identical pinned windows; checksums make BACKTESTS.md claims checkable</div>'
    : '<div class="empty">no pinned fetches yet — run a backtest with ' +
      '<code>--start YYYY-MM-DD --end YYYY-MM-DD</code></div>';
}

function renderEvidenceCards() {
  const r = EV_REPORTS[$('#evReportSel').value] || null;
  const cards = r ? [
    ['PBO', r.pbo ? r.pbo.pbo : '—',
      r.pbo && r.pbo.pbo >= 0.5 ? 'neg' : (r.pbo && r.pbo.pbo >= 0.35 ? '' : 'pos'),
      r.pbo ? r.pbo.verdict : 'needs a ≥2-strategy family'],
    ['Deflated Sharpe', r.deflated_sharpe ? r.deflated_sharpe.deflated_sharpe : '—',
      r.deflated_sharpe && r.deflated_sharpe.deflated_sharpe >= 0.95 ? 'pos' : '',
      r.deflated_sharpe ? r.deflated_sharpe.verdict : 'pass --trial-sharpes'],
    ['MC terminal p5', r.monte_carlo && r.monte_carlo.n_sims
        ? '$' + r.monte_carlo.terminal_p5.toLocaleString() : '—', 'neg',
      '5th-percentile resampled outcome'],
    ['MinTRL', r.min_trl && r.min_trl.min_bars ? r.min_trl.min_years + 'y' : '—', '',
      r.min_trl && r.min_trl.min_bars
        ? Number(r.min_trl.min_bars).toLocaleString() + ' OOS bars @95%' : 'needs a positive Sharpe'],
    ['Backtest return', r.backtest ? fmtPct(r.backtest.return_pct) : '—',
      r.backtest && r.backtest.return_pct > 0 ? 'pos' : 'neg',
      r.backtest ? (r.backtest.trades + ' trades · sharpe ' + r.backtest.sharpe +
        ' · window ' + (r.start ? r.start + '→' + (r.end || 'now') : (r.days || '?') + 'd')) : ''],
  ] : [
    ['PBO', '—', '', 'run make validate'], ['Deflated Sharpe', '—', '', 'run make validate'],
    ['MC terminal p5', '—', '', 'run make validate'], ['MinTRL', '—', '', 'run make validate'],
    ['Backtest return', '—', '', 'no reports yet'],
  ];
  $('#evCards').innerHTML = cards.map(c =>
    '<div class="stat"><div class="label">' + esc(c[0]) + '</div>' +
    '<div class="value ' + c[2] + '">' + esc(c[1]) + '</div>' +
    '<div class="sub">' + esc(c[3]) + '</div></div>').join('');
}

function renderEvidencePaths() {
  if (cvChart) { cvChart.destroy(); cvChart = null; }
  const r = EV_REPORTS[$('#evReportSel').value] || null;
  const paths = r && r.purged_cv ? (r.purged_cv.paths || []) : [];
  $('#cvEmpty').hidden = paths.some(p => p.trades);
  if (!paths.length) return;
  cvChart = new Chart($('#cvChart'), {
    type: 'bar',
    data: { labels: paths.map((p, i) => 'p' + (i + 1) + (p.trades ? '' : ' ·')),
      datasets: [{ label: 'OOS return %',
        data: paths.map(p => p.trades ? p.return_pct : null),
        backgroundColor: paths.map(p => p.trades
          ? (p.return_pct >= 0 ? cssVar('--color-pos') : cssVar('--color-neg'))
          : cssVar('--color-muted')) }]},
    options: { responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false } },
      scales: { x: { ticks: { color: cssVar('--color-muted-foreground'), font: { size: 9 } },
                     grid: { display: false } },
                y: { ticks: { color: cssVar('--color-muted-foreground'), font: { size: 10 },
                              callback: v => v + '%' },
                     grid: { color: cssVar('--chart-grid') } } } }
  });
}
$('#evReportSel').addEventListener('change', () => { renderEvidenceCards(); renderEvidencePaths(); });

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
    chatLoaded.v = false;
    $('#chatlog').innerHTML = '';
    refreshAccount(); refreshStats(); refreshEquity();
  } catch (e) { toastErr('Reset failed', e); }
});

/* ===================================================== chat */
function addMsg(text, role) {
  const log = $('#chatlog');
  const d = document.createElement('div');
  d.className = 'msg ' + (role === 'user' ? 'user' : 'bot');
  d.textContent = text;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}
async function loadChatHistory() {
  chatLoaded.v = true;
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
   Chart.js is VENDORED (served from this app at /chart.umd.min.js, no CDN
   call): on venue/offline WiFi the SPA works regardless. If the vendored
   library somehow fails to load we show a notice and render everything else —
   the equity chart canvas just stays empty instead of killing routing,
   polling and every button listener with a ReferenceError. */
if (typeof Chart === 'undefined') {
  const banner = document.createElement('div');
  banner.className = 'chart-offline';
  banner.textContent = 'Chart.js could not load (offline?) — the equity chart is ' +
    'disabled, everything else works normally.';
  const main = document.querySelector('main') || document.body;
  main.insertBefore(banner, main.firstChild);
} else {
  buildEquityChart();
  buildHftEquityChart();
  buildLabEquityChart();
}
/* sync the switcher + browser chrome with the theme <head> already applied;
   runs after buildEquityChart so applyChartTheme sees a live chart */
applyTheme(document.documentElement.getAttribute('data-theme') || 'light', false);
setView(location.hash.slice(1) || 'overview');
poll();
</script>
</body>
</html>
"""
