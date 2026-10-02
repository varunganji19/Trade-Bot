"""Standard book API: headline stats, equity, trades and decisions, the
watchlist, open positions, engine controls and the manual pause. Shared
state (the journal, the engine handle and its lock) is read from
bot.dashboard at call time."""
from __future__ import annotations

import threading
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query

from bot import dashboard as core
from bot.api.models import EmptyIn, EngineIn, PauseIn, PositionCloseIn, WatchlistIn
from bot.pause import is_paused, set_paused
from config import CONFIG, MAX_WATCHLIST_SPECS, MarketSpec

router = APIRouter()


# ---------------------------------------------------------------------------
# stats / history
@router.get("/api/stats")
def api_stats():
    # the headline cards are the standard book's OWN paper record, always.
    # Falling back to every mode (for the since-deleted demo seeder) put the
    # demo trades' P&L and a demo+fast-book drawdown beside a paper equity
    # card — e.g. right after an account reset.
    modes = core.journal.trade_mode_counts()
    stats = core.journal.stats(mode="paper")
    # seeded demo rows (seed-demo backtest replays) are labeled mode='demo' —
    # surface the split so the UI can badge them instead of passing them off
    # as the bot's own paper record
    stats["trade_modes"] = modes
    # boot-resume notice: the engine started by auto-resume (not by the
    # operator's click) — the UI toasts it once so trading never silently begins
    stats["auto_resumed"] = core._AUTO_RESUMED_AT_BOOT
    eng = core._get_engine()
    stats["engine_running"] = eng is not None
    stats["engine_state"] = ("running" if eng is not None else "starting" if core._engine_starting
                             else "stopping" if core._engine_thread is not None
                             and core._engine_thread.is_alive() else "stopped")
    stats["entries_halted"] = bool(eng is not None and eng.risk.halted)
    stats["vetoes"] = core._veto_payload(eng)
    stats["strategies"] = core._voting_payload("standard")
    stats["cycles"] = eng.cycles if eng is not None else 0
    # the chosen cadence rides the SAME poll the Interval select follows
    # (/api/stats, not /api/engine/status — the UI polls this one), so a
    # change made from another tab or session shows up within a tick
    stats["interval"] = core._engine_interval
    stats["watchlist_count"] = len(CONFIG.watchlist)
    # manual pause rides the same poll as health_note (the banner + button
    # must flip within one 4s tick, without a second request). Reported for a
    # stopped engine too — the flag file outlives any single engine run.
    paused, pause_note = is_paused()
    if eng is not None:
        paused = paused or bool(getattr(eng.risk, "paused", False))
    stats["paused"] = paused
    stats["paused_note"] = pause_note
    # the books' cash must reconcile with their own history on every poll
    # (Journal.ledger_check); a gap is shown, never averaged away
    stats["ledger"] = {book: core._ledger_payload(mode) for book, mode in
                       (("standard", "paper"), ("fast", "hft"))}
    # strategies the drift monitor demoted this week (bot/drift.py): the
    # Strategies box shows them as silent, the banner says it just happened
    try:
        from bot.drift import recent_alerts
        stats["drift_alerts"] = recent_alerts()
    except Exception:
        stats["drift_alerts"] = []
    if eng is not None:
        stats["llm_mode"] = eng.llm.provider if eng.llm.enabled else "quant"
        # live-state trio via the shared helper (marks come from the engine's
        # TTL-cached frames; iterating the live positions dict would race the
        # engine's open/close mutations -> "dict changed size" 500s)
        positions, marks, price_map = core._live_state(eng)
        stats["open_positions"] = [core._position_dict(p, marks) for p in positions]
        stats["broker_equity"] = round(eng.broker.equity(price_map), 2)
        stats["engine_error"] = eng.last_error
        # degraded-but-alive conditions (e.g. a held position behind a dead
        # feed) ride here — the UI turns the pill amber and shows a banner
        stats["health_note"] = getattr(eng, "health_note", None)
    else:
        stats["llm_mode"] = "quant"
        stats["open_positions"] = [core._journal_position_dict(t)
                                   for t in core.journal.open_trades(mode="paper")]
        stats["health_note"] = None
    return stats


def _downsample(rows: list, cap: int = 500) -> list:
    if len(rows) <= cap:
        return rows
    step = len(rows) / cap
    return [rows[int(i * step)] for i in range(cap)]


@router.get("/api/equity")
def api_equity(limit: int = Query(default=500, ge=1, le=3000),
               since_id: int | None = Query(default=None, ge=0)):
    # the account curve is the paper record; NEVER fall back to all modes
    # (a paper-empty book must read empty, labeled demo_only — mixing demo
    # rows into the paper curve overstated the account). Empty returns an
    # envelope so the UI can badge demo_only; non-empty stays a bare list
    # (the chart's eq.map contract).
    try:
        rows = core.journal.equity_curve(limit=min(limit, 3000), mode="paper",
                                    since_id=since_id)
    except Exception as exc:
        if core._is_busy_error(exc):
            raise HTTPException(503, "journal is busy — retry shortly")
        raise
    if not rows:
        return {"rows": [], "demo_only": True}
    # downsample to <=500 points for the chart (a year of sub-minute points
    # is thousands of rows per 4s poll); paged reads skip downsampling
    if since_id is None:
        rows = _downsample(rows, 500)
    return rows


@router.get("/api/trades")
def api_trades(limit: int = Query(default=100, ge=1, le=1000),
               since_id: int | None = Query(default=None, ge=0)):
    try:
        # the standard book's history: its paper trades plus the badged demo
        # replays — never the fast book's rows (they have their own view)
        return core.journal.recent_trades(limit=limit, mode=("paper", "demo"), since_id=since_id)
    except Exception as exc:
        if core._is_busy_error(exc):
            raise HTTPException(503, "journal is busy — retry shortly")
        raise


@router.get("/api/decisions")
def api_decisions(limit: int = Query(default=40, ge=1, le=500),
                  since_id: int | None = Query(default=None, ge=0)):
    # paper feed first; with no paper decisions the demo rows render, badged
    # as backtest replays. Never the fast book's decisions (its own view).
    try:
        rows = core.journal.recent_decisions(limit=limit, mode="paper",
                                        since_id=since_id)
        if not rows and since_id is None:
            rows = core.journal.recent_decisions(limit=limit, mode="demo")
    except Exception as exc:
        if core._is_busy_error(exc):
            raise HTTPException(503, "journal is busy — retry shortly")
        raise
    return rows


# ---------------------------------------------------------------------------
# watchlist CRUD — the markets the bot trades
@router.get("/api/watchlist")
def api_watchlist_get():
    with core._wl_lock:
        return [{"kind": s.kind, "symbol": s.symbol, "timeframe": s.timeframe,
                 "display": s.display, "strategies": core.STRATEGIES_BY_TF.get(s.timeframe, [])}
                for s in CONFIG.watchlist]


@router.post("/api/watchlist", status_code=201)
def api_watchlist_add(body: WatchlistIn):
    kind, symbol, timeframe = core._validate_spec(body.kind, body.symbol, body.timeframe)
    # USD-only book: the india kind (and its INR accounting) was removed on
    # 2026-09-19, so the guard is now a straight kind check
    allowed = {"crypto", "forex"}
    if kind not in allowed:
        raise HTTPException(409, f"kind {kind!r} is not tradable "
                                 f"(allowed: {sorted(allowed)})")
    display = (body.display or "").strip()
    with core._wl_lock:
        current = list(CONFIG.watchlist)
        if any(s.symbol == symbol and s.timeframe == timeframe for s in current):
            raise HTTPException(409, f"{symbol} {timeframe} is already on the watchlist")
        if len(current) >= MAX_WATCHLIST_SPECS:
            raise HTTPException(422, f"watchlist is full ({MAX_WATCHLIST_SPECS} specs max)")
        spec = MarketSpec(kind, symbol, timeframe, display)
        current.append(spec)
        core._persist_and_install(current)
    return {"status": "added", "spec": spec.to_dict(), "count": len(current)}


@router.delete("/api/watchlist/{kind}/{symbol:path}/{timeframe}")
def api_watchlist_delete(kind: str, symbol: str, timeframe: str):
    # `:path` lets the crypto symbol's own slash live in the URL (BTC/USDT);
    # %2F-encoded symbols unquote to the same thing
    kind, symbol, timeframe = core._validate_spec(kind, unquote(symbol), timeframe)
    with core._wl_lock:
        current = list(CONFIG.watchlist)
        target = next((s for s in current
                       if s.symbol == symbol and s.timeframe == timeframe), None)
        if target is None:
            raise HTTPException(404, f"{symbol} {timeframe} is not on the watchlist")
        eng = core._get_engine()
        if eng is not None and any(k == (symbol, timeframe) for k in eng.broker.positions):
            raise HTTPException(
                409, f"cannot remove {symbol} {timeframe}: an open position is held on it — "
                     f"close the position first (Portfolio tab)")
        # also guard the engine-off case: an OPEN journal trade on the spec
        # would restore on next start into a watchlist that no longer manages
        # it (stops never checked, marks never fetched — a zombie position)
        if any(t.get("symbol") == symbol and (t.get("timeframe") or core._LEGACY_TF) == timeframe
               for t in core.journal.open_trades(mode="paper")):
            raise HTTPException(
                409, f"cannot remove {symbol} {timeframe}: an open journaled trade exists "
                     f"on it — close the position first (Portfolio tab)")
        current = [s for s in current if not (s.symbol == symbol and s.timeframe == timeframe)]
        core._persist_and_install(current)
    return {"status": "removed", "symbol": symbol, "timeframe": timeframe, "count": len(current)}


# ---------------------------------------------------------------------------
# open positions + manual close
@router.get("/api/positions")
def api_positions():
    """Alias for the open-positions block of /api/stats — the name an operator
    (or a curl sanity check) guesses first."""
    eng = core._get_engine()
    if eng is not None:
        positions, marks, _ = core._live_state(eng)
        return {"live": True, "positions": [core._position_dict(p, marks) for p in positions]}
    return {"live": False,
            "positions": [core._journal_position_dict(t) for t in core.journal.open_trades(mode="paper")]}


@router.post("/api/positions/close")
def api_position_close(body: PositionCloseIn):
    eng = core._get_engine()
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


@router.post("/api/engine/start")
def api_engine_start(body: EngineIn):
    # cheap guard BEFORE any construction: a repeat POST while running must
    # not pay for a full TradingEngine build (position restore) only to throw
    # it away
    existing = core._get_engine()
    if existing is not None:
        return {"status": "already_running", "cycles": existing.cycles}
    result = core._spawn_engine(body.interval)
    if result["status"] == "owned":
        raise HTTPException(409, result["detail"])
    if result["status"] == "started":
        core._state_warning(result, core._write_engine_state(True, body.interval), "engine started")
    return result


@router.post("/api/engine/stop")
def api_engine_stop(body: EmptyIn):
    """Quiesce: clear the global and return immediately; the bounded join runs
    in a background thread, so the endpoint never blocks the UI or the reset
    flow for up to 300s. The desired=stopped state is persisted
    SYNCHRONOUSLY before returning so an immediate state-file read (and a
    dashboard restart) sees the stop even while the join is still in flight."""
    with core._engine_lock:
        if core._engine is None:
            ok = core._write_engine_state(False, CONFIG.live_interval_seconds)
            status = "starting" if core._engine_starting else (
                "stopping" if core._engine_thread is not None and core._engine_thread.is_alive()
                else "not_running")
            return core._state_warning({"status": status}, ok, "engine stopped")
        core._engine = None
        stop_interval = core._engine_interval
    th = core._engine_thread
    if th is not None and th is not threading.current_thread() and th.is_alive():
        ok = core._write_engine_state(False, stop_interval)
        core._join_in_background(th, lambda iv: core._write_engine_state(False, iv),
                            stop_interval)
        return core._state_warning({"status": "stopping"}, ok, "engine stopped")
    ok = core._write_engine_state(False, stop_interval)
    return core._state_warning({"status": "stopped"}, ok, "engine stopped")


@router.post("/api/engine/interval")
def api_engine_interval(body: EngineIn):
    """Retune the cycle cadence — for a RUNNING engine too.

    Both loops re-read the interval every cycle (bot/engines.py), so this
    takes effect on the next wake without a stop/start (which would rebuild
    the engine and re-claim the book lease)."""
    core._engine_interval = body.interval
    running = core._get_engine() is not None
    # persist so auto-resume comes back on the NEW cadence (a stop writes the
    # then-current value, so the two never disagree)
    core._write_engine_state(running, body.interval)
    return {"status": "ok", "interval": body.interval, "running": running}


@router.get("/api/engine/status")
def api_engine_status():
    # paused = the operator's manual halt. It is shown for a STOPPED engine
    # too: the flag is a file (outlives the engine) and a fresh start must
    # visibly carry the pause, never silently resume trading.
    paused, _ = is_paused()
    eng = core._get_engine()
    th = core._engine_thread
    if eng is not None:
        paused = paused or bool(getattr(eng.risk, "paused", False))
        return {"running": True, "cycles": eng.cycles,
                "llm": eng.llm.provider if eng.llm.enabled else "quant",
                "positions": len(eng.broker.positions_snapshot()),
                "interval": core._engine_interval,
                "alive": bool(th is not None and th.is_alive()),
                "last_error": eng.last_error or core._last_engine_error,
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
            "interval": core._engine_interval,
            "alive": bool(th is not None and th.is_alive()),
            "last_error": core._last_engine_error, "health_note": None,
            "paused": paused,
            }


@router.post("/api/trading/pause")
def api_trading_pause(body: PauseIn):
    """Set the manual pause. The file write persists the halt for a future
    engine/dashboard restart; when an engine IS live its risk.paused is set
    in the same call so the halt takes effect before the next cycle (a new
    entry could otherwise slip in during the interval). Entries only — open
    positions keep their stops/targets/exits; nothing is force-closed."""
    if not set_paused(True, body.note):
        raise HTTPException(500, "could not write the pause flag (disk error?) — "
                               "trading is NOT paused")
    eng = core._get_engine()
    if eng is not None:
        eng.risk.paused = True
    return {"status": "paused",
            "note": body.note,
            "semantics": "blocks new entries only — open positions are still "
                         "managed (stops, targets, strategy exits). Nothing is "
                         "force-closed."}


@router.post("/api/trading/resume")
def api_trading_resume(body: EmptyIn):
    """Clear the manual pause (new entries allowed again; every other risk
    gate still applies). Resuming WRITES paused:false rather than deleting
    the flag — a visible record beats an absence that reads as 'never paused'."""
    if not set_paused(False):
        raise HTTPException(500, "could not write the pause flag (disk error?) — "
                               "trading is still paused")
    eng = core._get_engine()
    if eng is not None:
        eng.risk.paused = False
    return {"status": "resumed"}
