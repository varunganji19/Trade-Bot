"""
Trading engine — the autonomous loop.

Each cycle, for every market in the watchlist:
  1. Fetch the latest candles and compute indicators.
  2. Manage open positions first: hard stop/target (bar high/low), then the
     owning strategy's exit rules (donchian exit, RSI snapback, VWAP loss,
     time stop, breakeven trail).
  3. Otherwise ask the orchestrator for a decision; journal it (HOLDs included,
     with full reasoning); RiskManager has the final veto; approved entries are
     filled by the PaperBroker and journaled with strategy attribution.
  4. Record a mark-to-market equity point.

Positions survive restarts: open journal trades are restored into the broker.
"""
from __future__ import annotations

import sqlite3
import time
import threading
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone

import pandas as pd

from bot.broker import PaperBroker
from bot.data import MarketData
from bot.journal import BookOwnedError, Journal
from bot.llm import LLMClient
from bot.orchestrator import Orchestrator
from bot.pause import is_paused
from bot.positions import PositionManager
from bot.risk import RiskManager
from config import CONFIG, MarketSpec, infer_kind


class TradingEngine(PositionManager):
    # data-outage policy for a spec with an OPEN position (the position is
    # unguarded while its feed is down): warn at N consecutive failed fetches,
    # force-close at the last known good mark at N2
    FETCH_FAIL_WARN = 3
    FETCH_FAIL_CLOSE = 10
    # entry attempts with zero approvals before the health note calls it out
    VETO_ALERT_ATTEMPTS = 10

    def _build_market_data(self) -> MarketData:
        """Latency profile per book. The fast book polls 5m bars every ~10s,
        so its cache TTL follows its own cadence: a TTL longer than the poll
        would serve the same stale frame the new-bar gate is waiting on, and
        a much shorter one just refetches bars that cannot have changed. The
        standard book keeps the default timeframe-scaled TTL."""
        if self.mode == "hft":
            return MarketData(ttl_seconds=float(
                max(1, min(30, self.cfg.live_interval_seconds))))
        return MarketData()

    def __init__(self, cfg=None, mode: str = "paper", quiet: bool = False,
                 journal=None):
        self.cfg = cfg or CONFIG
        self.mode = mode
        self.quiet = quiet
        # one Journal per process is the caller's job to pass (the dashboard
        # shares its instance so engine + API writes serialize through the
        # journal's process lock); a private instance is created only for
        # standalone CLI runs
        self.journal = journal or Journal()
        self.broker = PaperBroker(starting_capital=self.cfg.paper_capital,
                                  costs=self.cfg.costs)
        # guards broker-state mutations (fills, marks, account patches) across
        # the engine thread and dashboard API threads — the dashboard's
        # deposit/withdraw and stats endpoints take it via lock_for_cycle
        self.cycle_lock = threading.RLock()
        self.last_error: str | None = None
        self.risk = RiskManager(self.cfg, state_db_path=self.journal.db_path, mode=self.mode)
        self.llm = LLMClient(self.cfg.llm)
        # the decision path is deterministic: no LLM, no news overlay (both
        # were removed on 2026-09-19 — see bot/orchestrator.py). self.llm
        # stays for the chatbot, which explains the book but never trades it.
        self.orchestrator = Orchestrator(cfg=self.cfg,
                                          book="fast" if mode == "hft" else "standard")
        self.market_data = self._build_market_data()
        self.cycles = 0
        # per-spec health state (see _note_fetch_fail / _refresh_health_note):
        # a held position whose feed keeps failing is UNGUARDED — visible and
        # bounded beats green-and-silent
        self._fetch_fails: dict[tuple[str, str], int] = {}
        self._last_good_price: dict[tuple[str, str], float] = {}
        self.health_note: str | None = None
        # VETO TELEMETRY — the fix for the failure mode that cost a week:
        # RiskManager.approve refused 100% of this book's entries ("tiny stop
        # (dust)") and the only trace was a log line per cycle. Nothing
        # counted them, so the dashboard showed a healthy engine with an
        # empty trade table. Now every refusal is counted by category, the
        # counts are served with the stats, and a book that keeps trying to
        # enter and never does says so in its health note.
        self.veto_counts: dict[str, int] = {}
        self.last_cycle_seconds: float = 0.0
        self.slow_cycles = 0           # cycles that outran their own interval
        self.entry_attempts = 0        # decisions that reached risk.approve
        self.entries_approved = 0
        # restart recovery work, keyed by (symbol, timeframe)
        self._replay_pending: set[tuple[str, str]] = set()     # bars missed while offline
        self._unguarded_pending: set[tuple[str, str]] = set()  # restored rows with no stop
        # resting maker-limit orders (HFT book), keyed by (symbol, timeframe):
        # {decision, qty, limit, side, waited, decision_bar_ts}. Un-journaled
        # by design — a pending order that dies with the process just expires.
        self._pending: dict[tuple[str, str], dict] = {}
        # last PROCESSED closed-bar timestamp per (symbol, timeframe) — the
        # HFT book's low-latency gate (see _build_market_data / _run_cycle)
        self._last_bar_ts: dict[tuple[str, str], str] = {}
        # start-of-cycle marked equity for entry sizing (pass 1 of
        # _run_cycle_locked); None until the first cycle runs or when no mark
        # exists — _approval_equity() falls back to the cash basis then
        self._cycle_equity: float | None = None
        # cross-process lease on this book, taken by own_book() when the
        # engine actually starts trading (construction alone claims nothing,
        # so backtests, the Lab and tests never contend for it)
        self.book_token: str | None = None
        self._restore_positions()
        self._refresh_health_note()

    def shutdown(self):
        """Release background workers. Nothing holds one today — the forecast
        model moved out of the live loop on 2026-09-19 — but every engine
        retirement funnels through here, so a future worker has one obvious
        place to be stopped."""
        return


    # ------------------------------------------------------------- main loop
    @contextmanager
    def own_book(self):
        """Hold this book's cross-process lease for the duration.

        Every process that trades a book takes it: the standalone CLI engine
        and the dashboard's engine thread alike. A second engine on the same
        book then fails loudly at startup instead of silently overwriting the
        first one's account, and the dashboard refuses deposits, withdrawals
        and resets while any process owns the book.
        """
        self.book_token = self.journal.claim_book(self.mode)
        try:
            yield self.book_token
        finally:
            token, self.book_token = self.book_token, None
            try:
                self.journal.release_book(self.mode, token)
            except sqlite3.Error:
                pass   # the lease expires on its own; never mask a real error

    def _heartbeat_book(self):
        """Refresh the lease before a cycle writes anything."""
        if self.book_token is None:
            return
        if not self.journal.heartbeat_book(self.mode, self.book_token):
            raise BookOwnedError(f"the {self.mode} book's lease was taken over — "
                                 f"this engine must stop before it writes again")

    def run_cycle(self) -> dict:
        """One full cycle. Holds `cycle_lock` for the duration: the dashboard's
        account deposit/withdraw and manual close take the same lock, so an
        API thread can never interleave with fills and overwrite cash deltas.
        TODO(cycle_lock): this intentionally holds the lock across per-spec
        network IO (fetches) so the whole cycle is atomic — shrinking it to a
        snapshot/commit shape is the future direction, but splitting fills
        from marks risks interleaved cash deltas today. close_manual already
        fetches OUTSIDE the lock (short-lock pattern); the dashboard's
        deposit/withdraw paths (bot/dashboard.py, out of this file's scope)
        still take the full lock and are the next candidates."""
        with self.cycle_lock:
            self._heartbeat_book()
            return self._run_cycle_locked()

    def _check_drift(self, summary: dict) -> None:
        """Compare this book's promoted strategies with their expected range
        (bot/drift.py) before they vote this cycle. A failure is reported
        with the cycle's errors and never stops the cycle."""
        try:
            from bot.drift import check
            demoted = check(self.orchestrator.book, self.journal.db_path)
        except Exception as exc:
            summary["errors"].append(f"drift check: {type(exc).__name__}: {exc}")
            return
        if demoted:
            summary["drift_demoted"] = demoted
            if not self.quiet:
                print(f"[engine] demoted for drift (live results below their expected "
                      f"range): {', '.join(demoted)}")

    def _run_cycle_locked(self) -> dict:
        summary = {"cycle": self.cycles + 1, "opened": [], "closed": [], "holds": 0, "errors": []}
        cycle_started = time.monotonic()
        price_map: dict[str, float] = {}
        # keyed by (symbol, timeframe): one symbol has several books and a
        # position must be marked with its OWN book's close, never a sibling
        # timeframe's frame that overwrote the symbol slot
        histories: dict[tuple[str, str], pd.DataFrame] = {}

        try:
            # manual pause flag: read at cycle start and mirror it into the
            # risk manager, where the entry veto lives. Position management
            # below is untouched by it: stops, targets, strategy exits and
            # marks keep running while paused; only new entries are blocked.
            # The start-of-cycle read can go stale while a long watchlist is
            # evaluated, so _process_market re-checks the flag before every
            # approve/fill (see _paused_now) — backtests construct their own
            # RiskManager and never touch the file, so they stay hermetic.
            paused, pause_note = is_paused()
            self.risk.paused = paused
            summary["paused"] = paused
            if pause_note:
                summary["paused_note"] = pause_note
            if paused and not self.quiet:
                print("[engine] manual pause active — new entries blocked "
                      "(open positions still managed)"
                      + (f" · {pause_note}" if pause_note else ""))
            self._check_drift(summary)

            # PASS 1 — fetch every book first, decide second. Entry sizing
            # needs START-of-cycle marked equity (approve must see unrealized,
            # not bare cash), and marks need every book's history — so all
            # fetches land before any _process_market call. `due` preserves
            # watchlist order, so management/entries still run spec by spec
            # exactly as before.
            # EVERY spec this cycle must touch: the watchlist (for entries)
            # PLUS any spec that HOLDS a position but is no longer on it.
            # Marks already followed held positions; MANAGEMENT did not, so
            # editing the watchlist (or, as on 2026-09-19, moving the whole
            # book from 1m to 5m) left an open position with no stop checks,
            # no exits and no time stop — unmanaged, not just unwatched.
            cycle_specs = list(self.cfg.watchlist)
            watched = {(sp.symbol, sp.timeframe) for sp in cycle_specs}
            for pos in self.broker.positions_snapshot():
                if (pos.symbol, pos.timeframe) in watched:
                    continue
                orphan = self._spec_for(pos.symbol)
                if orphan is None or orphan.timeframe != pos.timeframe:
                    orphan = MarketSpec(infer_kind(pos.symbol), pos.symbol,
                                        pos.timeframe)
                cycle_specs.append(orphan)
                watched.add((pos.symbol, pos.timeframe))
                if not self.quiet:
                    print(f"[engine] managing off-watchlist position "
                          f"{pos.symbol} {pos.timeframe} (exits only)")

            due: list[tuple] = []
            for spec in cycle_specs:
                key = (spec.symbol, spec.timeframe)
                try:
                    df = self.market_data.latest(spec)
                except Exception as exc:
                    df = None
                    summary["errors"].append(f"{spec.symbol}: {type(exc).__name__}: {exc}")
                    if not self.quiet:
                        traceback.print_exc()
                if df is None or not len(df):
                    # one dead market never aborts the cycle — but a held
                    # position behind a dead feed must not stay silent
                    self._note_fetch_fail(spec, summary)
                    continue
                self._fetch_fails[key] = 0
                self._last_good_price[key] = float(df["close"].iloc[-1])
                histories[key] = df
                # HFT low-latency gate: with a 2s poll against 1m bars, most
                # cycles see the SAME closed bar. Processing it again would
                # journal duplicate HOLD decisions and re-run management for
                # nothing — act only when a NEW closed bar prints. (The
                # standard book keeps its evaluate-every-cycle behavior.)
                bar_ts = str(df.index[-1])
                if self.mode == "hft":
                    if (self._last_bar_ts.get(key) == bar_ts
                            and key not in self._replay_pending):
                        continue
                due.append((spec, df))

            # marks come from HELD positions (any book — including a spec no
            # longer on the watchlist), never from the watchlist itself. This
            # ONE result feeds both entry sizing (via _cycle_equity) and the
            # cycle-end equity point (topped up below for cycle-opened
            # positions) — a single marks pipeline, no ad-hoc re-marking.
            price_map = self._mark_held_positions(histories)
            if price_map or not self.broker.positions:
                self._cycle_equity = self.broker.equity(price_map)
                # Restore happened in __init__; roll the UTC day and enforce
                # marked losses BEFORE approving this cycle's new entries.
                self.risk.note_equity(self._cycle_equity)
            else:
                # no mark for any open position: no marked basis exists, so
                # approvals fall back to the cash basis (see _approval_equity)
                self._cycle_equity = None

            # PASS 2 — manage + decide in watchlist order.
            for spec, df in due:
                self._process_market(spec, summary, df)
                if self.mode == "hft":
                    # A failed journal write must leave this bar eligible for
                    # the next poll, including stop updates and exits.
                    self._last_bar_ts[(spec.symbol, spec.timeframe)] = str(df.index[-1])

            # progressed = this cycle actually saw a new bar. The HFT book
            # polls every 2s against 1m bars, so most cycles are idle re-polls:
            # re-running the allocator and re-writing an identical equity point
            # every 2s is spam (duplicate curve points, wasted IC/quant cycles).
            # The standard book always progresses (evaluate-every-cycle).
            progressed = (self.mode != "hft") or bool(due)

            # portfolio allocation: divide the book's risk budget across symbols
            # (skfolio inverse-vol/HRP over the watchlist's realized returns).
            # One return series per symbol — the first spec's timeframe in
            # watchlist order, so every symbol's vol is measured on one scale.
            # Skipped on HFT idle cycles (no new bars -> weights unchanged).
            if self.cfg.portfolio.enabled and progressed:
                try:
                    from bot.allocator import allocation_weights
                    per_symbol: dict[str, pd.DataFrame] = {}
                    for spec in self.cfg.watchlist:
                        if spec.symbol not in per_symbol:
                            df = histories.get((spec.symbol, spec.timeframe))
                            if df is not None:
                                per_symbol[spec.symbol] = df
                    weights = allocation_weights(self.cfg.watchlist, per_symbol)
                    if weights:
                        self.risk.set_allocation(weights)
                except Exception as exc:
                    if not self.quiet:
                        print(f"[engine] allocator unavailable, equal risk split: "
                              f"{type(exc).__name__}: {exc}")

            # top-up: positions OPENED this cycle are not in the pass-1 map
            # (they did not exist when it was built). Their spec was fetched in
            # pass 1, so _last_good_price is fresh — no refetch, entry price as
            # the last resort (never 0.0). Gated on the spec being fetched THIS
            # cycle: a pre-existing position behind a dead feed must stay
            # unmarked so the no-marks skip below still fires (an entry-price
            # top-up here would silently re-mark dead books and journal a
            # flat-but-false equity point).
            for pos in self.broker.positions_snapshot():
                if pos.symbol not in price_map and (pos.symbol, pos.timeframe) in histories:
                    price_map[pos.symbol] = self._last_good_price.get(
                        (pos.symbol, pos.timeframe), pos.entry_price)
            if price_map or not self.broker.positions:
                equity = self.broker.equity(price_map)
                # the kill-switch/day rollover runs on the WALL clock: the
                # journal write may be skipped on HFT idle cycles below, but
                # the risk clock must never stall (a stall would pin yesterday's
                # halted state across UTC midnight).
                self.risk.note_equity(equity)
                if progressed:
                    self.journal.add_equity(equity, self.broker.cash, mode=self.mode,
                                            owner_token=self.book_token)
                # else: HFT idle cycle — the curve point would be a near-exact
                # duplicate of the last one; the live number is still reported
                # in the summary for the status poll.
                summary["equity"] = round(equity, 2)
                summary["cash"] = round(self.broker.cash, 2)
            else:
                # every held symbol failed to fetch: carrying the last equity
                # point forward is a lie too — skip the write entirely and say
                # so. The risk clock still must roll across a UTC-day boundary
                # (otherwise a halted kill switch pins forever mid-outage) —
                # but ONLY then: feeding the cash basis mid-day would fake a
                # loss (deployed capital reads as missing) and could spuriously
                # trip the switch. A rollover anchors the new day at cash, the
                # only number known; the next marked cycle corrects course.
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if self.risk.daily_day != today:
                    self.risk.note_equity(self.broker.equity({}))
                summary["errors"].append("equity point skipped: no marks available "
                                          "for any open position")
        except BookOwnedError:
            # losing the book mid-cycle is not a degraded cycle: swallowing it
            # here would keep trading a book another process owns until the
            # next heartbeat noticed, a whole interval later
            raise
        except Exception as exc:
            # the cycle tail must never kill the standalone loop (run_forever)
            # or be swallowed silently by the dashboard's loop
            self.last_error = f"{type(exc).__name__}: {exc}"
            summary["errors"].append(f"cycle: {self.last_error}")
            if not self.quiet:
                traceback.print_exc()
        finally:
            # CYCLE BUDGET: a cycle that outruns its own interval is a
            # latency bug that only shows up in production — the inline
            # forecast model made a 2s cycle take minutes and nothing in the
            # process said so until the machine stopped responding. Measure
            # it here (both loops call run_cycle) and let the health note
            # carry it; the sleep maths already absorbs a slow cycle.
            self.last_cycle_seconds = round(time.monotonic() - cycle_started, 3)
            summary["seconds"] = self.last_cycle_seconds
            budget = float(self.cfg.live_interval_seconds or 0)
            self.slow_cycles += int(bool(budget and self.last_cycle_seconds > budget))
            self.cycles += 1
            self._refresh_health_note()
            if not self.quiet:
                self._print_summary(summary)
        return summary

    def _note_fetch_fail(self, spec: MarketSpec, summary: dict):
        """Count consecutive failed fetches per spec. With an OPEN position the
        spec is unguarded for as long as the outage lasts: warn early (status
        surface), force-close at the last known good mark once the outage is
        clearly persistent — cutting a loser on stale data beats holding it
        blind forever."""
        key = (spec.symbol, spec.timeframe)
        n = self._fetch_fails.get(key, 0) + 1
        self._fetch_fails[key] = n
        if not self.broker.positions.get(self.broker.position_key(*key)):
            return
        if n == self.FETCH_FAIL_WARN:
            summary["errors"].append(f"{spec.symbol} {spec.timeframe}: {n} consecutive "
                                     f"fetch failures — open position is unguarded")
        elif n > self.FETCH_FAIL_WARN and n % 5 == 0:
            summary["errors"].append(f"{spec.symbol} {spec.timeframe}: still unfetchable "
                                     f"({n} cycles) — position unguarded")
        if n >= self.FETCH_FAIL_CLOSE:
            price = self._last_good_price.get(key)
            if price is None:
                # opened during the outage: there IS no last good mark. Keep
                # the counter growing so the close is retried EVERY cycle —
                # resetting it would leave the position unguarded for another
                # FETCH_FAIL_CLOSE failures before the next attempt.
                summary["errors"].append(f"{spec.symbol} {spec.timeframe}: no last-good "
                                         f"mark — force-close deferred, retrying each cycle")
                return
            try:
                self._close(spec, float(price), "data outage", summary)
                self._fetch_fails[key] = 0   # only a successful close clears the count
            except Exception as exc:
                summary["errors"].append(f"{spec.symbol} {spec.timeframe}: data-outage "
                                         f"close failed ({type(exc).__name__}: {exc}) — "
                                         f"retrying next cycle")

    def _refresh_health_note(self):
        """/api/engine/status reads `health_note` — the non-fatal companion to
        last_error (the dashboard tears the engine down on last_error, so
        degraded-but-alive conditions must NOT land there)."""
        notes = []
        if self.risk.persistence_error:
            notes.append(self.risk.persistence_error + " — new entries blocked; exits remain enabled")
        for key, n in self._fetch_fails.items():
            if n < self.FETCH_FAIL_WARN:
                continue
            if self.broker.positions.get(self.broker.position_key(*key)):
                notes.append(f"{key[0]} {key[1]}: {n} consecutive fetch failures "
                             f"(position unguarded)")
        # "running but never entering" is a silent failure unless someone
        # names it: an engine that has asked the risk manager for an entry
        # many times and never got one is broken, not idle
        budget = float(self.cfg.live_interval_seconds or 0)
        if budget and self.last_cycle_seconds > budget:
            notes.append(f"cycle took {self.last_cycle_seconds:.1f}s against a "
                         f"{budget:.0f}s interval ({self.slow_cycles} slow so far) — "
                         f"decisions are landing late")
        if self.entry_attempts >= self.VETO_ALERT_ATTEMPTS and self.entries_approved == 0:
            top = max(self.veto_counts.items(), key=lambda kv: kv[1], default=None)
            if top is not None:
                notes.append(f"{self.entry_attempts} entry attempts, 0 approved — "
                             f"top blocker: {top[0]} ({top[1]}x)")
        self.health_note = "; ".join(notes) or None

    def run_forever(self, interval: int | None = None):
        with self.own_book():
            self._run_forever_owned(interval)

    def _run_forever_owned(self, interval: int | None = None):
        interval = interval or self.cfg.live_interval_seconds
        print(f"[engine] starting paper trading loop (every {interval}s, "
              f"LLM: {self.llm.provider if self.llm.enabled else 'quant mode'}). Ctrl-C to stop.")
        fails = 0   # consecutive error cycles (exponential backoff below)
        while True:
            cycle_t0 = time.monotonic()
            try:
                summary = self.run_cycle()
                # run_cycle guards its own body, so a returned summary with
                # errors means a degraded cycle, not a dead engine: back off
                # instead of hot-spinning the failure every `interval` seconds
                # (at a 2s fast-book cadence that is errors and logs at full
                # rate indefinitely)
                if isinstance(summary, dict) and summary.get("errors"):
                    fails += 1
                else:
                    fails = 0
            except BookOwnedError as exc:
                # Another process owns this book now. Backing off and retrying
                # would keep writing from a broker this engine no longer owns.
                self.last_error = str(exc)
                print(f"[engine] STOPPING — {exc}")
                return
            except Exception as exc:
                fails += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                if not self.quiet:
                    traceback.print_exc()
            # sleep the REMAINDER of the (possibly backed-off) interval from
            # cycle START — like the dashboard loop — so a slow cycle does not
            # drift the cadence, and consecutive failures sleep
            # min(interval * 2**fails, 300)s instead of crash-spinning
            wait = min(interval * (2 ** fails), 300) if fails else interval
            # sliced sleep: wake quickly for Ctrl-C, and on the HFT book's 2s
            # cadence a full-interval sleep would add up to `interval` seconds
            # of extra latency after the loop wakes (the dashboard threads
            # already slice the same way)
            deadline = time.monotonic() + max(0.0, wait - (time.monotonic() - cycle_t0))
            while time.monotonic() < deadline:
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


    def _print_summary(self, summary: dict):
        holds = summary.get("holds", 0)
        vetoes = summary.get("vetoes") or {}
        veto_s = ""
        if vetoes:
            veto_s = " | vetoed " + ", ".join(f"{k} x{v}" for k, v in
                                              sorted(vetoes.items(), key=lambda kv: -kv[1]))
        print(f"[cycle {summary['cycle']}] equity {summary.get('equity')} | "
              f"opened {len(summary['opened'])} | closed {len(summary['closed'])} | "
              f"holds {holds} | errors {len(summary['errors'])}{veto_s} | "
              f"{summary.get('seconds', 0):.1f}s")
        for err in summary["errors"][-3:]:
            print(f"    ! {err[:160]}")
