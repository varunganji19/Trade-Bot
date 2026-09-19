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

from bot.broker import PaperBroker, limit_fill_price
from bot.data import MarketData
from bot.indicators import add_all_indicators
from bot.journal import BookOwnedError, Journal
from bot.llm import LLMClient
from bot.orchestrator import Orchestrator
from bot.pause import is_paused
from bot.risk import RiskManager
from bot.strategies import get_strategy
from config import (CONFIG, TIMEFRAME_SECONDS, MarketSpec, infer_kind, utc_now)


class TradingEngine:
    # data-outage policy for a spec with an OPEN position (the position is
    # unguarded while its feed is down): warn at N consecutive failed fetches,
    # force-close at the last known good mark at N2
    FETCH_FAIL_WARN = 3
    FETCH_FAIL_CLOSE = 10

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

    # ------------------------------------------------------------- recovery
    def _restore_positions(self):
        """Restore broker cash and open positions from the journal so a restart
        continues the account instead of resetting it to paper_capital."""
        self.broker.cash = self.journal.recover_cash(self.cfg.paper_capital, mode=self.mode)
        if not self.quiet:
            print(f"[engine] restored account: cash ${self.broker.cash:,.2f}")
        for row in self.journal.open_trades(mode=self.mode):
            spec = self._spec_for(row["symbol"])
            kind = spec.kind if spec else "crypto"
            timeframe = row.get("timeframe") or (spec.timeframe if spec else "1h")
            key = (row["symbol"], timeframe)
            if not row.get("stop_price"):
                print(f"[engine] WARNING: restored {row['symbol']} {row['side']} "
                      f"{timeframe} has NO stop on record (legacy/crashed row) — "
                      f"closing it at the first mark rather than trading unguarded")
                self._unguarded_pending.add(key)
            else:
                # bars that closed while the engine was offline must be
                # replayed: a stop breach during downtime is still a breach,
                # even if price has since recovered above the level
                self._replay_pending.add(key)
            self.broker.restore_position(row, kind, timeframe=timeframe)
            pos = self.broker.positions[self.broker.position_key(row["symbol"], timeframe)]
            if row.get("decision_bar_ts"):
                # newer journal rows anchor replay to the DECISION bar's own
                # clock (bar time, directly comparable with bar_ts in
                # _replay_missed_bars) instead of the wall clock below
                try:
                    pos.entry_bar_ts = float(row["decision_bar_ts"])
                except (TypeError, ValueError):
                    pass
            # else: the trades table carries no decision-bar clock (schema:
            # opened_ts only), so entry_bar_ts stays wall-clock derived from
            # opened_ts by restore_position. opened_ts is written at decision
            # time, so it trails the decision bar by at most one cycle
            # interval; _replay_missed_bars scans bar_ts > entry_bar_ts, so the
            # error direction is conservative (replay may scan one extra bar,
            # never skip a breach bar).
            if not self.quiet:
                print(f"[engine] restored open {row['side']} {row['symbol']} "
                      f"{timeframe} (strategy: {row['strategy']})")

    def _spec_for(self, symbol: str) -> MarketSpec | None:
        for spec in self.cfg.watchlist:
            if spec.symbol == symbol:
                return spec
        return None

    def _mark_held_positions(self, histories: dict) -> dict[str, float]:
        """Mark-to-market price map for EVERY open position, keyed by the
        HELD position's own book — not the watchlist. Marks used to iterate
        cfg.watchlist, so a position whose (symbol, timeframe) was off the
        active book (watchlist edited mid-hold, a mode switch's orphan, or a
        spec pinned by a test) could never be marked: the equity walk
        stalled forever with 'no marks available'. Positions own the mark;
        the watchlist only owns entries.

        A symbol held by several books is marked at its OWN book's close
        (histories is keyed by (symbol, timeframe) for exactly that); a
        position behind a dead feed is skipped (None) — pricing it at 0.0
        would journal a phantom -100% drawdown and could trip the kill
        switch.

        NOTE on the key type: PaperBroker.equity() keys its price_map by
        SYMBOL only (engine must not assume per-(symbol, timeframe) marks —
        see broker.equity), so two books holding the same symbol share one
        slot. The one-position-per-symbol risk gate normally prevents that
        collision entirely (a restored legacy row is the exception); when it
        still happens the first book in sorted (symbol, timeframe) order wins
        deterministically (setdefault below) instead of last-write-wins on
        dict order."""
        marks: dict[str, float] = {}
        for pos in sorted(self.broker.positions_snapshot(),
                          key=lambda p: (p.symbol, p.timeframe)):
            px = self._last_price_from_history(pos.symbol, pos.timeframe, histories)
            if px is not None:
                marks.setdefault(pos.symbol, px)
        return marks

    def _last_price_from_history(self, symbol: str, timeframe: str,
                                 histories: dict) -> float | None:
        """Last closed bar's close for a held position's own book — from the
        cycle's fetched histories first (no extra fetch), else the symbol's
        spec on the watchlist, else one direct fetch. None when no mark is
        available (outage, or the symbol is off the active book AND the
        direct fetch failed)."""
        df = histories.get((symbol, timeframe))
        if df is not None and len(df):
            return float(df["close"].iloc[-1])
        spec = self._spec_for(symbol)
        if spec is not None:
            # limit=2: a mark needs only the last close (and its cache key
            # must differ from the engine's lookback fetch — see bot/data.py)
            return self._last_price(spec, None)
        # off-watchlist held position: fetch directly so its equity keeps
        # flowing (the watchlist gates ENTRIES, never marks)
        try:
            df = self.market_data.latest(MarketSpec(infer_kind(symbol), symbol, timeframe),
                                         limit=2)
        except Exception:
            return None
        if df is None or not len(df):
            return None
        return float(df["close"].iloc[-1])

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

    def _run_cycle_locked(self) -> dict:
        summary = {"cycle": self.cycles + 1, "opened": [], "closed": [], "holds": 0, "errors": []}
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

            # PASS 1 — fetch every book first, decide second. Entry sizing
            # needs START-of-cycle marked equity (approve must see unrealized,
            # not bare cash), and marks need every book's history — so all
            # fetches land before any _process_market call. `due` preserves
            # watchlist order, so management/entries still run spec by spec
            # exactly as before.
            due: list[tuple] = []
            for spec in self.cfg.watchlist:
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
                    if self._last_bar_ts.get(key) == bar_ts:
                        continue
                    self._last_bar_ts[key] = bar_ts
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
                # resetting it here used to leave the position unguarded for
                # another FETCH_FAIL_CLOSE failures before the next attempt.
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
                # (a persistently failing fetch at 2s HFT cadence used to spin
                # errors + logs at full rate indefinitely)
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

    # ------------------------------------------------------------- per market
    def _last_price(self, spec: MarketSpec, df: pd.DataFrame | None = None) -> float | None:
        """Last CLOSED bar's close, or None when no data is available. Callers
        must skip the mark on None — pricing a position at 0.0 journals a
        phantom -100% drawdown permanently."""
        if df is None:
            df = self.market_data.latest(spec, limit=2)
        if df is None or len(df) == 0:
            return None
        return float(df["close"].iloc[-1])

    def _paused_now(self) -> bool:
        """Mid-cycle re-read of the operator pause flag. The cycle-start read
        in _run_cycle_locked can go stale while a long watchlist is evaluated;
        a pause engaged mid-cycle must block every LATER approve/fill in the
        same cycle (is_paused() returns a (paused, note) tuple — a bare truth
        test on it is always truthy, so the [0] index matters). Mirrors into
        RiskManager.paused exactly like the cycle-start read so approve()
        vetoes on fresh truth. Never raises (fails toward paused)."""
        try:
            paused, _note = is_paused()
        except Exception:
            paused = True
        self.risk.paused = bool(paused)
        return self.risk.paused

    def _approval_equity(self) -> float:
        """Marked equity basis for entry sizing: the pass-1 mark-to-market
        equity (unrealized included), NOT bare broker cash. broker.equity({})
        prices every position at its entry (unrealized 0), i.e. cash — sizing
        off it ignores open P&L and mis-feeds the gross gate. Falls back to
        the cash basis only when no mark exists (or before the first cycle)."""
        if self._cycle_equity is not None and self._cycle_equity > 0:
            return self._cycle_equity
        return self.broker.equity({})

    def _cross_book_gross_notional(self, summary: dict | None = None) -> float:
        """The OTHER book's open notional, for the cross-mode gross gate. The
        two books (standard 'paper' and 'hft') share wider market risk while
        each broker only sees its own positions — capping per-book lets 4
        standard positions + the HFT book stack past the intended portfolio
        leverage. Estimated at entry price (the other book's live marks are
        not visible here — a stale-but-sane bound beats 0.0). Fail-safe: when
        the journal is unreadable the gate degrades to the local book only
        and says so loudly instead of blocking entries on a torn read."""
        other = "hft" if self.mode == "paper" else "paper"
        try:
            rows = self.journal.open_trades(mode=other)
        except Exception as exc:
            msg = (f"cross-book gross unreadable ({type(exc).__name__}: {exc}) — "
                   f"gross gate uses the local {self.mode} book only")
            if summary is not None:
                summary["errors"].append(msg)
            if not self.quiet:
                print(f"[engine] {msg}")
            return 0.0
        total = 0.0
        for row in rows:
            try:
                total += abs(float(row.get("qty") or 0.0)) * abs(float(row.get("entry_price") or 0.0))
            except (TypeError, ValueError):
                continue
        return total

    def _open_gross_notional(self) -> float:
        """Mark-priced gross notional of every open position — the risk
        manager's gross-leverage gate input. Each book is marked at its OWN
        (symbol, timeframe)'s last good close; a book with no mark yet (e.g.
        a restored position behind a dead feed) falls back to its entry
        price — a stale-but-sane estimate beats both 0.0 (which would let the
        gate under-count exposure) and a sibling timeframe's price (which
        prices a 15m position with the 1h book's close)."""
        gross = 0.0
        for pos in self.broker.positions_snapshot():
            mark = self._last_good_price.get((pos.symbol, pos.timeframe), pos.entry_price)
            gross += pos.qty * mark
        return gross

    def _age_pending(self, spec: MarketSpec, summary: dict) -> bool:
        """Waited/expiry bookkeeping for this book's resting maker limit.
        Returns True when an order EXPIRED (and was removed — the caller falls
        through to a fresh decision); False when none exists or it is still
        resting (the caller stands down). Fill attempts live with the caller:
        the sibling-hold path ages WITHOUT filling (a fill would open a second
        position on the same symbol against the risk gate)."""
        pkey = self.broker.position_key(spec.symbol, spec.timeframe)
        pend = self._pending.get(pkey)
        if pend is None:
            return False
        pend["waited"] += 1
        if pend["waited"] > self.cfg.hft.limit_wait_bars:
            del self._pending[pkey]
            if not self.quiet:
                print(f"[engine] {spec.symbol} {spec.timeframe}: resting limit "
                      f"expired unfilled after {self.cfg.hft.limit_wait_bars} bars")
            return True
        return False

    def _process_market(self, spec: MarketSpec, summary: dict, df: pd.DataFrame | None = None):
        if df is None:
            df = self.market_data.latest(spec)
        if df is None or len(df) < 60:
            return
        df = add_all_indicators(df, self.cfg.params)
        i = len(df) - 1  # last closed candle
        bar_epoch = float(df.index[i].timestamp())

        pos = self.broker.positions.get(self.broker.position_key(spec.symbol, spec.timeframe))
        if pos is not None:
            key = (spec.symbol, spec.timeframe)
            if key in self._unguarded_pending:
                # restored legacy row with no stop: it cannot be managed, so
                # cut it at the first mark instead of trading unguarded
                self._unguarded_pending.discard(key)
                self._close(spec, float(df["close"].iloc[-1]), "restored without stop",
                            summary, bar_epoch=bar_epoch, write_equity=False)
                return
            if key in self._replay_pending:
                self._replay_pending.discard(key)
                if self._replay_missed_bars(spec, pos, df, summary):
                    return
            self._manage_position(spec, pos, df, i, summary, bar_epoch=bar_epoch)
            return
        if self.broker.has_position(spec.symbol):
            # one position per symbol across timeframes (risk rule): the 15m
            # and 4h specs stand down while e.g. the 1h book holds the symbol.
            # A resting limit on THIS book still ages here (no fill attempt —
            # that would breach the gate): without this the early return
            # leaked _pending entries that never expired.
            self._age_pending(spec, summary)
            return

        # resting maker limit (HFT book): try to fill on THIS closed bar
        # before considering any new decision — one resting order per book
        pkey = self.broker.position_key(spec.symbol, spec.timeframe)
        pend = self._pending.get(pkey)
        if pend is not None:
            fill_price = limit_fill_price(pend["side"], pend["limit"], df.iloc[i],
                                          self.cfg.hft.maker_penetration_bps)
            if fill_price is not None:
                del self._pending[pkey]
                if self.risk.halted or self.risk.persistence_error or self._paused_now():
                    # kill switch / operator pause engaged while resting (the
                    # pause flag is re-read here — the cycle-start read may be
                    # stale; is_paused() returns a tuple, so index [0] via the
                    # helper: a bare `or is_paused()` is always truthy and used
                    # to cancel EVERY resting fill):
                    # the order dies unfilled — it never crosses the spread
                    if not self.quiet:
                        print(f"[engine] {spec.symbol} {spec.timeframe}: resting "
                              f"{pend['side']} cancelled (halted/paused at fill)")
                    return
                pend["decision"].price = fill_price
                self._fill_entry(spec, pend["decision"], pend["qty"], fill_price,
                                 maker_entry=True,
                                 bar_epoch=float(pend["decision_bar_ts"]), summary=summary)
                return
            if not self._age_pending(spec, summary):
                return   # still resting — no new decisions while it waits
            # expired — fall through to a fresh decision below

        decision = self.orchestrator.decide(df, i, spec)
        self.journal.add_decision(spec.symbol, spec.timeframe, decision, mode=self.mode)
        if decision.action == "HOLD":
            summary["holds"] += 1
            return

        open_positions = len(self.broker.positions)
        # fresh pause truth before sizing: a flag engaged mid-cycle blocks
        # this and every later entry (mirrored into risk, so approve vetoes)
        if self._paused_now():
            if not self.quiet:
                print(f"[engine] {spec.symbol} {spec.timeframe}: {decision.action} "
                      f"blocked by risk: manual pause engaged mid-cycle")
            return
        approval = self.risk.approve(decision, spec, self._approval_equity(), open_positions,
                                     has_position_on_symbol=self.broker.has_position(spec.symbol),
                                     bar_epoch=bar_epoch,
                                     open_gross_notional=(self._open_gross_notional()
                                                          + self._cross_book_gross_notional(summary)))
        if not approval.approved:
            if not self.quiet:
                print(f"[engine] {spec.symbol} {spec.timeframe}: {decision.action} blocked by risk: {approval.reason}")
            return

        # maker entry (HFT book): the order RESTS at the quoted level instead
        # of crossing the spread — filled against later closed bars (maker
        # fee, no slippage), expiring after cfg.hft.limit_wait_bars. Unfilled
        # pending orders are un-journaled by design: the DECISION row above
        # already records the intent, and a dead process just expires it.
        if getattr(decision, "limit_price", None):
            self._pending[self.broker.position_key(spec.symbol, spec.timeframe)] = {
                "decision": decision, "qty": approval.qty,
                "limit": float(decision.limit_price), "side": decision.action,
                "waited": 0, "decision_bar_ts": bar_epoch,
                "strategy_name": decision.strategy_name or "orchestrator",
            }
            if not self.quiet:
                print(f"[engine] REST {decision.action} {spec.symbol} {spec.timeframe} "
                      f"limit @ {float(decision.limit_price):.6g} via {decision.strategy_name}")
            return

        self._fill_entry(spec, decision, approval.qty, decision.price,
                         maker_entry=False, bar_epoch=bar_epoch, summary=summary)

    def _fill_entry(self, spec: MarketSpec, decision, qty: float, fill_price: float,
                    maker_entry: bool, bar_epoch: float, summary: dict):
        """Journal-first entry fill, shared by the market path (fills next
        cycle at the live price + slippage) and the resting-limit path (fills
        at the quoted level, maker fee). Self-healing: if the broker fill
        (which derives stop/target from the actual fill price) fails after
        the INSERT, the trade row is aborted — no stop-less ghost OPEN row
        survives to the next cycle/restart."""
        if self.risk.halted or self.risk.persistence_error or self._paused_now():
            # halt/pause flipped between approve and fill: the journaled
            # DECISION already records the intent, but no order crosses
            if not self.quiet:
                print(f"[engine] {spec.symbol} {spec.timeframe}: entry cancelled "
                      f"(halted/paused between approve and fill)")
            return
        key = self.broker.position_key(spec.symbol, spec.timeframe)
        if key in self.broker.positions:
            raise RuntimeError(f"position already exists for {key}")
        before = (self.broker.cash, self.broker.fees_paid, self.broker.realized_pnl)
        trade_id = self.journal.open_trade(
            symbol=spec.symbol, side="long" if decision.action == "LONG" else "short",
            qty=qty, entry_price=fill_price, stop=None, target=None,
            strategy=decision.strategy_name or "orchestrator",
            rationale=decision.rationale, mode=self.mode, opened_ts=utc_now(),
            timeframe=spec.timeframe, pending_fill=True,
        )
        try:
            self.broker.open_position(spec, decision, qty, fill_price, trade_id, ts=utc_now(),
                                      decision_bar_ts=bar_epoch, maker_entry=maker_entry)
            pos = self.broker.positions[self.broker.position_key(spec.symbol, spec.timeframe)]
            # single follow-up write carries the FILL-derived stop/target, the
            # fill itself (the journal's entry_price must record the fill, not
            # the decision price — exits are journaled at fills), the entry
            # leg's fee, and the INITIAL stop latch (R ground truth, never
            # trailed over)
            self.journal.record_fill(trade_id, entry_price=pos.entry_price,
                                     stop=pos.stop, target=pos.target,
                                     entry_fee=pos.entry_fee,
                                     initial_stop=pos.initial_stop,
                                     decision_bar_ts=bar_epoch)
        except Exception:
            # The broker fill may have succeeded before record_fill failed.
            # Undo the simulated fill as well as aborting its journal row.
            self.broker.positions.pop(key, None)
            self.broker.cash, self.broker.fees_paid, self.broker.realized_pnl = before
            self.journal.abort_trade(trade_id)
            raise
        summary["opened"].append({
            "symbol": spec.symbol, "side": pos.side, "qty": pos.qty, "price": pos.entry_price,
            "strategy": pos.strategy, "stop": round(pos.stop, 6) if pos.stop else None,
        })
        if not self.quiet:
            print(f"[engine] OPEN {pos.side.upper()} {spec.symbol} qty {pos.qty:.6g} @ "
                  f"{pos.entry_price:.6g} via {pos.strategy} (stop {pos.stop:.6g})")

    def _replay_missed_bars(self, spec: MarketSpec, pos, df, summary: dict) -> bool:
        """After a restart, scan every closed bar since the position's decision
        bar — the engine was down for some of them, and a stop breach during
        downtime must still exit even if the latest bar's range is back inside
        the levels. Hard stop/target only: strategy check_exit needs the
        current bar's state and cannot be replayed honestly. Returns True when
        the replay closed the position.

        Clock note: pos.entry_bar_ts is the DECISION bar's epoch when the
        journal row carries decision_bar_ts (set at fill time from bar_epoch),
        else the wall-clock opened_ts stamped at decision time (see
        _restore_positions) — at most one cycle interval after the decision
        bar, so the `bar_ts <= entry_bar_ts` skip errs toward replaying one
        extra bar, never toward skipping a breach bar."""
        if not pos.entry_bar_ts:
            return False
        for j in range(len(df)):
            bar_ts = float(df.index[j].timestamp())
            if bar_ts <= pos.entry_bar_ts:
                continue
            reason, exit_price = self.broker.scan_bar_exits(spec, df.iloc[j])
            if reason:
                self._close(spec, float(exit_price), reason, summary, bar_epoch=bar_ts,
                            write_equity=False)
                return True
        return False

    def _manage_position(self, spec: MarketSpec, pos, df, i: int, summary: dict,
                         bar_epoch: float | None = None):
        # bars_held counts CLOSED BARS since the decision bar (the strategy's
        # time stops are defined in bars) — not 60s engine cycles, which would
        # fire a 12-bar 4h stop in 12 minutes
        if bar_epoch is not None and pos.entry_bar_ts:
            tf_seconds = TIMEFRAME_SECONDS[spec.timeframe]
            pos.bars_held = max(0, int(round((bar_epoch - pos.entry_bar_ts) / tf_seconds)))
        bar = df.iloc[i]

        # 1) hard stop / target from this bar's range — but never on the entry
        # (decision) bar itself: the stop was computed FROM that bar, and scanning
        # it would phantom-stop a position on a bar that closed before the entry.
        # The FILL bar (the bar after the decision bar) IS scanned: the
        # backtester fills at its open, so its whole range is post-fill there.
        # Live, the fill lands up to one cycle after that open, so this scan
        # can include a bounded pre-fill sliver — the conservative direction
        # (live may stop out where the backtest survives, never the reverse).
        entry_bar_scan = (pos.entry_bar_ts and bar_epoch is not None
                          and bar_epoch <= pos.entry_bar_ts)
        if not entry_bar_scan:
            reason, exit_price = self.broker.scan_bar_exits(spec, bar)
            if reason:
                self._close(spec, float(exit_price), reason, summary, bar_epoch=bar_epoch,
                            write_equity=False)
                return

        # 2) strategy-specific exits and stop updates
        strat = get_strategy(pos.strategy, self.cfg.params)
        exit_reason, new_stop = strat.check_exit(df, i, pos)
        if new_stop is not None and new_stop != pos.stop:
            pos.stop = new_stop
            self.journal.update_trade_stops(pos.trade_id, stop=new_stop)
            if not self.quiet:
                print(f"[engine] {spec.symbol}: stop trailed to {new_stop:.6g}")
        if exit_reason:
            # fills at the last CLOSED bar's close: bounded staleness of one
            # engine interval (the cycle sees the bar within LIVE_INTERVAL of
            # its close) — the only closed-bar-honest price available
            self._close(spec, float(bar["close"]), exit_reason, summary, bar_epoch=bar_epoch,
                        write_equity=False)
            return

    def _close(self, spec: MarketSpec, exit_price: float, reason: str, summary: dict,
               bar_epoch: float | None = None, write_equity: bool = True):
        """Close via the broker + journal. write_equity bundles the cycle-end
        equity point into the SAME transaction as the close (crash-safety).

        Callers on the normal cycle path pass write_equity=False: the cycle
        tail always writes one point with REAL marks afterwards, and writing
        two points per close cycle (the bundled one marks sibling positions
        at their ENTRY price — stale) distorted the equity curve. The crash
        window between close and tail-write is covered by the restart's
        closed_cash_delta_since reconciliation. Manual and data-outage closes
        keep write_equity=True — they have no cycle tail behind them."""
        # one position per (symbol, timeframe) book: any resting limit for it
        # is superseded the moment the position closes
        key = self.broker.position_key(spec.symbol, spec.timeframe)
        before = (self.broker.cash, self.broker.fees_paid, self.broker.realized_pnl)
        closed_pos, pnl, pnl_pct, fees, exit_fill = self.broker.close_position(
            spec, exit_price, reason)
        # exact cash effect of THIS close event: the broker added
        # gross - exit_fee; pnl = gross - exit_fee - entry_fee, so the
        # close-event delta = pnl + entry_fee (entry fee was charged at open,
        # already reflected in any pre-close equity anchor)
        entry_fee = closed_pos.entry_fee if closed_pos.entry_fee is not None else 0.0
        cash_delta = pnl + entry_fee
        # close + the cycle's equity point in ONE transaction: a crash between
        # two separate writes used to leave the trade CLOSED while the restart
        # anchor still held pre-exit cash — the proceeds vanished from the account
        try:
            self.journal.close_trade(
                trade_id=closed_pos.trade_id, exit_price=exit_fill, pnl=round(pnl, 2),
                pnl_pct=round(pnl_pct, 3), fees=round(fees, 4),
                exit_reason=reason, rationale_close=closed_pos.rationale,
                equity=self.broker.equity({}) if write_equity else None,
                cash=self.broker.cash if write_equity else None, mode=self.mode,
                entry_fee=round(entry_fee, 6) if closed_pos.entry_fee is not None else None,
                realized_cash_delta=round(cash_delta, 6),
                owner_token=self.book_token,
            )
        except Exception:
            # A failed SQLite transaction leaves the trade OPEN. Keep the
            # broker aligned so the next cycle can manage/retry its exit.
            self.broker.positions[key] = closed_pos
            self.broker.cash, self.broker.fees_paid, self.broker.realized_pnl = before
            raise
        self._pending.pop(key, None)
        # cooldown after any exit so the next cycle can't instantly re-enter;
        # stored in epoch seconds so every timeframe of the symbol reads the
        # same clock (bar-index scales are not comparable across timeframes)
        self.risk.apply_exit_cooldown(spec, closed_pos, reason, bar_epoch)
        summary["closed"].append({
            "symbol": spec.symbol, "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 3),
            "reason": reason, "strategy": closed_pos.strategy,
        })
        if not self.quiet:
            print(f"[engine] CLOSE {closed_pos.side.upper()} {spec.symbol} @ {exit_fill:.6g} "
                  f"({reason}) P&L {pnl:+.2f}")

    def close_manual(self, symbol: str, timeframe: str) -> dict:
        """Dashboard close button: close (symbol, timeframe) at the last CLOSED
        bar's close. Reuses the engine's own close path (broker fills + journal
        + risk cooldown) so a manual exit is identical to an engine exit.

        Short-lock pattern: the market-data fetch (network IO) happens BEFORE
        the cycle lock is taken; only the broker/journal mutation holds the
        lock, so a stalled feed can never wedge the engine cycle behind a
        dashboard click. The position is re-checked under the lock (it may
        have closed between fetch and lock); the exit price carries bounded
        staleness of one fetch, identical to the cycle path's closed-bar
        pricing."""
        spec = next((s for s in self.cfg.watchlist
                     if s.symbol == symbol and s.timeframe == timeframe), None)
        if spec is None:
            spec = MarketSpec(infer_kind(symbol), symbol, timeframe)
        df = self.market_data.latest(spec)
        if df is None or len(df) == 0:
            raise RuntimeError(f"no market data for {symbol} {timeframe}")
        exit_price = float(df["close"].iloc[-1])
        exit_bar_ts = float(df.index[-1].timestamp())
        with self.cycle_lock:
            pos = self.broker.positions.get(self.broker.position_key(symbol, timeframe))
            if pos is None:
                raise KeyError(f"no open position on {symbol} {timeframe}")
            summary: dict = {"cycle": self.cycles, "opened": [], "closed": [], "holds": 0, "errors": []}
            self._close(spec, exit_price, "manual close", summary,
                        bar_epoch=exit_bar_ts)
        return {"symbol": symbol, "timeframe": timeframe, "exit_price": exit_price}

    def _print_summary(self, summary: dict):
        holds = summary.get("holds", 0)
        print(f"[cycle {summary['cycle']}] equity {summary.get('equity')} | "
              f"opened {len(summary['opened'])} | closed {len(summary['closed'])} | "
              f"holds {holds} | errors {len(summary['errors'])}")
        for err in summary["errors"][-3:]:
            print(f"    ! {err[:160]}")
