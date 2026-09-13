"""HFT strategies — high-frequency PAPER strategies on 1m bars.

These run ONLY on the separate high-frequency book (mode='hft', bot/hft/):
same stateless evaluate/check_exit contract as every other strategy, so the
backtester and the live engine execute identical code.

Research grounding (full citations + fee math in HFT.md; strategy scraping
done via the agent-reach channels + web research):

- hft_micro_breakout (taker) — Zarattini & Aziz 2023 (SSRN 4416622) opening-
  range-breakout logic at a 1m cadence: rolling micro-range breakout, 2R
  take-profit, volume confirmation, and a volatility-regime gate (the 2R
  target must clear the taker round trip, so dead-flat minutes are refused).
- hft_exhaustion_fade (maker entry) — Rob Carver (2025) documents mean-
  reversion predictability peaking at 4-8 minute horizons that "struggles to
  overcome trading costs"; we fade volume-spike exhaustion candles with a
  RESTING LIMIT at the exhaustion close (maker leg), exiting on reversion.
- hft_market_maker (maker entry) — Avellaneda & Stoikov (2008) inventory-
  skewed quoting, simplified to a single-position OHLCV loop: quote half-
  width scales with realized 1m volatility (the gamma*sigma^2 term), the
  quote side fades the short-horizon drift, and the bracket inverts the
  swing-trade ratio (small target vs wider inventory stop — hence the HFT
  book's own min_rr floor).

Fee reality (measured by the harness, not assumed): with spot base-tier
fees the 1m taker round trip (~30bp) exceeds nearly every 1m gross edge —
the HFT book therefore models a perp-style tier (maker 2bp / taker 5bp,
configurable HFT_FEE_TIER=spot to measure the difference). Any backtest
gross of fees at this cadence is fiction; the harness always reports both.
"""
from __future__ import annotations

import numpy as np

from .base import BaseStrategy, Signal


def _clv(df, i: int) -> float:
    """Close Location Value in [-1, +1]: where the bar closed inside its
    range (+1 = at the high). The OHLCV proxy for order-flow direction."""
    high = float(df["high"].iloc[i])
    low = float(df["low"].iloc[i])
    close = float(df["close"].iloc[i])
    if high <= low:
        return 0.0
    return (2.0 * close - high - low) / (high - low)


class HFTMicroBreakout(BaseStrategy):
    """Rolling micro-range breakout with a 2R target, volatile regimes only."""
    name = "hft_micro_breakout"
    preferred_timeframes = ("1m",)

    def evaluate(self, df, i: int) -> Signal:
        p = self.p
        close = self._at(df, "close", i)
        atr = self._at(df, "atr", i)
        vol = self._at(df, "vol_ratio", i)
        ema50 = self._at(df, "ema50", i)
        if not all(self._ok(v) for v in (close, atr, ema50)) or atr <= 0:
            return Signal(self.name, "FLAT", 0.0, rationale="warmup")
        # regime gate: in dead-flat minutes the 2R target cannot clear the
        # taker round trip — this gate IS the fee defense (HFT.md §costs)
        if close <= 0 or atr / close < p.hft_bo_atr_min_pct:
            return Signal(self.name, "FLAT", 0.0,
                          rationale=f"ATR {atr / close * 1e4:.1f}bp below "
                                    f"{p.hft_bo_atr_min_pct * 1e4:.1f}bp floor")
        hi = float(df["high"].rolling(p.hft_bo_range).max().shift(1).iloc[i])
        lo = float(df["low"].rolling(p.hft_bo_range).min().shift(1).iloc[i])
        vol_ok = (not self._ok(vol)) or vol >= p.hft_bo_vol_ratio_min
        if close > hi and close > ema50 and vol_ok:
            strength = min(1.0, (close - hi) / atr)
            conf = self._clip_conf(0.55 + 0.25 * strength + 0.10)
            return Signal(self.name, "LONG", conf,
                          stop_distance=p.hft_bo_stop_atr * atr,
                          target_rr=p.hft_bo_target_rr,
                          rationale=f"1m breakout over {p.hft_bo_range}-bar high "
                                    f"{hi:.6g} (ATR {atr / close * 1e4:.1f}bp, vol {vol:.2f}x)")
        if close < lo and close < ema50 and vol_ok:
            strength = min(1.0, (lo - close) / atr)
            conf = self._clip_conf(0.55 + 0.25 * strength + 0.10)
            return Signal(self.name, "SHORT", conf,
                          stop_distance=p.hft_bo_stop_atr * atr,
                          target_rr=p.hft_bo_target_rr,
                          rationale=f"1m breakdown under {p.hft_bo_range}-bar low "
                                    f"{lo:.6g} (ATR {atr / close * 1e4:.1f}bp, vol {vol:.2f}x)")
        return Signal(self.name, "FLAT", 0.0, rationale="no breakout")

    def check_exit(self, df, i: int, position) -> tuple[str | None, float | None]:
        if position.bars_held >= self.p.hft_bo_time_stop:
            return "time stop", None
        return None, None


class HFTExhaustionFade(BaseStrategy):
    """Fade volume-spike exhaustion candles via a resting maker limit.

    Long: z of log(close/ema50) <= -z_entry, volume >= spike multiple of its
    20-bar mean, close in the bar's low tail (CLV <= clv_max). The entry
    RESTS as a limit at the exhaustion close (liquidity provision — we get
    paid the spread instead of paying it); exit on reversion through the
    mean or the time stop inside Carver's documented reversion band."""
    name = "hft_exhaustion_fade"
    preferred_timeframes = ("1m",)

    def _z(self, df, i: int) -> float:
        dev = np.log(df["close"] / df["ema50"])
        # min_periods=60: ema50 itself is NaN for its first min_periods bars,
        # and those NaNs would poison a strict 100-bar window forever after
        sigma = dev.rolling(100, min_periods=60).std()
        z = dev / sigma
        return float(z.iloc[i])

    def evaluate(self, df, i: int) -> Signal:
        p = self.p
        close = self._at(df, "close", i)
        atr = self._at(df, "atr", i)
        ema50 = self._at(df, "ema50", i)
        vol = self._at(df, "vol_ratio", i)
        z = self._z(df, i)
        if not all(self._ok(v) for v in (close, atr, ema50, vol, z)) or atr <= 0:
            return Signal(self.name, "FLAT", 0.0, rationale="warmup")
        clv = _clv(df, i)
        vol_ok = vol >= p.hft_fade_vol_spike
        if z <= -p.hft_fade_z_entry and clv <= p.hft_fade_clv_max and vol_ok and close < ema50:
            conf = self._clip_conf(0.60 + 0.10 * min(1.0, abs(z) - p.hft_fade_z_entry))
            return Signal(self.name, "LONG", conf,
                          stop_distance=p.hft_fade_stop_atr * atr,
                          target_rr=None,
                          limit_price=close,
                          rationale=f"exhaustion fade LONG: z {z:.2f}, CLV {clv:.2f}, "
                                    f"vol {vol:.1f}x — maker bid at {close:.6g}")
        if z >= p.hft_fade_z_entry and clv >= -p.hft_fade_clv_max and vol_ok and close > ema50:
            conf = self._clip_conf(0.60 + 0.10 * min(1.0, abs(z) - p.hft_fade_z_entry))
            return Signal(self.name, "SHORT", conf,
                          stop_distance=p.hft_fade_stop_atr * atr,
                          target_rr=None,
                          limit_price=close,
                          rationale=f"exhaustion fade SHORT: z {z:.2f}, CLV {clv:.2f}, "
                                    f"vol {vol:.1f}x — maker ask at {close:.6g}")
        return Signal(self.name, "FLAT", 0.0, rationale="no exhaustion")

    def check_exit(self, df, i: int, position) -> tuple[str | None, float | None]:
        z = self._z(df, i)
        if self._ok(z):
            if position.side == "long" and z >= 0.0:
                return "reversion to mean", None
            if position.side == "short" and z <= 0.0:
                return "reversion to mean", None
        if position.bars_held >= self.p.hft_fade_time_stop:
            return "time stop", None
        return None, None


class HFTMarketMaker(BaseStrategy):
    """Avellaneda-Stoikov-inspired maker loop, simplified to one position.

    Quote half-width = width_frac x 1m ATR (the gamma*sigma^2 term: quotes
    widen with realized volatility), clamped to [min,max] bps. The quote
    SIDE fades the short-horizon drift (price above EMA20 -> rest a short
    ask above; below -> rest a long bid below), the OHLCV analogue of
    inventory-skewed quoting. Bracket: target ~one half-width against a
    3-half-width inventory stop — the inverted ratio the HFT book's own
    min_rr floor permits (HFT.md)."""
    name = "hft_market_maker"
    preferred_timeframes = ("1m",)

    def _half_width(self, df, i: int, close: float) -> float | None:
        """A-S width in PRICE units: sigma-scaled, floored and capped."""
        p = self.p
        atr = self._at(df, "atr", i)
        if not self._ok(atr) or atr <= 0 or close <= 0:
            return None
        width_bps = p.hft_mm_width_frac_atr * (atr / close) * 1e4
        # gamma*sigma^2 widening is monotone in sigma — the ATR multiple
        # already scales with realized vol, so gamma scales the slope
        width_bps *= (1.0 + p.hft_mm_gamma)
        width_bps = min(p.hft_mm_max_width_bps, max(p.hft_mm_min_width_bps, width_bps))
        return close * width_bps / 1e4

    def evaluate(self, df, i: int) -> Signal:
        p = self.p
        close = self._at(df, "close", i)
        ema20 = self._at(df, "ema20", i)
        if not all(self._ok(v) for v in (close, ema20)) or close <= 0:
            return Signal(self.name, "FLAT", 0.0, rationale="warmup")
        hw = self._half_width(df, i, close)
        if hw is None or hw <= 0:
            return Signal(self.name, "FLAT", 0.0, rationale="no vol to quote")
        # quote the side that fades the drift: drift up -> rest an ask above
        # (short at mid + hw); drift down -> rest a bid below (long at mid - hw)
        if close > ema20:
            limit = close + hw
            return Signal(self.name, "SHORT", 0.60,
                          stop_distance=p.hft_mm_stop_widths * hw,
                          target_rr=p.hft_mm_target_rr,
                          limit_price=limit,
                          rationale=f"MM ask {limit:.6g} (hw {hw / close * 1e4:.1f}bp "
                                    f"over EMA20 drift)")
        limit = close - hw
        return Signal(self.name, "LONG", 0.60,
                      stop_distance=p.hft_mm_stop_widths * hw,
                      target_rr=p.hft_mm_target_rr,
                      limit_price=limit,
                      rationale=f"MM bid {limit:.6g} (hw {hw / close * 1e4:.1f}bp "
                                f"under EMA20 drift)")

    def check_exit(self, df, i: int, position) -> tuple[str | None, float | None]:
        if position.bars_held >= self.p.hft_mm_time_stop:
            return "time stop", None
        return None, None
