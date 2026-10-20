"""Public waitlist acceptance; storage stays in temporary databases."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from bot import waitlist
from bot.website import create_app


@pytest.fixture
def web(tmp_path, monkeypatch):
    path = tmp_path/"waitlist.db"
    monkeypatch.setenv("ALGO_WAITLIST_DB", str(path))
    monkeypatch.setenv("ALGO_WEBSITE_HOSTS", "localhost,127.0.0.1")
    with TestClient(create_app(), base_url="http://127.0.0.1") as client:
        yield client, path


def test_signup_requires_explicit_consent_and_an_email(web):
    client, path = web
    for body in ({"email": "person@example.com"}, {"email": "person@example.com", "consent": False},
                 {"email": "person@example.com", "consent": "true"},
                 {"email": "bad@@example.com", "consent": True}):
        assert client.post("/api/waitlist", json=body).status_code == 422
    assert not path.exists()


def test_real_signup_persists_expected_fields_and_deletes_with_private_token(web):
    client, path = web
    response = client.post("/api/waitlist", json={"email": "person@EXAMPLE.COM", "consent": True})
    assert response.status_code == 201
    token = response.json()["removal_token"]
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT * FROM signups").fetchone()
        assert row[0] == hashlib.sha256(token.encode()).hexdigest()
        assert row[1] == "person@example.com"
        assert row[3]-row[2] == waitlist.RETENTION_SECONDS
    assert token.encode() not in path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert client.post("/api/waitlist/remove", json={"token": "x"*43}).status_code == 200
    assert len(waitlist.export()) == 1
    for _ in range(2):
        assert client.post("/api/waitlist/remove", json={"token": token}).json() == {"ok": True}
    assert waitlist.export() == []


def test_repeat_submissions_keep_their_own_working_removal_links(web):
    client, _ = web
    first, second = [client.post("/api/waitlist", json={"email": "person@example.com", "consent": True})
                     .json()["removal_token"] for _ in range(2)]
    assert first != second and len(waitlist.export()) == 1
    client.post("/api/waitlist/remove", json={"token": first})
    assert len(waitlist.export()) == 1
    client.post("/api/waitlist/remove", json={"token": second})
    assert waitlist.export() == []


def test_expired_signups_are_purged_and_excluded_from_exports(web, monkeypatch):
    client, path = web
    monkeypatch.setattr(waitlist.time, "time", lambda: 1000)
    token = client.post("/api/waitlist", json={"email": "person@example.com", "consent": True}).json()["removal_token"]
    monkeypatch.setattr(waitlist.time, "time", lambda: 1000+waitlist.RETENTION_SECONDS)
    assert waitlist.export() == []
    assert waitlist.purge() == 0
    waitlist.remove(token, path=path)


def test_public_service_does_not_serve_operator_routes_or_subscriber_lists(web):
    client, path = web
    for route in ("/", "/privacy", "/remove", "/site.css", "/site.js", "/sample"):
        response = client.get(route)
        assert response.status_code == 200
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    for route in ("/api/stats", "/api/account", "/api/waitlist", "/docs", "/openapi.json"):
        assert client.get(route).status_code in (404, 405)
    assert client.post("/api/engine/start", json={}).status_code == 404
    assert client.get("/", headers={"host": "attacker.example"}).status_code == 400
    assert not path.exists()


def test_body_limits_json_only_and_per_client_rate_limit(web):
    client, path = web
    assert client.post("/api/waitlist", data={"email": "x@example.com", "consent": "true"}).status_code == 415
    assert client.post("/api/waitlist", content=b"x"*4097, headers={"Content-Type": "application/json"}).status_code == 413
    for _ in range(8):
        assert client.post("/api/waitlist", json={"email": "bad", "consent": True}).status_code == 422
    response = client.post("/api/waitlist", json={"email": "x@example.com", "consent": True})
    assert response.status_code == 429 and response.headers["retry-after"] == "60"
    assert not path.exists()


@pytest.mark.parametrize("email", [".x@example.com", "x..y@example.com", "x@-example.com",
                                    "x@example-.com", "x@localhost", "x\n@example.com"])
def test_invalid_email_shapes_are_refused(email):
    with pytest.raises(ValueError):
        waitlist.normalize_email(email)


def test_concurrent_signups_do_not_lose_rows_or_invalidate_removal_tokens(tmp_path):
    path = tmp_path/"waitlist.db"
    with ThreadPoolExecutor(max_workers=4) as pool:
        tokens = list(pool.map(lambda i: waitlist.subscribe(f"person{i}@example.com", True, path=path), range(12)))
    assert len(waitlist.export(path=path)) == 12
    for token in tokens:
        waitlist.remove(token, path=path)
    assert waitlist.export(path=path) == []


def test_json_export_is_operator_local_and_contains_no_removal_tokens(web, capsys, monkeypatch):
    client, _ = web
    token = client.post("/api/waitlist", json={"email": "person@example.com", "consent": True}).json()["removal_token"]
    monkeypatch.setattr("sys.argv", ["waitlist", "export"])
    waitlist.main()
    text = capsys.readouterr().out
    assert json.loads(text)[0]["email"] == "person@example.com"
    assert token not in text and "token_hash" not in text
