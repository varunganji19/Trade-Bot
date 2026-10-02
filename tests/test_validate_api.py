"""The dashboard's "Validate a strategy" endpoint (bot/api/validate.py)."""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from tests.test_validator import EDGE, LOSER, _ft_export


@pytest.fixture(scope="module")
def client():
    from bot.dashboard import app
    return TestClient(app, base_url="http://127.0.0.1")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_a_freqtrade_export_comes_back_judged(client):
    raw = json.dumps(_ft_export({"A": EDGE, "B": LOSER})).encode()
    r = client.post("/api/validate", json={"filename": "backtest-result.json",
                                           "content_b64": _b64(raw), "trials": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    rep = body["report"]
    assert rep["input"] == "backtest-result.json" and rep["trials"] == 3
    assert rep["strategies"]["B"]["verdict"] == "likely overfit"
    assert rep["strategies"]["A"]["verdict"] == "fragile"       # no regimes checked
    assert body["markdown"].startswith("# Strategy validation: backtest-result.json")


def test_a_csv_upload_is_read(client):
    rows = "open_time,close_time,pnl\n" + "".join(
        f"2024-01-{d:02d} 00:00,2024-01-{d:02d} 04:00,{p}\n"
        for d, p in zip(range(1, 29), [3, -2] * 14))
    r = client.post("/api/validate", json={"filename": "mine.csv",
                                           "content_b64": _b64(rows.encode())})
    assert r.status_code == 200, r.text
    assert r.json()["report"]["strategies"]["upload"]["trades"] == 28


@pytest.mark.parametrize("body, code, text", [
    ({"filename": "x.exe", "content_b64": _b64(b"{}")}, 422, ".json, .zip or .csv"),
    ({"filename": "x.json", "content_b64": "not base64!!"}, 422, "could not be decoded"),
    ({"filename": "x.json", "content_b64": _b64(b"not json")}, 422, "cannot validate"),
    ({"filename": "x.csv", "content_b64": _b64(b"time,price\n1,2\n")}, 422, "net P&L"),
    ({"filename": "x.json", "content_b64": _b64(b"{}"), "regime_market": "DOGE/USDT"},
     422, "regime market"),
])
def test_bad_uploads_are_refused_with_a_reason(client, body, code, text):
    r = client.post("/api/validate", json=body)
    assert r.status_code == code
    assert text in r.json()["detail"]


def test_the_upload_name_is_never_used_as_a_path(client, tmp_path):
    raw = json.dumps(_ft_export({"A": EDGE})).encode()
    r = client.post("/api/validate", json={"filename": "../../etc/evil.json",
                                           "content_b64": _b64(raw)})
    assert r.status_code == 200
    assert r.json()["report"]["input"] == "evil.json"


def test_the_page_offers_the_tab(client):
    html = client.get("/").text
    assert 'data-view="validate"' in html and 'id="valRunBtn"' in html
    assert "/api/validate" in client.get("/app.js").text
