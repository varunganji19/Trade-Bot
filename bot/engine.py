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

import time
import threading
import traceback

import pandas as pd

from bot.broker import PaperBroker
from bot.data import MarketData
from bot.indicators import add_all_indicators
from bot.journal import Journal
from bot.llm import LLMClient
from bot.orchestrator import Orchestrator
from bot.risk import RiskManager
from bot.sentiment import SentimentOverlay
from bot.strategies import get_strategy
from config import CONFIG, TIMEFRAME_SECONDS, MarketSpec, infer_kind, utc_now


class TradingEngine:
    # data-outage policy for a spec with an OPEN position (the position is
    # unguarded while its feed is down): warn at N consecutive failed fetches,
    # force-close at the last known good mark at N2
    FETCH_FAIL_WARN = 3
    FETCH_FAIL_CLOSE = 10

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
        self.broker = PaperBroker(costs=self.cfg.costs)
        # guards broker-state mutations (fills, marks, account patches) across
        # the engine thread and dashboard API threads — the dashboard's
        # deposit/withdraw and stats endpoints take it via lock_for_cycle
        self.cycle_lock = threading.RLock()
        self.last_error: str | None = None
        self.risk = RiskManager(self.cfg)
        self.llm = LLMClient(self.cfg.llm)
        self.sentiment = SentimentOverlay(self.llm if self.llm.enabled else None)
        self.orchestrator = Orchestrator(llm_client=self.llm, sentiment_overlay=self.sentiment,
                                          cfg=self.cfg, kronos_engine=None)
        self.market_data = MarketData()
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
        self.kronos = None
        self._kronos_last_bar: dict[tuple[str, str], int] = {}
        self._kronos_promoted = False
        self._kronos_last_error: str | None = None
        self._init_kronos()
        self._restore_positions()

    def _init_kronos(self):
        """Attach the Kronos engine when deps + weights are available; the bot
        runs fully without it (no signal, no vote, no crash)."""
        try:
            from bot.kronos_signal import KronosSignalEngine
            self.kronos = KronosSignalEngine()
            if not self.kronos.predictor.available:
                if not self.quiet:
                    print("[engine] Kronos unavailable (model not vendored or "
                          "torch missing) — running without the forecast voter")
                self.kronos = None
            else:
                if not self.quiet:
                    print("[engine] Kronos loaded — tracked non-voter until its "
                          "IC earns voting rights")
                self.orchestrator.kronos = self.kronos
        except Exception as exc:
            if not self.quiet:
                print(f"[engine] Kronos init skipped: {type(exc).__name__}: {exc}")
            self.kronos = None

    # ------------------------------------------------------------- recovery
    def _restore_positions(self):
        """Restore broker cash and open positions from the journal so a restart
        continues the account instead of resetting it to paper_capital."""
        last = self.journal.last_equity_point(mode=self.mode)
        if last is not None:
            # the journal's cash column IS broker cash at the last cycle's close
            # (engine writes add_equity(equity, broker.cash)); open positions
            # restore separately and are re-marked at current prices each cycle
            self.broker.cash = float(last["cash"])
            # reconcile trades closed after the anchor: a crash between a
            # close and the next equity write would otherwise drop the proceeds
            gap = self.journal.closed_cash_delta_since(last["ts"], mode=self.mode)
            if abs(gap) > 0.005:
                self.broker.cash += gap
                if not self.quiet:
                    print(f"[engine] reconciled {gap:+.2f} from trades closed after "
                          f"the last equity point (crash-window recovery)")
            if not self.quiet:
                print(f"[engine] restored account: cash ${self.broker.cash:,.2f} "
                      f"(as of {last['ts']})")
        for row in self.journal.open_trades():
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
            if not self.quiet:
                print(f"[engine] restored open {row['side']} {row['symbol']} "
                      f"{timeframe} (strategy: {row['strategy']})")

    def _spec_for(self, symbol: str) -> MarketSpec | None:
        for spec in self.cfg.watchlist:
            if spec.symbol == symbol:
                return spec
        return None

    # ------------------------------------------------------------- main loop
    def run_cycle(self) -> dict:
        """One full cycle. Holds `cycle_lock` for the duration: the dashboard's
        account deposit/withdraw and manual close take the same lock, so an
        API thread can never interleave with fills and overwrite cash deltas."""
        with self.cycle_lock:
            return self._run_cycle_locked()

    def _run_cycle_locked(self) -> dict:
        summary = {"cycle": self.cycles + 1, "opened": [], "closed": [], "holds": 0, "errors": []}
        price_map: dict[str, float] = {}
        # keyed by (symbol, timeframe): one symbol has several books and a
        # position must be marked with its OWN book's close, never a sibling
        # timeframe's frame that overwrote the symbol slot
        histories: dict[tuple[str, str], pd.DataFrame] = {}

        try:
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
                self._process_market(spec, summary, df)

            # portfolio allocation: divide the book's risk budget across symbols
            # (skfolio inverse-vol/HRP over the watchlist's realized returns).
            # One return series per symbol — the first spec's timeframe in
            # watchlist order, so every symbol's vol is measured on one scale.
            if self.cfg.portfolio.enabled:
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

            for spec in self.cfg.watchlist:
                pos = self.broker.positions.get(self.broker.position_key(spec.symbol, spec.timeframe))
                if pos:
                    px = self._last_price(spec, histories.get((spec.symbol, spec.timeframe)))
                    if px is not None:
                        # a failed fetch must SKIP the mark, never price the
                        # position at 0.0 — a phantom -100% drawdown would be
                        # journaled permanently and could trip the kill switch
                        price_map[spec.symbol] = px
            if price_map or not self.broker.positions:
                equity = self.broker.equity(price_map)
                self.risk.note_equity(equity)
                self.journal.add_equity(equity, self.broker.cash, mode=self.mode)
                summary["equity"] = round(equity, 2)
                summary["cash"] = round(self.broker.cash, 2)
            else:
                # every held symbol failed to fetch: carrying the last equity
                # point forward is a lie too — skip the write entirely and say so
                summary["errors"].append("equity point skipped: no marks available "
                                          "for any open position")
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
        for key, n in self._fetch_fails.items():
            if n < self.FETCH_FAIL_WARN:
                continue
            if self.broker.positions.get(self.broker.position_key(*key)):
                notes.append(f"{key[0]} {key[1]}: {n} consecutive fetch failures "
                             f"(position unguarded)")
        self.health_note = "; ".join(notes) or None

    def run_forever(self, interval: int | None = None):
        interval = interval or self.cfg.live_interval_seconds
        print(f"[engine] starting paper trading loop (every {interval}s, "
              f"LLM: {self.llm.provider if self.llm.enabled else 'quant mode'}). Ctrl-C to stop.")
        while True:
            self.run_cycle()
            time.sleep(interval)

    # ------------------------------------------------------------- per market
    @staticmethod
    def _kronos_horizon(timeframe: str) -> int:
        """Forecast horizon in bars, normalized to ~1 day ahead regardless of
        the book's timeframe — a hardcoded 24 made the 4h book forecast four
        days out and the 15m book four hours."""
        return max(1, 86400 // TIMEFRAME_SECONDS[timeframe])

    def _kronos_eval(self, spec: MarketSpec, df, i: int):
        """Forecast + IC bookkeeping for Kronos. Returns (signal, promoted).

        Throttled to once per KRONOS_EVERY closed bars per symbol — the model
        is a slow CPU inference (~seconds) and the IC ledger only needs one
        observation per bar boundary, not one per 60s cycle."""
        if self.kronos is None:
            return None, False
        try:
            bar_key = int(df.index[i].timestamp() // TIMEFRAME_SECONDS[spec.timeframe])
            last = self._kronos_last_bar.get((spec.symbol, spec.timeframe))
            every = max(1, self.kronos.cfg.evaluate_every_bars)
            if last is not None and bar_key - last < every:
                return None, self.kronos.promoted()
            sig = self.kronos.evaluate(df.iloc[: i + 1],
                                       horizon=self._kronos_horizon(spec.timeframe))
            # ledger key committed only AFTER a successful evaluation: a
            # transient failure used to book the bar and then silently skip
            # Kronos for `every` more bars with no retry
            self._kronos_last_bar[(spec.symbol, spec.timeframe)] = bar_key
            if sig is None:
                err = getattr(self.kronos, "last_error", None)
                if err and err != self._kronos_last_error and not self.quiet:
                    # evaluate() swallows exceptions internally; surface a NEW
                    # failure once instead of silently forecasting nothing
                    print(f"[engine] kronos forecast failed for {spec.symbol} "
                          f"{spec.timeframe}: {err}")
                self._kronos_last_error = err
            else:
                # IC ledger is keyed by market: a BTC forecast must never be
                # scored against whichever sibling symbol's frame resolved first
                self.kronos.log_and_maybe_resolve(
                    df.iloc[: i + 1], sig, market=f"{spec.symbol}|{spec.timeframe}")
                self._kronos_promoted = self.kronos.promoted()
            return sig, self._kronos_promoted
        except Exception as exc:
            if not self.quiet:
                print(f"[engine] kronos eval failed for {spec.symbol} "
                      f"{spec.timeframe}: {type(exc).__name__}: {exc}")
            return None, False

    def _last_price(self, spec: MarketSpec, df: pd.DataFrame | None = None) -> float | None:
        """Last CLOSED bar's close, or None when no data is available. Callers
        must skip the mark on None — pricing a position at 0.0 journals a
        phantom -100% drawdown permanently."""
        if df is None:
            df = self.market_data.latest(spec, limit=2)
        if df is None or len(df) == 0:
            return None
        return float(df["close"].iloc[-1])

    def _process_market(self, spec: MarketSpec, summary: dict, df: pd.DataFrame | None = None):
        if df is None:
            df = self.market_data.latest(spec)
        if df is None or len(df) < 60:
            return
        df = add_all_indicators(df, self.cfg.params)
        i = len(df) - 1  # last closed candle
        bar_epoch = float(df.index[i].timestamp())

        # Kronos probabilistic forecast: tracked every KRONOS_EVERY bars, votes
        # only when its rolling IC earned rights (bot/kronos_signal.py)
        kronos_sig, kronos_promoted = self._kronos_eval(spec, df, i)

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
            # and 4h specs stand down while e.g. the 1h book holds the symbol
            return

        decision = self.orchestrator.decide(df, i, spec, kronos_signal=kronos_sig,
                                            kronos_promoted=kronos_promoted)
        self.journal.add_decision(spec.symbol, spec.timeframe, decision, mode=self.mode)
        if decision.action == "HOLD":
            summary["holds"] += 1
            return

        open_positions = len(self.broker.positions)
        approval = self.risk.approve(decision, spec, self.broker.equity({}), open_positions,
                                     has_position_on_symbol=self.broker.has_position(spec.symbol),
                                     bar_epoch=bar_epoch)
        if not approval.approved:
            if not self.quiet:
                print(f"[engine] {spec.symbol} {spec.timeframe}: {decision.action} blocked by risk: {approval.reason}")
            return

        # Journal-first, but self-healing: if the broker fill (which derives
        # stop/target from the actual fill price) fails after the INSERT, the
        # trade row must not linger as OPEN — the next cycle would duplicate
        # it and restart would restore a stop-less ghost position.
        trade_id = self.journal.open_trade(
            symbol=spec.symbol, side="long" if decision.action == "LONG" else "short",
            qty=approval.qty, entry_price=decision.price, stop=None, target=None,
            strategy=decision.strategy_name or "orchestrator",
            rationale=decision.rationale, mode=self.mode, opened_ts=utc_now(),
            timeframe=spec.timeframe,
        )
        try:
            self.broker.open_position(spec, decision, approval.qty, decision.price, trade_id, ts=utc_now(),
                                      decision_bar_ts=bar_epoch)
            pos = self.broker.positions[self.broker.position_key(spec.symbol, spec.timeframe)]
            # single follow-up write carries the FILL-derived stop/target and
            # the fill itself (the journal's entry_price must record the fill,
            # not the decision price — exits are journaled at fills)
            self.journal.update_trade_stops(trade_id, stop=pos.stop, target=pos.target,
                                            entry_price=pos.entry_price)
        except Exception:
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
        the replay closed the position."""
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
        closed_pos, pnl, pnl_pct, fees, exit_fill = self.broker.close_position(
            spec, exit_price, reason)
        # close + the cycle's equity point in ONE transaction: a crash between
        # two separate writes used to leave the trade CLOSED while the restart
        # anchor still held pre-exit cash — the proceeds vanished from the account
        self.journal.close_trade(
            trade_id=closed_pos.trade_id, exit_price=exit_fill, pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct, 3), fees=round(fees, 4),
            exit_reason=reason, rationale_close=closed_pos.rationale,
            equity=self.broker.equity({}) if write_equity else None,
            cash=self.broker.cash if write_equity else None, mode=self.mode,
        )
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
        Takes the cycle lock so it can never interleave with an in-flight
        engine cycle on the same book."""
        with self.cycle_lock:
            spec = next((s for s in self.cfg.watchlist
                         if s.symbol == symbol and s.timeframe == timeframe), None)
            if spec is None:
                spec = MarketSpec(infer_kind(symbol), symbol, timeframe)
            pos = self.broker.positions.get(self.broker.position_key(symbol, timeframe))
            if pos is None:
                raise KeyError(f"no open position on {symbol} {timeframe}")
            df = self.market_data.latest(spec)
            if df is None or len(df) == 0:
                raise RuntimeError(f"no market data for {symbol} {timeframe}")
            exit_price = float(df["close"].iloc[-1])
            summary: dict = {"cycle": self.cycles, "opened": [], "closed": [], "holds": 0, "errors": []}
            self._close(spec, exit_price, "manual close", summary,
                        bar_epoch=float(df.index[-1].timestamp()))
        return {"symbol": symbol, "timeframe": timeframe, "exit_price": exit_price}

    def _print_summary(self, summary: dict):
        holds = summary.get("holds", 0)
        print(f"[cycle {summary['cycle']}] equity {summary.get('equity')} | "
              f"opened {len(summary['opened'])} | closed {len(summary['closed'])} | "
              f"holds {holds} | errors {len(summary['errors'])}")
        for err in summary["errors"][-3:]:
            print(f"    ! {err[:160]}")
