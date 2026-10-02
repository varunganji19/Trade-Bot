"""The anonymised verdict store (bot/verdict_store.py)."""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from bot import validator as v
from bot import verdict_store as vs
from config import CONFIG
from tests.test_validator import EDGE, LOSER, _ft_export, _write


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(CONFIG, "db_path", str(tmp_path / "trading.db"))
    return tmp_path


def _report(tmp_path, trials=None):
    doc = _ft_export({"SecretAlphaV7": EDGE, "MyLoser": LOSER})
    return v.validate(_write(tmp_path, doc, "client-acme-btc.json"), trials=trials)


def test_a_record_holds_only_the_fixed_coarse_fields(sandbox):
    report = _report(sandbox, trials=12)
    assert vs.record(report) == 2
    rows = vs.load()
    assert all(set(r) == set(vs.FIELDS) for r in rows)
    text = vs.store_path() and open(vs.store_path()).read()
    # nothing identifying survives: names, file, market, dates, money
    for secret in ("SecretAlphaV7", "MyLoser", "client-acme", "BTC", "2024-01", "net_pnl"):
        assert secret not in text
    loser = next(r for r in rows if r["verdict"] == v.OVERFIT)
    assert "no_edge" in loser["fails"] and loser["trials"] == "10-99"
    assert loser["trades"] == "100-299" and loser["format"] == "freqtrade"
    assert loser["pf"] == 0.5


def test_unknown_trials_and_unchecked_inputs_are_coded(sandbox):
    vs.record(_report(sandbox))
    edge = next(r for r in vs.load() if r["verdict"] == v.FRAGILE)
    assert {"trials_unknown", "regimes_unchecked"} <= set(edge["fails"])
    assert edge["trials"] == "unknown"


def test_the_summary_counts_what_overfits(sandbox):
    vs.record(_report(sandbox, trials=3))
    vs.record(_report(sandbox))
    s = vs.summarise(vs.load())
    assert s["records"] == 4 and s["verdicts"] == {v.FRAGILE: 2, v.OVERFIT: 2}
    assert dict(s["most_common_failures"])["regimes_unchecked"] == 4
    assert set(s["verdicts_by_trials"]) == {"2-9", "unknown"}


def test_nothing_is_recorded_unless_asked(sandbox):
    from bot.dashboard import app
    client = TestClient(app, base_url="http://127.0.0.1")
    raw = base64.b64encode(json.dumps(_ft_export({"A": EDGE})).encode()).decode()
    r = client.post("/api/validate", json={"filename": "x.json", "content_b64": raw})
    assert r.json()["recorded"] == 0 and vs.load() == []
    r = client.post("/api/validate", json={"filename": "x.json", "content_b64": raw,
                                           "record": True})
    assert r.json()["recorded"] == 1 and len(vs.load()) == 1
