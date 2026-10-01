"""Pre-registered experiments and the experiment registry.

WHY. Every variant ever tried is a draw from the same lottery: try enough
settings and one will look good on any data. The Deflated Sharpe ratio can
price that selection in, but only if the number of trials is known — and a
number someone types in by hand is a number that drifts. So experiments are
declared in a small TOML file under `experiments/`, committed to git BEFORE
they run, and this runner:

  * refuses a declaration that is not committed (or has uncommitted edits),
    so the plan provably predates the results;
  * runs exactly the declared variants, strategies and markets — nothing
    else can be run through it;
  * writes the results next to the declaration (`<id>.results.json`) and
    rebuilds the registry (`experiments.jsonl` beside the journal), from
    which the Deflated Sharpe reads its trial count.

Two kinds of experiment:

  gate   walk-forward over the whole window for every declared strategy;
         writes the book's promotion verdicts (bot/promotion.py, rule v2).
  study  one or more parameter variants; optional selection/holdout split
         (variants judged on the last `holdout_days`, which selection never
         saw). Never touches the promotion gate.

TOML rather than YAML: tomllib ships with Python 3.11+, so pre-registration
adds no dependency.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import time
import tomllib
from concurrent.futures import ProcessPoolExecutor

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPERIMENTS_DIR = os.path.join(REPO, "experiments")
HISTORY_FILE = os.path.join(EXPERIMENTS_DIR, "history.jsonl")
KINDS = ("gate", "study")
KEYS = {"id", "kind", "book", "hypothesis", "created", "note", "tier", "strategies",
        "markets", "days", "folds", "regime_days", "holdout_days", "variants"}
WARMUP = {"standard": 220, "fast": 400}


class DeclarationError(ValueError):
    """The declaration is malformed or may not run."""


@dataclasses.dataclass
class Declaration:
    path: str
    id: str
    kind: str
    book: str
    hypothesis: str
    created: str
    tier: str
    strategies: list
    markets: list           # [(kind, symbol, timeframe)]
    days: int
    folds: int
    regime_days: int
    variants: list          # [{"name": str, "params": dict}]
    holdout_days: int | None
    sha256: str


def registry_path() -> str:
    from config import db_dir
    return os.path.join(db_dir(), "experiments.jsonl")


def results_path(decl: Declaration) -> str:
    return os.path.splitext(decl.path)[0] + ".results.json"


# ----------------------------------------------------------------- declarations
def load_declaration(path: str) -> Declaration:
    from bot.strategies import STRATEGY_CLASSES
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        d = tomllib.loads(raw.decode())
    except tomllib.TOMLDecodeError as exc:
        raise DeclarationError(f"{path}: not valid TOML ({exc})") from exc

    unknown = set(d) - KEYS
    if unknown:
        raise DeclarationError(f"{path}: unknown key(s) {sorted(unknown)}")

    def need(key, typ):
        if key not in d:
            raise DeclarationError(f"{path}: missing `{key}`")
        if not isinstance(d[key], typ):
            raise DeclarationError(f"{path}: `{key}` must be {typ.__name__}")
        return d[key]

    eid = need("id", str)
    stem = os.path.splitext(os.path.basename(path))[0]
    if eid != stem:
        raise DeclarationError(f"{path}: id {eid!r} must match the file name {stem!r}")
    kind = need("kind", str)
    if kind not in KINDS:
        raise DeclarationError(f"{path}: kind must be one of {KINDS}")
    book = need("book", str)
    if book not in WARMUP:
        raise DeclarationError(f"{path}: book must be standard or fast")
    strategies = need("strategies", list)
    for s in strategies:
        cls = STRATEGY_CLASSES.get(s)
        if cls is None:
            raise DeclarationError(f"{path}: unknown strategy {s!r}")
        if getattr(cls, "book", "standard") != book:
            raise DeclarationError(f"{path}: {s} belongs to the "
                                   f"{getattr(cls, 'book', 'standard')} book, not {book}")
    markets = []
    for m in need("markets", list):
        try:
            for tf in m["timeframes"]:
                markets.append((m["kind"], m["symbol"], tf))
        except (KeyError, TypeError) as exc:
            raise DeclarationError(f"{path}: each market needs kind, symbol, "
                                   f"timeframes") from exc
    if not markets:
        raise DeclarationError(f"{path}: no markets declared")
    variants = d.get("variants") or [{"name": "default", "params": {}}]
    if kind == "gate" and (len(variants) != 1 or variants[0].get("params")):
        raise DeclarationError(f"{path}: a gate measures the shipped settings; "
                               f"declare parameter variants as a study")
    names = [v.get("name") for v in variants]
    if len(set(names)) != len(names) or not all(names):
        raise DeclarationError(f"{path}: variants need unique names")
    for v in variants:
        for key in v.get("params", {}):
            _check_param(path, key)
    holdout = d.get("holdout_days")
    if kind == "gate" and holdout:
        raise DeclarationError(f"{path}: a gate uses walk-forward folds, not a holdout")
    days = need("days", int)
    if holdout is not None and not (0 < holdout < days):
        raise DeclarationError(f"{path}: holdout_days must be between 0 and days")
    return Declaration(
        path=os.path.abspath(path), id=eid, kind=kind, book=book,
        hypothesis=need("hypothesis", str), created=str(need("created", object)),
        tier=d.get("tier", "standard" if book == "standard" else "perp"),
        strategies=strategies, markets=markets, days=days,
        folds=int(d.get("folds", 4)), regime_days=int(d.get("regime_days", 1000)),
        variants=[{"name": v["name"], "params": dict(v.get("params", {}))} for v in variants],
        holdout_days=holdout, sha256=hashlib.sha256(raw).hexdigest())


def _check_param(path: str, key: str) -> None:
    from config import CONFIG
    section, _, field = key.rpartition(".")
    target = {"": CONFIG.params, "params": CONFIG.params, "hft": CONFIG.hft,
              "costs": CONFIG.costs, "risk": CONFIG.risk}.get(section)
    if target is None or not hasattr(target, field):
        raise DeclarationError(f"{path}: unknown parameter {key!r}")


def preregistration_problem(path: str) -> str | None:
    """None when `path` is committed and unmodified in git, else the reason."""
    rel = os.path.relpath(os.path.abspath(path), REPO)
    try:
        subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=REPO,
                       check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return f"{rel} is not committed — commit the declaration before running it"
    dirty = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", rel], cwd=REPO)
    if dirty.returncode != 0:
        return f"{rel} has uncommitted changes — commit them before running"
    return None


def declaration_commit(path: str) -> str | None:
    rel = os.path.relpath(os.path.abspath(path), REPO)
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%H", "--", rel], cwd=REPO,
                             check=True, capture_output=True, text=True).stdout.strip()
        return out or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


# ------------------------------------------------------------------- execution
def _config_for(book: str, tier: str, overrides: dict):
    from config import CONFIG
    if book == "fast":
        from bot.hft import build_hft_config
        cfg = build_hft_config(fee_tier=tier)
    else:
        cfg = CONFIG
    groups: dict[str, dict] = {}
    for key, val in overrides.items():
        section, _, field = key.rpartition(".")
        groups.setdefault(section or "params", {})[field] = val
    for section, vals in groups.items():
        cfg = dataclasses.replace(cfg, **{section: dataclasses.replace(
            getattr(cfg, section), **vals)})
    return cfg


def _trade_rows(trades: list) -> list:
    return [{"entry_ts": str(t.get("entry_ts")), "exit_ts": str(t.get("exit_ts")),
             "pnl": float(t.get("pnl") or 0.0)}
            for t in trades if t.get("status", "CLOSED") == "CLOSED"]


def run_unit(job: dict) -> dict:
    """One (variant, strategy, market): walk-forward on the selection window,
    plus the holdout run when declared. Top-level so worker processes can
    import it."""
    from bot.backtest import Backtester
    cfg = _config_for(job["book"], job["tier"], job["params"])
    bt = Backtester(cfg, book=job["book"])
    spec, df, warm = job["spec"], job["frame"], WARMUP[job["book"]]
    out = {k: job[k] for k in ("variant", "strategy")}
    out.update(symbol=spec.symbol, timeframe=spec.timeframe)
    sel = df if job["holdout_bars"] is None else df.iloc[:len(df) - job["holdout_bars"]]
    try:
        wf = bt.run_walk_forward(spec, sel, folds=job["folds"], strategy=job["strategy"],
                                 progress=False, warmup_bars=warm)
    except ValueError as exc:        # not enough bars for the folds
        out["error"] = str(exc)
        return out
    agg = wf.get("aggregate") or {}
    out["oos"] = {"trades": len(_trade_rows(wf.get("trades", []))),
                  "fold_pf": [f.get("profit_factor") for f in wf["folds"]],
                  "sharpe": agg.get("sharpe"), "total_pnl": agg.get("total_pnl"),
                  "fees": agg.get("fees")}
    out["trades"] = _trade_rows(wf.get("trades", []))
    if job["holdout_bars"]:
        hold = df.iloc[len(df) - job["holdout_bars"] - warm:]
        res = bt.run(spec, hold, strategy=job["strategy"], warmup_bars=warm)
        s = res.stats()
        gross = round((s.get("total_pnl") or 0.0) + (s.get("fees") or 0.0), 2)
        out["holdout"] = {"trades": s["trades"], "profit_factor": s["profit_factor"],
                          "sharpe": s["sharpe"], "net_pnl": s["total_pnl"],
                          "fees": s["fees"], "gross_pnl": gross}
        out["holdout_trades"] = _trade_rows(res.trades)
    return out


def _fetch_frames(decl: Declaration, quiet: bool) -> tuple[dict, dict, list]:
    """Market frames and daily regime labels, fetched once in the parent so
    workers never race on the cache."""
    from bot.data import fetch_history
    from bot.evidence_stats import label_regimes
    from config import MarketSpec
    frames, regimes, errors = {}, {}, []
    for kind, symbol, tf in decl.markets:
        spec = MarketSpec(kind, symbol, tf, symbol)
        try:
            df = fetch_history(spec, days=decl.days)
            if decl.book == "fast" and kind == "crypto":
                from bot.funding import attach_funding
                df = attach_funding(df, symbol)
            frames[(kind, symbol, tf)] = (spec, df)
        except Exception as exc:
            errors.append(f"{symbol} {tf}: data error {type(exc).__name__}: {exc}")
            continue
        if symbol not in regimes:
            try:
                daily = fetch_history(MarketSpec(kind, symbol, "1d", symbol),
                                      days=decl.regime_days)
                regimes[symbol] = label_regimes(daily)
            except Exception as exc:
                errors.append(f"{symbol} 1d (regimes): {type(exc).__name__}: {exc}")
        if not quiet:
            print(f"  [data] {symbol:10s} {tf:4s} {len(df):6d} bars", flush=True)
    return frames, regimes, errors


def _jobs(decl: Declaration, frames: dict) -> list:
    from bot.strategies import get_strategy
    from bot.strategies.base import strategy_applies
    from config import TIMEFRAME_SECONDS
    jobs = []
    for v in decl.variants:
        for strat in decl.strategies:
            inst = get_strategy(strat)
            for (kind, symbol, tf), (spec, df) in frames.items():
                if tf not in inst.preferred_timeframes or not strategy_applies(inst, symbol):
                    continue
                hb = None
                if decl.holdout_days:
                    hb = int(decl.holdout_days * 86400 / TIMEFRAME_SECONDS[tf])
                jobs.append({"variant": v["name"], "params": v["params"], "strategy": strat,
                             "book": decl.book, "tier": decl.tier, "spec": spec,
                             "frame": df, "folds": decl.folds, "holdout_bars": hb})
    return jobs


def _pooled(units: list, regimes: dict, key: str = "trades") -> dict:
    """Pool every market's trades for one (variant, strategy): time-ordered
    P&Ls with bootstrap intervals and the regime each trade was opened in."""
    from bot.evidence_stats import regime_at, trade_evidence
    rows = []
    for u in units:
        lab = regimes.get(u["symbol"])
        for t in u.get(key) or []:
            rows.append((t["exit_ts"], t["pnl"], regime_at(lab, t["entry_ts"])))
    rows.sort(key=lambda r: r[0])
    ev = trade_evidence([r[1] for r in rows])
    counts: dict[str, int] = {}
    for r in rows:
        counts[r[2] or "unlabelled"] = counts.get(r[2] or "unlabelled", 0) + 1
    ev["regimes"] = counts
    ev["net_pnl"] = round(sum(r[1] for r in rows), 2)
    return ev


def run_declaration(path: str, *, workers: int | None = None, quiet: bool = False,
                    require_preregistered: bool = True,
                    write_verdicts: bool = True) -> dict:
    decl = load_declaration(path)
    if require_preregistered:
        problem = preregistration_problem(path)
        if problem:
            raise DeclarationError(f"refused: {problem}")
    if decl.kind == "gate" and decl.book == "fast":
        from bot.hft import hft_fee_tier
        if decl.tier != hft_fee_tier():
            raise DeclarationError(f"refused: {decl.id} measures the {decl.tier} tier but the "
                                   f"live fast book runs {hft_fee_tier()}")
    started = time.time()
    if not quiet:
        print(f"[experiment] {decl.id} ({decl.kind}, {decl.book} book): {decl.hypothesis}")
    frames, regimes, errors = _fetch_frames(decl, quiet)
    if errors and decl.kind == "gate":
        # a partial gate would silently turn the missing strategies back into
        # unmeasured ones; publish nothing instead
        raise RuntimeError("gate evidence incomplete; nothing written:\n  " + "\n  ".join(errors))
    jobs = _jobs(decl, frames)
    workers = workers or max(1, min(8, (os.cpu_count() or 2) - 2))
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            units = list(pool.map(run_unit, jobs))
    else:
        units = [run_unit(j) for j in jobs]
    unit_errors = [f"{u['variant']} {u['strategy']} {u['symbol']}: {u['error']}"
                   for u in units if "error" in u]
    if unit_errors and decl.kind == "gate":
        raise RuntimeError("gate evidence incomplete; nothing written:\n  "
                           + "\n  ".join(unit_errors))

    summary = []
    for v in decl.variants:
        for strat in decl.strategies:
            mine = [u for u in units if u["variant"] == v["name"]
                    and u["strategy"] == strat and "error" not in u]
            entry = {"variant": v["name"], "params": v["params"], "strategy": strat,
                     "markets": len(mine), "oos": _pooled(mine, regimes)}
            if decl.holdout_days:
                entry["holdout"] = _pooled(mine, regimes, key="holdout_trades")
                entry["holdout"]["fees"] = round(sum(u["holdout"]["fees"] or 0 for u in mine), 2)
                entry["holdout"]["gross_pnl"] = round(
                    sum(u["holdout"]["gross_pnl"] for u in mine), 2)
            summary.append(entry)

    run_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    windows = {f"{s.symbol} {s.timeframe}": [str(df.index[0]), str(df.index[-1])]
               for s, df in frames.values()}
    result = {
        "id": decl.id, "kind": decl.kind, "book": decl.book, "tier": decl.tier,
        "hypothesis": decl.hypothesis, "declared": decl.created,
        "declaration_sha256": decl.sha256, "declaration_commit": declaration_commit(path),
        "run_at": run_at, "runtime_s": round(time.time() - started, 1),
        "days": decl.days, "folds": decl.folds, "holdout_days": decl.holdout_days,
        "windows": windows, "skipped": errors + unit_errors, "summary": summary,
        "markets": [{k: u[k] for k in ("variant", "strategy", "symbol", "timeframe",
                                       "oos", "holdout") if k in u}
                    for u in units if "error" not in u],
    }
    if decl.kind == "gate" and write_verdicts:
        from bot.promotion import save_verdicts, verdicts_from_evidence
        verdicts = verdicts_from_evidence({e["strategy"]: e["oos"] for e in summary})
        result["verdicts"] = verdicts
        result["verdicts_path"] = save_verdicts(verdicts, decl.tier, book=decl.book,
                                                experiment=decl.id)
    with open(results_path(decl), "w") as fh:
        json.dump(result, fh, indent=1, default=str)
        fh.write("\n")
    rebuild_registry()
    if not quiet:
        _print_summary(result)
    return result


def _fmt_ci(ev: dict) -> str:
    if ev.get("pf") is None:
        return "no trades"
    lo, hi = ev.get("pf_lo"), ev.get("pf_hi")
    band = f" [{lo:.2f}–{hi:.2f}]" if lo is not None else ""
    return f"PF {ev['pf']:.2f}{band}, {ev['trades']} trades"


def _print_summary(result: dict) -> None:
    print(f"\n[experiment] {result['id']}: {len(result['markets'])} market runs in "
          f"{result['runtime_s']}s")
    for e in result["summary"]:
        line = f"  {e['variant']:16s} {e['strategy']:22s} OOS {_fmt_ci(e['oos'])}"
        if "holdout" in e:
            line += f" | holdout net ${e['holdout']['net_pnl']:+,.0f}"
        print(line)
    for name, v in sorted((result.get("verdicts") or {}).items()):
        print(f"  verdict {v['status']:9s} {name:22s} {v['why']}")
    for s in result["skipped"]:
        print(f"  skipped: {s}")


# --------------------------------------------------------------------- registry
def declarations() -> list:
    if not os.path.isdir(EXPERIMENTS_DIR):
        return []
    return sorted(os.path.join(EXPERIMENTS_DIR, f) for f in os.listdir(EXPERIMENTS_DIR)
                  if f.endswith(".toml"))


def _records_from_result(res: dict) -> list:
    base = {"experiment": res["id"], "kind": res["kind"], "book": res["book"],
            "tier": res["tier"], "run_at": res["run_at"], "source": "run",
            "declaration_sha256": res["declaration_sha256"]}
    out = []
    for e in res["summary"]:
        rec = dict(base, level="pooled", variant=e["variant"], params=e["params"],
                   strategy=e["strategy"], oos=e["oos"])
        if "holdout" in e:
            rec["holdout"] = e["holdout"]
        if (res.get("verdicts") or {}).get(e["strategy"]):
            rec["verdict"] = res["verdicts"][e["strategy"]]["status"]
        out.append(rec)
    for m in res["markets"]:
        out.append(dict(base, level="market", variant=m["variant"], strategy=m["strategy"],
                        symbol=m["symbol"], timeframe=m["timeframe"], oos=m["oos"],
                        **({"holdout": m["holdout"]} if "holdout" in m else {})))
    return out


def rebuild_registry(path: str | None = None) -> str:
    """Regenerate the registry from the committed history and every result
    file. Derived data: deleting it loses nothing."""
    path = path or registry_path()
    records = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as fh:
            records += [json.loads(line) for line in fh if line.strip()]
    for decl_path in declarations():
        rp = os.path.splitext(decl_path)[0] + ".results.json"
        if os.path.exists(rp):
            with open(rp) as fh:
                records += _records_from_result(json.load(fh))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        for r in records:
            fh.write(json.dumps(r, default=str) + "\n")
    os.replace(tmp, path)
    return path


def load_registry(path: str | None = None) -> list:
    path = path or registry_path()
    if not os.path.exists(path):
        rebuild_registry(path)
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def trials_for(strategy: str, timeframe: str | None = None,
               records: list | None = None) -> dict:
    """Every recorded trial of `strategy`: the count, and the annualised
    out-of-sample Sharpes of those that have one (the Deflated Sharpe's
    input). One trial per (experiment, variant, market); re-running the same
    declaration on newer data is more evidence, not another trial."""
    records = load_registry() if records is None else records
    trials = {}
    for r in records:
        if r.get("strategy") != strategy or r.get("level") == "pooled":
            continue
        if timeframe and r.get("timeframe") not in (None, timeframe):
            continue
        key = (r.get("experiment"), r.get("variant"), r.get("symbol"), r.get("timeframe"))
        trials[key] = r
    sharpes = [r["oos"]["sharpe"] for r in trials.values()
               if isinstance(r.get("oos"), dict) and r["oos"].get("sharpe") is not None]
    return {"n_trials": len(trials), "sharpes": sharpes}
