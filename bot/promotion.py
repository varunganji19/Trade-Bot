"""Promotion gate — a strategy's vote is a measurement, not a citation.

WHY THIS EXISTS. The fast book's vote weights were set from research lineage:
`hft_micro_breakout` carried the largest weight because it descends from a
published ORB result. When the book was finally measured at 5m over 14 days
it was the WORST cell on the board (PF 0.39 / 0.43 / 0.0 on 81 trades) while
strategies with smaller weights did better. Nothing in the code noticed,
because nothing in the code ever compared a strategy's weight to its record.

THE RULE (v2, written by `make evidence` — bot/experiments.py):

  promoted   >= V2_MIN_TRADES out-of-sample trades, at least V2_MIN_PER_REGIME
             of them in each of V2_MIN_REGIMES market regimes, AND the LOWER
             end of the 90% bootstrap interval on profit factor >= 1.0
             -> votes
  demoted    >= V2_DEMOTE_MIN_TRADES trades AND even the UPPER end of the
             interval is below 1.0 -> does not vote; a measured loser
  probation  anything else (too few trades, too few regimes, or an
             interval that straddles 1.0) -> does NOT vote under v2: the
             bar is "proven", not "not yet disproven". This can leave a book
             with nothing allowed to trade, and the dashboard says so.

A median profit factor over a handful of folds (rule v1, below) cannot tell a
lucky streak from an edge; an interval can, and a strategy that only ever saw
one kind of market has not been tested on the others.

RULE v1 (verdict files written before v2; shown as stale until regenerated):
promoted = median fold PF >= PROMOTE_PF over >= MIN_TRADES trades; demoted =
median <= DEMOTE_PF; probation in between, and probation still voted.

Each book reads its own verdicts, written by that book's battery from the SAME
backtests the docs quote. The fast book keeps the original
`data/results/promotions.json` path for backwards compatibility; the standard
book uses `promotions_standard.json`. No file means no evidence has been
gathered yet, and every registered strategy votes — the gate can only ever
take a vote away on evidence, never grant one silently.

Only cells at the book's LIVE fee tier count: a strategy that clears costs on
a perp tier and drowns on spot has not earned a vote on spot.
"""
from __future__ import annotations

import json
import os
import statistics
import time

# Thirty trades is deliberately a LOW evidence floor: below it, one large win
# can dominate profit factor; above it, the gate may reject a clear loser but
# still labels the 0.8-1.0 uncertainty band probation. Two independent OOS
# folds prevent one market episode from being the entire case for a verdict.
MIN_TRADES = 30          # total OOS trades before a verdict is possible
MIN_CELLS = 2            # ...spread over at least this many OOS fold cells
PROMOTE_PF = 1.0         # median profit factor to earn a vote
DEMOTE_PF = 0.8          # ...and to lose one (the band between is probation)

PROMOTED, PROBATION, DEMOTED = "promoted", "probation", "demoted"

# Rule v2. A hundred trades over three regimes is the minimum at which a 90%
# interval on profit factor is usually narrow enough to clear 1.0 for a
# modest real edge; ten per regime keeps one regime from being a token.
RULE_VERSION = 2
V2_MIN_TRADES = 100
V2_MIN_REGIMES = 3
V2_MIN_PER_REGIME = 10
V2_DEMOTE_MIN_TRADES = 30
V2_RULE = {"version": RULE_VERSION, "min_trades": V2_MIN_TRADES,
           "min_regimes": V2_MIN_REGIMES, "min_per_regime": V2_MIN_PER_REGIME,
           "promote": "pf_lo >= 1.0", "demote": "pf_hi < 1.0",
           "interval": "90% circular block bootstrap"}


def _book_name(book: str) -> str:
    """Canonical promotion-book name; reject typos instead of sharing a gate."""
    if book in ("fast", "hft"):
        return "fast"
    if book in ("standard", "paper"):
        return "standard"
    raise ValueError(f"unknown promotion book {book!r}; expected standard or fast")


def promotions_path(book: str = "fast") -> str:
    """Book-specific verdict path under the ACTIVE journal directory.

    Fast deliberately retains the original filename: existing measured
    verdicts must not disappear merely because book isolation was added.
    """
    from config import db_dir
    name = "promotions.json" if _book_name(book) == "fast" else "promotions_standard.json"
    return os.path.join(db_dir(), "results", name)


def verdicts_from_cells(cells: list, tier: str) -> dict:
    """Per-strategy verdict from OUT-OF-SAMPLE fold cells.

    `cells` are the battery's walk-forward fold dicts: strategy, tier, trades,
    profit_factor. Full-window cells must never be passed here. Cells from
    other tiers and cells with no trades are ignored — a strategy that never
    fired in a fold has said nothing about that episode, in either direction.
    """
    by_strategy: dict[str, list] = {}
    for c in cells:
        if c.get("tier") != tier:
            continue
        # The contract is fail-permissive: accidentally handing this function
        # the full-window battery cells produces NO verdict, never an
        # authoritative-looking in-sample demotion.
        if c.get("fold") is None:
            continue
        trades = int(c.get("trades") or 0)
        pf = c.get("profit_factor")
        if trades <= 0 or pf is None:
            continue
        by_strategy.setdefault(str(c.get("strategy")), []).append(
            {"symbol": c.get("symbol"), "fold": c.get("fold"),
             "trades": trades, "pf": float(pf)})

    out = {}
    for name, cs in by_strategy.items():
        trades = sum(c["trades"] for c in cs)
        pf_median = statistics.median(c["pf"] for c in cs)
        if trades < MIN_TRADES or len(cs) < MIN_CELLS:
            status, why = PROBATION, (f"only {trades} OOS trades over {len(cs)} fold(s) — "
                                      f"need {MIN_TRADES} over {MIN_CELLS} to judge")
        elif pf_median >= PROMOTE_PF:
            status, why = PROMOTED, (f"median OOS PF {pf_median:.2f} over {len(cs)} folds "
                                     f"({trades} trades)")
        elif pf_median <= DEMOTE_PF:
            status, why = DEMOTED, (f"median OOS PF {pf_median:.2f} over {len(cs)} folds "
                                    f"({trades} trades) — measured loser, no vote")
        else:
            status, why = PROBATION, (f"median OOS PF {pf_median:.2f} is between the "
                                      f"{DEMOTE_PF} and {PROMOTE_PF} lines")
        out[name] = {"status": status, "why": why, "trades": trades,
                     "cells": len(cs), "pf_median": round(pf_median, 3),
                     "evidence": "walk_forward_oos"}
    return out


def _fmt_interval(ev: dict) -> str:
    pf, lo, hi = ev.get("pf"), ev.get("pf_lo"), ev.get("pf_hi")
    if pf is None:
        return "no OOS trades"
    band = f" (90% interval {lo:.2f}–{hi:.2f})" if lo is not None else ""
    return f"OOS PF {pf:.2f}{band}"


def verdicts_from_evidence(evidence: dict) -> dict:
    """Rule v2 verdicts from pooled out-of-sample evidence per strategy:
    {name: {"trades", "pf", "pf_lo", "pf_hi", "regimes": {label: n}, ...}}
    as produced by bot/experiments.py."""
    from bot.evidence_stats import REGIMES
    out = {}
    for name, ev in evidence.items():
        n = int(ev.get("trades") or 0)
        regimes = ev.get("regimes") or {}
        covered = [r for r in REGIMES if regimes.get(r, 0) >= V2_MIN_PER_REGIME]
        lo, hi = ev.get("pf_lo"), ev.get("pf_hi")
        head = f"{_fmt_interval(ev)}, {n} trades, {len(covered)} of {len(REGIMES)} regimes"
        if n >= V2_DEMOTE_MIN_TRADES and hi is not None and hi < 1.0:
            status, why = DEMOTED, f"{head} — even the top of the interval is below 1.0"
        elif n < V2_MIN_TRADES:
            status, why = PROBATION, f"{head} — need {V2_MIN_TRADES} trades to judge"
        elif len(covered) < V2_MIN_REGIMES:
            status, why = PROBATION, (f"{head} — need {V2_MIN_PER_REGIME}+ trades in each "
                                      f"of {V2_MIN_REGIMES} regimes")
        elif lo is not None and lo >= 1.0:
            status, why = PROMOTED, head
        else:
            status, why = PROBATION, f"{head} — the interval straddles 1.0: not proven"
        out[name] = {"status": status, "why": why, "trades": n,
                     "pf": ev.get("pf"), "pf_lo": lo, "pf_hi": hi,
                     "sharpe": ev.get("sharpe"), "sharpe_lo": ev.get("sharpe_lo"),
                     "sharpe_hi": ev.get("sharpe_hi"), "regimes": regimes,
                     "evidence": "walk_forward_oos", "rule": RULE_VERSION}
    return out


def save_verdicts(verdicts: dict, tier: str, path: str | None = None, *,
                  book: str = "fast", experiment: str | None = None) -> str:
    book = _book_name(book)
    path = path or promotions_path(book)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    v2 = any(v.get("rule") == RULE_VERSION for v in verdicts.values())
    rule = V2_RULE if v2 else {"version": 1, "min_trades": MIN_TRADES,
                               "min_cells": MIN_CELLS, "promote_pf": PROMOTE_PF,
                               "demote_pf": DEMOTE_PF}
    payload = {"book": book, "tier": tier, "rule": rule, "strategies": verdicts}
    if experiment:
        payload["experiment"] = experiment
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=1)
    os.replace(tmp, path)
    return path


_CACHE: dict = {"path": None, "mtime": None, "verdicts": {}}


def load_verdicts(path: str | None = None, *, book: str = "fast") -> dict:
    """Never raises: an unreadable file means 'no evidence', which is the
    permissive state (see the module docstring).

    Cached on the file's mtime and re-checked on every call, so a battery
    run mid-session takes effect without a restart (a gate read once at
    construction goes stale while looking exactly like a working one). The
    stat is one syscall; the caller is about to compute indicators over
    hundreds of bars."""
    path = path or promotions_path(book)
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


def gate_state(path: str | None = None, *, book: str = "fast") -> dict:
    """Is the gate ACTUALLY on, and where is it looking?

    The verdicts file resolves under db_dir(), so pointing the app at a
    different data directory silently returns the gate to its permissive
    state — which is how a strategy measured at PF 0.39 went back to voting
    on the live book without a single line of output. "No evidence" is a
    state the operator has to be able to SEE, not infer."""
    book = _book_name(book)
    path = path or promotions_path(book)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {"state": "no_evidence", "book": book, "path": path,
                "why": f"no verdicts at {path} — every registered strategy "
                       f"votes UNMEASURED; run `make evidence` to gather them"}
    verdicts = load_verdicts(path, book=book)
    demoted = [n for n, v in verdicts.items() if v.get("status") == DEMOTED]
    # WHICH EVIDENCE this verdict set rests on. Verdicts with no `evidence`
    # key came from the SAME full-window cells used to develop the
    # strategies, which measures fit, not persistence. An in-sample file must
    # not present itself as an out-of-sample one: the gate's whole claim is
    # the quality of its evidence, so a weaker basis has to be visible.
    bases = {v.get("evidence", "in_sample") for v in verdicts.values()}
    oos = bases == {"walk_forward_oos"}
    evidence = "walk_forward_oos" if oos else "in_sample"
    rule = rule_version(verdicts)
    if rule >= RULE_VERSION:
        promoted = [n for n, v in verdicts.items() if v.get("status") == PROMOTED]
        why = (f"rule v{rule}: {len(verdicts)} strategies measured, {len(promoted)} "
               f"promoted, {len(demoted)} demoted")
    else:
        why = f"{len(verdicts)} strategies measured, {len(demoted)} demoted"
    if not oos:
        why += " — IN-SAMPLE evidence, predates walk-forward; regenerate it"
    elif rule < RULE_VERSION:
        why += (" — OLD RULE (median PF over 30 trades, probation votes); "
                "run `make evidence` for the stricter bar")
    return {"state": "active", "book": book, "path": path,
            "generated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
            "measured": len(verdicts), "demoted": demoted, "rule": rule,
            "evidence": evidence, "stale": not oos or rule < RULE_VERSION, "why": why}


def voting_strategies(book: str) -> dict:
    """Which strategies actually vote on `book`, and why the others do not.

    "The engine is running" and "the engine has anything to trade with" are
    different claims. After the first promotion run the fast book had ONE
    voter left (two strategies demoted on their record, one a candidate) and
    nothing in the UI said so — a book that cannot trade would have looked
    identical to a quiet market."""
    from bot.strategies import CANDIDATE_STRATEGIES, STRATEGY_CLASSES
    want = _book_name(book)
    verdicts = load_verdicts(book=want)
    voting, voters, silent = [], [], []
    for name, cls in sorted(STRATEGY_CLASSES.items()):
        if getattr(cls, "book", "standard") != want:
            continue
        # A measured loss outranks "candidate": both are silent, but only one
        # of them is an unknown.
        if is_demoted(name, verdicts):
            silent.append({"name": name,
                           "why": verdicts.get(name, {}).get("why", "demoted")})
        elif name in CANDIDATE_STRATEGIES:
            why = "candidate — not voting until measured"
            if name in verdicts:
                why = f"candidate ({verdicts[name]['status']}) — {verdicts[name].get('why', '')}"
            silent.append({"name": name, "why": why})
        elif not may_vote(name, verdicts):
            v = verdicts.get(name)
            silent.append({"name": name, "why": (f"{v['status']} — {v.get('why', '')}" if v
                                                 else "not measured by the gate — no vote")})
        else:
            voting.append(name)
            # A voter on probation trades without proof; the UI has to be
            # able to say so next to its name.
            v = verdicts.get(name) or {}
            voters.append({"name": name, "status": v.get("status", "unmeasured"),
                           "why": v.get("why", "no verdict yet — votes unmeasured")})
    return {"voting": voting, "voters": voters, "silent": silent,
            "registered": len(voting) + len(silent),
            "gate": gate_state(book=want)}


def rule_version(verdicts: dict) -> int:
    """Which rule wrote this verdict set (v1 files carry no marker)."""
    return max((int(v.get("rule") or 1) for v in verdicts.values()), default=1)


def may_vote(name: str, verdicts: dict | None = None) -> bool:
    """Does the gate let `name` vote?

    No verdicts at all: yes — nothing has been measured, and the dashboard
    shows the gate as UNMEASURED. Rule v1: everything but a measured loser.
    Rule v2: only a promoted strategy; probation and unmeasured stay silent."""
    verdicts = load_verdicts() if verdicts is None else verdicts
    if not verdicts:
        return True
    if rule_version(verdicts) >= RULE_VERSION:
        return (verdicts.get(name) or {}).get("status") == PROMOTED
    return not is_demoted(name, verdicts)


def is_demoted(name: str, verdicts: dict | None = None) -> bool:
    """True only for a MEASURED loser. Unknown strategies are not demoted."""
    v = (verdicts if verdicts is not None else load_verdicts()).get(name)
    return bool(v and v.get("status") == DEMOTED)
