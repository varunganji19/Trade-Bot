"""
Time-Series Momentum — India (single-symbol absolute momentum, long-only).

Grounding (all three on Indian equity data):
- "Momentum in Indian Equity Markets: Positive Convexity and Positive Alpha"
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3345280
- "Implementing a Systematic Long-only Momentum Strategy: Evidence From India"
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3510433
- "The 52-Week High Effect and Momentum Investing: Evidence from India"
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4587697

ARCHITECTURAL NOTE: those papers rank a WHOLE stock universe cross-sectionally
every month and hold a top-decile portfolio. This bot's BaseStrategy is a
single-symbol time-series evaluator — a different shape. Per HISTORY.md
Milestone C1, the MVP is path (a): a TIME-SERIES/absolute momentum variant
scored per symbol (fits BaseStrategy, no new infrastructure); the papers'
6-12 month momentum horizon becomes a trailing-N-bar return threshold, and the
top decile of the cross-section becomes an absolute +8% gate. Path (b) — true
cross-sectional decile ranking — needs a portfolio runner and is deliberately
deferred until (a)'s measured results justify it. Long-only: NSE cash
equities have no practical short leg on the books this bot trades, so no
SHORT signal is ever emitted regardless of downside momentum.

Entry (long only): all three must hold —
  1. momentum:   trailing N-bar return > tsmom_min_ret (+8% over the papers'
                 6-12 month horizon; the decile portfolio's top cut is the
                 single-symbol absolute analogue),
  2. anchoring:  close within tsmom_52w_prox of the rolling 1-year high —
                 the 52w-high paper's effect is anchor-proximity, and entry
                 far below the anchor is where momentum crashes live,
  3. structure:  close > EMA200 — the same trend-structure discipline turtle
                 applies to its breakouts (momentum without structure is the
                 classic bear-rally trap).
Exit: long exits when the momentum regime decays — trailing return falls
below tsmom_exit_ret (default 0.0: the regime flipped non-positive), OR close
breaks the prior-10-bar low channel (turtle's prior-channel convention,
shift=1 — never the decision bar's own low), OR close loses the EMA200 (the
structure that justified the trade is gone). First one that fires wins.
Stop: tsmom_stop_atr x ATR — wide, for a slow strategy. No fixed target: a
signal-exit strategy like turtle (the exit is the regime flip, not a TP).

Causality: the trailing return and the 52-week rolling high are computed from
df.iloc[:i+1] (the truncated frame the caller hands us), so no bar after i
can ever enter the calculation — the same guarantee the rvol/halflife
indicators carry by construction.
"""
from __future__ import annotations

from .base import BaseStrategy, Signal


class TimeSeriesMomentum(BaseStrategy):
    name = "ts_momentum"
    # momentum needs lookback: 240 1h bars (~10 months of NSE sessions) on 1h,
    # 4h as the coarser alternative. Short timeframes churn a slow strategy
    # into the cost model (see BACKTESTS.md cost studies).
    preferred_timeframes = ("1h", "4h")

    def _trailing_ret(self, df, i: int) -> float:
        """Return over the trailing tsmom_lookback bars ending AT i (close_i /
        close_{i-lookback} - 1). Computed on the caller's frame directly: the
        full-frame indicator columns stop at add_all_indicators' menu, and the
        backtester hands evaluate() the COMPLETE frame — a value precomputed
        from all n bars would still be causal (rolling ops only look back),
        but this keeps the window read obviously and locally causal."""
        p = self.p
        j = i - p.tsmom_lookback
        if j < 0:
            return float("nan")
        now, then = float(df["close"].iloc[i]), float(df["close"].iloc[j])
        if then <= 0:
            return float("nan")
        return now / then - 1.0

    def _high_prox(self, df, i: int) -> float:
        """close / rolling max of HIGH over the trailing tsmom_52w_bars bars,
        1.0 = at the 1-year high. Computed on the truncated slice iloc[:i+1]
        so the decision bar i is the LAST bar of the window — rolling ops are
        causal anyway, but slicing makes that explicit and testable.

        ADAPTIVE ANCHOR (P0 fix): the papers' 2450-bar 1-year window exceeded
        every live frame (~400 bars), freezing the strategy in permanent
        warmup. The anchor now uses whatever history exists —
        min(tsmom_52w_bars, i+1) bars — once the evaluate() warmup below is
        satisfied. On short history this is an N-bar-high proxy, weaker than
        the papers' true 1-year anchor; on full history it IS the 1-year
        high. NaN only when the slice is degenerate (never for short
        history alone)."""
        p = self.p
        window = min(p.tsmom_52w_bars, i + 1)
        lo = i - window + 1
        if lo < 0 or window <= 0:
            return float("nan")
        hi = float(df["high"].iloc[lo:i + 1].max())
        if not self._ok(hi) or hi <= 0:
            return float("nan")
        return float(df["close"].iloc[i]) / hi

    def evaluate(self, df, i: int) -> Signal:
        p = self.p
        # ADAPTIVE WARMUP (P0 fix): the old gate max(lookback, 52w_bars, 30)+2
        # = 2452 bars could never clear on live ~400-bar frames, so the
        # strategy was permanently FLAT. The 52w anchor is now a partial
        # window (see _high_prox), so the hard gate only covers what is
        # TRULY insufficient: the trailing-return lookback plus indicator
        # readiness. FLAT "warming up" fires only below that line.
        warmup = max(p.tsmom_lookback, 30) + 2
        if i < warmup:
            return Signal(self.name, "FLAT", 0.0, rationale="warming up")

        close = self._at(df, "close", i)
        atr_ = self._at(df, "atr", i)
        ema200 = self._at(df, "ema200", i)
        if not all(self._ok(v) for v in (close, atr_, ema200)) or atr_ <= 0:
            return Signal(self.name, "FLAT", 0.0, rationale="indicators not ready")

        mom = self._trailing_ret(df, i)
        prox = self._high_prox(df, i)
        if not self._ok(mom):
            return Signal(self.name, "FLAT", 0.0, rationale="lookback incomplete")
        if not self._ok(prox):
            return Signal(self.name, "FLAT", 0.0, rationale="52w anchor not computable (degenerate window)")

        # 1. momentum filter — the decile gate's single-symbol analogue
        if mom <= p.tsmom_min_ret:
            return Signal(self.name, "FLAT", 0.0,
                          rationale=f"momentum below threshold (trailing return {mom * 100:.1f}% "
                                    f"<= {p.tsmom_min_ret * 100:.0f}%)")

        # 2. 52-week-high anchor proximity (paper 3): entry far below the
        # 1-year high is where momentum crashes cluster
        if prox < 1.0 - p.tsmom_52w_prox:
            return Signal(self.name, "FLAT", 0.0,
                          rationale=f"too far below the 1-year high ({(1 - prox) * 100:.1f}% "
                                    f"> {p.tsmom_52w_prox * 100:.0f}% — 52w-high anchoring gate)")

        # 3. trend structure — same discipline as turtle's breakout filter
        if close < ema200:
            return Signal(self.name, "FLAT", 0.0,
                          rationale=f"momentum ok but below EMA200 (close {close:.6g} < "
                                    f"{ema200:.6g}) — the structure that justifies the trade is missing")

        conf = self._clip_conf(0.50
                               + 0.25 * min((mom - p.tsmom_min_ret) / 0.25, 1.0)
                               + 0.15 * min((prox - (1.0 - p.tsmom_52w_prox)) / p.tsmom_52w_prox, 1.0))
        return Signal(
            self.name, "LONG", conf,
            stop_distance=p.tsmom_stop_atr * atr_,
            rationale=(f"time-series momentum: trailing {p.tsmom_lookback}-bar return "
                       f"{mom * 100:+.1f}% (> {p.tsmom_min_ret * 100:.0f}%), within "
                       f"{(1 - prox) * 100:.1f}% of the 1-year high, above EMA200"),
            meta={"lookback_ret": round(mom, 4), "high_prox": round(prox, 4)},
        )

    def check_exit(self, df, i: int, position):
        """First of the three decay conditions to fire wins. The short side
        does not exist (NSE cash equities: long-only by design) — a short
        position, if one ever arrived, still gets the mirrored channel exit
        rather than being silently ignored."""
        p = self.p
        close = self._at(df, "close", i)
        ema200 = self._at(df, "ema200", i)
        if not self._ok(close):
            return None, None

        if position.side == "long":
            # 1. regime flip: trailing momentum decayed to/below the exit line
            mom = self._trailing_ret(df, i)
            if self._ok(mom) and mom < p.tsmom_exit_ret:
                return (f"momentum regime decayed (trailing return {mom * 100:.1f}% "
                        f"< {p.tsmom_exit_ret * 100:.0f}%)", None)
            # 2. prior 10-bar channel break (shift=1 — the decision bar's own
            # low can never be part of its own exit channel; same convention
            # and same history as turtle's exit fix)
            exit_low = self._at(df, "don_exit_low", i, shift=1)
            if self._ok(exit_low) and close < exit_low:
                return f"prior-{p.turtle_exit_period}-bar low channel break", None
            # 3. structure loss: the EMA200 that justified the trade is gone
            if self._ok(ema200) and close < ema200:
                return "close lost EMA200 structure", None
        else:
            exit_up = self._at(df, "don_exit_up", i, shift=1)
            if self._ok(exit_up) and close > exit_up:
                return f"prior-{p.turtle_exit_period}-bar high channel break", None
        return None, None
