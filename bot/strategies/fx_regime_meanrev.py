"""
FX Regime-Conditioned Mean Reversion — single-pair z-score deviation, gated
by the AR(1) reversion regime of the deviation itself (1h bars).

Grounding: "A Regime-Conditioned Statistical Mean Reversion Framework for
Intraday FX Markets" — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6087107
The paper's core claim: intraday FX mean reversion only pays when the
deviation series is actually IN a reverting regime — fading every stretch in
a random-walk regime bleeds the spread. This strategy is the single-pair MVP
of that framework: one instrument, its own deviation, regime-gated.

SCOPE FLAG: the paper's framework covers multi-instrument statistical mean
reversion; true pairs/cointegration trading (e.g. EUR/USD vs GBP/USD traded
as one position — SSRN 4771108, "Cointegration-Based Strategies in Forex
Pairs Trading") needs two instruments managed as a single position. That is
the STRETCH GOAL, deliberately OUT of scope for this milestone per the
project plan — not built, only flagged here.

Entry:  z = (log(close/EMA20) - mean) / sigma over a rolling fxmr_z_window
        (default 100 bars), computed causally on the bars up to and including
        the decision bar. Long when z < -fxmr_z_entry (stretched below the
        mean), short when z > +fxmr_z_entry — forex allows both sides (unlike
        the India equity books). Gated by:
        1. REGIME GATE (the paper's regime conditioning): the AR(1)/OU
           half-life of the same deviation window (the repo's Chan
           machinery, indicators.halflife_ar1 — REUSED, not reinvented) must
           be finite and <= fxmr_halflife_max (12 bars, the same discipline
           connors uses: reversion must happen inside the strategy's own time
           horizon). phi >= 1 or a NaN fit (random-walk/explosive regime)
           REFUSES — there is no reversion to trade.
        2. VOL FLOOR: ATR >= fxmr_min_atr_pct x price. Dead-flat markets give
           the spread the whole edge; insane-vol regimes the stop can't
           manage.
Stop:   fxmr_stop_atr x ATR (1.5 on 1h FX bars).
TARGET: fxmr_target_rr = 1.5, a FIXED declared R. The z-snapback IS the
        economic target, but risk.approve's reward floor requires any
        DECLARED target to be >= 1.2R, and a signal exit (target_rr=None)
        would silently claim no reward while still paying full market-leg
        costs on the exit. Declaring 1.5R keeps the bracket honest (a
        modest, risk-floor-clearing multiple) and lets the acceptance run
        measure what the snapback actually delivers on N-bar horizons.
Exit:   z crosses back through -/+fxmr_z_exit toward the mean (the snapback
        complete) OR the half-life gate RE-REFUSES while holding (the
        regime died — mean reversion's precondition is gone; exit rather
        than wait for a reversion that no longer has a time scale) OR the
        1.5R target OR the hard stop OR the time stop (fxmr_time_stop_bars
        = 24 bars ~ one trading day: intraday reversion must not become a
        position trade).

FOREX COST REALITY: the round trip here is ~0.06% of notional (taker
0.02% + slippage 0.01% per market leg, both legs) — a 2-sigma snapback edge
on 1h EUR/USD-style bars must clear that bar to be worth trading. The
pinned-window acceptance run in BACKTESTS.md is the honest measurement; no
parameter was tuned to pass it.
"""
from __future__ import annotations

import math

from .base import BaseStrategy, Signal


class FXRegimeMeanRev(BaseStrategy):
    name = "fx_regime_meanrev"
    # 1h: matches the paper's intraday framing and the bot's forex books
    # (EURUSD=X / GBPUSD=X trade 1h specs).
    preferred_timeframes = ("1h",)

    # ---- causal deviation statistics -----------------------------------
    def _z_now(self, df, i: int) -> float:
        """z-score of the CURRENT deviation log(close/ema20) against its own
        rolling fxmr_z_window history — computed causally on
        df.iloc[i-window+1 : i+1], mirroring how meanrev's halflife column is
        built (rolling ops over closed bars only; the truncated-frame test
        pins this). NaN when the window is incomplete or degenerate."""
        p = self.p
        close = self._at(df, "close", i)
        ema20 = self._at(df, "ema20", i)
        if not self._ok(close) or not self._ok(ema20) or close <= 0:
            return float("nan")
        dev = math.log(close / ema20)
        lo = i - p.fxmr_z_window + 1
        if lo < 0:
            return float("nan")
        closes = df["close"].iloc[lo: i + 1].astype(float).to_numpy()
        emas = df["ema20"].iloc[lo: i + 1].astype(float).to_numpy()
        if len(closes) < p.fxmr_z_window:
            return float("nan")
        import numpy as np
        devs = np.log(closes / emas)
        mean = float(devs.mean())
        sigma = float(devs.std())
        if not math.isfinite(sigma) or sigma <= 0:
            return float("nan")
        return (dev - mean) / sigma

    # ---- regime gate ---------------------------------------------------
    def _hl_refusal(self, hl) -> str | None:
        """None = reverting regime, else the refusal reason. Unlike connors
        (where the NaN-auto-pass convention exists to not freeze warmup
        entries behind a slow warmup column), this strategy's whole premise
        is the paper's regime conditioning — a NaN or non-finite half-life
        means the reversion time scale is UNKNOWN, and entering a mean
        reversion without a measured reversion time scale is the exact
        random-walk-regime bleed the paper warns about. NaN REFUSES here.
        The knob-off escape hatch (fxmr_halflife_max = 0) stays, for A/B."""
        p = self.p
        if p.fxmr_halflife_max <= 0:
            return None
        if not self._ok(hl):
            return "regime unknown (AR(1) half-life NaN — no measured reversion time scale)"
        if math.isinf(hl):
            return "no reversion regime (AR(1) half-life inf — phi >= 1, deviation diverging)"
        if hl > p.fxmr_halflife_max:
            return (f"regime too slow (half-life {hl:.0f}b > "
                    f"{p.fxmr_halflife_max:.0f}b horizon)")
        return None

    def evaluate(self, df, i: int) -> Signal:
        p = self.p
        if i < max(p.fxmr_z_window, 20, 14) + 2:
            return Signal(self.name, "FLAT", 0.0,
                          rationale=f"warming up (needs fxmr_z_window={p.fxmr_z_window} bars of deviation history)")

        close = self._at(df, "close", i)
        atr_ = self._at(df, "atr", i)
        if not self._ok(close) or not self._ok(atr_) or atr_ <= 0:
            return Signal(self.name, "FLAT", 0.0, rationale="indicators not ready")

        # vol floor: dead-flat (spread eats the whole edge) and insane-vol
        # regimes are both refused
        atr_pct = atr_ / close
        if atr_pct < p.fxmr_min_atr_pct:
            return Signal(self.name, "FLAT", 0.0,
                          rationale=(f"volatility below floor (ATR {atr_pct:.4%} of price < "
                                     f"{p.fxmr_min_atr_pct:.2%} — the spread would eat the edge)"))

        z = self._z_now(df, i)
        if not self._ok(z):
            return Signal(self.name, "FLAT", 0.0,
                          rationale="deviation z-score not ready (window incomplete or degenerate)")

        # REGIME GATE (the paper's core claim): only fade stretches when the
        # deviation has actually been reverting inside our horizon
        hl = self._at(df, "halflife", i)
        hl_refusal = self._hl_refusal(hl)
        if hl_refusal:
            return Signal(self.name, "FLAT", 0.0, rationale=hl_refusal)

        def _entry(side: str, z_val: float) -> Signal:
            stretch = abs(z_val) - p.fxmr_z_entry
            # the gate has already passed on this path (0.10 term); stretch
            # beyond the entry line adds the rest; must be able to clear the
            # 0.55 orchestrator floor
            conf = self._clip_conf(0.50 + 0.30 * min(stretch, 1.0) / 1.0 + 0.10)
            direction = "below" if side == "LONG" else "above"
            return Signal(
                self.name, side, conf,
                stop_distance=p.fxmr_stop_atr * atr_,
                target_rr=p.fxmr_target_rr,
                rationale=(f"z-score {z_val:+.2f} stretched {direction} the mean "
                           f"(|z| > {p.fxmr_z_entry:.1f}), reversion regime confirmed "
                           f"(half-life {hl:.1f}b <= {p.fxmr_halflife_max:.0f}b) — "
                           f"regime-conditioned snapback trade"),
                meta={"z": round(z_val, 3), "halflife": round(hl, 1),
                      "atr_pct": round(atr_pct, 5)},
            )

        if z < -p.fxmr_z_entry:
            return _entry("LONG", z)
        if z > p.fxmr_z_entry:
            return _entry("SHORT", z)

        return Signal(self.name, "FLAT", 0.0,
                      rationale=f"inside the z-band ({z:+.2f} within ±{p.fxmr_z_entry:.1f}) — no stretch to fade")

    def check_exit(self, df, i: int, position):
        p = self.p
        z = self._z_now(df, i)

        # snapback complete: z crossed back through the exit band toward the
        # mean, sign-aware per side
        if self._ok(z):
            if position.side == "long" and z >= -p.fxmr_z_exit:
                return f"z-score snapback to {z:+.2f} (>= -{p.fxmr_z_exit:.1f})", None
            if position.side == "short" and z <= p.fxmr_z_exit:
                return f"z-score snapback to {z:+.2f} (<= +{p.fxmr_z_exit:.1f})", None

        # regime died while holding: the half-life gate re-refusing at exit
        # time means mean reversion's precondition is gone — exit rather
        # than wait for a reversion with no time scale
        hl = self._at(df, "halflife", i)
        if self._hl_refusal(hl):
            return (f"reversion regime died while holding (half-life now "
                    f"{hl if self._ok(hl) else 'NaN'})", None)

        if position.bars_held >= p.fxmr_time_stop_bars:
            return f"time stop ({p.fxmr_time_stop_bars} bars)", None
        return None, None
