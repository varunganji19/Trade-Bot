"""Opt-in, coarse client counters; no external services or real journal."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from bot import usage
from bot.api.validate import router
from config import CONFIG
from tests.test_validator import _ft_export, _write, EDGE, LOSER


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(CONFIG, "db_path", str(tmp_path/"trading.db"))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, tmp_path


def payload(**extra):
    raw = json.dumps(_ft_export({"A": EDGE, "B": LOSER})).encode()
    return {"filename": "private-client-market.json",
            "content_b64": base64.b64encode(raw).decode(), **extra}


def test_default_validation_and_stats_create_no_client_or_counter_store(env):
    client, _ = env
    response = client.post("/api/validate", json=payload())
    assert response.status_code == 200 and response.json()["usage_recorded"] is False
    assert client.get("/api/usage").json() == {"validations": 0, "consenting_clients": 0, "repeat_clients": 0}
    assert not usage.store_path().exists() and not usage.client_path().exists()


def test_opt_in_counts_one_run_not_each_strategy_and_tracks_repeat_clients(env):
    client, _ = env
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    for identity in (first, first, second):
        response = client.post("/api/validate", json=payload(usage_opt_in=True, usage_client_id=identity))
        assert response.status_code == 200 and response.json()["usage_recorded"] is True
    assert usage.stats() == {"validations": 3, "consenting_clients": 2, "repeat_clients": 1}
    with sqlite3.connect(usage.store_path()) as conn:
        rows = conn.execute("SELECT * FROM clients").fetchall()
        assert len(rows) == 2 and len(rows[0]) == 4
    raw = usage.store_path().read_bytes()
    for secret in (first, second, "private-client-market", "BTC", "net_pnl", "Strategy"):
        assert secret.encode() not in raw
    assert usage.store_path().stat().st_mode & 0o777 == 0o600


def test_declining_a_later_run_does_not_reuse_a_previous_opt_in(env):
    client, _ = env
    identity = str(uuid.uuid4())
    client.post("/api/validate", json=payload(usage_opt_in=True, usage_client_id=identity))
    client.post("/api/validate", json=payload(usage_client_id=identity))
    assert usage.stats()["validations"] == 1


def test_failed_validation_is_not_counted(env):
    client, _ = env
    body = payload(usage_opt_in=True, usage_client_id=str(uuid.uuid4()))
    body["content_b64"] = base64.b64encode(b"not a trade list").decode()
    assert client.post("/api/validate", json=body).status_code == 422
    assert not usage.store_path().exists()


@pytest.mark.parametrize("extra", [{"usage_opt_in": True}, {"usage_opt_in": "true"},
                                   {"usage_opt_in": True, "usage_client_id": "person@example.com"}])
def test_usage_requires_an_explicit_flag_and_random_id(env, extra):
    client, _ = env
    assert client.post("/api/validate", json=payload(**extra)).status_code == 422
    assert not usage.store_path().exists()


def test_optional_counter_failure_keeps_the_report_available(env, monkeypatch):
    client, _ = env
    def fail(*args):
        raise sqlite3.OperationalError("disk unavailable")
    monkeypatch.setattr(usage, "record", fail)
    response = client.post("/api/validate", json=payload(usage_opt_in=True, usage_client_id=str(uuid.uuid4())))
    assert response.status_code == 200
    assert len(response.json()["report"]["strategies"]) == 2
    assert response.json()["usage_recorded"] is False and response.json()["usage_warning"]


def test_forgetting_one_client_preserves_the_other_and_can_be_retried(env):
    client, _ = env
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    usage.record(first)
    usage.record(second)
    for _ in range(2):
        assert client.post("/api/usage/forget", json={"client_id": first}).json() == {"ok": True}
    assert usage.stats() == {"validations": 1, "consenting_clients": 1, "repeat_clients": 0}


@pytest.mark.parametrize("previous_runs", [0, 1])
def test_deletion_blocks_an_inflight_validation_even_before_its_first_count(env, monkeypatch, previous_runs):
    from bot import validator
    client, _ = env
    identity = str(uuid.uuid4())
    if previous_runs:
        usage.record(identity)
    entered, resume = threading.Event(), threading.Event()
    original = validator.validate

    def delayed(*args, **kwargs):
        entered.set()
        assert resume.wait(5), "validation was not released"
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(validator, "validate", delayed)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.post, "/api/validate",
                                  json=payload(usage_opt_in=True, usage_client_id=identity))
            try:
                assert entered.wait(5), "validation did not start"
                assert client.post("/api/usage/forget", json={"client_id": identity}).status_code == 200
                assert usage.stats()["validations"] == 0
            finally:
                resume.set()
            response = pending.result(timeout=5)
    assert response.status_code == 200 and response.json()["report"]["strategies"]
    assert response.json()["usage_recorded"] is False
    assert usage.stats() == {"validations": 0, "consenting_clients": 0, "repeat_clients": 0}
    # A separate request/process using the old ID must also stay forgotten.
    assert usage.record(identity) is False
    assert usage.stats()["validations"] == 0
    assert identity.encode() not in usage.store_path().read_bytes()
    # Explicitly opting in with a fresh ID still works after deletion.
    fresh = str(uuid.uuid4())
    assert client.post("/api/validate", json=payload(usage_opt_in=True, usage_client_id=fresh)).json()["usage_recorded"] is True
    assert usage.stats()["validations"] == 1


def test_deletion_marker_expires_after_ninety_utc_days(env, monkeypatch):
    identity = str(uuid.uuid4())
    monkeypatch.setattr(usage.time, "time", lambda: 1000*86400)
    usage.forget(identity)
    monkeypatch.setattr(usage.time, "time", lambda: (1000+usage.RETENTION_DAYS-1)*86400)
    assert usage.record(identity) is False
    assert usage.stats()["validations"] == 0
    monkeypatch.setattr(usage.time, "time", lambda: (1000+usage.RETENTION_DAYS)*86400)
    assert usage.stats()["validations"] == 0
    assert usage.record(identity) is True


def test_inactive_clients_expire_at_ninety_days(env, monkeypatch):
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    monkeypatch.setattr(usage.time, "time", lambda: 1000*86400)
    usage.record(first)
    monkeypatch.setattr(usage.time, "time", lambda: (1000+usage.RETENTION_DAYS)*86400)
    usage.record(second)
    assert usage.stats() == {"validations": 1, "consenting_clients": 1, "repeat_clients": 0}


def test_concurrent_counts_and_cli_ids_are_stable(env):
    identity = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: usage.record(identity), range(20)))
        client_ids = list(pool.map(lambda _: usage.cli_client_id(), range(12)))
    assert usage.stats() == {"validations": 20, "consenting_clients": 1, "repeat_clients": 1}
    assert len(set(client_ids)) == 1
    assert usage.client_path().stat().st_mode & 0o777 == 0o600


def test_cli_opt_in_is_per_invocation_and_forget_removes_installation_state(env, capsys, monkeypatch):
    from bot.cli.research import cmd_validate_trades
    _, tmp = env
    path = _write(tmp, _ft_export({"A": EDGE}))
    args = SimpleNamespace(file=str(path), prices=None, regime_market=None, trials=None,
                           strategy=None, report=None, json=None, record=False, usage=False)
    cmd_validate_trades(args)
    assert not usage.client_path().exists() and not usage.store_path().exists()
    args.usage = True
    cmd_validate_trades(args)
    cmd_validate_trades(args)
    assert usage.stats()["validations"] == 2 and usage.stats()["repeat_clients"] == 1
    monkeypatch.setattr("sys.argv", ["usage", "forget"])
    usage.main()
    assert not usage.client_path().exists() and usage.stats()["validations"] == 0
    assert "removed" in capsys.readouterr().out
