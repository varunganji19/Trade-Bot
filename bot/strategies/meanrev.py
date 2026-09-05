"""
Connors Mean Reversion — RSI(2) pullback with long-term trend filter (Larry Connors).

Entry:  long when close is above the EMA(200) (the filter that pushes historical
        win rates to 75%+) and RSI(2) < 10 (deep short-term pullback); short mirrored.
Exit:   snapback — RSI(2) > 65 (long) or close above EMA(5); plus a time stop.
Stop:   3 x ATR(14). Connors ran this without a hard stop; we add one because
        unbounded loss is not acceptable in an autonomous bot (negative-skew
        profile is the documented weakness of this strategy).
"""
from __future__ import annotations

from .base import BaseStrategy, Signal


class ConnorsMeanReversion(BaseStrategy):
    name = "connors_meanrev"
    # Connors' RSI-2 evidence is on DAILY bars — 1d is the strategy's home
    # timeframe; 4h was only the closest analogue our infrastructure traded
    # before 1d support was enabled. 1h churn destroyed the edge (BACKTESTS.md).
    preferred_timeframes = ("4h", "1d")

    def evaluate(self, df, i: int) -> Signal:
        p = self.p
        if i < p.mr_trend_ema + 5:
            return Signal(self.name, "FLAT", 0.0, rationale="warming up (needs EMA200)")

        close = self._at(df, "close", i)
        rsi2 = self._at(df, "rsi2", i)
        ema200 = self._at(df, "ema200", i)
        atr_ = self._at(df, "atr", i)
        if not all(self._ok(v) for v in (close, rsi2, ema200, atr_)) or atr_ <= 0:
            return Signal(self.name, "FLAT", 0.0, rationale="indicators not ready")

        if close > ema200:
            # longs only in DEEP bull regimes: measured on BTC 4h, pullback buys
            # just above the EMA200 lost -0.3%/trade; >10% above it they won
            # (the classic Connors "buy dips in strong bulls" profile). n is
            # small either way — see BACKTESTS.md.
            if close < ema200 * 1.10:
                return Signal(self.name, "FLAT", 0.0,
                              rationale=f"uptrend too shallow ({(close/ema200-1)*100:.1f}% above "
                                        f"EMA200 < 10% — pullback buys here measured negative)")
            if rsi2 < p.mr_rsi_buy_below:
                conf = self._clip_conf(0.50 + (p.mr_rsi_buy_below - rsi2) / 20.0)
                return Signal(
                    self.name, "LONG", conf,
                    stop_distance=p.mr_stop_atr * atr_,
                    rationale=(f"RSI(2) pullback to {rsi2:.1f} (< {p.mr_rsi_buy_below:.0f}) inside a "
                               f"deep bull regime ({(close/ema200-1)*100:.0f}% above EMA200) — "
                               f"classic Connors RSI-2 buy setup"),
                    meta={"rsi2": rsi2, "trend": "up", "dist_to_ema200_pct": (close / ema200 - 1) * 100},
                )
            return Signal(self.name, "FLAT", 0.0, rationale=f"deep bull but no pullback (RSI2 {rsi2:.1f})")

        if close < ema200:
            if rsi2 > p.mr_rsi_sell_above:
                conf = self._clip_conf(0.50 + (rsi2 - p.mr_rsi_sell_above) / 20.0)
                return Signal(
                    self.name, "SHORT", conf,
                    stop_distance=p.mr_stop_atr * atr_,
                    rationale=(f"RSI(2) spike to {rsi2:.1f} (> {p.mr_rsi_sell_above:.0f}) inside a "
                               f"downtrend (close < EMA200) — classic Connors RSI-2 short setup"),
                    meta={"rsi2": rsi2, "trend": "down", "dist_to_ema200_pct": (close / ema200 - 1) * 100},
                )
            return Signal(self.name, "FLAT", 0.0, rationale=f"downtrend but no spike (RSI2 {rsi2:.1f})")

        return Signal(self.name, "FLAT", 0.0, rationale="price pinned to EMA200 — no edge")

    def check_exit(self, df, i: int, position):
        p = self.p
        close = self._at(df, "close", i)
        rsi2 = self._at(df, "rsi2", i)
        ema5 = self._at(df, "ema5", i)

        if position.side == "long":
            if self._ok(rsi2) and rsi2 > p.mr_exit_long_rsi:
                return f"RSI(2) snapback to {rsi2:.1f}", None
            if self._ok(ema5) and self._ok(close) and close > ema5:
                return "close back above EMA(5)", None
        else:
            if self._ok(rsi2) and rsi2 < p.mr_exit_short_rsi:
                return f"RSI(2) reset to {rsi2:.1f}", None
            if self._ok(ema5) and self._ok(close) and close < ema5:
                return "close back below EMA(5)", None

        if position.bars_held >= p.mr_time_stop_bars:
            return f"time stop ({p.mr_time_stop_bars} bars)", None
        return None, None
