"""Engine lifecycle for both books: build, start, stop and auto-resume the
paper engines that the dashboard controls.

The engine handles, threads, intervals and locks live in bot.dashboard, and
everything here reads and writes them there (`core.<name>`), so one copy of
the state exists and the book-ownership tests that patch it exercise these
exact code paths. Each book persists its desired state to a small JSON file
next to the journal so a dashboard restart resumes a running book and never
resurrects a stopped one."""
from __future__ import annotations

import json
import os
import threading
import time
import traceback

from bot import dashboard as core
from bot.engine import TradingEngine
from bot.journal import BookOwnedError
from config import CONFIG


def _engine_state_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(CONFIG.db_path)),
                        "engine_state.json")


def _write_state_file(path: str, running: bool, interval: int) -> bool:
    """Persist a book's desired engine state so a dashboard restart can
    auto-resume it (a stop must win over a stale 'running' file). Returns
    False (and logs loudly) when the write fails so endpoints can surface it
    instead of silently losing auto-resume."""
    try:
        # atomic: a torn state file would silently disable auto-resume
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"desired": "running" if running else "stopped",
                       "interval": interval}, f)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        print(f"[dashboard] FAILED to persist {os.path.basename(path)} (desired="
              f"{'running' if running else 'stopped'}): {exc}")
        traceback.print_exc()
        return False


def _read_resume_interval(path: str, default: int, floor: int) -> int | None:
    """The interval to auto-resume a book with, or None when it must stay
    stopped: auto-resume disabled, the last session stopped it, or the state
    file is missing/unreadable. Skipped under pytest: tests swap
    CONFIG.db_path to temp dirs, but the real state file may say 'running'
    and must never spawn a live engine there."""
    if "PYTEST_CURRENT_TEST" in os.environ:
        return None
    if os.environ.get("ALGO_NO_AUTO_RESUME", "") not in ("", "0", "false"):
        return None
    try:
        with open(path) as f:
            state = json.load(f)
        if not isinstance(state, dict) or state.get("desired") != "running":
            return None
        return max(floor, min(3600, int(state.get("interval", default))))
    except (OSError, ValueError, TypeError, OverflowError):
        return None


def _state_warning(result: dict, ok: bool, what: str) -> dict:
    if not ok:
        result["state_warning"] = (f"{what} but desired-state persist failed — "
                                   f"auto-resume may be stale")
    return result


def _write_engine_state(running: bool, interval: int) -> bool:
    return _write_state_file(_engine_state_path(), running, interval)


def _release_book(eng, mode: str) -> None:
    """Retire an engine: stop its background workers and drop its
    cross-process lease. Every call site is discarding the engine, and a
    stopped book must not leave a Kronos worker forecasting into its ledger
    (a stop/start used to leave one running per start)."""
    try:
        eng.shutdown()
    except Exception:
        pass
    token, eng.book_token = eng.book_token, None
    if token is None:
        return
    try:
        core.journal.release_book(mode, token)
    except Exception:
        pass   # the lease expires on its own (see Journal._lease_is_live)


def _spawn_engine(interval: int) -> dict:
    """Build + start the engine thread (shared by the API endpoint and the
    startup auto-resume). Returns the API response dict."""
    # check-and-set under lock FIRST: a double-POST used to build two engines
    # (torch/Kronos probe each) before discovering the race under the lock.
    with core._engine_lock:
        if core._engine is not None:
            return {"status": "already_running", "cycles": core._engine.cycles}
        if core._engine_starting:
            return {"status": "starting", "cycles": 0}
        if core._engine_thread is not None and core._engine_thread.is_alive():
            return {"status": "stopping", "cycles": 0}
        core._engine_starting = True
    # build OUTSIDE _engine_lock: TradingEngine.__init__ probes the Kronos stack
    # (imports, no weight load — that happens lazily in the first engine cycle)
    # and holding the lock froze every stats/status poll
    try:
        eng = TradingEngine(mode="paper", quiet=False, journal=core.journal)
        # Take the book's cross-process lease BEFORE publishing the engine: a
        # standalone `main.py run` in another process owns the same account,
        # and two engines on one book fork it (see Journal.claim_book).
        eng.book_token = core.journal.claim_book("paper")
    except BookOwnedError as exc:
        with core._engine_lock:
            core._engine_starting = False
        return {"status": "owned", "cycles": 0, "detail": str(exc)}
    except Exception:
        with core._engine_lock:
            core._engine_starting = False
        raise
    with core._engine_lock:
        if core._engine is not None:
            core._engine_starting = False
            _release_book(eng, "paper")
            return {"status": "already_running", "cycles": core._engine.cycles}
        # a previous thread may still be finishing its last cycle (stop only
        # clears the global); two engines writing one journal fork the account
        if core._engine_thread is not None and core._engine_thread.is_alive():
            core._engine_starting = False
            _release_book(eng, "paper")
            return {"status": "stopping", "cycles": 0}
        core._engine = eng
        core._engine_interval = interval

    def _loop(eng_ref, interval):
        try:
            _engine_cycles(eng_ref, interval)
        finally:
            _release_book(eng_ref, "paper")

    def _engine_cycles(eng_ref, interval):
        while _get_engine() is eng_ref:
            cycle_t0 = time.monotonic()
            try:
                eng_ref.run_cycle()
            except Exception as exc:
                # run_cycle already guards its own body, so reaching here means
                # the engine itself is broken: report it, clear the global so
                # the UI shows a stopped engine (never a green zombie), and stop
                eng_ref.last_error = f"{type(exc).__name__}: {exc}"
                core._last_engine_error = eng_ref.last_error
                traceback.print_exc()
                eng_ref.cycles += 1        # count the failed cycle so the UI moves
                with core._engine_lock:
                    if core._engine is eng_ref:
                        core._engine = None
                break
            # sleep the REMAINDER of the interval from cycle START (a 2-minute
            # Kronos cycle at interval=60 used to land one decision burst every
            # ~2.5 min), and wake the SECOND the identity check flips so a stop
            # is near-instant instead of stranding the UI for up to interval-300s.
            # The interval is re-read from the module global EVERY cycle so
            # /api/engine/interval can retune a RUNNING engine (the captured
            # argument made the cadence unchangeable without a stop/start).
            remaining = max(0.0, core._engine_interval - (time.monotonic() - cycle_t0))
            deadline = time.monotonic() + remaining
            while time.monotonic() < deadline:
                if _get_engine() is not eng_ref:
                    return
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            if _get_engine() is not eng_ref:
                break

    with core._engine_lock:
        try:
            core._engine_thread = threading.Thread(target=_loop, args=(eng, interval), daemon=True)
            core._engine_thread.start()
        except Exception:
            core._engine = None
            _release_book(eng, "paper")
            raise
        finally:
            core._engine_starting = False
    return {"status": "started", "interval": interval}


def _get_engine() -> TradingEngine | None:
    with core._engine_lock:
        return core._engine


def _hft_state_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(CONFIG.db_path)),
                        "hft_engine_state.json")


def _write_hft_state(running: bool, interval: int) -> bool:
    return _write_state_file(_hft_state_path(), running, interval)


def _spawn_hft_engine(interval: int) -> dict:
    """Build + start the HFT engine thread (mirrors _spawn_engine)."""
    from bot.hft import build_hft_engine
    with core._hft_lock:
        if core._hft_engine is not None:
            return {"status": "already_running", "cycles": core._hft_engine.cycles}
        if core._hft_starting:
            return {"status": "starting", "cycles": 0}
        if core._hft_thread is not None and core._hft_thread.is_alive():
            return {"status": "stopping", "cycles": 0}
        core._hft_starting = True
    try:
        eng = build_hft_engine(journal=core.journal, quiet=False)
        eng.book_token = core.journal.claim_book("hft")   # see _spawn_engine
    except BookOwnedError as exc:
        with core._hft_lock:
            core._hft_starting = False
        return {"status": "owned", "cycles": 0, "detail": str(exc)}
    except Exception:
        with core._hft_lock:
            core._hft_starting = False
        raise
    with core._hft_lock:
        core._hft_starting = False
        if core._hft_engine is not None:
            _release_book(eng, "hft")
            return {"status": "already_running", "cycles": core._hft_engine.cycles}
        if core._hft_thread is not None and core._hft_thread.is_alive():
            _release_book(eng, "hft")
            return {"status": "stopping", "cycles": 0}
        core._hft_engine = eng
        core._hft_interval = interval

    def _hft_loop(eng_ref, interval):
        try:
            _hft_cycles(eng_ref, interval)
        finally:
            _release_book(eng_ref, "hft")

    def _hft_cycles(eng_ref, interval):
        while _get_hft_engine() is eng_ref:
            cycle_t0 = time.monotonic()
            try:
                eng_ref.run_cycle()
            except Exception as exc:
                eng_ref.last_error = f"{type(exc).__name__}: {exc}"
                core._last_hft_error = eng_ref.last_error
                traceback.print_exc()
                eng_ref.cycles += 1
                with core._hft_lock:
                    if core._hft_engine is eng_ref:
                        core._hft_engine = None
                break
            # re-read each cycle: /api/hft/engine/interval retunes a RUNNING
            # book without a stop/start (see the standard loop)
            remaining = max(0.0, core._hft_interval - (time.monotonic() - cycle_t0))
            deadline = time.monotonic() + remaining
            while time.monotonic() < deadline:
                if _get_hft_engine() is not eng_ref:
                    return
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            if _get_hft_engine() is not eng_ref:
                break

    with core._hft_lock:
        try:
            core._hft_thread = threading.Thread(target=_hft_loop, args=(eng, interval), daemon=True)
            core._hft_thread.start()
        except Exception:
            # the loop's finally never runs if the thread never starts, so the
            # lease would be held by this live pid for the process's lifetime,
            # making the book unstartable and unresettable
            core._hft_engine = None
            _release_book(eng, "hft")
            raise
    return {"status": "started", "interval": interval}


def _get_hft_engine() -> TradingEngine | None:
    with core._hft_lock:
        return core._hft_engine


def _join_in_background(th: threading.Thread, done, interval: int):
    """Bounded join off the request path; persists stopped state on completion."""

    def _wait():
        th.join(timeout=300)
        if not th.is_alive():
            done(interval)

    t = threading.Thread(target=_wait, daemon=True)
    t.start()


def _auto_resume_engine():
    """Restart the engine when the last session left it running (the operator's
    'the bot trades autonomously' expectation survives a dashboard restart).
    A manual stop persists desired=stopped, so it always wins. Skipped under
    pytest: tests swap CONFIG.db_path to temp dirs, but the real state file
    may exist with desired=running and must never spawn a live engine there.
    ALGO_NO_AUTO_RESUME=1 disables the resume entirely (a rehearsal/demo
    machine that must NOT start trading on boot)."""
    interval = _read_resume_interval(_engine_state_path(),
                                     CONFIG.live_interval_seconds, floor=5)
    if interval is None:
        return
    result = _spawn_engine(interval)
    if result["status"] == "owned":
        # a standalone CLI engine already trades this book — resuming here
        # would give one account two owners
        print(f"[dashboard] engine NOT auto-resumed — {result['detail']}")
        return
    if result["status"] == "started":
        core._AUTO_RESUMED_AT_BOOT = True
        print(f"[dashboard] engine auto-resumed (interval {interval}s) — stop it "
              f"from the top bar, or set ALGO_NO_AUTO_RESUME=1 before boot")


def _auto_resume_hft_engine():
    """Same contract as the standard engine's auto-resume, for the HFT book:
    a stopped book stays stopped, a running one resumes with a toast."""
    interval = _read_resume_interval(_hft_state_path(),
                                     CONFIG.hft.live_interval_seconds, floor=1)
    if interval is None:
        return
    result = _spawn_hft_engine(interval)
    if result["status"] == "owned":
        print(f"[dashboard] fast book NOT auto-resumed — {result['detail']}")
        return
    if result["status"] == "started":
        core._HFT_AUTO_RESUMED_AT_BOOT = True
        print(f"[dashboard] fast book auto-resumed (interval {interval}s)")
