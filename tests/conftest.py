"""Hermetic test session ground.

The bot persists live state next to CONFIG.db_path (journal, watchlist,
market mode, pause flag, engine state, manifest) and under
CONFIG.data_cache_dir (parquet frames). Before this file existed the suite
silently inherited whatever the REAL data/ dir contained: a live dashboard
left in india mode (or a leaked TEST/USDT spec) changed CONFIG.watchlist for
every test in the session via the dashboard's import-time
apply_saved_watchlist(), and engine tests fetched real cached parquets.

This conftest runs BEFORE tests/test_bot.py imports anything: it points
CONFIG at a per-session tmp dir, then per-test restores the watchlist to the
shipped default (tests that install their own — india mode, CRUD —
monkeypatch CONFIG themselves and restore in their finally).

Deliberately process-local only: tests that swap CONFIG.db_path to their OWN
tmp dir keep working (every state path derives from CONFIG.db_path's dir at
CALL time — the pause/mode/state file pattern), and tests that overwrite
CONFIG.data_cache_dir keep their override.
"""
from __future__ import annotations

import os
import tempfile

import pytest

# --- the operator's own .env must not change the test result ---------------
# config.py loads .env at import, so a developer who sets DASHBOARD_TOKEN (as
# the security docs tell them to) silently installs the auth middleware into
# every TestClient in this suite — and two evidence/API tests started failing
# with 401 the moment a real .env existed. A test suite whose verdict depends
# on the developer's local secrets is not a test suite. Clear it BEFORE the
# first import of config/bot.*, where the middleware decision is made.
for _var in ("DASHBOARD_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(_var, None)
os.environ["ALGO_SKIP_DOTENV"] = "1"

# --- session scope: redirect ALL persistent state away from the real data/ --
# Must happen before the first import of bot.* / config: bot.dashboard runs
# apply_saved_watchlist() at import time, so CONFIG must already point at
# the sandbox when that line executes.
_TMP = tempfile.TemporaryDirectory(prefix="algo-tests-")

import config as config_mod            # noqa: E402  (path setup must precede)
from config import CONFIG, DEFAULT_WATCHLIST   # noqa: E402

_REAL_DB = CONFIG.db_path
_REAL_CACHE = CONFIG.data_cache_dir
CONFIG.db_path = f"{_TMP.name}/trading.db"
CONFIG.data_cache_dir = f"{_TMP.name}/cache"
config_mod.WATCHLIST_PATH = f"{_TMP.name}/watchlist.json"
# import-time apply_saved_watchlist() inside bot.dashboard now reads the
# sandbox (missing file -> defaults), never the operator's live watchlist.


@pytest.fixture(autouse=True)
def _hermetic_default_watchlist():
    """Per-test ground truth: CONFIG.watchlist is the SHIPPED default book.
    Tests may install their own universe (india-mode switch, CRUD, one-spec
    pins) — they own their restore; this fixture only guarantees every test
    STARTS from the same 9-spec crypto+forex default regardless of what an
    earlier test (or a leaked live data/watchlist.json) left behind."""
    CONFIG.watchlist[:] = list(DEFAULT_WATCHLIST)
    yield
    CONFIG.watchlist[:] = list(DEFAULT_WATCHLIST)


def pytest_sessionfinish(session, exitstatus):
    # tmp state dies with the process; the REAL data/ dir was never touched
    _TMP.cleanup()
