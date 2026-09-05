"""
VWAP Scalper — intraday momentum with VWAP + volume confirmation.

Combines three evidenced intraday ideas:
1. VWAP reclaim/loss: institutions benchmark execution to VWAP, so price
   reclaiming (or losing) VWAP with momentum is a structurally-supported entry.
2. Rolling range breakout/breakdown — the 24/7-market analogue of the Opening
   Range Breakout quantified by Zarattini & Aziz (2023).
3. Volume confirmation (multiple of average volume), from the team's earlier
   VWAP-intraday prototype (since retired).

Exits: VWAP cross-down, breakeven trail after +1R, time stop (scalps should
not turn into swing trades), hard stop 1.2 x ATR, target 1.8R.

On volume-less feeds (forex from Yahoo) the volume gate auto-passes.
"""
from __future__ import annotations

import math

from .base import BaseStrategy, Signal


class VWAPScalper(BaseStrategy):
    name = "vwap_scalper"
    # 15m preferred: measured 12-bar forward edge (~+0.1%) vs crypto round-trip
    # costs (~0.3%) makes 5m scalping structurally unprofitable (BACKTESTS.md:
    # -28..-31% over 30 days on majors). 5m stays enabled only by explicit user
    # decision — revert this tuple to ("15m",) to pull it back.
    preferred_timeframes = ("15m", "5m")

    # ------------------------------------------------------------- helpers
    def _context(self, df, i: int):
        p = self.p
        ctx = {
            "close": self._at(df, "close", i),
            "atr": self._at(df, "atr", i),
            "vwap": self._at(df, "vwap_roll", i),
            "ema_f": self._at(df, "ema9", i),
            "ema_s": self._at(df, "ema21", i),
            "vol_r": self._at(df, "vol_ratio", i),
            "rsi3": self._at(df, "rsi3", i),
            "adx": self._at(df, "adx", i),
        }
        j = i - 1
        if j - p.scalper_range_period >= 0:
            ctx["range_high"] = float(df["high"].iloc[j - p.scalper_range_period + 1: j + 1].max())
            ctx["range_low"] = float(df["low"].iloc[j - p.scalper_range_period + 1: j + 1].min())
        else:
            ctx["range_high"] = ctx["range_low"] = float("nan")
        return ctx

    @staticmethod
    def _reclaimed_2bars(df, i: int, side: str) -> bool:
        """Price crossed VWAP (up for longs / down for shorts) within last 2 bars."""
        for shift in (1, 2):
            prev_close = VWAPScalper._at(df, "close", i, shift)
            prev_vwap = VWAPScalper._at(df, "vwap_roll", i, shift)
            if not VWAPScalper._ok(prev_close) or not VWAPScalper._ok(prev_vwap):
                continue
            if side == "long" and prev_close <= prev_vwap:
                return True
            if side == "short" and prev_close >= prev_vwap:
                return True
        return False

    def _confidence(self, base: float, ctx: dict, extra: float = 0.0) -> float:
        p = self.p
        bump = 0.0
        if ctx["vol_r"] and not math.isnan(ctx["vol_r"]):
            bump += min(max(ctx["vol_r"] - p.scalper_vol_ratio_min, 0.0) * 0.5, 0.15)
        return self._clip_conf(base + bump + extra)

    def _vol_note(self, ctx: dict) -> str:
        return (f"volume x{ctx['vol_r']:.2f}"
                if ctx["vol_r"] and not math.isnan(ctx["vol_r"]) else "volume n/a")

    # ------------------------------------------------------------- entries
    def _long_signal(self, df, i: int) -> Signal:
        p = self.p
        ctx = self._context(df, i)
        ctx["ema200"] = self._at(df, "ema200", i)
        if not all(self._ok(ctx[k]) for k in ("close", "atr", "vwap", "ema_f", "ema_s")) or ctx["atr"] <= 0:
            return Signal(self.name, "FLAT", 0.0, rationale="indicators not ready")

        momentum = ctx["ema_f"] > ctx["ema_s"]
        vol_ok = (not self._ok(ctx["vol_r"])) or ctx["vol_r"] >= p.scalper_vol_ratio_min
        reclaim = ctx["close"] > ctx["vwap"] and self._reclaimed_2bars(df, i, "long")
        breakout = (self._ok(ctx["range_high"]) and ctx["close"] > ctx["range_high"])
        trend_ok = (not p.scalper_trend_filter) or (
            self._ok(ctx["ema200"]) and ctx["close"] > ctx["ema200"])
        adx_ok = (not self._ok(ctx["adx"])) or ctx["adx"] >= p.scalper_adx_min

        base_conf = 0.0
        if trend_ok and vol_ok and adx_ok:
            if reclaim and momentum and self._ok(ctx["rsi3"]) and ctx["rsi3"] >= 45:
                base_conf = 0.55
            if breakout and momentum:
                base_conf = max(base_conf, 0.50 + (0.15 if reclaim else 0.0))
        if base_conf <= 0.0:
            reason = "no aligned VWAP reclaim / range breakout"
            if not trend_ok:
                reason = "below EMA200 or ADX too low (chop filtered)"
            return Signal(self.name, "FLAT", 0.0,
                          rationale=f"{reason} (trend {'up' if momentum else 'down'}, {self._vol_note(ctx)})")
        conf = self._confidence(base_conf, ctx)
        if conf < p.scalper_min_confidence:
            return Signal(self.name, "FLAT", 0.0,
                          rationale=f"setup present but conviction {conf:.2f} below {p.scalper_min_confidence:.2f} floor")

        mode = "vwap_reclaim" if reclaim else "range_breakout"
        detail = (f"reclaimed rolling VWAP {ctx['vwap']:.6g}" if mode == "vwap_reclaim"
                  else f"broke prior {p.scalper_range_period}-bar high {ctx['range_high']:.6g}")
        return Signal(
            self.name, "LONG", conf,
            stop_distance=p.scalper_stop_atr * ctx["atr"],
            target_rr=p.scalper_target_rr,
            rationale=(f"{mode} long: close {ctx['close']:.6g} {detail}, "
                       f"EMA9>EMA21 momentum, {self._vol_note(ctx)}"),
            meta={"mode": mode, "vwap": ctx["vwap"],
                  "vol_ratio": ctx["vol_r"] if self._ok(ctx["vol_r"]) else None},
        )

    def _short_signal(self, df, i: int) -> Signal:
        p = self.p
        ctx = self._context(df, i)
        ctx["ema200"] = self._at(df, "ema200", i)
        if not all(self._ok(ctx[k]) for k in ("close", "atr", "vwap", "ema_f", "ema_s")) or ctx["atr"] <= 0:
            return Signal(self.name, "FLAT", 0.0, rationale="indicators not ready")

        momentum = ctx["ema_f"] < ctx["ema_s"]
        vol_ok = (not self._ok(ctx["vol_r"])) or ctx["vol_r"] >= p.scalper_vol_ratio_min
        loss = ctx["close"] < ctx["vwap"] and self._reclaimed_2bars(df, i, "short")
        breakdown = (self._ok(ctx["range_low"]) and ctx["close"] < ctx["range_low"])
        trend_ok = (not p.scalper_trend_filter) or (
            self._ok(ctx["ema200"]) and ctx["close"] < ctx["ema200"])
        adx_ok = (not self._ok(ctx["adx"])) or ctx["adx"] >= p.scalper_adx_min

        base_conf = 0.0
        if trend_ok and vol_ok and adx_ok:
            if loss and momentum and self._ok(ctx["rsi3"]) and ctx["rsi3"] <= 55:
                base_conf = 0.55
            if breakdown and momentum:
                base_conf = max(base_conf, 0.50 + (0.15 if loss else 0.0))
        if base_conf <= 0.0:
            reason = "no aligned VWAP loss / range breakdown"
            if not trend_ok:
                reason = "above EMA200 or ADX too low (chop filtered)"
            return Signal(self.name, "FLAT", 0.0,
                          rationale=f"{reason} (trend {'down' if momentum else 'up'}, {self._vol_note(ctx)})")
        conf = self._confidence(base_conf, ctx)
        # shorts fight crypto's upward drift: demand extra conviction
        if conf < p.scalper_short_min_confidence:
            return Signal(self.name, "FLAT", 0.0,
                          rationale=f"short setup present but conviction {conf:.2f} below "
                                    f"{p.scalper_short_min_confidence:.2f} short floor")

        mode = "vwap_loss" if loss else "range_breakdown"
        detail = (f"lost rolling VWAP {ctx['vwap']:.6g}" if mode == "vwap_loss"
                  else f"broke prior {p.scalper_range_period}-bar low {ctx['range_low']:.6g}")
        return Signal(
            self.name, "SHORT", conf,
            stop_distance=p.scalper_stop_atr * ctx["atr"],
            target_rr=p.scalper_target_rr,
            rationale=(f"{mode} short: close {ctx['close']:.6g} {detail}, "
                       f"EMA9<EMA21 momentum, {self._vol_note(ctx)}"),
            meta={"mode": mode, "vwap": ctx["vwap"],
                  "vol_ratio": ctx["vol_r"] if self._ok(ctx["vol_r"]) else None},
        )

    # ------------------------------------------------------------- interface
    def evaluate(self, df, i: int) -> Signal:
        """Strongest of the two sides at bar i."""
        # ATR is computed with turtle_atr_period (see indicators.add_all_indicators),
        # so the warm-up must cover that window too.
        if i < max(self.p.scalper_ema_slow, self.p.scalper_range_period, self.p.turtle_atr_period) + 3:
            return Signal(self.name, "FLAT", 0.0, rationale="warming up")
        long_sig = self._long_signal(df, i)
        short_sig = self._short_signal(df, i)
        return max([long_sig, short_sig], key=lambda s: s.confidence)

    def check_exit(self, df, i: int, position):
        p = self.p
        close = self._at(df, "close", i)
        vwap_ = self._at(df, "vwap_roll", i)
        atr_ = self._at(df, "atr", i)

        new_stop = None
        risk = position.risk_per_unit
        if risk and risk > 0:
            r_now = (close - position.entry_price) / risk if position.side == "long" \
                else (position.entry_price - close) / risk
            if r_now >= p.scalper_break_even_rr:
                be = position.entry_price
                if position.side == "long" and (position.stop is None or position.stop < be):
                    new_stop = be
                elif position.side == "short" and (position.stop is None or position.stop > be):
                    new_stop = be

        # buffered VWAP exit: a single noisy close across VWAP must not eject the
        # trade — require N consecutive closes beyond VWAP ± buffer (ATR-scaled).
        if self._ok(vwap_) and self._ok(close) and self._ok(atr_):
            buffer = p.scalper_vwap_buffer_atr * atr_
            def _beyond(shift: int, side: str) -> bool:
                c = self._at(df, "close", i, shift)
                v = self._at(df, "vwap_roll", i, shift)
                if not self._ok(c) or not self._ok(v):
                    return False
                return c < v - buffer if side == "long" else c > v + buffer
            if position.side == "long" and all(
                    _beyond(s, "long") for s in range(p.scalper_exit_confirm_bars)):
                return "VWAP cross-down (confirmed)", new_stop
            if position.side == "short" and all(
                    _beyond(s, "short") for s in range(p.scalper_exit_confirm_bars)):
                return "VWAP cross-up (confirmed)", new_stop

        if position.bars_held >= p.scalper_time_stop_bars:
            return f"time stop ({p.scalper_time_stop_bars} bars)", new_stop
        return None, new_stop
