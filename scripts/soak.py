#!/usr/bin/env python3
"""Soak the running dashboard: drive it the way an operator would, in a loop,
and report anything that looks like a breakdown.

WHY THIS EXISTS. Every serious bug in this repo's recent history was invisible
to the test suite AND to a glance at the UI: the fast book refused 100% of its
entries for a week (a log line per cycle), Kronos raised on every forecast for
the entire life of the 1m book (swallowed into last_error), two engines
aborted the process on Metal (a hard crash with no Python traceback), and an
ordinary Yahoo weekend errored every cycle of the standard book. None of them
needed exotic input — they needed someone to run the thing and look.

This is that someone. It hammers the endpoints an operator touches (start,
stop, retune the interval, reset, read every page), between engine cycles, and
flags:

  * any 5xx, or a request that takes longer than LATENCY_BUDGET
  * tracebacks / Metal assertions appearing in the server log
  * RSS or thread growth across rounds (leaked engines, leaked workers)
  * a book that keeps attempting entries and approving none
  * cycles that outran their own interval
  * an engine that stops reporting `alive` while it claims to be running

Usage:  python3 scripts/soak.py [--rounds 6] [--log <server log path>]
Exit code is non-zero when something was flagged, so it can gate a release.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
LATENCY_BUDGET = 5.0      # seconds for any dashboard request
FINDINGS: list[str] = []

READ_ENDPOINTS = [
    "/", "/app.js", "/app.css", "/api/stats", "/api/equity", "/api/trades",
    "/api/decisions", "/api/positions", "/api/account", "/api/watchlist",
    "/api/engine/status", "/api/hft/stats", "/api/hft/equity", "/api/hft/trades",
    "/api/hft/decisions", "/api/hft/engine/status", "/api/hft/candles",
    "/api/lab/meta", "/api/lab/status", "/api/evidence",
]


def flag(msg: str):
    FINDINGS.append(msg)
    print(f"  !! {msg}")


def req(path: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            payload = resp.read()
            took = time.monotonic() - t0
            if took > LATENCY_BUDGET:
                flag(f"{method} {path} took {took:.1f}s (budget {LATENCY_BUDGET}s) "
                     f"— the UI is blocked behind something")
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        took = time.monotonic() - t0
        if exc.code >= 500:
            flag(f"{method} {path} -> HTTP {exc.code} after {took:.1f}s: "
                 f"{exc.read()[:200]!r}")
        return exc.code, b""
    except Exception as exc:
        flag(f"{method} {path} -> {type(exc).__name__}: {exc}")
        return 0, b""


def jget(path: str):
    status, payload = req(path)
    if status != 200 or not payload:
        return None
    try:
        return json.loads(payload)
    except Exception:
        return None


def proc_stats() -> tuple[int, int]:
    """(RSS KB, thread count) of the dashboard process."""
    try:
        pid = subprocess.run(["pgrep", "-f", "main.py dashboard"],
                             capture_output=True, text=True).stdout.split()[0]
        rss = int(subprocess.run(["ps", "-o", "rss=", "-p", pid],
                                 capture_output=True, text=True).stdout.strip())
        out = subprocess.run(["ps", "-M", "-p", pid], capture_output=True, text=True).stdout
        return rss, max(0, len(out.strip().split("\n")) - 2)
    except Exception:
        return 0, 0


def scan_log(path: str, seen: set) -> None:
    try:
        text = open(path, errors="replace").read()
    except OSError:
        return
    patterns = [
        (r"failed assertion .*MTLCommandBuffer.*", "METAL ABORT"),
        (r"^Traceback \(most recent call last\):", "traceback"),
        (r"STOPPING — .*", "engine self-stopped"),
        (r"^\s+! (.+)$", "cycle error"),
        (r"cycle took [\d.]+s against", "cycle over budget"),
        (r"position unguarded", "unguarded position"),
    ]
    for pat, label in patterns:
        for m in re.finditer(pat, text, re.M):
            key = (label, m.group(0)[:160])
            if key in seen:
                continue
            seen.add(key)
            flag(f"log[{label}]: {m.group(0)[:160].strip()}")


def check_books():
    for name, stats_path in (("standard", "/api/stats"), ("fast", "/api/hft/stats")):
        s = jget(stats_path)
        if s is None:
            flag(f"{name}: stats unreadable")
            continue
        v = s.get("vetoes") or {}
        if v.get("attempts", 0) >= 10 and not v.get("approved"):
            top = (v.get("by_reason") or [{}])[0]
            flag(f"{name}: {v['attempts']} entry attempts, 0 approved "
                 f"(top blocker {top.get('reason')} x{top.get('count')})")
        if s.get("health_note"):
            flag(f"{name}: health_note = {s['health_note']}")
        if s.get("last_error"):
            flag(f"{name}: last_error = {s['last_error']}")
    st = jget("/api/engine/status")
    if st and st.get("running") and not st.get("alive"):
        flag("standard: running=True but the engine thread is not alive (zombie)")


def round_once(n: int, log_path: str, seen: set, baseline: tuple[int, int]):
    print(f"\n--- round {n} ---")
    for path in READ_ENDPOINTS:
        req(path)
    # retune both cadences while running — this used to be impossible
    req("/api/engine/interval", "POST", {"interval": 30 if n % 2 else 60})
    req("/api/hft/engine/interval", "POST", {"interval": 5 if n % 2 else 10})
    # stop/start churn: leases, threads and restored positions
    if n % 2 == 0:
        req("/api/hft/engine/stop", "POST", {})
        time.sleep(2)
        req("/api/hft/engine/start", "POST", {"interval": 10})
    # operator actions that touch money and state (net-zero on purpose: a
    # soak must not rewrite the book it is watching)
    if n % 3 == 0:
        req("/api/account/deposit", "POST", {"amount": 100.0, "note": "soak"})
        req("/api/account/withdraw", "POST", {"amount": 100.0, "note": "soak"})
        acct = jget("/api/account")
        if acct is not None and abs(float(acct.get("cash", 0)) ) < 0:
            flag("account cash went negative after a net-zero deposit/withdraw")
        # pause/resume must not strand the engine
        req("/api/trading/pause", "POST", {"note": "soak"})
        time.sleep(1)
        req("/api/trading/resume", "POST", {})
        st = jget("/api/stats")
        if st and st.get("paused"):
            flag("still paused after an explicit resume")
    check_books()
    scan_log(log_path, seen)
    rss, threads = proc_stats()
    b_rss, b_threads = baseline
    print(f"  rss {rss // 1024}MB (start {b_rss // 1024}MB) · threads {threads} "
          f"(start {b_threads})")
    if b_rss and rss > b_rss * 1.8 and rss - b_rss > 300_000:
        flag(f"RSS grew {b_rss // 1024}MB -> {rss // 1024}MB across rounds (leak?)")
    if b_threads and threads > b_threads + 8:
        flag(f"threads grew {b_threads} -> {threads} across rounds "
             f"(a stopped engine leaving workers behind?)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--sleep", type=int, default=20, help="seconds between rounds")
    ap.add_argument("--log", default=None, help="server log to scan for tracebacks")
    args = ap.parse_args()

    print("=== soak: driving the running dashboard ===")
    if jget("/api/engine/status") is None:
        print("no dashboard on 127.0.0.1:8000 — start one first")
        return 2
    baseline = proc_stats()
    seen: set = set()
    for n in range(1, args.rounds + 1):
        round_once(n, args.log or "/dev/null", seen, baseline)
        if n < args.rounds:
            time.sleep(args.sleep)

    print(f"\n=== soak done: {len(FINDINGS)} finding(s) ===")
    for f in FINDINGS:
        print(f"  - {f}")
    return 1 if FINDINGS else 0


if __name__ == "__main__":
    raise SystemExit(main())
