#!/usr/bin/env python3
"""Offline replay of saved research inputs, preserving historical reports.

Run from any directory:
  python3 scripts/rerun_corrected_research.py --workers 4 --baseline-ref 0c74d1b \
    --prior-results docs/research/verified_metrics_v2

All OHLC, funding and flow inputs come from existing Parquets. Experiment
windows must match the historical report's endpoints exactly. The v1 replay
loads a pinned Git commit's backtest source into an isolated module, using the
SAME current strategy configuration, costs and shared risk/broker implementation.
Verified prior evidence may supply the unchanged v1 results; current results
are always computed afresh. An invalid explicit prior cache is rejected.
No experiment registry, promotion verdict or historical report is written.
"""
# ruff: noqa: E402 -- the repository root is added before local imports.
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from bot.backtest import Backtester, METRICS_VERSION
from bot.data.cache import _load_cached
from bot.experiments import _jobs, load_declaration, run_unit
from bot.flow import attach_taker_flow, binance_symbol
from bot.funding import attach_funding, perp_symbol
from bot.validation import oos_trade_distribution
from config import CONFIG, MarketSpec, infer_kind
from scripts.pinned_runs import RUNS

SHARED_SOURCE_PATHS = ("bot/broker.py", "bot/risk.py", "config.py")
REPLAY_SOURCE_PATHS = ("bot/backtest.py", *SHARED_SOURCE_PATHS,
                       "bot/experiments.py", "bot/validation.py",
                       "scripts/pinned_runs.py", "scripts/rerun_corrected_research.py")


def offline_guard(event, args):
    if event == "socket.connect":
        raise RuntimeError("research replay is offline; network connections are disabled")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resolve_baseline(ref):
    """Resolve once in the parent; workers receive this immutable commit ID."""
    return subprocess.check_output(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
                                   cwd=ROOT).decode().strip()


def git_source(path, baseline_commit):
    return subprocess.check_output(["git", "show", f"{baseline_commit}:{path}"], cwd=ROOT).decode()


def legacy_backtest(baseline_commit):
    name = f"_research_v1_backtest_{baseline_commit}"
    if name not in sys.modules:
        module = types.ModuleType(name)
        sys.modules[name] = module  # dataclasses resolve their defining module
        exec(compile(git_source("bot/backtest.py", baseline_commit), name, "exec"), module.__dict__)
    return sys.modules[name]


def artifact(path):
    path = Path(path).resolve()
    return {"path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
            "sha256": digest(path)}


def configuration():
    return {"params": asdict(CONFIG.params), "risk": asdict(CONFIG.risk),
            "costs": asdict(CONFIG.costs), "capital": CONFIG.paper_capital}


def artifact_references(value):
    """Yield recorded source/input/declaration/report artifacts recursively."""
    if isinstance(value, dict):
        if "path" in value and "sha256" in value:
            yield value
        for child in value.values():
            yield from artifact_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from artifact_references(child)


def unit_key(unit):
    return tuple(unit[key] for key in ("variant", "strategy", "symbol", "timeframe"))


class PriorResults:
    """Only verified, identical-input v1 results are eligible for reuse."""

    def __init__(self, directory, baseline_sha256, current_sources, current_configuration):
        self.directory = Path(directory).resolve()
        self.hashes = {}
        checks_path = self.directory / "export_checks.json"
        if not checks_path.is_file():
            raise ValueError("prior results require passed export_checks.json evidence")
        checks = self._read(checks_path)
        comparison_path = self.directory / "comparison.json"
        if (checks.get("status") != "passed" or checks.get("metrics_version") != 2
                or checks.get("historical_reports_unchanged") is not True):
            raise ValueError("prior export checks must have passed for metrics version 2")
        self._check_hash(comparison_path, checks.get("comparison_sha256"))
        self.comparison = self._read(comparison_path)
        if (self.comparison.get("metrics_version") != 2
                or self.comparison.get("historical_reports_unchanged") is not True):
            raise ValueError("prior comparison is not verified metrics version 2 evidence")
        if self.comparison.get("v1_backtest_source_sha256") != baseline_sha256:
            raise ValueError("prior baseline backtest source hash differs")
        if self.comparison.get("configuration") != current_configuration:
            raise ValueError("prior configuration differs from current params/risk/costs/capital")
        recorded_sources = self.comparison.get("current_source_sha256", {})
        if recorded_sources != checks.get("current_source_sha256"):
            raise ValueError("prior source hashes disagree with export checks")
        for path in SHARED_SOURCE_PATHS:
            if recorded_sources.get(path) != current_sources[path]:
                raise ValueError(f"prior shared source hash differs: {path}")

        self.pinned = {}
        pinned_checks = {row["file"]: row for row in checks.get("pinned", [])}
        for row in self.comparison.get("pinned", []):
            filename = f"{row['run']}.json"
            checked = pinned_checks.get(filename, {})
            if checked.get("passed") is not True:
                raise ValueError(f"missing verified pinned export: {filename}")
            detail_path = self.directory / filename
            self._check_hash(detail_path, checked.get("sha256"))
            detail = self._read(detail_path)
            if detail.get("metrics_version") != 2 or detail.get("stats") != row.get("v2"):
                raise ValueError(f"prior pinned summary differs from its export: {filename}")
            if row["run"] in self.pinned:
                raise ValueError(f"duplicate prior pinned run: {row['run']}")
            self.pinned[row["run"]] = row
        if len(self.pinned) != self.comparison.get("pinned_completed"):
            raise ValueError("prior pinned count differs from comparison")

        self.experiments = {}
        experiment_checks = {row["file"]: row for row in checks.get("experiments", [])}
        for summary in self.comparison.get("experiments", []):
            filename = f"{summary['id']}.comparison.json"
            checked = experiment_checks.get(filename, {})
            if checked.get("passed") is not True:
                raise ValueError(f"missing verified experiment export: {filename}")
            path = self.directory / filename
            self._check_hash(path, checked.get("sha256"))
            report = self._read(path)
            if {k: v for k, v in report.items() if k != "units"} != summary:
                raise ValueError(f"prior experiment summary differs from export: {filename}")
            units = {unit_key(unit): unit for unit in report["units"]}
            if (len(units) != len(report["units"]) or len(units) != summary["rerun_units"]
                    or len(units) != checked.get("completed_cases")):
                raise ValueError(f"prior experiment unit count differs: {filename}")
            if any("v1_same_config" not in unit or "v2" not in unit for unit in units.values()):
                raise ValueError(f"prior experiment lacks comparison results: {filename}")
            self.experiments[summary["id"]] = (summary, units)
            self._check_references(report)
        self._check_references(self.comparison)
        self.provenance = {"directory": str(self.directory), "metrics_version": 2,
                           "comparison": artifact(comparison_path),
                           "export_checks": artifact(checks_path),
                           "verified_files": len(self.hashes),
                           "reuse": "v1_same_config_only; current results computed afresh"}

    def _read(self, path):
        self.hashes[str(path)] = digest(path)
        return json.loads(path.read_text())

    def _check_hash(self, path, expected):
        if not isinstance(expected, str) or digest(path) != expected:
            raise ValueError(f"prior evidence hash mismatch: {path}")
        self.hashes[str(path)] = expected

    def _check_references(self, value):
        for reference in artifact_references(value):
            path = (ROOT / reference["path"]).resolve()
            if not path.is_relative_to(ROOT):
                raise ValueError(f"prior referenced artifact escapes repository: {path}")
            self._check_hash(path, reference["sha256"])

    def assert_unchanged(self):
        if any(digest(path) != sha for path, sha in self.hashes.items()):
            raise RuntimeError("prior evidence or its referenced inputs changed during replay")

    def pinned_row(self, metadata):
        row = self.pinned.get(metadata["run"])
        if row is None or any(row.get(key) != value for key, value in metadata.items()):
            raise ValueError(f"prior pinned inputs or request differ: {metadata['run']}")
        return row

    def experiment_units(self, name, metadata, job_keys):
        if name not in self.experiments:
            raise ValueError(f"prior experiment missing: {name}")
        summary, units = self.experiments[name]
        if any(summary.get(key) != value for key, value in metadata.items()):
            raise ValueError(f"prior experiment inputs/declaration/report differ: {name}")
        if set(units) != set(job_keys):
            raise ValueError(f"prior experiment job geometry differs: {name}")
        return units


def cached_frame(path, spec):
    df = _load_cached(str(path), spec)
    if df is None or len(df) == 0 or not df.index.is_monotonic_increasing or not df.index.is_unique:
        raise ValueError(f"invalid cached frame: {path}")
    return df


def experiment_frame(decl, symbol, tf, kind, window):
    safe = symbol.replace("/", "").replace("=X", "")
    candidates = sorted((ROOT / "data/cache").glob(f"{kind}_{safe}_{tf}_{decl.days}d_*.parquet"))
    spec = MarketSpec(kind, symbol, tf)
    for path in candidates:
        df = cached_frame(path, spec)
        if [str(df.index[0]), str(df.index[-1])] == window:
            return spec, df, path
    raise FileNotFoundError(f"no exact cached endpoints for {symbol} {tf}: {window}")


def attach_cached_aux(df, symbol, tf):
    inputs = []
    safe = symbol.replace("/", "")
    if perp_symbol(symbol) is not None:
        path = ROOT / "data/cache" / f"funding_{safe}.parquet"
        prints = pd.read_parquet(path)["funding_rate"]
        if prints.index[-1] < df.index[-1] - pd.Timedelta(hours=8):
            raise ValueError(f"funding cache does not cover {symbol}'s final bar")
        df = attach_funding(df, symbol, history=lambda sym, start: prints[prints.index >= start])
        inputs.append(artifact(path))
    if binance_symbol(symbol) is not None:
        path = ROOT / "data/cache" / f"takerflow_{safe}_{tf}.parquet"
        flow = pd.read_parquet(path)
        if not df.index.isin(flow.index).all():
            raise ValueError(f"flow cache does not cover all {symbol} {tf} bars")
        df = attach_taker_flow(df, symbol, tf, history=lambda *args: flow)
        inputs.append(artifact(path))
    return df, inputs


def compare(current, before):
    return {key: {"before": before.get(key), "after": value,
                  "delta": round(value - before[key], 6)
                  if isinstance(value, (int, float)) and isinstance(before.get(key), (int, float))
                  else None}
            for key, value in current.items() if key in before}


def unit_pair(job, baseline_commit, prior_v1=None):
    sys.addaudithook(offline_guard)
    with tempfile.TemporaryDirectory(prefix="algo-research-") as td:
        with patch.object(CONFIG, "db_path", str(Path(td) / "isolated.db")):
            if prior_v1 is None:
                legacy = legacy_backtest(baseline_commit)
                with patch("bot.backtest.Backtester", legacy.Backtester):
                    v1 = run_unit(job)
            else:
                v1 = deepcopy(prior_v1)
            current = run_unit(job)
    # The full trades were consumed by the backtester but are not needed in
    # the comparison artifact; historical experiment files also omit them.
    for result in (v1, current):
        result.pop("trades", None)
        result.pop("holdout_trades", None)
    return v1, current


def run_pinned(outdir, baseline_commit, prior=None):
    rows, skipped = [], []
    for symbol, tf, start, end, strategy, name in RUNS:
        safe = symbol.replace("/", "").replace("=X", "")
        modern = ROOT / "data/cache" / f"{infer_kind(symbol)}_{safe}_{tf}_{start}_{end}.parquet"
        historical = ROOT / "data/cache" / f"{safe}_{tf}_{start}_{end}.parquet"
        cache = modern if modern.exists() else historical
        if not cache.exists():
            skipped.append({"run": name, "reason": "exact pinned cache unavailable",
                            "expected": [str(modern.relative_to(ROOT)), str(historical.relative_to(ROOT))]})
            continue
        spec = MarketSpec(infer_kind(symbol), symbol, tf)
        df = cached_frame(cache, spec)
        metadata = {"run": name, "symbol": symbol, "timeframe": tf, "strategy": strategy,
                    "requested_window": [start, end], "actual_window": [str(df.index[0]), str(df.index[-1])],
                    "bars": len(df), "input": artifact(cache)}
        prior_row = prior.pinned_row(metadata) if prior is not None else None
        if prior_row is None:
            legacy = legacy_backtest(baseline_commit)
            stats1 = legacy.Backtester().run(spec, df, strategy=strategy).stats()
        else:
            stats1 = deepcopy(prior_row["v1_same_config"])
        current = Backtester().run(spec, df, strategy=strategy)
        stats_current = current.stats()
        histories = {}
        for stage in ("before", "mid", "after"):
            path = ROOT / "data/results" / f"pinned_{stage}" / f"{name}.json"
            if path.exists():
                previous = json.loads(path.read_text())
                histories[stage] = {"report": artifact(path), "stats": previous["stats"],
                                    "differences": compare(stats_current, previous["stats"])}
        validation = None
        if current.trades:
            validation = oos_trade_distribution(current.trades, df, starting_capital=current.start_equity)
        row = {**metadata, "input_attrs": df.attrs,
               "metrics_version": METRICS_VERSION, "baseline_metrics_version": 1,
               "v1_same_config": stats1, f"v{METRICS_VERSION}": stats_current,
               "v1_reused": prior_row is not None,
               "same_config_differences": compare(stats_current, stats1),
               "historical_reports": histories, "corrected_cv": validation}
        if prior_row is not None:
            row.update(prior_metrics_version=2, prior_v2=deepcopy(prior_row["v2"]),
                       prior_v2_differences=compare(stats_current, prior_row["v2"]))
        detail = {"metrics_version": METRICS_VERSION, "stats": stats_current,
                  "trades": current.trades, "equity_curve": current.equity_curve,
                  "input": artifact(cache), "requested_window": [start, end]}
        (outdir / f"{name}.json").write_text(json.dumps(detail, indent=1, default=str) + "\n")
        rows.append(row)
        print(f"[pinned] {name}: v1 Sharpe {stats1['sharpe']} -> v{METRICS_VERSION} {stats_current['sharpe']}; "
              f"PnL {stats1['total_pnl']} -> {stats_current['total_pnl']}", flush=True)
    return rows, skipped


def run_experiment(name, workers, baseline_commit, prior=None):
    decl_path = ROOT / "experiments" / f"{name}.toml"
    report_path = decl_path.with_suffix(".results.json")
    decl = load_declaration(str(decl_path))
    previous = json.loads(report_path.read_text())
    frames, inputs, skipped = {}, [], []
    for kind, symbol, tf in decl.markets:
        try:
            spec, df, cache = experiment_frame(decl, symbol, tf, kind,
                                               previous["windows"][f"{symbol} {tf}"])
            aux = []
            if decl.book == "fast":
                df, aux = attach_cached_aux(df, symbol, tf)
            frames[(kind, symbol, tf)] = (spec, df)
            inputs.append({"symbol": symbol, "timeframe": tf, "bars": len(df),
                           "ohlcv": artifact(cache), "auxiliary": aux})
        except (ValueError, FileNotFoundError, KeyError) as exc:
            skipped.append({"symbol": symbol, "timeframe": tf, "reason": str(exc)})
    jobs = _jobs(decl, frames)
    historical = {(u["variant"], u["strategy"], u["symbol"], u["timeframe"]): u
                  for u in previous["markets"]}
    metadata = {"declaration": artifact(decl_path), "historical_report": artifact(report_path),
                "inputs": inputs}
    job_keys = [(job["variant"], job["strategy"], job["spec"].symbol, job["spec"].timeframe)
                for job in jobs]
    prior_units = prior.experiment_units(name, metadata, job_keys) if prior is not None else {}
    units = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(unit_pair, job, baseline_commit,
                               prior_units[key]["v1_same_config"] if key in prior_units else None): job
                   for job, key in zip(jobs, job_keys)}
        for future in as_completed(futures):
            job = futures[future]
            v1, current = future.result()
            key = (job["variant"], job["strategy"], job["spec"].symbol, job["spec"].timeframe)
            old = historical.get(key, {})
            unit = {"variant": key[0], "strategy": key[1], "symbol": key[2], "timeframe": key[3],
                    "metrics_version": METRICS_VERSION, "baseline_metrics_version": 1,
                    "v1_same_config": v1, f"v{METRICS_VERSION}": current, "historical": old,
                    "v1_reused": key in prior_units,
                    "same_config_oos_differences": compare(current.get("oos", {}), v1.get("oos", {})),
                    "historical_oos_differences": compare(current.get("oos", {}), old.get("oos", {}))}
            if key in prior_units:
                prior_v2 = prior_units[key]["v2"]
                unit.update(prior_metrics_version=2, prior_v2=deepcopy(prior_v2),
                            prior_v2_oos_differences=compare(current.get("oos", {}), prior_v2.get("oos", {})))
                if "holdout" in current:
                    unit["prior_v2_holdout_differences"] = compare(current["holdout"], prior_v2.get("holdout", {}))
            units.append(unit)
            if len(units) % 10 == 0 or len(units) == len(jobs):
                print(f"[experiment] {name}: {len(units)}/{len(jobs)} units", flush=True)
    units.sort(key=lambda u: (u["variant"], u["strategy"], u["symbol"], u["timeframe"]))
    return {"id": name, **metadata, "metrics_version": METRICS_VERSION,
            "baseline_metrics_version": 1, "v1_reused_units": len(prior_units),
            "declaration_matches_historical": decl.sha256 == previous.get("declaration_sha256"),
            "historical_units": len(historical), "rerun_units": len(units),
            "skipped": skipped, "units": units}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--experiments", nargs="*", default=["standard_gate", "fast_gate", "market_maker_fill_model"])
    parser.add_argument("--baseline-ref", default="HEAD", help="Git revision pinned once before replay")
    parser.add_argument("--prior-results", type=Path, help="verified v2 evidence; reuse only unchanged v1 results")
    parser.add_argument("--output", type=Path, default=ROOT / f"docs/research/verified_metrics_v{METRICS_VERSION}")
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be positive")
    baseline_commit = resolve_baseline(args.baseline_ref)
    baseline_sha256 = hashlib.sha256(git_source("bot/backtest.py", baseline_commit).encode()).hexdigest()
    startup_head = resolve_baseline("HEAD")
    sys.addaudithook(offline_guard)
    args.output = args.output.resolve()
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("research output directory must be empty; preserve earlier evidence")
    if args.prior_results is not None and args.output.is_relative_to(args.prior_results.resolve()):
        raise ValueError("research output must be separate from prior evidence")
    history_paths = list((ROOT / "data/results").glob("pinned_*/*.json"))
    history_paths += list((ROOT / "experiments").glob("*.results.json"))
    history_hashes = {str(p): digest(p) for p in history_paths}
    source_hashes = {p: digest(ROOT / p) for p in REPLAY_SOURCE_PATHS}
    replay_configuration = configuration()
    prior = (PriorResults(args.prior_results, baseline_sha256, source_hashes, replay_configuration)
             if args.prior_results is not None else None)
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="algo-research-") as td:
        with patch.object(CONFIG, "db_path", str(Path(td) / "isolated.db")):
            pinned, skipped = run_pinned(args.output, baseline_commit, prior)
            experiments = []
            for name in args.experiments:
                result = run_experiment(name, args.workers, baseline_commit, prior)
                experiments.append(result)
                (args.output / f"{name}.comparison.json").write_text(json.dumps(result, indent=1, default=str) + "\n")
    history_unchanged = all(digest(path) == sha for path, sha in history_hashes.items())
    sources_unchanged = all(digest(ROOT / path) == sha for path, sha in source_hashes.items())
    if not history_unchanged or not sources_unchanged or configuration() != replay_configuration:
        raise RuntimeError("historical reports, replay source or configuration changed during execution; rerun required")
    for reference in artifact_references([pinned, experiments]):
        if digest(ROOT / reference["path"]) != reference["sha256"]:
            raise RuntimeError("referenced input/declaration/report changed during replay")
    if prior is not None:
        prior.assert_unchanged()
    result = {"metrics_version": METRICS_VERSION, "capital_model": "fixed_starting_capital",
              "return_basis": "net_pnl_over_starting_capital", "network": "disabled",
              "historical_reports_unchanged": history_unchanged,
              "git_head": startup_head, "baseline_ref": args.baseline_ref,
              "baseline_commit": baseline_commit, "baseline_metrics_version": 1,
              "current_source_sha256": source_hashes,
              "v1_backtest_source_sha256": baseline_sha256,
              "comparison_scope": f"v1/v{METRICS_VERSION} backtest loops and statistics use identical current configuration and shared risk/broker modules; verified prior_v2 uses the same inputs; archived differences may also include historical configuration changes",
              "configuration": replay_configuration,
              "prior_reuse": ({**prior.provenance,
                               "pinned_v1_reused": sum(row["v1_reused"] for row in pinned),
                               "experiment_v1_reused": sum(e["v1_reused_units"] for e in experiments)}
                              if prior is not None else {"reuse": "none; v1 and current results computed afresh"}),
              "pinned_completed": len(pinned), "pinned_declared": len(RUNS),
              "pinned": pinned, "skipped_pinned": skipped,
              "experiments": [{k: v for k, v in e.items() if k != "units"} for e in experiments]}
    (args.output / "comparison.json").write_text(json.dumps(result, indent=1, default=str) + "\n")
    print(f"[research] {len(pinned)}/{len(RUNS)} pinned windows; "
          f"{sum(e['rerun_units'] for e in experiments)} experiment units -> {args.output}", flush=True)


if __name__ == "__main__":
    main()
