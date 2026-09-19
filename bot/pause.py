"""
Manual "pause all trading" flag — the operator's halt button.

Semantics (must stay unambiguous to a non-technical reader): pausing blocks
NEW entries ONLY. Open positions remain fully managed — hard stops, targets,
strategy exits, marks, cooldowns and restart reconciliation all keep working.
Nothing is ever force-closed by this flag. It is completely independent of the
automatic daily kill switch (that one is equity-triggered and resets at the
next UTC day; this one is manual and stays until `resume`).

State lives in `trading_paused.json` next to the journal (CONFIG.db_path's
directory, derived at CALL time so tests that swap CONFIG.db_path to temp
dirs stay hermetic). Resuming writes {"paused": false} instead of deleting
the file: a visible record of the last state beats an absence that reads the
same as "never paused" and cannot be told apart from a lost file.
"""
from __future__ import annotations

import json
import os
import time

from config import CONFIG, utc_now


def _pause_path() -> str:
    # CALL-time derivation (not import time): the flag must follow the
    # ACTIVE journal — tests (and any BOT_DB_PATH override) swap
    # CONFIG.db_path after this module is first imported
    return os.path.join(os.path.dirname(CONFIG.db_path), "trading_paused.json")


def is_paused() -> tuple[bool, str | None]:
    """(paused, note). Missing file -> (False, None); a valid flag with
    "paused": true -> (True, note).

    A corrupt/unreadable flag is quarantined aside (".corrupt.<epoch>",
    the journal/watchlist pattern: keep the torn file for inspection, out of
    the read path) and treated as PAUSED. Rationale: a flag the operator
    cannot read must fail toward "not trading", never toward trading —
    silently resuming on a torn file would do the OPPOSITE of what the
    operator last asked for. Never raises.
    """
    path = _pause_path()
    try:
        if not os.path.exists(path):
            return False, None
        with open(path) as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict):
            raise ValueError("pause flag is not a JSON object")
        if bool(payload.get("paused")):
            return True, str(payload.get("note") or "")
        return False, None
    except Exception:
        try:
            os.replace(path, f"{path}.corrupt.{time.time():.6f}")
        except OSError:
            pass
        print("[pause] trading_paused.json unreadable — quarantined to "
              ".corrupt.* and treated as PAUSED (fail-safe direction)")
        return True, "corrupt pause flag quarantined — treated as PAUSED (fail-safe direction)"


def set_paused(paused: bool, note: str = "") -> bool:
    """Atomically write the flag (tmp + os.replace, save_watchlist pattern) so
    a crash mid-write can never leave a torn JSON that is_paused() would then
    quarantine into an unintended pause. Returns False on OSError, never
    raises; callers surface the failure loudly (a pause that failed to write
    must NEVER read back as if it had succeeded)."""
    path = _pause_path()
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump({"paused": bool(paused), "note": note, "ts": utc_now()}, fh, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.path.exists(tmp) and os.remove(tmp)
        except OSError:
            pass
        return False
