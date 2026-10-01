"""Pre-registered experiments, the registry, and promotion rule v2."""
import json

import numpy as np
import pandas as pd
import pytest

import bot.experiments as ex
import bot.promotion as promo


def _ev(trades, pf, lo, hi, regimes):
    return {"trades": trades, "pf": pf, "pf_lo": lo, "pf_hi": hi, "regimes": regimes}


ALL3 = {"trend_up": 40, "trend_down": 40, "range": 40}


# ----------------------------------------------------------------- rule v2
def test_rule_v2_promotes_only_proven_strategies():
    v = promo.verdicts_from_evidence({
        "proven": _ev(150, 1.4, 1.1, 1.8, ALL3),
        "straddles": _ev(150, 1.2, 0.9, 1.5, ALL3),
        "one_regime": _ev(150, 1.6, 1.2, 2.0, {"trend_up": 150}),
        "few": _ev(60, 1.56, 1.2, 2.1, ALL3),
        "loser": _ev(60, 0.6, 0.45, 0.8, ALL3),
        "silent": _ev(0, None, None, None, {}),
    })
    assert v["proven"]["status"] == promo.PROMOTED
    assert v["straddles"]["status"] == promo.PROBATION
    assert "straddles 1.0" in v["straddles"]["why"]
    assert v["one_regime"]["status"] == promo.PROBATION
    assert "regimes" in v["one_regime"]["why"]
    # today's voters (PF ~1.56 on ~60 trades) cannot pass the new bar
    assert v["few"]["status"] == promo.PROBATION
    assert v["loser"]["status"] == promo.DEMOTED       # whole interval below 1.0
    assert v["silent"]["status"] == promo.PROBATION
    assert {x["rule"] for x in v.values()} == {promo.RULE_VERSION}
    assert "90% interval" in v["proven"]["why"]


def test_may_vote_by_rule_version():
    assert promo.may_vote("anything", {})                       # unmeasured gate
    v1 = {"a": {"status": promo.PROBATION}, "b": {"status": promo.DEMOTED}}
    assert promo.may_vote("a", v1) and not promo.may_vote("b", v1)
    assert promo.may_vote("never_measured", v1)
    v2 = promo.verdicts_from_evidence({"p": _ev(150, 1.4, 1.1, 1.8, ALL3),
                                       "q": _ev(60, 1.5, 1.1, 2.0, ALL3)})
    assert promo.may_vote("p", v2)
    assert not promo.may_vote("q", v2)                          # probation: silent
    assert not promo.may_vote("never_measured", v2)


def test_orchestrator_silences_probation_under_rule_v2(tmp_path, monkeypatch):
    """The live decision path follows the gate: a v2 file with no promoted
    strategy leaves the book with nothing that may vote."""
    import config as config_mod
    from bot.orchestrator import Orchestrator
    from bot.strategies import STRATEGY_CLASSES
    monkeypatch.setattr(config_mod.CONFIG, "db_path", str(tmp_path / "t.db"))
    promo._CACHE.update(path=None, mtime=None, verdicts={})
    names = [n for n, c in STRATEGY_CLASSES.items() if getattr(c, "book", "standard") == "standard"]
    promo.save_verdicts(promo.verdicts_from_evidence(
        {n: _ev(60, 1.5, 1.1, 2.0, ALL3) for n in names}), "standard", book="standard")
    st = promo.voting_strategies("standard")
    assert st["voting"] == [] and len(st["silent"]) == st["registered"]
    assert all("probation" in x["why"] for x in st["silent"])
    assert st["gate"]["rule"] == promo.RULE_VERSION and not st["gate"]["stale"]

    idx = pd.date_range("2025-01-01", periods=400, freq="1h", tz="UTC")
    close = pd.Series(100 + np.cumsum(np.random.default_rng(0).normal(0, 1, 400)), index=idx)
    df = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1,
                       "close": close, "volume": 1000.0})
    from bot.indicators import add_all_indicators
    from config import MarketSpec
    d = Orchestrator(book="standard").decide(add_all_indicators(df), 399,
                                             MarketSpec("crypto", "BTC/USDT", "1h"))
    assert d.action == "HOLD"
    promo._CACHE.update(path=None, mtime=None, verdicts={})


def test_v1_verdict_file_is_flagged_as_old_rule(tmp_path):
    path = str(tmp_path / "p.json")
    promo.save_verdicts({"x": {"status": promo.PROMOTED, "why": "m",
                               "evidence": "walk_forward_oos"}}, "perp", path=path)
    promo._CACHE.update(path=None, mtime=None, verdicts={})
    g = promo.gate_state(path)
    assert g["rule"] == 1 and g["stale"] and "make evidence" in g["why"]
    assert json.load(open(path))["rule"]["version"] == 1
    promo._CACHE.update(path=None, mtime=None, verdicts={})


# ------------------------------------------------------------ declarations
GATE = '''id = "{id}"
kind = "{kind}"
book = "standard"
created = 2026-10-01
hypothesis = "test"
strategies = {strategies}
days = 60
folds = 2
{extra}
[[markets]]
kind = "crypto"
symbol = "BTC/USDT"
timeframes = ["1h", "4h"]

[[markets]]
kind = "crypto"
symbol = "ETH/USDT"
timeframes = ["1h", "4h"]
'''


def _write(tmp_path, eid="g", kind="gate", strategies='["turtle_trend", "connors_meanrev"]',
           extra=""):
    p = tmp_path / f"{eid}.toml"
    p.write_text(GATE.format(id=eid, kind=kind, strategies=strategies, extra=extra))
    return str(p)


@pytest.mark.parametrize("kwargs, msg", [
    ({"extra": "colour = 1"}, "unknown key"),
    ({"eid": "other", "extra": ""}, None),                          # id == stem: fine
    ({"strategies": '["hft_exhaustion_fade"]'}, "fast book"),
    ({"strategies": '["no_such"]'}, "unknown strategy"),
    ({"extra": '[[variants]]\nname = "v"\nparams = {turtle_adx_min = 25.0}'},
     "declare parameter variants as a study"),
    ({"kind": "study", "extra": '[[variants]]\nname = "v"\nparams = {nope = 1}'},
     "unknown parameter"),
])
def test_declaration_validation(tmp_path, kwargs, msg):
    path = _write(tmp_path, **kwargs)
    if msg is None:
        assert ex.load_declaration(path).id == "other"
        return
    with pytest.raises(ex.DeclarationError, match=msg):
        ex.load_declaration(path)


def test_id_must_match_file_name(tmp_path):
    p = tmp_path / "x.toml"
    p.write_text(GATE.format(id="y", kind="gate", strategies='["turtle_trend"]', extra=""))
    with pytest.raises(ex.DeclarationError, match="must match the file name"):
        ex.load_declaration(str(p))


def test_uncommitted_declaration_is_refused(tmp_path):
    path = _write(tmp_path)
    assert "not committed" in ex.preregistration_problem(path)
    with pytest.raises(ex.DeclarationError, match="refused"):
        ex.run_declaration(path, quiet=True)


def test_committed_gate_declarations_are_valid():
    for path in ex.declarations():
        d = ex.load_declaration(path)
        assert d.kind in ex.KINDS


# ---------------------------------------------------------- end to end, fake
def _fake_world(tmp_path, monkeypatch, trades_per_fold=40, edge=6.0):
    """Fake market data and a fake backtester: deterministic trades whose
    entries span two years, so every regime is visited."""
    import bot.backtest as bt_mod
    import bot.data as data_mod
    import config as config_mod

    monkeypatch.setattr(config_mod.CONFIG, "db_path", str(tmp_path / "db" / "t.db"))
    monkeypatch.setattr(ex, "EXPERIMENTS_DIR", str(tmp_path))
    monkeypatch.setattr(ex, "HISTORY_FILE", str(tmp_path / "history.jsonl"))
    promo._CACHE.update(path=None, mtime=None, verdicts={})

    def fake_fetch(spec, days=None, **_):
        if spec.timeframe == "1d":
            n = 1000
            t = np.arange(n)
            close = 100 + 40 * np.sin(t / 90.0) + t * 0.05      # up, down and flat stretches
            idx = pd.date_range("2023-06-01", periods=n, freq="1D", tz="UTC")
        else:
            n = 2000
            close = np.full(n, 100.0)
            idx = pd.date_range("2025-10-01", periods=n, freq="1h", tz="UTC")
        c = pd.Series(close, index=idx)
        return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
                             "volume": 1.0})

    class FakeBT:
        def __init__(self, cfg=None, book="standard"):
            pass

        def run_walk_forward(self, spec, df, folds, strategy, progress, warmup_bars):
            rng = np.random.default_rng(abs(hash((spec.symbol, spec.timeframe, strategy))) % 2**32)
            trades = []
            start = pd.Timestamp("2024-03-01", tz="UTC")
            for k in range(folds * trades_per_fold):
                t0 = start + pd.Timedelta(days=int(k * 700 / (folds * trades_per_fold)))
                trades.append({"entry_ts": str(t0), "exit_ts": str(t0 + pd.Timedelta(hours=5)),
                               "pnl": float(rng.normal(edge, 20)), "status": "CLOSED"})
            return {"folds": [{"profit_factor": 1.2}] * folds,
                    "aggregate": {"sharpe": 0.8, "total_pnl": 1.0, "fees": 0.5},
                    "trades": trades}

    monkeypatch.setattr(data_mod, "fetch_history", fake_fetch)
    monkeypatch.setattr(bt_mod, "Backtester", FakeBT)


def test_gate_run_writes_v2_verdicts_results_and_registry(tmp_path, monkeypatch):
    """`make evidence` end to end: every declared strategy gets a v2 verdict
    with an interval and regime counts, results land next to the
    declaration, and the registry is rebuilt from them."""
    _fake_world(tmp_path, monkeypatch)
    path = _write(tmp_path, strategies='["turtle_trend", "connors_meanrev", "ts_momentum"]')
    res = ex.run_declaration(path, workers=1, quiet=True, require_preregistered=False)

    verdict_file = tmp_path / "db" / "results" / "promotions_standard.json"
    payload = json.loads(verdict_file.read_text())
    assert payload["rule"]["version"] == promo.RULE_VERSION
    assert payload["experiment"] == "g"
    assert set(payload["strategies"]) == {"turtle_trend", "connors_meanrev", "ts_momentum"}
    for v in payload["strategies"].values():
        assert v["pf_lo"] is not None and v["pf_lo"] <= v["pf"] <= v["pf_hi"]
        assert sum(v["regimes"].values()) == v["trades"]
    # ts_momentum runs on 1h AND 4h, the others on one timeframe each
    assert payload["strategies"]["ts_momentum"]["trades"] == 2 * payload["strategies"][
        "turtle_trend"]["trades"]

    saved = json.loads((tmp_path / "g.results.json").read_text())
    assert saved["declaration_sha256"] == res["declaration_sha256"]
    assert {e["strategy"] for e in saved["summary"]} == set(payload["strategies"])

    records = ex.load_registry()
    assert {r["level"] for r in records} == {"pooled", "market"}
    trials = ex.trials_for("ts_momentum", records=records)
    assert trials["n_trials"] == 4 and trials["sharpes"] == [0.8] * 4   # 2 markets x 2 tf
    assert ex.trials_for("ts_momentum", "4h", records=records)["n_trials"] == 2
    promo._CACHE.update(path=None, mtime=None, verdicts={})


def test_study_never_touches_the_gate_and_reports_the_holdout(tmp_path, monkeypatch):
    _fake_world(tmp_path, monkeypatch)
    import bot.backtest as bt_mod

    class Res:
        trades = [{"entry_ts": "2025-12-01 00:00:00+00:00",
                   "exit_ts": "2025-12-01 05:00:00+00:00", "pnl": -3.0, "status": "CLOSED"}]

        def stats(self):
            return {"trades": 1, "profit_factor": None, "sharpe": None,
                    "total_pnl": -3.0, "fees": 1.0}

    monkeypatch.setattr(bt_mod.Backtester, "run", lambda self, *a, **k: Res(), raising=False)
    path = _write(tmp_path, eid="s", kind="study", strategies='["turtle_trend"]',
                  extra='holdout_days = 20\n[[variants]]\nname = "a"\nparams = {turtle_adx_min = 25.0}\n'
                        '[[variants]]\nname = "b"\nparams = {turtle_adx_min = 30.0}')
    res = ex.run_declaration(path, workers=1, quiet=True, require_preregistered=False)
    assert not (tmp_path / "db" / "results" / "promotions_standard.json").exists()
    assert [e["variant"] for e in res["summary"]] == ["a", "b"]
    assert res["summary"][0]["holdout"]["net_pnl"] == -6.0           # 2 markets x -3
    assert res["summary"][0]["holdout"]["gross_pnl"] == -4.0
    assert ex.trials_for("turtle_trend")["n_trials"] == 4             # 2 variants x 2 markets


def test_gate_refuses_to_publish_partial_evidence(tmp_path, monkeypatch):
    _fake_world(tmp_path, monkeypatch)
    import bot.data as data_mod
    real = data_mod.fetch_history

    def flaky(spec, days=None, **kw):
        if spec.symbol == "ETH/USDT" and spec.timeframe == "4h":
            raise ConnectionError("exchange down")
        return real(spec, days=days, **kw)

    monkeypatch.setattr(data_mod, "fetch_history", flaky)
    path = _write(tmp_path)
    with pytest.raises(RuntimeError, match="incomplete"):
        ex.run_declaration(path, workers=1, quiet=True, require_preregistered=False)
    assert not (tmp_path / "db" / "results" / "promotions_standard.json").exists()


def test_a_trial_is_a_distinct_configuration():
    """The same settings declared in two studies, or re-run on newer data,
    are one trial; different settings, or the same settings on another
    market, are separate trials. Retroactive records without params count
    once each."""
    def rec(exp, params, symbol, sharpe):
        return {"experiment": exp, "variant": "v", "level": "market", "strategy": "s",
                "tier": "perp", "params": params, "symbol": symbol, "timeframe": "5m",
                "oos": {"sharpe": sharpe}}
    records = [rec("a", {"z": 2.5}, "BTC", 0.1), rec("b", {"z": 2.5}, "BTC", 0.3),
               rec("a", {"z": 2.0}, "BTC", 0.2), rec("a", {"z": 2.5}, "ETH", 0.4),
               {"experiment": "old", "variant": "x", "level": "retro", "strategy": "s"},
               dict(rec("a", {"z": 9}, "BTC", 9.0), level="pooled")]
    t = ex.trials_for("s", records=records)
    assert t["n_trials"] == 4
    assert sorted(t["sharpes"]) == [0.2, 0.3, 0.4]     # latest of the duplicate wins


def test_default_settings_written_out_are_the_same_trial():
    from config import CONFIG
    z = CONFIG.params.hft_fade_z_entry
    assert ex.canonical_params({"hft_fade_z_entry": z}) == {}
    assert ex.canonical_params({"hft_fade_z_entry": z + 1}) == {"hft_fade_z_entry": z + 1}
    recs = [{"experiment": e, "variant": "v", "level": "market", "strategy": "s", "tier": "perp",
             "params": p, "symbol": "BTC", "timeframe": "5m", "oos": {"sharpe": 0.1}}
            for e, p in (("a", {}), ("b", {"hft_fade_z_entry": z}))]
    assert ex.trials_for("s", records=recs)["n_trials"] == 1


def test_experiment_log_shows_runs_and_history_newest_first(tmp_path, monkeypatch):
    """The Evidence tab's experiment log: one row per (experiment, strategy)
    with its verdict, and the retroactive history beside it."""
    _fake_world(tmp_path, monkeypatch)
    (tmp_path / "history.jsonl").write_text(json.dumps(
        {"experiment": "old", "variant": "x", "level": "retro", "strategy": "kronos",
         "run_at": "2026-09-19", "result": "IC -0.075", "verdict": "rejected"}) + "\n")
    path = _write(tmp_path, strategies='["turtle_trend", "connors_meanrev"]')
    ex.run_declaration(path, workers=1, quiet=True, require_preregistered=False)
    rows = ex.experiment_log()
    assert [r["experiment"] for r in rows][-1] == "old"          # newest first
    gate = {r["strategy"]: r for r in rows if r["experiment"] == "g"}
    assert set(gate) == {"turtle_trend", "connors_meanrev"}
    assert all(r["verdict"] in (promo.PROMOTED, promo.PROBATION, promo.DEMOTED)
               for r in gate.values())
    assert "OOS PF" in gate["turtle_trend"]["result"]
    assert gate["turtle_trend"]["hypothesis"] == "test"

    import bot.dashboard as dash
    from fastapi.testclient import TestClient
    import bot.api.evidence as evidence_api
    evidence_api._EVIDENCE_CACHE.update(key=None, payload=None, ts=0.0)
    payload = TestClient(dash.app, base_url="http://127.0.0.1").get("/api/evidence").json()
    assert payload["experiments"]["n"] == len(rows)
    promo._CACHE.update(path=None, mtime=None, verdicts={})


def test_every_silent_v2_strategy_names_its_status(monkeypatch):
    """Under rule v2 a silent row says whether it is demoted or on probation,
    so the two kinds of "not trading" cannot be confused on the dashboard."""
    names = ["turtle_trend", "connors_meanrev"]
    v2 = promo.verdicts_from_evidence({names[0]: _ev(150, 0.6, 0.5, 0.7, ALL3),
                                       names[1]: _ev(150, 1.1, 0.9, 1.3, ALL3)})
    monkeypatch.setattr(promo, "load_verdicts", lambda path=None, **kw: v2)
    silent = {x["name"]: x["why"] for x in promo.voting_strategies("standard")["silent"]}
    assert silent[names[0]].startswith("demoted — ")
    assert silent[names[1]].startswith("probation — ")


def test_a_candidate_is_never_presented_as_promoted(monkeypatch):
    """The fast gate measured the candidate market maker at PF 1.41 under
    touch fills, an artefact of the fill model. Neither the Strategies box
    nor the Experiment log may present a candidate as promoted."""
    from bot.strategies import CANDIDATE_NOTES
    mm = "hft_market_maker"
    v2 = promo.verdicts_from_evidence({mm: _ev(36000, 1.41, 1.36, 1.47, ALL3)})
    assert v2[mm]["status"] == promo.PROMOTED
    monkeypatch.setattr(promo, "load_verdicts", lambda path=None, **kw: v2)
    st = promo.voting_strategies("fast")
    row = {x["name"]: x["why"] for x in st["silent"]}[mm]
    assert row == CANDIDATE_NOTES[mm] and "promoted" not in row
    assert mm not in st["voting"]

    rec = {"experiment": "fast_gate", "strategy": mm, "level": "pooled", "variant": "default",
           "book": "fast", "run_at": "2026-10-01", "verdict": "promoted",
           "oos": {"pf": 1.41, "pf_lo": 1.36, "pf_hi": 1.47, "trades": 36094}}
    log = ex.experiment_log([rec])
    assert log[0]["verdict"] == "candidate — never votes"
    assert "not an edge" in log[0]["result"]


def test_gate_header_counts_voters_not_promoted_candidates(tmp_path):
    path = str(tmp_path / "p.json")
    promo.save_verdicts(promo.verdicts_from_evidence(
        {"hft_market_maker": _ev(36000, 1.41, 1.36, 1.47, ALL3),
         "hft_exhaustion_fade": _ev(880, 0.67, 0.53, 0.82, ALL3)}), "perp", path=path)
    promo._CACHE.update(path=None, mtime=None, verdicts={})
    why = promo.gate_state(path)["why"]
    assert "0 may vote" in why and "promoted" not in why
    promo._CACHE.update(path=None, mtime=None, verdicts={})
