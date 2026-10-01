"""Evidence tab API: the generated artifacts behind every honesty claim,
read-only — the Kronos IC ledger, validation reports, the Shadow Account
report, the pinned-data manifest and the experiment log."""
from __future__ import annotations

import json
import os
import time

from fastapi import APIRouter

from config import CONFIG, db_dir

router = APIRouter()


def _results_dir() -> str:
    return os.path.join(os.path.dirname(CONFIG.db_path), "results")


def _ic_of(records: list, cfg) -> float | None:
    """Rolling rank-IC over `records` — the same window promoted() uses."""
    from bot.kronos_signal import KronosICTracker
    tr = KronosICTracker.__new__(KronosICTracker)
    tr.records, tr.half_life = list(records), cfg.ic_half_life
    return tr.ic()


# The pooled ledger mixes markets and horizons (legacy records carry no market
# key), and pooling unlike series can manufacture rank correlation — its IC is
# not the vote. The vote is decided per market by `main.py kronos`; this is
# the last such run (docs/RESULTS.md §2, CHANGELOG 2026-09-19).
KRONOS_LAST_VERDICT = {"market": "BTC/USDT 1h", "forecasts": 128, "ic": -0.0754,
                       "promoted": False, "date": "2026-09-19"}


def _evidence_kronos() -> dict:
    """The Kronos IC ledger as a series: rolling rank-IC (same math as
    promoted()'s gate) computed over the persisted records, so the UI can draw
    the model's evidence curve against its own promotion hurdle."""
    try:
        import pandas as pd
        from bot.kronos_signal import KronosConfig, KronosICTracker
        cfg = KronosConfig()
        # per-BOOK ledgers (one shared file would let the books overwrite each
        # other): read whichever exist, plus the legacy single-file ledger, so
        # the evidence curve keeps its history
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
                "note": "records resolved before 2026-09 predate per-market keying",
                "verdict": KRONOS_LAST_VERDICT}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _evidence_experiments() -> dict:
    """The experiment log (bot/experiments.py), read from the committed
    declarations, results and history — never written from here."""
    try:
        from bot.experiments import experiment_log
        rows = experiment_log()
        return {"rows": rows, "n": len(rows)}
    except Exception as exc:
        return {"rows": [], "n": 0, "error": f"{type(exc).__name__}: {exc}"}


def _evidence_validations(limit: int = 20) -> list:
    """Newest first (by file mtime), so the dropdown's default '0' is the
    current report, never a stale one sorted first by name.
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
        from bot.experiments import EXPERIMENTS_DIR
        if os.path.isdir(EXPERIMENTS_DIR):
            paths += [os.path.join(EXPERIMENTS_DIR, f) for f in os.listdir(EXPERIMENTS_DIR)
                      if f.endswith((".json", ".jsonl", ".toml"))]
        return tuple(sorted((p, os.path.getmtime(p), os.path.getsize(p))
                           for p in paths if os.path.exists(p)))
    except OSError:
        return None


@router.get("/api/evidence")
def api_evidence():
    """Everything the Evidence tab renders, in one read-only payload: the
    Kronos IC ledger, generated validation reports, the shadow report, and
    the pinned-data manifest. No computation on trade data — these are the
    artifacts `main.py validate` / `main.py shadow` / fetch_history wrote.

    Cached by artifact (mtime,size): the rolling-IC series costs ~0.7s at the
    ledger cap, too much for a 4s poll when the numbers only change when an
    artifact is rewritten."""
    key = _evidence_cache_key()
    now = time.time()
    if key is not None and _EVIDENCE_CACHE["key"] == key and now - _EVIDENCE_CACHE["ts"] < 60:
        return _EVIDENCE_CACHE["payload"]
    payload = {"kronos": _evidence_kronos(),
               "validations": _evidence_validations(),
               "shadow": _evidence_shadow(),
               "manifest": _evidence_manifest(),
               "experiments": _evidence_experiments()}
    _EVIDENCE_CACHE.update({"key": key, "payload": payload, "ts": now})
    return payload
