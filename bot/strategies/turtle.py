"""
Turtle Trend — Donchian channel breakout (Donchian / Richard Dennis's Turtles).

Entry:  close breaks above the prior 20-bar high (long) / below the prior 20-bar
        low (short), gated by (a) an ADX regime filter (per the SSRN study that
        improved breakouts with a volatility-regime filter) and (b) EMA
        structure alignment — longs only above EMA50>EMA200, shorts only below
        EMA50<EMA200. ADX alone cannot tell a wide-swinging range from a trend
        (breakouts at range edges spike ADX too); the structure filter removes
        those.
Exit:   close breaks the opposite 10-bar channel (the Turtle's S1 exit).
Stop:   2 x ATR(14) — the Turtle "2N" rule.
Sizing note: the classic Turtles risked 2%/trade; our RiskManager uses 1%
because crypto/forex volatility is higher than 1980s commodities.
"""
from __future__ import annotations

from .base import BaseStrategy, Signal


class TurtleTrend(BaseStrategy):
    name = "turtle_trend"
    label = "Turtle Trend (Donchian breakout)"
    # validated on 1h (BACKTESTS.md): on 4h the same rules churn stops in
    # violent chops; mean reversion owns 4h, the scalper owns 15m
    preferred_timeframes = ("1h",)

    def evaluate(self, df, i: int) -> Signal:
        p = self.p
        if i < max(p.turtle_entry_period, p.turtle_atr_period, 30) + 2:
            return Signal(self.name, "FLAT", 0.0, rationale="warming up")

        close = self._at(df, "close", i)
        atr_ = self._at(df, "atr", i)
        adx_ = self._at(df, "adx", i)
        if not self._ok(atr_) or atr_ <= 0 or not self._ok(adx_):
            return Signal(self.name, "FLAT", 0.0, rationale="indicators not ready")

        if adx_ < p.turtle_adx_min:
            return Signal(self.name, "FLAT", 0.0,
                          rationale=f"no trend regime (ADX {adx_:.1f} < {p.turtle_adx_min:.0f})")

        up_prev = self._at(df, "don_up20", i, shift=1)   # prior 20-bar high (no lookahead)
        low_prev = self._at(df, "don_low20", i, shift=1)  # prior 20-bar low

        if self._ok(up_prev) and close > up_prev:
            ema50 = self._at(df, "ema50", i)
            ema200 = self._at(df, "ema200", i)
            if self._ok(ema50) and self._ok(ema200) and not (close > ema50 > ema200):
                return Signal(self.name, "FLAT", 0.0,
                              rationale=f"breakout up but EMA structure not aligned (close {close:.6g}, "
                                        f"EMA50 {ema50:.6g}, EMA200 {ema200:.6g}) — likely a range edge, not a trend")
            strength = (close - up_prev) / atr_
            conf = self._clip_conf(0.50 + 0.25 * min(strength, 1.0) + 0.15 * min((adx_ - 20) / 30.0, 1.0))
            return Signal(
                self.name, "LONG", conf,
                stop_distance=p.turtle_stop_atr * atr_,
                rationale=(f"Donchian-20 breakout: close {close:.6g} broke prior 20-bar high "
                           f"{up_prev:.6g} ({strength:.2f} ATRs through), ADX {adx_:.1f} confirms trend regime, "
                           f"EMA50>EMA200 aligned"),
                meta={"breakout": "up", "adx": adx_, "strength_atr": strength},
            )

        if self._ok(low_prev) and close < low_prev:
            ema50 = self._at(df, "ema50", i)
            ema200 = self._at(df, "ema200", i)
            if self._ok(ema50) and self._ok(ema200) and not (close < ema50 < ema200):
                return Signal(self.name, "FLAT", 0.0,
                              rationale=f"breakdown but EMA structure not aligned (close {close:.6g}, "
                                        f"EMA50 {ema50:.6g}, EMA200 {ema200:.6g}) — likely a range edge, not a trend")
            strength = (low_prev - close) / atr_
            conf = self._clip_conf(0.50 + 0.25 * min(strength, 1.0) + 0.15 * min((adx_ - 20) / 30.0, 1.0))
            return Signal(
                self.name, "SHORT", conf,
                stop_distance=p.turtle_stop_atr * atr_,
                rationale=(f"Donchian-20 breakdown: close {close:.6g} broke prior 20-bar low "
                           f"{low_prev:.6g} ({strength:.2f} ATRs through), ADX {adx_:.1f} confirms trend regime, "
                           f"EMA50<EMA200 aligned"),
                meta={"breakout": "down", "adx": adx_, "strength_atr": strength},
            )

        return Signal(self.name, "FLAT", 0.0, rationale=f"inside the 20-bar channel, ADX {adx_:.1f}")

    def check_exit(self, df, i: int, position):
        p = self.p
        close = self._at(df, "close", i)
        if position.side == "long":
            exit_low = self._at(df, "don_exit_low", i)
            if self._ok(exit_low) and close < exit_low:
                return f"donchian-{p.turtle_exit_period} opposite-channel exit", None
        else:
            exit_up = self._at(df, "don_exit_up", i)
            if self._ok(exit_up) and close > exit_up:
                return f"donchian-{p.turtle_exit_period} opposite-channel exit", None
        return None, None
