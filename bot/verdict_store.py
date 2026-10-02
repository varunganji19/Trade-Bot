"""Anonymised verdict store (roadmap V7): a growing record of what overfits.

Each validation (`validate-trades --record`, or the Validate tab's checkbox)
can add one line per strategy to results/verdicts.jsonl beside the journal.
Over time the file answers questions no single report can: how often do
backtests with few trades pass, how often does doubling fees sink a
strategy, how much does the number of variants tried matter.

WHAT A RECORD HOLDS. It is built from a fixed list of coarse fields, never
by stripping the report, so nothing identifying can slip through:
  * the month it was recorded (not the day), the input format;
  * the verdict and which checks failed (codes, not the report's prose);
  * rounded statistics (PF and its interval, Deflated Sharpe, PBO);
  * size buckets: trades, variants tried, months traded.
It never holds a file name, strategy name, market, trade, timestamp or P&L.

OPT-IN AND LOCAL. Nothing is recorded unless asked for, and nothing leaves
this machine: there is no upload. Sharing verdicts across users would need
the consent and notice questions in docs/COMPLIANCE.md (Q10) answered first.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from collections import Counter

FIELDS = ("v", "month", "format", "verdict", "fails", "pf", "pf_lo", "pf_hi", "dsr",
          "pbo", "trades", "trials", "months")
VERSION = 1


def store_path() -> str:
    from config import db_dir
    return os.path.join(db_dir(), "results", "verdicts.jsonl")


def _bucket(n, edges: tuple, labels: tuple) -> str:
    for edge, label in zip(edges, labels):
        if n < edge:
            return label
    return labels[-1]


def _r(v, nd=1):
    return None if v is None else round(float(v), nd)


def _format(source: str) -> str:
    s = (source or "").lower()
    return "freqtrade" if "freqtrade" in s else "csv" if "csv" in s else "json"


def fail_codes(r: dict) -> list[str]:
    """Which checks a strategy failed, as stable codes."""
    from bot import validator as v
    ev, codes = r["intervals"], []
    if ev["pf"] is not None and ev["pf"] < 1.0:
        codes.append("no_edge")
    if ev["trades"] < v.MIN_TRADES:
        codes.append("few_trades")
    if ev["pf_lo"] is None or ev["pf_lo"] < 1.0:
        codes.append("interval_below_1")
    st = r["stability"]
    if st["measured"] < v.SEGMENTS or st["profitable"] < v.SEGMENTS - 1:
        codes.append("unstable")
    c = r["costs"]
    if not c["available"]:
        codes.append("costs_unchecked")
    elif any(row["fee_mult"] == 2.0 and (row["pf"] or 0) < 1.0 for row in c["rows"]):
        codes.append("fails_doubled_fees")
    ds = r["deflated_sharpe"]["dsr"]
    if ds is None or ds < v.DSR_PASS:
        codes.append("dsr_low")
    if r["pbo"]["pbo"] is not None and r["pbo"]["pbo"] >= 0.5:
        codes.append("pbo_high")
    g = r["regimes"]
    if not g["available"]:
        codes.append("regimes_unchecked")
    elif g["covered"] < v.MIN_REGIMES:
        codes.append("few_regimes")
    if r.get("trials_given") is None:
        codes.append("trials_unknown")
    return codes


def anonymise(r: dict, source: str, now: dt.datetime | None = None) -> dict:
    """One record from one strategy's analysis, from FIELDS only."""
    now = now or dt.datetime.now(dt.timezone.utc)
    ev = r["intervals"]
    months = None
    if r.get("period"):
        a, b = (dt.date.fromisoformat(x) for x in r["period"])
        months = _bucket((b - a).days / 30.4, (3, 6, 12, 24), ("<3", "3-6", "6-12", "12-24", "24+"))
    trials = r.get("trials_given")
    rec = {
        "v": VERSION, "month": now.strftime("%Y-%m"), "format": _format(source),
        "verdict": r["verdict"], "fails": fail_codes(r),
        "pf": _r(ev["pf"]), "pf_lo": _r(ev["pf_lo"]), "pf_hi": _r(ev["pf_hi"]),
        "dsr": _r(r["deflated_sharpe"]["dsr"]), "pbo": _r(r["pbo"]["pbo"]),
        "trades": _bucket(ev["trades"], (30, 100, 300, 1000),
                          ("<30", "30-99", "100-299", "300-999", "1000+")),
        "trials": "unknown" if trials is None else _bucket(
            trials, (2, 10, 100), ("1", "2-9", "10-99", "100+")),
        "months": months,
    }
    assert set(rec) == set(FIELDS)
    return rec


def record(report: dict, path: str | None = None) -> int:
    """Append one record per strategy in a validator report; returns how many."""
    path = path or store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = [anonymise(r, report.get("source", "")) for r in report["strategies"].values()]
    with open(path, "a") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    return len(rows)


def load(path: str | None = None) -> list[dict]:
    try:
        with open(path or store_path()) as fh:
            return [json.loads(s) for s in fh if s.strip()]
    except OSError:
        return []


def summarise(rows: list[dict]) -> dict:
    """What the store says so far."""
    verdicts = Counter(r["verdict"] for r in rows)
    fails = Counter(code for r in rows for code in r["fails"])
    by_trials = {}
    for r in rows:
        b = by_trials.setdefault(r["trials"], Counter())
        b[r["verdict"]] += 1
    return {"records": len(rows), "verdicts": dict(verdicts),
            "most_common_failures": fails.most_common(),
            "verdicts_by_trials": {k: dict(v) for k, v in sorted(by_trials.items())}}
