"""Fast book API (mode='hft', experimental): its own stats, equity, trades,
decisions, candles and engine controls. Every read filters mode='hft', so
the standard book's pages never show these rows. Shared state (the journal,
the fast engine handle, its lock) is read from bot.dashboard at call time."""
from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException, Query

from bot import dashboard as core
from bot.api.models import EmptyIn, HftEngineIn
from bot.pause import is_paused
from config import CONFIG

router = APIRouter()


# ------------------------------------------------------------- HFT book API
# The high-frequency paper book (mode='hft'): separate engine thread, account,
# universe (1m crypto + forex), and the ONE PLACE all HFT trades live. Every
# read filters mode='hft' — the standard book's pages never show these rows.
@router.get("/api/hft/stats")
def api_hft_stats():
    h = CONFIG.hft
    stats = core.journal.stats(mode="hft")
    eng = core._get_hft_engine()
    positions = []
    if eng is not None:
        positions, marks, price_map = core._live_state(eng)
        stats["engine_running"] = True
        stats["cycles"] = eng.cycles
        stats["broker_equity"] = round(eng.broker.equity(price_map), 2)
        stats["last_error"] = eng.last_error
        stats["health_note"] = getattr(eng, "health_note", None)
    else:
        marks = {}
        stats["engine_running"] = False
        stats["cycles"] = 0
        stats["last_error"] = core._last_hft_error
    stats["positions"] = [core._position_dict(p, marks) for p in positions]
    stats["vetoes"] = core._veto_payload(eng)
    stats["strategies"] = core._voting_payload("fast")
    stats["capital"] = h.paper_capital
    from bot.hft import hft_fee_tier
    stats["fee_tier"] = hft_fee_tier()
    stats["interval"] = core._hft_interval     # chosen cadence, running or not
    stats["auto_resumed"] = core._HFT_AUTO_RESUMED_AT_BOOT
    stats["paused"], _ = is_paused()
    return stats


@router.get("/api/hft/equity")
def api_hft_equity():
    return core.journal.equity_curve(limit=2000, mode="hft")


@router.get("/api/hft/trades")
def api_hft_trades(limit: int = Query(default=1000, ge=1, le=1000)):
    """ALL high-frequency trades in one place (mode='hft' rows, newest first)."""
    return core.journal.recent_trades(limit=limit, mode="hft")


@router.get("/api/hft/decisions")
def api_hft_decisions(limit: int = Query(default=50, ge=1, le=200)):
    return core.journal.recent_decisions(limit=limit, mode="hft")


@router.get("/api/hft/candles")
def api_hft_candles(symbol: str = Query(default=""),
                    limit: int = Query(default=180, ge=20, le=1000)):
    """Recent candles + EMA20 for ONE market on the fast book.

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
        raise HTTPException(404, f"{sym} is not on the fast-book watchlist")
    eng = core._get_hft_engine()
    md = eng.market_data if eng is not None else MarketData(ttl_seconds=2.0)
    try:
        df = md.latest(spec)
    except Exception as exc:
        raise HTTPException(503, f"{sym}: {type(exc).__name__}: {exc}")
    if df is None or not len(df):
        return {"symbol": sym, "timeframe": spec.timeframe,
                "markets": list(specs), "bars": []}
    df = df.tail(limit)
    ema = df["close"].ewm(span=20, adjust=False).mean()
    bars = [{"ts": str(ts), "close": float(c), "ema20": float(e)}
            for ts, c, e in zip(df.index, df["close"], ema)]
    first, last = bars[0]["close"], bars[-1]["close"]
    return {"symbol": sym, "timeframe": spec.timeframe,
            "markets": list(specs), "bars": bars,
            "change_pct": round((last / first - 1.0) * 100.0, 3) if first else 0.0}


@router.post("/api/hft/engine/start")
def api_hft_engine_start(body: HftEngineIn):
    if not CONFIG.hft.enabled:
        raise HTTPException(409, "fast book disabled via HFT_ENABLED=0")
    existing = core._get_hft_engine()
    if existing is not None:
        return {"status": "already_running", "cycles": existing.cycles}
    result = core._spawn_hft_engine(body.interval)
    if result["status"] == "owned":
        raise HTTPException(409, result["detail"])
    if result["status"] == "started":
        core._state_warning(result, core._write_hft_state(True, body.interval), "fast book engine started")
    return result


@router.post("/api/hft/engine/stop")
def api_hft_engine_stop(body: EmptyIn):
    with core._hft_lock:
        if core._hft_engine is None:
            ok = core._write_hft_state(False, CONFIG.hft.live_interval_seconds)
            return core._state_warning({"status": "not_running"}, ok, "fast book engine stopped")
        core._hft_engine = None
        stop_interval = core._hft_interval
    th = core._hft_thread
    if th is not None and th is not threading.current_thread() and th.is_alive():
        ok = core._write_hft_state(False, stop_interval)
        core._join_in_background(th, lambda iv: core._write_hft_state(False, iv),
                            stop_interval)
        return core._state_warning({"status": "stopping"}, ok, "fast book engine stopped")
    ok = core._write_hft_state(False, stop_interval)
    return core._state_warning({"status": "stopped"}, ok, "fast book engine stopped")


@router.get("/api/hft/engine/status")
def api_hft_engine_status():
    eng = core._get_hft_engine()
    th = core._hft_thread
    if eng is not None:
        return {"running": True, "cycles": eng.cycles,
                "positions": len(eng.broker.positions_snapshot()),
                "interval": core._hft_interval,
                "alive": bool(th is not None and th.is_alive()),
                "last_error": eng.last_error or core._last_hft_error,
                "health_note": getattr(eng, "health_note", None),
                "paused": is_paused()[0]}
    return {"running": False, "cycles": 0, "positions": 0,
            "interval": CONFIG.hft.live_interval_seconds,
            "alive": bool(th is not None and th.is_alive()),
            "last_error": core._last_hft_error, "health_note": None,
            "paused": is_paused()[0]}


@router.post("/api/hft/engine/interval")
def api_hft_engine_interval(body: HftEngineIn):
    """Retune the HFT book's cadence (1s floor), running or stopped."""
    core._hft_interval = body.interval
    running = core._get_hft_engine() is not None
    core._write_hft_state(running, body.interval)
    return {"status": "ok", "interval": body.interval, "running": running}
