#!/usr/bin/env python3
"""Offline replay of a standard-book gate on its committed windows, adding the
portfolio view (bot/portfolio_evidence.py) beside the trade-pooled evidence.

  python3 scripts/portfolio_replay.py experiments/standard_gate.toml \
      --cache-dir data/cache --output docs/research/portfolio_v1

Each market's frame is the cached Parquet whose first and last bars equal the
window recorded in the declaration's results file, so the replay sees exactly
the bars the gate saw, run through the current code. Networking is disabled
and every worker journals to a temporary file. Nothing in experiments/, the
registry or the promotion verdicts is written: the output is one JSON per
declaration. Regime labels are not replayed (the daily caches they came from
are not all kept), so the trade-pooled rows carry no regime counts.
"""
# ruff: noqa: E402 -- the repository root is added before local imports.
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.data.cache import _load_cached
from bot.experiments import _jobs, _pooled, load_declaration, results_path, run_unit
from bot.portfolio_evidence import book_returns, family_deflated_sharpe, portfolio_evidence
from config import CONFIG, MarketSpec

SOURCES = ("bot/portfolio_evidence.py", "bot/experiments.py", "bot/evidence_stats.py",
           "bot/backtest.py", "bot/broker.py", "bot/risk.py", "config.py",
           "scripts/portfolio_replay.py")
COMPARED = ("trades", "pf", "pf_lo", "pf_hi")


def offline_guard(event, args):
    if event == "socket.connect":
        raise RuntimeError("portfolio replay is offline; network connections are disabled")


def digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def exact_frame(cache_dir: Path, decl, kind: str, symbol: str, tf: str, window: list):
    safe = symbol.replace("/", "").replace("=X", "")
    spec = MarketSpec(kind, symbol, tf)
    for path in sorted(cache_dir.glob(f"{kind}_{safe}_{tf}_{decl.days}d_*.parquet")):
        df = _load_cached(str(path), spec)
        if df is not None and len(df) and [str(df.index[0]), str(df.index[-1])] == window:
            return spec, df, {"file": path.name, "sha256": digest(path), "window": window}
    raise FileNotFoundError(f"no cached frame with the committed window for "
                            f"{symbol} {tf}: {window}")


def isolated_unit(job: dict) -> dict:
    sys.addaudithook(offline_guard)
    with tempfile.TemporaryDirectory(prefix="algo-portfolio-") as td:
        with patch.object(CONFIG, "db_path", str(Path(td) / "isolated.db")):
            return run_unit(job)


def replay(decl_path: str, cache_dir: Path, workers: int) -> dict:
    decl = load_declaration(decl_path)
    if decl.kind != "gate" or decl.book != "standard":
        raise SystemExit(f"{decl.id}: only standard-book gates are replayed here")
    committed_path = Path(results_path(decl))
    committed = json.loads(committed_path.read_text())
    frames, inputs = {}, []
    for kind, symbol, tf in decl.markets:
        spec, df, ref = exact_frame(cache_dir, decl, kind, symbol, tf,
                                    committed["windows"][f"{symbol} {tf}"])
        frames[(kind, symbol, tf)] = (spec, df)
        inputs.append(ref)
    jobs = _jobs(decl, frames)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        units = list(pool.map(isolated_unit, jobs))
    failed = [f"{u['strategy']} {u['symbol']} {u['timeframe']}: {u['error']}"
              for u in units if "error" in u]
    if failed:
        raise RuntimeError("replay incomplete:\n  " + "\n  ".join(failed))

    before = {e["strategy"]: e["oos"] for e in committed["summary"]}
    verdicts = committed.get("verdicts") or {}
    rows, books = [], {}
    for strat in decl.strategies:
        mine = [u for u in units if u["strategy"] == strat]
        pooled = _pooled(mine, {})
        pooled.pop("regimes", None)
        rows.append({
            "strategy": strat, "markets": len(mine),
            "committed_verdict": (verdicts.get(strat) or {}).get("status"),
            "committed_trade_pooled": {k: before.get(strat, {}).get(k) for k in COMPARED},
            "trade_pooled": pooled,
            "trade_pooled_matches_committed": all(
                pooled.get(k) == before.get(strat, {}).get(k) for k in COMPARED),
            "portfolio": portfolio_evidence(mine),
        })
        books[strat] = book_returns(mine)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                          text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", *SOURCES], cwd=ROOT,
                           capture_output=True, text=True).stdout.strip()
    return {
        "id": decl.id, "kind": "portfolio_replay",
        "declaration": {"path": f"experiments/{decl.id}.toml", "sha256": decl.sha256},
        "committed_results": {"path": f"experiments/{committed_path.name}",
                              "sha256": digest(committed_path),
                              "run_at": committed.get("run_at")},
        "git_head": head, "sources_modified_since_head": bool(dirty),
        "source_sha256": {p: digest(ROOT / p) for p in SOURCES},
        "inputs": inputs, "units": len(units),
        "note": ("current code on the committed windows; trade-pooled figures can "
                 "differ from the committed run where later fixes changed trades. "
                 "Portfolio figures are reported, never used by the gate."),
        "strategies": rows,
        "portfolio_family": family_deflated_sharpe(books),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("declarations", nargs="+")
    ap.add_argument("--cache-dir", default=str(ROOT / "data" / "cache"))
    ap.add_argument("--output", required=True)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    for path in args.declarations:
        res = replay(path, Path(args.cache_dir), args.workers)
        dest = out / f"{res['id']}.portfolio.json"
        dest.write_text(json.dumps(res, indent=1, default=str) + "\n")
        print(f"[portfolio] {res['id']}: {res['units']} market runs -> {dest}")
        for r in res["strategies"]:
            t, b = r["trade_pooled"], r["portfolio"]
            print(f"  {r['strategy']:18s} {str(r['committed_verdict']):10s} "
                  f"PF {t['pf']} [{t['pf_lo']}–{t['pf_hi']}] {t['trades']} trades | "
                  f"Sharpe {b.get('sharpe')} [{b.get('sharpe_lo')}–{b.get('sharpe_hi')}] "
                  f"{b.get('markets')} mkts ≈ {b.get('effective_bets')} bets, "
                  f"DD {b.get('max_drawdown_pct')}%"
                  + ("" if r["trade_pooled_matches_committed"] else "  (trades differ)"))
        fam = res["portfolio_family"]
        if fam.get("deflated_sharpe") is not None:
            print(f"  best book {fam['best']}: Deflated Sharpe {fam['deflated_sharpe']} "
                  f"({fam['counted']})")


if __name__ == "__main__":
    main()
