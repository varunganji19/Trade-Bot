"""Offline replay provenance and verified baseline reuse; no real research runs."""
from __future__ import annotations

from concurrent.futures import Future
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts import rerun_corrected_research as replay


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "ROOT", tmp_path)
    monkeypatch.setattr(replay.sys, "addaudithook", lambda hook: None)
    for name in replay.REPLAY_SOURCE_PATHS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"source for {name}")
    artifacts = {}
    for name in ("data/cache/crypto_BTCUSDT_1h_start_end.parquet",
                 "data/cache/funding_BTCUSDT.parquet",
                 "experiments/demo.toml", "experiments/demo.results.json"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"saved input for {name}")
        artifacts[name] = replay.artifact(path)
    sources = {name: replay.digest(tmp_path / name) for name in replay.REPLAY_SOURCE_PATHS}
    recorded_sources = {name: sources[name] for name in ("bot/backtest.py", *replay.SHARED_SOURCE_PATHS)}
    recorded_sources["bot/backtest.py"] = "old-v2-source-hash"
    directory = tmp_path / "prior"
    directory.mkdir()
    baseline_hash = hashlib.sha256(b"original baseline").hexdigest()
    stats1 = {"total_pnl": -1.0, "sharpe": -0.5}
    stats2 = {"total_pnl": 2.0, "sharpe": 0.7, "metrics_version": 2}
    pinned = {"run": "BTC_test", "symbol": "BTC/USDT", "timeframe": "1h", "strategy": "demo",
              "requested_window": ["start", "end"],
              "actual_window": ["2024-01-01 00:00:00+00:00", "2024-01-01 01:00:00+00:00"],
              "bars": 2, "input": artifacts["data/cache/crypto_BTCUSDT_1h_start_end.parquet"],
              "v1_same_config": stats1, "v2": stats2}
    write_json(directory / "BTC_test.json", {"metrics_version": 2, "stats": stats2})
    identity = {"variant": "default", "strategy": "demo", "symbol": "BTC/USDT", "timeframe": "1h"}
    unit = {**identity, "v1_same_config": {**identity, "oos": {"total_pnl": -1.0}},
            "v2": {**identity, "oos": {"total_pnl": 2.0}}}
    experiment = {"id": "demo", "declaration": artifacts["experiments/demo.toml"],
                  "historical_report": artifacts["experiments/demo.results.json"], "rerun_units": 1,
                  "inputs": [{"symbol": "BTC/USDT", "timeframe": "1h", "bars": 2,
                              "ohlcv": pinned["input"],
                              "auxiliary": [artifacts["data/cache/funding_BTCUSDT.parquet"]]}]}
    write_json(directory / "demo.comparison.json", {**experiment, "units": [unit]})
    comparison = {"metrics_version": 2, "historical_reports_unchanged": True,
                  "configuration": replay.configuration(), "current_source_sha256": recorded_sources,
                  "v1_backtest_source_sha256": baseline_hash, "pinned_completed": 1,
                  "pinned": [pinned], "experiments": [experiment]}
    write_json(directory / "comparison.json", comparison)
    checks = {"metrics_version": 2, "status": "passed", "historical_reports_unchanged": True,
              "comparison_sha256": replay.digest(directory / "comparison.json"),
              "current_source_sha256": recorded_sources,
              "pinned": [{"file": "BTC_test.json", "passed": True,
                          "sha256": replay.digest(directory / "BTC_test.json")}],
              "experiments": [{"file": "demo.comparison.json", "passed": True, "completed_cases": 1,
                               "sha256": replay.digest(directory / "demo.comparison.json")}]}
    write_json(directory / "export_checks.json", checks)
    return SimpleNamespace(root=tmp_path, directory=directory, baseline_hash=baseline_hash,
                           sources=sources, configuration=replay.configuration(),
                           comparison=comparison, checks=checks, pinned=pinned, unit=unit,
                           experiment=experiment, artifacts=artifacts)


def load_prior(evidence, **overrides):
    values = {"directory": evidence.directory, "baseline_sha256": evidence.baseline_hash,
              "current_sources": evidence.sources, "current_configuration": evidence.configuration}
    values.update(overrides)
    return replay.PriorResults(**values)


def test_verified_cache_accepts_changed_current_backtest_without_reusing_current_results(evidence):
    prior = load_prior(evidence)
    assert prior.pinned["BTC_test"]["v1_same_config"]["total_pnl"] == -1.0
    assert len(prior.experiments["demo"][1]) == 1
    assert evidence.sources["bot/backtest.py"] != evidence.comparison["current_source_sha256"]["bot/backtest.py"]
    assert prior.provenance["reuse"].startswith("v1_same_config_only")
    prior.assert_unchanged()


def test_cache_rejects_a_different_baseline(evidence):
    with pytest.raises(ValueError, match="baseline"):
        load_prior(evidence, baseline_sha256="different")


@pytest.mark.parametrize("source", replay.SHARED_SOURCE_PATHS)
def test_cache_rejects_shared_source_changes(evidence, source):
    (evidence.root / source).write_text("changed shared implementation")
    sources = {**evidence.sources, source: replay.digest(evidence.root / source)}
    with pytest.raises(ValueError, match="shared source"):
        load_prior(evidence, current_sources=sources)


@pytest.mark.parametrize("group", ["params", "risk", "costs", "capital"])
def test_cache_rejects_configuration_changes(evidence, group):
    configuration = deepcopy(evidence.configuration)
    if group == "capital":
        configuration[group] += 1
    else:
        key = next(iter(configuration[group]))
        configuration[group][key] = "changed"
    with pytest.raises(ValueError, match="configuration"):
        load_prior(evidence, current_configuration=configuration)


@pytest.mark.parametrize("path", ["data/cache/crypto_BTCUSDT_1h_start_end.parquet",
                                  "data/cache/funding_BTCUSDT.parquet",
                                  "experiments/demo.toml", "experiments/demo.results.json"])
def test_cache_rejects_changed_input_declaration_or_historical_report(evidence, path):
    (evidence.root / path).write_text("changed artifact")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_prior(evidence)


@pytest.mark.parametrize("filename", ["comparison.json", "BTC_test.json", "demo.comparison.json"])
def test_cache_rejects_tampered_exported_results(evidence, filename):
    path = evidence.directory / filename
    value = json.loads(path.read_text())
    value["tampered"] = True
    write_json(path, value)
    with pytest.raises(ValueError, match="hash mismatch"):
        load_prior(evidence)


def test_cache_requires_passed_independent_export_evidence(evidence):
    (evidence.directory / "export_checks.json").unlink()
    with pytest.raises(ValueError, match="require passed"):
        load_prior(evidence)


def test_cache_rejects_failed_export_checks(evidence):
    evidence.checks["status"] = "failed"
    write_json(evidence.directory / "export_checks.json", evidence.checks)
    with pytest.raises(ValueError, match="must have passed"):
        load_prior(evidence)


def test_replay_detects_prior_evidence_changes_after_initial_validation(evidence):
    prior = load_prior(evidence)
    (evidence.directory / "demo.comparison.json").write_text("changed during run")
    with pytest.raises(RuntimeError, match="changed during replay"):
        prior.assert_unchanged()


def test_cached_experiments_must_match_current_jobs_and_request(evidence):
    prior = load_prior(evidence)
    key = replay.unit_key(evidence.unit)
    metadata = {name: evidence.experiment[name] for name in ("declaration", "historical_report", "inputs")}
    assert prior.experiment_units("demo", metadata, [key])[key] == evidence.unit
    with pytest.raises(ValueError, match="job geometry"):
        prior.experiment_units("demo", metadata, [key, ("new", *key[1:])])
    with pytest.raises(ValueError, match="inputs/declaration/report"):
        prior.experiment_units("demo", {**metadata, "inputs": []}, [key])
    with pytest.raises(ValueError, match="pinned inputs or request"):
        prior.pinned_row({"run": "BTC_test", "strategy": "changed"})


def test_worker_reuses_only_v1_and_runs_current_code_with_a_temporary_journal(evidence, monkeypatch):
    old_db_path = replay.CONFIG.db_path
    prior_v1 = {"oos": {"total_pnl": -1.0}, "trades": [{"pnl": -1.0}]}
    calls = []

    def run_current(job):
        calls.append((job, replay.CONFIG.db_path))
        assert replay.CONFIG.db_path != old_db_path
        return {"oos": {"total_pnl": 7.0}, "trades": [{"pnl": 7.0}]}

    monkeypatch.setattr(replay, "run_unit", run_current)
    monkeypatch.setattr(replay, "legacy_backtest", lambda commit: pytest.fail("cached v1 must not run"))
    v1, current = replay.unit_pair({"job": 1}, "a" * 40, prior_v1)
    assert len(calls) == 1
    assert current["oos"]["total_pnl"] == 7.0
    assert v1["oos"]["total_pnl"] == -1.0
    assert "trades" not in v1 and "trades" not in current
    assert "trades" in prior_v1
    assert replay.CONFIG.db_path == old_db_path


def test_fresh_worker_uses_the_supplied_commit_and_restores_current_backtester(evidence, monkeypatch):
    import bot.backtest
    original = bot.backtest.Backtester
    baseline = type("Baseline", (), {})
    commits, implementations = [], []

    def legacy(commit):
        commits.append(commit)
        return SimpleNamespace(Backtester=baseline)

    def run(job):
        implementations.append(bot.backtest.Backtester)
        return {"oos": {"total_pnl": 1.0}}

    monkeypatch.setattr(replay, "legacy_backtest", legacy)
    monkeypatch.setattr(replay, "run_unit", run)
    replay.unit_pair({}, "a" * 40)
    assert commits == ["a" * 40]
    assert implementations == [baseline, original]
    assert bot.backtest.Backtester is original


def test_legacy_module_cache_is_keyed_by_pinned_commit(monkeypatch):
    commits = []

    def source(path, commit):
        commits.append(commit)
        return f"COMMIT = {commit!r}\nclass Backtester: pass\n"

    monkeypatch.setattr(replay, "git_source", source)
    for commit in ("test-commit-a", "test-commit-b"):
        monkeypatch.delitem(replay.sys.modules, f"_research_v1_backtest_{commit}", raising=False)
    try:
        a = replay.legacy_backtest("test-commit-a")
        assert replay.legacy_backtest("test-commit-a") is a
        b = replay.legacy_backtest("test-commit-b")
        assert a.COMMIT == "test-commit-a" and b.COMMIT == "test-commit-b"
        assert commits == ["test-commit-a", "test-commit-b"]
    finally:
        replay.sys.modules.pop("_research_v1_backtest_test-commit-a", None)
        replay.sys.modules.pop("_research_v1_backtest_test-commit-b", None)


def test_git_source_uses_resolved_commit_instead_of_moving_head(monkeypatch):
    commands = []

    def git(command, **kwargs):
        commands.append(command)
        return b"a" * 40 + b"\n" if command[1] == "rev-parse" else b"baseline source"

    monkeypatch.setattr(replay.subprocess, "check_output", git)
    commit = replay.resolve_baseline("moving-branch")
    assert replay.git_source("bot/backtest.py", commit) == "baseline source"
    assert commands == [["git", "rev-parse", "--verify", "moving-branch^{commit}"],
                        ["git", "show", f"{'a' * 40}:bot/backtest.py"]]


def test_pinned_current_result_is_fresh_and_exports_prior_v2_differences(evidence, monkeypatch):
    prior = load_prior(evidence)
    monkeypatch.setattr(replay, "RUNS", [("BTC/USDT", "1h", "start", "end", "demo", "BTC_test")])
    frame = pd.DataFrame(index=pd.date_range("2024-01-01", periods=2, freq="h", tz="UTC"))
    monkeypatch.setattr(replay, "cached_frame", lambda *args: frame)
    monkeypatch.setattr(replay, "legacy_backtest", lambda commit: pytest.fail("cached v1 must not run"))
    calls = []
    fresh = SimpleNamespace(stats=lambda: {"total_pnl": 7.0, "sharpe": 1.2, "metrics_version": 3},
                            trades=[], equity_curve=[{"equity": 10007.0}], start_equity=10000)
    monkeypatch.setattr(replay, "Backtester", lambda: SimpleNamespace(run=lambda *args, **kwargs: calls.append(1) or fresh))
    monkeypatch.setattr(replay, "METRICS_VERSION", 3)
    outdir = evidence.root / "v3"
    outdir.mkdir()
    rows, skipped = replay.run_pinned(outdir, "a" * 40, prior)
    assert calls == [1] and skipped == []
    row = rows[0]
    assert row["v1_reused"] is True
    assert row["v1_same_config"]["total_pnl"] == -1.0
    assert row["prior_v2"]["total_pnl"] == 2.0
    assert row["v3"]["total_pnl"] == 7.0
    assert row["prior_v2_differences"]["total_pnl"]["delta"] == 5.0
    assert json.loads((outdir / "BTC_test.json").read_text())["stats"] == fresh.stats()
    prior.assert_unchanged()


def test_all_experiment_workers_receive_the_same_pinned_baseline(evidence, monkeypatch):
    identity = {name: evidence.unit[name] for name in ("variant", "strategy", "symbol", "timeframe")}
    write_json(evidence.root / "experiments/demo.results.json",
               {"windows": {"BTC/USDT 1h": ["start", "end"]}, "markets": [identity]})
    declaration = SimpleNamespace(markets=[("crypto", "BTC/USDT", "1h")], book="standard", sha256="decl")
    monkeypatch.setattr(replay, "load_declaration", lambda path: declaration)
    spec = SimpleNamespace(symbol="BTC/USDT", timeframe="1h")
    cache = evidence.root / evidence.pinned["input"]["path"]
    monkeypatch.setattr(replay, "experiment_frame", lambda *args: (spec, pd.DataFrame(index=[0, 1]), cache))
    jobs = [{"variant": name, "strategy": "demo", "spec": spec} for name in ("a", "b", "c")]
    monkeypatch.setattr(replay, "_jobs", lambda *args: jobs)
    calls = []

    class InlinePool:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def submit(self, function, job, commit, prior_v1):
            calls.append((function, commit, prior_v1))
            future = Future()
            future.set_result(({"oos": {"total_pnl": 1.0}}, {"oos": {"total_pnl": 2.0}}))
            return future

    monkeypatch.setattr(replay, "ProcessPoolExecutor", InlinePool)
    result = replay.run_experiment("demo", 2, "a" * 40)
    assert len(calls) == 3 and {call[1] for call in calls} == {"a" * 40}
    assert all(call[2] is None for call in calls)
    assert result["rerun_units"] == 3


def test_main_pins_baseline_once_even_when_head_changes(evidence, monkeypatch):
    resolutions, pinned_commits, experiment_commits = [], [], []

    def resolve(ref):
        resolutions.append(ref)
        return "a" * 40 if ref == "moving-branch" else "b" * 40

    monkeypatch.setattr(replay, "resolve_baseline", resolve)
    monkeypatch.setattr(replay, "git_source", lambda path, commit: "original baseline")
    monkeypatch.setattr(replay, "run_pinned", lambda output, commit, prior: pinned_commits.append(commit) or ([], []))

    def experiment(name, workers, commit, prior):
        experiment_commits.append(commit)
        return {"id": name, "rerun_units": 1, "v1_reused_units": 0, "units": []}

    monkeypatch.setattr(replay, "run_experiment", experiment)
    output = evidence.root / "v3"
    replay.main(["--baseline-ref", "moving-branch", "--output", str(output),
                 "--experiments", "demo", "another"])
    exported = json.loads((output / "comparison.json").read_text())
    assert resolutions == ["moving-branch", "HEAD"]
    assert pinned_commits == ["a" * 40] and experiment_commits == ["a" * 40, "a" * 40]
    assert exported["baseline_commit"] == "a" * 40
    assert exported["git_head"] == "b" * 40


def test_main_preserves_existing_evidence_in_output_directory(evidence, monkeypatch):
    monkeypatch.setattr(replay, "resolve_baseline", lambda ref: "a" * 40)
    monkeypatch.setattr(replay, "git_source", lambda path, commit: "original baseline")
    original_hash = replay.digest(evidence.directory / "comparison.json")
    with pytest.raises(ValueError, match="must be empty"):
        replay.main(["--output", str(evidence.directory), "--experiments"])
    assert replay.digest(evidence.directory / "comparison.json") == original_hash


@pytest.mark.parametrize("change", ["source", "configuration", "input", "historical_report"])
def test_main_rejects_changes_during_execution(evidence, monkeypatch, change):
    monkeypatch.setattr(replay, "resolve_baseline", lambda ref: "a" * 40)
    monkeypatch.setattr(replay, "git_source", lambda path, commit: "original baseline")
    reference = evidence.pinned["input"]
    monkeypatch.setattr(replay, "run_pinned", lambda *args: ([{"input": reference}], []))

    def experiment(*args):
        if change == "configuration":
            monkeypatch.setattr(replay.CONFIG.risk, "risk_per_trade", replay.CONFIG.risk.risk_per_trade + 0.01)
        else:
            path = {"source": "bot/backtest.py", "input": reference["path"],
                    "historical_report": "experiments/demo.results.json"}[change]
            (evidence.root / path).write_text("changed during replay")
        return {"id": "demo", "rerun_units": 0, "v1_reused_units": 0, "units": []}

    monkeypatch.setattr(replay, "run_experiment", experiment)
    output = evidence.root / "v3"
    with pytest.raises(RuntimeError, match="changed during"):
        replay.main(["--output", str(output), "--experiments", "demo"])
    assert not (output / "comparison.json").exists()


def test_main_rejects_prior_evidence_modified_during_replay(evidence, monkeypatch):
    monkeypatch.setattr(replay, "resolve_baseline", lambda ref: "a" * 40)
    monkeypatch.setattr(replay, "git_source", lambda path, commit: "original baseline")

    def pinned(*args):
        (evidence.directory / "BTC_test.json").write_text("changed during replay")
        return [], []

    monkeypatch.setattr(replay, "run_pinned", pinned)
    output = evidence.root / "v3"
    with pytest.raises(RuntimeError, match="changed during replay"):
        replay.main(["--output", str(output), "--prior-results", str(evidence.directory), "--experiments"])
    assert not (output / "comparison.json").exists()


def test_network_connections_are_explicitly_denied():
    replay.offline_guard("unrelated.event", ())
    with pytest.raises(RuntimeError, match="network connections are disabled"):
        replay.offline_guard("socket.connect", ())
