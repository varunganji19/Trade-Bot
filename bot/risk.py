"""
Risk manager — the final veto layer before any order.

Implements the risk framework from RESEARCH.md §2.5:
  - 1% of equity risked per trade, size computed from the stop distance
  - notional cap per position, concurrent position cap, gross-notional
    leverage cap across the whole book
  - one position per symbol
  - manual pause (operator flag, see bot.pause): blocks new entries until
    resumed — open positions are never force-closed
  - daily loss kill switch (stops new entries for the UTC day)
  - cooldown per symbol after a stop-out
  - minimum confidence floor
  - R-distance sanity gate: stops wider than a share of entry price are refused (vol-explosion guard)
  - reward floor: declared fixed targets must be ≥ min RR (signal-exit strategies pass None)
The backtester uses `size_position`; the live engine uses `approve`.

Determinism: the daily kill switch compares equity against the start of the
*trading* day, not the wall clock. `note_equity(equity, ts=...)` derives the
day from a bar timestamp when given one (backtests pass the current bar's ts);
live trading passes none and the wall clock is used. The same rule therefore
simulates identically in backtest and paper — no calendar divergence.
"""
from __future__ import annotations

import math
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

from config import CONFIG, MarketSpec, TIMEFRAME_SECONDS, parse_utc


@dataclass
class RiskDecision:
    approved: bool
    qty: float = 0.0
    reason: str = ""
    # STABLE machine-readable veto category, beside the human `reason`.
    # WHY: the tiny-stop veto fired on 100% of the fast book's entries for a
    # week and nobody could see it — the reason was a formatted string in a
    # log line, so nothing counted it. Categories are what the engine
    # aggregates and the dashboard shows (bot/engine.py veto_counts).
    category: str = ""


class RiskManager:
    """The final veto before any order.

    Two independent halts can block NEW entries: the manual pause (`paused`,
    operator-set via bot.pause, indefinite, entries-only) and the automatic
    daily kill switch (equity-triggered, scoped to the UTC day). Neither ever
    force-closes a position — open positions always keep their hard stops,
    targets and strategy exits while either is engaged.
    """

    def __init__(self, cfg=None, *, state_db_path=None, mode: str = "paper"):
        self.cfg = cfg or CONFIG
        self.daily_start_equity: float | None = None
        self.daily_day: str | None = None
        # symbol -> epoch seconds until which entries are blocked. Stored in
        # wall/bar time (not bar indexes) so a cooldown set after a 1h stop-out
        # means the same three hours to the 15m and 4h specs of that symbol.
        self.cooldowns: dict[str, float] = {}
        # manual pause flag (mirrored from bot.pause's file by the live engine
        # once per cycle; backtests construct their own RiskManager and never
        # read the file, so they stay hermetic). Blocks NEW entries only.
        self.paused: bool = False
        self.halted = False
        self.alloc_weights: dict[str, float] = {}  # symbol -> share of total risk budget
        # drawdown throttle state (see note_equity): peak equity, current DD,
        # and the risk multiplier it implies (1.0 -> 0.5 -> 0.25)
        self.peak_equity: float = 0.0
        self.dd_risk_scale: float = 1.0
        # Backtests remain memory-only. Live books share the journal database,
        # but each has an independent durable risk checkpoint.
        self.state_db_path = state_db_path
        self.mode = mode
        self.persistence_error: str | None = None
        self._invalid_checkpoint = False
        self._saved_state: str | None = None
        if state_db_path is not None:
            self._restore_state()

    def _state_json(self) -> str:
        return json.dumps({"version": 1, "daily_start_equity": self.daily_start_equity,
                           "daily_day": self.daily_day, "halted": self.halted,
                           "peak_equity": self.peak_equity,
                           "dd_risk_scale": self.dd_risk_scale,
                           "cooldowns": self.cooldowns}, sort_keys=True, allow_nan=False)

    def _restore_state(self):
        try:
            with closing(sqlite3.connect(self.state_db_path, timeout=10)) as conn, conn:
                conn.execute("CREATE TABLE IF NOT EXISTS risk_state "
                             "(mode TEXT PRIMARY KEY, state_json TEXT NOT NULL)")
                row = conn.execute("SELECT state_json FROM risk_state WHERE mode=?",
                                   (self.mode,)).fetchone()
            if row is None:
                self.persist_state()
                return
            state = json.loads(row[0])
            if not isinstance(state, dict) or state.get("version") != 1:
                raise ValueError("unsupported risk checkpoint")
            def finite(value):
                return type(value) in (int, float) and math.isfinite(value)
            day = state["daily_day"]
            baseline = state["daily_start_equity"]
            if day is not None:
                if not isinstance(day, str) or datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") != day:
                    raise ValueError("invalid daily date")
                if not finite(baseline):
                    raise ValueError("invalid daily baseline")
            elif baseline is not None:
                raise ValueError("baseline without daily date")
            if type(state["halted"]) is not bool:
                raise ValueError("invalid daily halt")
            if not finite(state["peak_equity"]) or state["peak_equity"] < 0:
                raise ValueError("invalid peak equity")
            if not finite(state["dd_risk_scale"]) or state["dd_risk_scale"] not in (0.25, 0.5, 1.0):
                raise ValueError("invalid drawdown scale")
            cooldowns = state["cooldowns"]
            if not isinstance(cooldowns, dict) or any(
                not isinstance(symbol, str) or not finite(until) or until < 0
                for symbol, until in cooldowns.items()
            ):
                raise ValueError("invalid cooldowns")
            self.daily_day, self.daily_start_equity = day, baseline
            self.halted = state["halted"]
            self.peak_equity = state["peak_equity"]
            self.dd_risk_scale = state["dd_risk_scale"]
            self.cooldowns = cooldowns
            self._saved_state = self._state_json()
        except (sqlite3.Error, ValueError, TypeError, KeyError, OverflowError) as exc:
            # Retain the bad row for diagnosis; do not overwrite it with clean
            # defaults on the next mark. Exits remain available, entries vetoed.
            self._invalid_checkpoint = True
            self.persistence_error = f"risk checkpoint restore failed: {exc}"

    def persist_state(self, conn=None):
        """Persist changed controls; an optional journal transaction is owned
        by the caller. Failed writes block new entries until a write succeeds."""
        if self.state_db_path is None or self._invalid_checkpoint:
            return
        try:
            payload = self._state_json()
            if conn is None and payload == self._saved_state and not self.persistence_error:
                return
            statement = ("INSERT INTO risk_state (mode, state_json) VALUES (?,?) "
                         "ON CONFLICT(mode) DO UPDATE SET state_json=excluded.state_json")
            if conn is not None:
                conn.execute(statement, (self.mode, payload))
                # The caller may roll its transaction back, so never cache
                # an externally-owned transaction as durably committed.
                self._saved_state = None
            else:
                with closing(sqlite3.connect(self.state_db_path, timeout=10)) as db, db:
                    db.execute(statement, (self.mode, payload))
                self._saved_state = payload
            self.persistence_error = None
        except (sqlite3.Error, ValueError, TypeError) as exc:
            self.persistence_error = f"risk checkpoint write failed: {exc}"
            if conn is not None:
                raise

    def adjust_cash_flow(self, delta: float, conn=None):
        """Shift both reference levels by external cash flow, preserving the
        accumulated trading loss and any halt/cooldowns. Never reset risk."""
        if self._invalid_checkpoint:
            raise ValueError(self.persistence_error)
        if not math.isfinite(delta):
            raise ValueError("cash-flow adjustment must be finite")
        if self.daily_start_equity is not None:
            self.daily_start_equity += delta
        self.peak_equity = max(0.0, self.peak_equity + delta)
        self.persist_state(conn=conn)

    def set_allocation(self, weights: dict[str, float]):
        """Install portfolio weights (bot.allocator) so `approve` can scale
        per-trade risk by the symbol's share of the book's total budget."""
        self.alloc_weights = weights or {}

    def clear_allocation(self):
        """Drop all portfolio weights (safe on empty/missing state).

        The engine calls this when the allocator is unavailable so the book
        falls back to equal risk split instead of trading on stale weights."""
        self.alloc_weights = {}

    # ------------------------------------------------------------------ core
    def size_position(self, equity: float, price: float, stop_distance: float,
                      kind: str = "crypto",
                      risk_fraction: float | None = None) -> float:
        """Position size such that hitting the stop loses ~risk of equity.
        `risk_fraction` overrides the default risk_per_trade (portfolio
        allocation passes a scaled share here). The drawdown throttle scales
        the result down in deep drawdowns (never up)."""
        if any(v is None or not math.isfinite(v) or v <= 0
               for v in (equity, price, stop_distance)):
            return 0.0
        frac = risk_fraction if risk_fraction is not None else self.cfg.risk.risk_per_trade
        frac *= self.dd_risk_scale
        if not math.isfinite(frac) or frac <= 0:
            return 0.0
        risk_amount = equity * frac
        qty = risk_amount / stop_distance
        max_qty = (equity * self.cfg.risk.max_position_pct) / price
        qty = min(qty, max_qty)
        if not math.isfinite(qty) or qty <= 0:
            return 0.0
        # round down to a sensible granularity; respect min notional
        # India equities are whole-share, like forex units: fractional
        # quantities don't exist on NSE cash (NSE's own minimum is the stock
        # price itself — one share; the bot's generic ₹10/$10 min-notional
        # floor still guards dust entries).
        if kind in ("forex", "india"):
            qty = float(math.floor(qty))
        else:
            qty = math.floor(qty * 1_000_000) / 1_000_000
        if qty * price < 10.0:
            return 0.0
        return qty

    # ------------------------------------------------------------------ state
    def note_equity(self, equity: float, ts: str | None = None):
        """Track daily loss for the kill switch and the drawdown throttle.

        `ts` (ISO bar timestamp) keeps backtests deterministic: the trading day
        is derived from the bar being simulated. Live trading omits it and uses
        the wall clock.
        """
        if ts:
            dt = parse_utc(ts)
            today = dt.strftime("%Y-%m-%d") if dt \
                else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        else:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if equity is None or not math.isfinite(equity):
            return
        if self.daily_day != today:
            self.daily_day = today
            self.daily_start_equity = equity
            self.halted = False
        elif self.daily_start_equity is not None and equity is not None and self.daily_start_equity > 0:
            day_loss = (equity / self.daily_start_equity - 1.0)
            if day_loss <= -self.cfg.risk.daily_loss_kill_switch:
                if not self.halted:
                    # a halt is a major event: it must never engage silently
                    print(f"[risk] DAILY KILL SWITCH engaged: {day_loss*100:.2f}% "
                          f"day loss on {today} — entries blocked until the next UTC day")
                self.halted = True
        # drawdown throttle: a rolling peak tracker (independent of the daily
        # reset) that scales risk down as drawdown deepens — half risk at -10%,
        # a quarter at -20%. Risk only scales; it never blocks like the kill
        # switch (deep DDs must still allow managed recovery exits/entries).
        if equity is not None and equity > 0:
            self.peak_equity = max(self.peak_equity, equity)
            dd = 1.0 - equity / self.peak_equity
            if dd >= self.cfg.risk.drawdown_quarter_risk_at:
                self.dd_risk_scale = 0.25
            elif dd >= self.cfg.risk.drawdown_half_risk_at:
                self.dd_risk_scale = 0.5
            else:
                self.dd_risk_scale = 1.0
        self.persist_state()

    def mark_stopped_out(self, symbol: str, bar_epoch: float, timeframe: str = "1h"):
        """Block re-entries on `symbol` for cooldown_bars_after_stop bars of the
        owning timeframe, counted in bar/wall time from `bar_epoch` (seconds)."""
        tf_seconds = TIMEFRAME_SECONDS.get(timeframe, 3600)
        self.set_cooldown(symbol, bar_epoch + self.cfg.risk.cooldown_bars_after_stop * tf_seconds)

    def set_cooldown(self, symbol: str, until_epoch: float):
        """Explicit cooldown: `symbol` may not re-enter before `until_epoch`."""
        if not math.isfinite(until_epoch) or until_epoch < 0:
            raise ValueError("cooldown expiry must be finite and nonnegative")
        self.cooldowns[symbol] = float(until_epoch)
        self.persist_state()

    def apply_exit_cooldown(self, spec, closed_pos, reason: str, bar_epoch: float | None):
        """One exit-cooldown policy for both live and backtest paths (they used to
        duplicate it; the copies could drift). bar_epoch is None only in manual
        dash-path closes — no clock, no cooldown bookkeeping."""
        if bar_epoch is None:
            return
        tf_seconds = TIMEFRAME_SECONDS[spec.timeframe]
        if reason == "stop loss":
            self.mark_stopped_out(spec.symbol, bar_epoch, spec.timeframe)
        else:
            cd = self.cfg.params.scalper_cooldown_bars \
                if closed_pos.strategy == "vwap_scalper" else 1
            self.set_cooldown(spec.symbol, bar_epoch + cd * tf_seconds)

    def approve(self, decision, spec: MarketSpec, equity: float, open_positions: int,
                has_position_on_symbol: bool,
                bar_epoch: float | None = None,
                open_gross_notional: float = 0.0) -> RiskDecision:
        """Final entry veto. `open_gross_notional` is the book's CURRENT open
        notional (every open position, marked at its own timeframe's last
        good close) — pre-computed by the caller so the gate sees the WHOLE
        book, not just this decision's slice; the 0.0 default leaves only the
        new entry's own notional in the comparison, which is what the
        single-position backtest call site passes.

        `decision.price` is the pre-fill ESTIMATE (the live fill lands on the
        next cycle, at its own slippage) — the gross gate therefore compares
        against a small underestimate of the true fill notional, which is the
        conservative direction for a cap that exists to bound the worst case.
        Single-symbol backtests can never trip the gate: max one open position
        at ≤ max_position_pct (25%) notional is far inside 1.0x equity — the
        gate protects the live multi-book engine, where 4 books × 25% can
        stack to the full cap.
        """
        r = self.cfg.risk
        if self.persistence_error:
            return RiskDecision(False, category="persistence_error", reason=self.persistence_error)
        # manual pause FIRST — it outranks every other consideration because
        # the operator set it deliberately; the automatic kill switch below is
        # a separate, equity-triggered mechanism
        if self.paused:
            return RiskDecision(False, category="paused", reason="manual pause active — blocks NEW entries "
                                             "only; open positions are still managed "
                                             "(stops, targets, exits)")
        if self.halted:
            return RiskDecision(False, category="daily_kill_switch", reason=f"daily kill switch active (limit {r.daily_loss_kill_switch:.0%})")
        if decision.action not in ("LONG", "SHORT"):
            return RiskDecision(False, category="no_signal", reason="no entry signal")
        if has_position_on_symbol:
            return RiskDecision(False, category="symbol_already_held", reason="already in a position on this symbol")
        if open_positions >= r.max_open_positions:
            return RiskDecision(False, category="max_positions", reason=f"max concurrent positions ({r.max_open_positions}) reached")
        if not math.isfinite(equity) or equity <= 0:
            return RiskDecision(False, category="invalid_equity", reason="invalid account equity")
        if not math.isfinite(open_gross_notional) or open_gross_notional < 0:
            return RiskDecision(False, category="invalid_gross", reason="invalid open gross notional")
        if not math.isfinite(decision.confidence) or not 0 <= decision.confidence <= 1:
            return RiskDecision(False, category="invalid_confidence", reason="invalid confidence")
        if decision.target_rr is not None and not math.isfinite(decision.target_rr):
            return RiskDecision(False, category="invalid_target", reason="non-finite reward target")
        if decision.confidence < r.min_confidence:
            return RiskDecision(False, category="confidence_floor", reason=f"confidence {decision.confidence:.2f} < floor {r.min_confidence:.2f}")
        if decision.stop_distance is None or decision.stop_distance <= 0:
            return RiskDecision(False, category="no_stop", reason="no valid stop distance")
        # NaN slips past every <= comparison: a NaN price or stop would size a
        # NaN qty and poison cash/equity permanently (defense-in-depth — the
        # strategies NaN-guard ATR today, but this must not depend on that)
        if not (math.isfinite(decision.price) and math.isfinite(decision.stop_distance)):
            return RiskDecision(False, category="non_finite", reason="non-finite price or stop distance")
        # tiny-stop dust: the stop must cover the modeled round-trip cost
        # (taker entry + taker exit, fee + slippage per kind). A stop tighter
        # than the round trip loses money even when "right" — the win can't
        # pay its own fees, so sizing it is manufacturing dust.
        rt = self.round_trip_rate(spec.kind)
        if decision.price > 0 and decision.stop_distance < rt * decision.price:
            return RiskDecision(False, category="tiny_stop_dust", reason=f"tiny stop (dust): stop distance "
                                             f"{decision.stop_distance:.6g} < round-trip cost "
                                             f"{rt * decision.price:.6g} "
                                             f"({rt * 1e4:.1f}bps of price {decision.price:.6g})")
        # R-distance sanity: a stop far beyond the norm means ATR exploded; the
        # trade would be sized to a vol regime the exit rules can't manage
        if decision.price > 0 and decision.stop_distance > decision.price * r.max_r_per_trade:
            return RiskDecision(False, category="vol_explosion", reason=f"stop distance {decision.stop_distance/decision.price:.1%} of price "
                                             f"exceeds cap {r.max_r_per_trade:.0%} (vol-explosion guard)")
        # reward floor: only when the strategy declares a fixed target —
        # signal-exit strategies (turtle/connors) pass target_rr=None
        if decision.target_rr is not None and decision.target_rr < r.min_rr_per_trade:
            return RiskDecision(False, category="reward_floor", reason=f"declared reward {decision.target_rr:.2f}R < floor {r.min_rr_per_trade:.2f}R")
        # cooldowns are epoch seconds; the (retired) legacy bar-index clock was
        # positional per bar and not comparable across timeframes — epoch time
        # reads coherently from every timeframe's clock
        if bar_epoch is not None and bar_epoch < self.cooldowns.get(spec.symbol, 0.0):
            return RiskDecision(False, category="cooldown", reason="cooldown after recent stop-out")

        qty = self.size_position(equity, decision.price, decision.stop_distance, spec.kind,
                                 risk_fraction=self.risk_fraction(spec.symbol))
        if qty <= 0:
            return RiskDecision(False, category="min_notional", reason="position size rounds to zero (min notional)")
        # gross leverage gate (audit Fix 2.2-lite): total open notional + this
        # entry must stay under max_gross_leverage x equity. The bound used to
        # be only implicit (25% per position x max 4 positions); explicit, it
        # is enforced as ONE cap across mixed timeframes/books and survives
        # any future change to either of the two factors that implied it.
        # equity <= 0 cannot be sensibly levered either way — skip rather than
        # divide by zero (an engine with zero equity has bigger problems).
        total_gross = open_gross_notional + qty * decision.price
        if equity > 0 and total_gross > r.max_gross_leverage * equity:
            return RiskDecision(False, category="gross_leverage", reason=f"gross notional {total_gross:.0f} "
                                             f"({total_gross / equity:.1f}x equity) > "
                                             f"cap {r.max_gross_leverage:.0f}x equity "
                                             f"{equity:.0f} (gross leverage gate)")
        return RiskDecision(True, qty=qty, reason=f"{decision.action} {qty:.6g} @ {decision.price:.6g}")

    def round_trip_rate(self, kind: str) -> float:
        """Modeled taker-entry + taker-exit cost as a fraction of price
        (fee + slippage per leg, per kind). Conservative by design: the live
        take-profit leg may earn maker pricing, but dust must fail against
        the worst case, not the best."""
        c = self.cfg.costs
        return c.fee(kind) + c.slippage(kind) + c.fee(kind) + c.slippage(kind)

    def risk_fraction(self, symbol: str) -> float:
        """Per-symbol risk fraction: base risk_per_trade scaled by the symbol's
        share of the portfolio budget when allocation weights are installed
        (equal shares otherwise). The scale never RAISES risk above the base."""
        base = self.cfg.risk.risk_per_trade
        share = self.alloc_weights.get(symbol)
        if not share:
            return base
        n = max(1, len(self.alloc_weights))
        return base * min(share * n, 1.0)
