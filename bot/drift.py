"""Drift monitor — stop trading what stopped working (roadmap V5).

A promoted strategy earned its vote on out-of-sample backtests: the gate
(bot/promotion.py) requires the lower end of a 90% bootstrap interval on its
profit factor to be at least 1.0. That interval is the strategy's EXPECTED
RANGE. Edges decay, so once a strategy votes live, its live results are
compared against that range every week, and a strategy that stays below it
is demoted automatically.

THE RULE. Watching starts the first time an engine cycle sees the strategy
promoted. At the end of each completed UTC week (Monday 00:00) the monitor
takes the strategy's last DRIFT_WINDOW closed trades in its book and computes
their profit factor. The week is "below" when there are at least
DRIFT_MIN_TRADES of them and the PF is under the interval's lower end.
DRIFT_WEEKS consecutive weeks below -> demoted.

The rule is deliberately quicker to demote than the gate is to promote: a
wrongly demoted strategy only stops trading, while a decayed one that keeps
its vote loses money. A trade-count window (not a time window) keeps a slow
strategy and a busy one on the same statistical footing, and the cap stops a
good first year from hiding a bad recent quarter.

A drift demotion is stored in results/drift_<book>.json beside the verdicts,
not in them: regenerating the evidence (`make evidence`) must not quietly
hand the vote back. Only `python3 main.py drift clear NAME` does, as a
deliberate act. Strategies without a measured interval (no verdict, or rule
v1) have no expected range to drift from; keeping them out is the gate's job.

Attribution: the journal credits each trade to the strategy that set its
stop (Decision.strategy_name), so in a multi-voter ensemble a strategy's
"live trades" are the ones it led, not every trade it voted for.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

from bot.evidence_stats import profit_factor

DRIFT_MIN_TRADES = 20    # fewer live trades than this says nothing either way
DRIFT_WINDOW = 50        # most recent trades judged at each week's end
DRIFT_WEEKS = 3          # consecutive weeks below the range before demotion
ALERT_DAYS = 7           # how long the dashboard banner shows a new demotion

BELOW, INSIDE, ABOVE, THIN = "below", "inside", "above", "thin"

_CACHE: dict = {}


def _book(book: str) -> str:
    from bot.promotion import _book_name
    return _book_name(book)


def drift_path(book: str) -> str:
    from config import db_dir
    return os.path.join(db_dir(), "results", f"drift_{_book(book)}.json")


def book_mode(book: str) -> str:
    """The journal mode whose trades are this book's live record."""
    return "hft" if _book(book) == "fast" else "paper"


def _utc(ts: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(ts)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def load_state(book: str) -> dict:
    """{"watching": {name: {"since"}}, "demoted": {name: record}}. Never
    raises; mtime-cached because the orchestrator consults the gate for every
    strategy on every bar."""
    path = drift_path(book)
    empty = {"watching": {}, "demoted": {}}
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return empty
    hit = _CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path) as fh:
            raw = json.load(fh)
        state = {"watching": dict(raw.get("watching") or {}),
                 "demoted": dict(raw.get("demoted") or {})}
    except Exception:
        state = empty
    _CACHE[path] = (mtime, state)
    return state


def _save_state(book: str, state: dict) -> None:
    path = drift_path(book)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump({"book": _book(book), "rule": {
            "min_trades": DRIFT_MIN_TRADES, "window": DRIFT_WINDOW, "weeks": DRIFT_WEEKS,
            "below": "live PF of the window < the verdict's pf_lo"},
            **state}, fh, indent=1)
    os.replace(tmp, path)


def demotions(book: str) -> dict:
    return load_state(book)["demoted"]


def week_ends(since: dt.datetime, now: dt.datetime) -> list[dt.datetime]:
    """Monday 00:00 UTC boundaries after `since`, up to `now`."""
    day = since.astimezone(dt.timezone.utc).date()
    first = day + dt.timedelta(days=7 - day.weekday())
    ends, end = [], dt.datetime.combine(first, dt.time(), dt.timezone.utc)
    while end <= now:
        ends.append(end)
        end += dt.timedelta(days=7)
    return ends


def assess(verdict: dict, trades: list[tuple[str, float]], since: dt.datetime,
           now: dt.datetime) -> dict:
    """Judge one strategy's live trades [(closed_ts, pnl), ...] in close
    order against its verdict's interval, week by week."""
    lo, hi = verdict.get("pf_lo"), verdict.get("pf_hi")
    weeks = []
    for end in week_ends(since, now):
        window = [p for ts, p in trades if _utc(ts) < end][-DRIFT_WINDOW:]
        pf = profit_factor(window)
        if len(window) < DRIFT_MIN_TRADES or pf is None:
            state = THIN
        elif pf < lo:
            state = BELOW
        elif hi is not None and pf > hi:
            state = ABOVE
        else:
            state = INSIDE
        weeks.append({"week_end": end.date().isoformat(), "trades": len(window),
                      "pf": None if pf is None else round(pf, 3), "state": state})
    recent = weeks[-DRIFT_WEEKS:]
    drifting = len(recent) == DRIFT_WEEKS and all(w["state"] == BELOW for w in recent)
    return {"expected": [lo, hi], "since": _iso(since), "weeks": weeks,
            "live_trades": len(trades), "drifting": drifting}


def _live_trades(db_path: str, mode: str, name: str, since: dt.datetime) -> list:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        return conn.execute(
            "SELECT closed_ts, COALESCE(pnl,0) FROM trades WHERE mode=? AND strategy=?"
            " AND status='CLOSED' AND closed_ts>=? ORDER BY closed_ts, id",
            (mode, name, _iso(since))).fetchall()


def _watchable(verdicts: dict) -> dict:
    """Strategies the gate lets vote on a measured interval."""
    from bot.promotion import PROMOTED, RULE_VERSION
    from bot.strategies import CANDIDATE_STRATEGIES
    return {n: v for n, v in verdicts.items()
            if v.get("status") == PROMOTED and v.get("pf_lo") is not None
            and int(v.get("rule") or 1) >= RULE_VERSION and n not in CANDIDATE_STRATEGIES}


def report(book: str, db_path: str, now: dt.datetime | None = None) -> dict:
    """Assessment of every watched strategy, without changing anything."""
    from bot.promotion import load_verdicts
    now = now or dt.datetime.now(dt.timezone.utc)
    state = load_state(book)
    verdicts = load_verdicts(book=book, drift=False)
    out = {}
    for name, verdict in _watchable(verdicts).items():
        since = _utc(state["watching"].get(name, {}).get("since") or _iso(now))
        trades = _live_trades(db_path, book_mode(book), name, since)
        out[name] = assess(verdict, trades, since, now)
    return out


def check(book: str, db_path: str, now: dt.datetime | None = None) -> list[str]:
    """Start watching newly promoted strategies, stop watching ones the gate
    no longer promotes, and demote any that drifted. Returns the names
    demoted by this call. Writes the state file only when it changes."""
    from bot.promotion import load_verdicts
    now = now or dt.datetime.now(dt.timezone.utc)
    state = load_state(book)
    watchable = _watchable(load_verdicts(book=book, drift=False))
    watching = {n: w for n, w in state["watching"].items()
                if n in watchable and n not in state["demoted"]}
    for name in watchable:
        if name not in watching and name not in state["demoted"]:
            watching[name] = {"since": _iso(now)}
    demoted = dict(state["demoted"])
    new = []
    for name, w in watching.items():
        since = _utc(w["since"])
        a = assess(watchable[name], _live_trades(db_path, book_mode(book), name, since),
                   since, now)
        if a["drifting"]:
            last = a["weeks"][-1]
            lo, hi = a["expected"]
            demoted[name] = {
                "demoted_at": _iso(now), "since": w["since"], "expected": [lo, hi],
                "live_pf": last["pf"], "trades": last["trades"],
                "why": (f"drift: live PF {last['pf']:.2f} over its last {last['trades']} "
                        f"trades has been below its expected {lo:.2f}–{hi:.2f} for "
                        f"{DRIFT_WEEKS} weeks (watched since {w['since'][:10]})"),
                "weeks": a["weeks"][-DRIFT_WEEKS:]}
            new.append(name)
    for name in new:
        watching.pop(name)
    fresh = {"watching": watching, "demoted": demoted}
    if fresh != state:
        _save_state(book, fresh)
    return new


def clear(book: str, name: str) -> bool:
    """Give a drift-demoted strategy its vote back (if the gate still
    promotes it); its watch restarts from the next check."""
    state = load_state(book)
    if name not in state["demoted"]:
        return False
    demoted = {n: r for n, r in state["demoted"].items() if n != name}
    _save_state(book, {"watching": state["watching"], "demoted": demoted})
    return True


def recent_alerts(now: dt.datetime | None = None) -> list[dict]:
    """Drift demotions from the last ALERT_DAYS, for the dashboard banner."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for book in ("standard", "fast"):
        for name, r in demotions(book).items():
            try:
                fresh = now - _utc(r["demoted_at"]) <= dt.timedelta(days=ALERT_DAYS)
            except (KeyError, ValueError):
                continue
            if fresh:
                out.append({"book": book, "name": name, "why": r.get("why", ""),
                            "demoted_at": r["demoted_at"]})
    return out
