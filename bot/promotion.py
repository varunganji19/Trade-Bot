"""Promotion gate — a strategy's vote is a measurement, not a citation.

WHY THIS EXISTS. The fast book's vote weights were set from research lineage:
`hft_micro_breakout` carried the largest weight because it descends from a
published ORB result. When the book was finally measured at 5m over 14 days
it was the WORST cell on the board (PF 0.39 / 0.43 / 0.0 on 81 trades) while
strategies with smaller weights did better. Nothing in the code noticed,
because nothing in the code ever compared a strategy's weight to its record.

THE RULE (deliberately three states, not two):

  promoted   enough trades AND median profit factor >= PROMOTE_PF
             -> votes, as configured
  probation  too few trades to judge
             -> votes, and says so: absence of evidence is not evidence of
                absence, and a book whose strategies are all silenced
                produces no new evidence either
  demoted    enough trades AND median profit factor <= DEMOTE_PF
             -> does NOT vote. This is a measured loser, not an unknown.

The gate reads `data/results/promotions.json`, written by the battery
(bot/hft/harness.py) from the SAME backtests the docs quote. No file means no
evidence has been gathered yet, and every registered strategy votes — the
gate can only ever take a vote away on evidence, never grant one silently.

Only cells at the book's LIVE fee tier count: a strategy that clears costs on
a perp tier and drowns on spot has not earned a vote on spot.
"""
from __future__ import annotations

import json
import os
import statistics
import time

MIN_TRADES = 30          # total trades across cells before a verdict is possible
MIN_CELLS = 2            # ...spread over at least this many symbol cells
PROMOTE_PF = 1.0         # median profit factor to earn a vote
DEMOTE_PF = 0.8          # ...and to lose one (the band between is probation)

PROMOTED, PROBATION, DEMOTED = "promoted", "probation", "demoted"


def promotions_path() -> str:
    """Beside the battery's own output, under the ACTIVE journal dir."""
    from config import db_dir
    return os.path.join(db_dir(), "results", "promotions.json")


def verdicts_from_cells(cells: list, tier: str) -> dict:
    """Per-strategy verdict from battery cells (see the module docstring).

    `cells` are the battery's own result dicts: strategy, tier, trades,
    profit_factor. Cells from other tiers and cells with no trades are
    ignored — a strategy that never fired on a symbol has said nothing about
    that symbol, in either direction."""
    by_strategy: dict[str, list] = {}
    for c in cells:
        if c.get("tier") != tier:
            continue
        trades = int(c.get("trades") or 0)
        pf = c.get("profit_factor")
        if trades <= 0 or pf is None:
            continue
        by_strategy.setdefault(str(c.get("strategy")), []).append(
            {"symbol": c.get("symbol"), "trades": trades, "pf": float(pf)})

    out = {}
    for name, cs in by_strategy.items():
        trades = sum(c["trades"] for c in cs)
        pf_median = statistics.median(c["pf"] for c in cs)
        if trades < MIN_TRADES or len(cs) < MIN_CELLS:
            status, why = PROBATION, (f"only {trades} trades over {len(cs)} cell(s) — "
                                      f"need {MIN_TRADES} over {MIN_CELLS} to judge")
        elif pf_median >= PROMOTE_PF:
            status, why = PROMOTED, (f"median PF {pf_median:.2f} over {len(cs)} cells "
                                     f"({trades} trades)")
        elif pf_median <= DEMOTE_PF:
            status, why = DEMOTED, (f"median PF {pf_median:.2f} over {len(cs)} cells "
                                    f"({trades} trades) — measured loser, no vote")
        else:
            status, why = PROBATION, (f"median PF {pf_median:.2f} is between the "
                                      f"{DEMOTE_PF} and {PROMOTE_PF} lines")
        out[name] = {"status": status, "why": why, "trades": trades,
                     "cells": len(cs), "pf_median": round(pf_median, 3)}
    return out


def save_verdicts(verdicts: dict, tier: str, path: str | None = None) -> str:
    path = path or promotions_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    payload = {"tier": tier, "rule": {"min_trades": MIN_TRADES, "min_cells": MIN_CELLS,
                                      "promote_pf": PROMOTE_PF, "demote_pf": DEMOTE_PF},
               "strategies": verdicts}
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=1)
    os.replace(tmp, path)
    return path


_CACHE: dict = {"path": None, "mtime": None, "verdicts": {}}


def load_verdicts(path: str | None = None) -> dict:
    """Never raises: an unreadable file means 'no evidence', which is the
    permissive state (see the module docstring).

    Cached on the file's mtime. A long-running engine used to read the
    verdicts once at construction, so a battery run mid-session changed
    nothing until a restart — a stale gate that looks exactly like a working
    one. The stat is one syscall per call; the caller is about to compute
    indicators over hundreds of bars."""
    path = path or promotions_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _CACHE.update(path=path, mtime=None, verdicts={})
        return {}
    if _CACHE["path"] == path and _CACHE["mtime"] == mtime:
        return _CACHE["verdicts"]
    try:
        with open(path) as fh:
            verdicts = dict(json.load(fh).get("strategies") or {})
    except Exception:
        verdicts = {}
    _CACHE.update(path=path, mtime=mtime, verdicts=verdicts)
    return verdicts


def gate_state(path: str | None = None) -> dict:
    """Is the gate ACTUALLY on, and where is it looking?

    The verdicts file resolves under db_dir(), so pointing the app at a
    different data directory silently returns the gate to its permissive
    state — which is how a strategy measured at PF 0.39 went back to voting
    on the live book without a single line of output. "No evidence" is a
    state the operator has to be able to SEE, not infer."""
    path = path or promotions_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {"state": "no_evidence", "path": path,
                "why": f"no verdicts at {path} — every registered strategy "
                       f"votes UNMEASURED; run `make hft-battery` to gather them"}
    verdicts = load_verdicts(path)
    demoted = [n for n, v in verdicts.items() if v.get("status") == DEMOTED]
    return {"state": "active", "path": path,
            "generated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
            "measured": len(verdicts), "demoted": demoted,
            "why": f"{len(verdicts)} strategies measured, {len(demoted)} demoted"}


def voting_strategies(book: str) -> dict:
    """Which strategies actually vote on `book`, and why the others do not.

    "The engine is running" and "the engine has anything to trade with" are
    different claims. After the first promotion run the fast book had ONE
    voter left (two strategies demoted on their record, one a candidate) and
    nothing in the UI said so — a book that cannot trade would have looked
    identical to a quiet market."""
    from bot.strategies import CANDIDATE_STRATEGIES, STRATEGY_CLASSES
    want = "fast" if book in ("fast", "hft") else "standard"
    verdicts = load_verdicts()
    voting, silent = [], []
    for name, cls in sorted(STRATEGY_CLASSES.items()):
        if getattr(cls, "book", "standard") != want:
            continue
        if name in CANDIDATE_STRATEGIES:
            silent.append({"name": name, "why": "candidate — not voting until measured"})
        elif is_demoted(name, verdicts):
            silent.append({"name": name,
                           "why": verdicts.get(name, {}).get("why", "demoted")})
        else:
            voting.append(name)
    return {"voting": voting, "silent": silent,
            "registered": len(voting) + len(silent),
            "gate": gate_state()}


def is_demoted(name: str, verdicts: dict | None = None) -> bool:
    """True only for a MEASURED loser. Unknown strategies are not demoted."""
    v = (verdicts if verdicts is not None else load_verdicts()).get(name)
    return bool(v and v.get("status") == DEMOTED)
