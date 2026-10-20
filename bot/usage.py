"""Opt-in local validation counters, without trade or contact metadata.

The stored client key is an HMAC of a random UUID. Counts identify consenting
browsers/installations, not verified people. Nothing is transmitted externally.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

RETENTION_DAYS = 90


def store_path() -> Path:
    from config import db_dir
    return Path(db_dir())/"usage.db"


def client_path() -> Path:
    from config import db_dir
    return Path(db_dir())/"usage-client.json"


def validate_client(value) -> str:
    try:
        client = uuid.UUID(str(value))
        if client.version != 4:
            raise ValueError("not a random UUID")
        return str(client)
    except (ValueError, TypeError, AttributeError):
        raise ValueError("usage client must be a random UUID v4")


@contextlib.contextmanager
def _connect():
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path, timeout=5)) as conn:
        os.chmod(path, 0o600)
        conn.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value BLOB NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS clients (client_hash TEXT PRIMARY KEY, "
                     "first_day INTEGER NOT NULL, last_day INTEGER NOT NULL, runs INTEGER NOT NULL)")
        with conn:
            conn.execute("INSERT OR IGNORE INTO settings VALUES ('hmac_key', ?)", (secrets.token_bytes(32),))
            yield conn


def _key(conn, client: str) -> str:
    secret = conn.execute("SELECT value FROM settings WHERE name='hmac_key'").fetchone()[0]
    return hmac.new(secret, client.encode(), hashlib.sha256).hexdigest()


def cli_client_id() -> str:
    """Create an installation ID only after an explicit --usage invocation."""
    path = client_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path)+".lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if path.exists():
            return validate_client(json.loads(path.read_text())["client_id"])
        client = str(uuid.uuid4())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as target:
            json.dump({"client_id": client}, target)
        return client


def record(client: str) -> None:
    client = validate_client(client)
    day = int(time.time() // 86400)
    with _connect() as conn:
        conn.execute("DELETE FROM clients WHERE last_day<=?", (day-RETENTION_DAYS,))
        conn.execute("INSERT INTO clients VALUES (?,?,?,1) ON CONFLICT(client_hash) "
                     "DO UPDATE SET last_day=excluded.last_day, runs=clients.runs+1",
                     (_key(conn, client), day, day))


def stats() -> dict:
    if not store_path().exists():
        return {"validations": 0, "consenting_clients": 0, "repeat_clients": 0}
    with _connect() as conn:
        cutoff = int(time.time() // 86400)-RETENTION_DAYS
        conn.execute("DELETE FROM clients WHERE last_day<=?", (cutoff,))
        row = conn.execute("SELECT COALESCE(SUM(runs),0), COUNT(*), "
                           "COALESCE(SUM(runs>=2),0) FROM clients").fetchone()
    return dict(zip(("validations", "consenting_clients", "repeat_clients"), row))


def forget(client: str) -> None:
    client = validate_client(client)
    if store_path().exists():
        with _connect() as conn:
            conn.execute("DELETE FROM clients WHERE client_hash=?", (_key(conn, client),))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stats", "forget"))
    args = parser.parse_args()
    if args.action == "stats":
        print(json.dumps(stats(), indent=2))
    else:
        path = client_path()
        if path.exists():
            forget(validate_client(json.loads(path.read_text())["client_id"]))
            path.unlink()
        print("Local CLI usage counters and installation ID removed.")


if __name__ == "__main__":
    main()
