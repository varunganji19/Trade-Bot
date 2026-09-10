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
from dataclasses import dataclass
from datetime import datetime, timezone

from config import CONFIG, MarketSpec, TIMEFRAME_SECONDS, parse_utc


@dataclass
class RiskDecision:
    approved: bool
    qty: float = 0.0
    reason: str = ""


class RiskManager:
    """The final veto before any order.

    Two independent halts can block NEW entries: the manual pause (`paused`,
    operator-set via bot.pause, indefinite, entries-only) and the automatic
    daily kill switch (equity-triggered, scoped to the UTC day). Neither ever
    force-closes a position — open positions always keep their hard stops,
    targets and strategy exits while either is engaged.
    """

    def __init__(self, cfg=None):
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
        self.current_drawdown: float = 0.0
        self.dd_risk_scale: float = 1.0

    def set_allocation(self, weights: dict[str, float]):
        """Install portfolio weights (bot.allocator) so `approve` can scale
        per-trade risk by the symbol's share of the book's total budget."""
        self.alloc_weights = weights or {}

    # ------------------------------------------------------------------ core
    def size_position(self, equity: float, price: float, stop_distance: float,
                      kind: str = "crypto",
                      risk_fraction: float | None = None) -> float:
        """Position size such that hitting the stop loses ~risk of equity.
        `risk_fraction` overrides the default risk_per_trade (portfolio
        allocation passes a scaled share here). The drawdown throttle scales
        the result down in deep drawdowns (never up)."""
        if equity <= 0 or price <= 0 or stop_distance is None or stop_distance <= 0:
            return 0.0
        frac = risk_fraction if risk_fraction is not None else self.cfg.risk.risk_per_trade
        frac *= self.dd_risk_scale
        risk_amount = equity * frac
        qty = risk_amount / stop_distance
        max_qty = (equity * self.cfg.risk.max_position_pct) / price
        qty = min(qty, max_qty)
        # round down to a sensible granularity; respect min notional
        if kind == "forex":
            qty = round(qty, 0)
        else:
            qty = round(qty, 6)
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
        if self.daily_day != today:
            self.daily_day = today
            self.daily_start_equity = equity
            self.halted = False
        elif self.daily_start_equity and equity is not None:
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
            self.current_drawdown = dd
            if dd >= self.cfg.risk.drawdown_quarter_risk_at:
                self.dd_risk_scale = 0.25
            elif dd >= self.cfg.risk.drawdown_half_risk_at:
                self.dd_risk_scale = 0.5
            else:
                self.dd_risk_scale = 1.0

    def mark_stopped_out(self, symbol: str, bar_epoch: float, timeframe: str = "1h"):
        """Block re-entries on `symbol` for cooldown_bars_after_stop bars of the
        owning timeframe, counted in bar/wall time from `bar_epoch` (seconds)."""
        tf_seconds = TIMEFRAME_SECONDS.get(timeframe, 3600)
        self.set_cooldown(symbol, bar_epoch + self.cfg.risk.cooldown_bars_after_stop * tf_seconds)

    def set_cooldown(self, symbol: str, until_epoch: float):
        """Explicit cooldown: `symbol` may not re-enter before `until_epoch`."""
        self.cooldowns[symbol] = float(until_epoch)

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
        # manual pause FIRST — it outranks every other consideration because
        # the operator set it deliberately; the automatic kill switch below is
        # a separate, equity-triggered mechanism
        if self.paused:
            return RiskDecision(False, reason="manual pause active — blocks NEW entries "
                                             "only; open positions are still managed "
                                             "(stops, targets, exits)")
        if self.halted:
            return RiskDecision(False, reason=f"daily kill switch active (limit {r.daily_loss_kill_switch:.0%})")
        if decision.action not in ("LONG", "SHORT"):
            return RiskDecision(False, reason="no entry signal")
        if has_position_on_symbol:
            return RiskDecision(False, reason="already in a position on this symbol")
        if open_positions >= r.max_open_positions:
            return RiskDecision(False, reason=f"max concurrent positions ({r.max_open_positions}) reached")
        if decision.confidence < r.min_confidence:
            return RiskDecision(False, reason=f"confidence {decision.confidence:.2f} < floor {r.min_confidence:.2f}")
        if decision.stop_distance is None or decision.stop_distance <= 0:
            return RiskDecision(False, reason="no valid stop distance")
        # NaN slips past every <= comparison: a NaN price or stop would size a
        # NaN qty and poison cash/equity permanently (defense-in-depth — the
        # strategies NaN-guard ATR today, but this must not depend on that)
        if not (math.isfinite(decision.price) and math.isfinite(decision.stop_distance)):
            return RiskDecision(False, reason="non-finite price or stop distance")
        # R-distance sanity: a stop far beyond the norm means ATR exploded; the
        # trade would be sized to a vol regime the exit rules can't manage
        if decision.price > 0 and decision.stop_distance > decision.price * r.max_r_per_trade:
            return RiskDecision(False, reason=f"stop distance {decision.stop_distance/decision.price:.1%} of price "
                                             f"exceeds cap {r.max_r_per_trade:.0%} (vol-explosion guard)")
        # reward floor: only when the strategy declares a fixed target —
        # signal-exit strategies (turtle/connors) pass target_rr=None
        if decision.target_rr is not None and decision.target_rr < r.min_rr_per_trade:
            return RiskDecision(False, reason=f"declared reward {decision.target_rr:.2f}R < floor {r.min_rr_per_trade:.2f}R")
        # cooldowns are epoch seconds; the (retired) legacy bar-index clock was
        # positional per bar and not comparable across timeframes — epoch time
        # reads coherently from every timeframe's clock
        if bar_epoch is not None and bar_epoch < self.cooldowns.get(spec.symbol, 0.0):
            return RiskDecision(False, reason="cooldown after recent stop-out")

        qty = self.size_position(equity, decision.price, decision.stop_distance, spec.kind,
                                 risk_fraction=self.risk_fraction(spec.symbol))
        if qty <= 0:
            return RiskDecision(False, reason="position size rounds to zero (min notional)")
        # gross leverage gate (audit Fix 2.2-lite): total open notional + this
        # entry must stay under max_gross_leverage x equity. The bound used to
        # be only implicit (25% per position x max 4 positions); explicit, it
        # is enforced as ONE cap across mixed timeframes/books and survives
        # any future change to either of the two factors that implied it.
        # equity <= 0 cannot be sensibly levered either way — skip rather than
        # divide by zero (an engine with zero equity has bigger problems).
        total_gross = open_gross_notional + qty * decision.price
        if equity > 0 and total_gross > r.max_gross_leverage * equity:
            return RiskDecision(False, reason=f"gross notional {total_gross:.0f} "
                                             f"({total_gross / equity:.1f}x equity) > "
                                             f"cap {r.max_gross_leverage:.0f}x equity "
                                             f"{equity:.0f} (gross leverage gate)")
        return RiskDecision(True, qty=qty, reason=f"{decision.action} {qty:.6g} @ {decision.price:.6g}")

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
