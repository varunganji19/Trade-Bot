"""Local opt-in waitlist. Separate from the trading journal; never sends mail."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

CONSENT_VERSION = "2026-10-20"
RETENTION_SECONDS = 30 * 24 * 60 * 60
_LOCAL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$")
_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def normalize_email(value: str) -> str:
    value = value.strip()
    parts = value.rsplit("@", 1)
    if len(value) > 254 or len(parts) != 2:
        raise ValueError("enter a valid email address")
    local, domain = parts
    labels = domain.split(".")
    if (not 1 <= len(local) <= 64 or not _LOCAL.fullmatch(local)
            or local.startswith(".") or local.endswith(".") or ".." in local
            or len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels)
            or not labels[-1].isalpha() or len(labels[-1]) < 2):
        raise ValueError("enter a valid email address")
    return local + "@" + domain.lower()


def store_path() -> Path:
    from config import db_dir
    return Path(os.environ.get("ALGO_WAITLIST_DB") or Path(db_dir()) / "waitlist.db")


@contextlib.contextmanager
def _connect(path: Path | None = None):
    path = path or store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path, timeout=5)) as conn:
        os.chmod(path, 0o600)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE IF NOT EXISTS signups (token_hash TEXT PRIMARY KEY, "
                     "email TEXT NOT NULL, created_at INTEGER NOT NULL, "
                     "expires_at INTEGER NOT NULL, consent_version TEXT NOT NULL)")
        with conn:
            yield conn


def subscribe(email: str, consent: bool, *, path: Path | None = None) -> str:
    if consent is not True:
        raise ValueError("consent is required")
    email = normalize_email(email)
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with _connect(path) as conn:
        conn.execute("DELETE FROM signups WHERE expires_at<=?", (now,))
        conn.execute("INSERT INTO signups VALUES (?,?,?,?,?)",
                     (hashlib.sha256(token.encode()).hexdigest(), email, now,
                      now + RETENTION_SECONDS, CONSENT_VERSION))
    return token


def remove(token: str, *, path: Path | None = None) -> None:
    with _connect(path) as conn:
        conn.execute("DELETE FROM signups WHERE token_hash=? OR expires_at<=?",
                     (hashlib.sha256(token.encode()).hexdigest(), int(time.time())))


def purge(*, path: Path | None = None) -> int:
    with _connect(path) as conn:
        return conn.execute("DELETE FROM signups WHERE expires_at<=?", (int(time.time()),)).rowcount


def export(*, path: Path | None = None) -> list[dict]:
    with _connect(path) as conn:
        conn.execute("DELETE FROM signups WHERE expires_at<=?", (int(time.time()),))
        return [dict(row) for row in conn.execute(
            "SELECT email, MIN(created_at) AS created_at, MAX(expires_at) AS expires_at, "
            "consent_version FROM signups GROUP BY email COLLATE NOCASE ORDER BY email")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stats", "export", "purge"))
    args = parser.parse_args()
    if args.action == "purge":
        print(json.dumps({"expired_signups_removed": purge()}))
    else:
        rows = export()
        print(json.dumps(rows if args.action == "export" else {"unique_emails": len(rows)}, indent=2))


if __name__ == "__main__":
    main()
