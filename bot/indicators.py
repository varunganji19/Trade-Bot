"""
Technical indicator library, hand-rolled on pandas/numpy.

Implemented with the standard definitions quants expect:
- RSI, ATR use Wilder's smoothing (RMA), matching TradingView/Connors conventions.
- EMA uses span-based weighting.
- VWAP (`vwap_roll`) is a rolling window VWAP, NOT session-anchored — strategies
  use it intraday; it falls back to a rolling typical-price average when the
  feed has no volume (forex from yfinance).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (RMA): alpha = 1/period. NOTE: ewm(adjust=False)
    seeds with the FIRST value of the series, not a simple MA — during the
    warmup (roughly the first 5*period bars) RSI/ATR deviate from a
    TradingView-identical RMA. Callers treat warmup NaN as no-signal."""
    return series.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, min_periods=period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = _rma(gain, period)
    avg_loss = _rma(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    # All gains (avg_loss==0) -> RSI 100; no gains (avg_gain==0, avg_loss>0)
    # -> RSI 0; flat (both 0) -> RSI 50. Warmup (avg_* NaN) stays NaN: NaN
    # comparisons are False in Python, so it can never auto-fire a gate — the
    # old blanket fillna(100.0) turned warmup into RSI 100, which would have
    # fired every >=95 short gate (it only looked harmless because an
    # unrelated trend guard masked it).
    out = out.mask((avg_gain > 0) & (avg_loss == 0), 100.0)
    out = out.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return out


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return _rma(tr, period)


def donchian(df: pd.DataFrame, period: int) -> tuple[pd.Series, pd.Series]:
    upper = df["high"].rolling(period, min_periods=period).max()
    lower = df["low"].rolling(period, min_periods=period).min()
    return upper, lower


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr_ = _rma(tr, period)
    plus_di = 100.0 * _rma(plus_dm, period) / atr_.replace(0.0, np.nan)
    minus_di = 100.0 * _rma(minus_dm, period) / atr_.replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return _rma(dx.fillna(0.0), period)


def rolling_vwap(df: pd.DataFrame, window: int = 96) -> pd.Series:
    """Rolling VWAP not anchored to a session — used by intraday scalping."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    volume = df["volume"].fillna(0.0) if "volume" in df else pd.Series(0.0, index=df.index)
    if volume.sum() <= 0:
        return typical.rolling(48, min_periods=10).mean()
    tpv = (typical * volume).rolling(window, min_periods=10).sum()
    cumv = volume.rolling(window, min_periods=10).sum().replace(0.0, np.nan)
    return tpv / cumv


def volume_ratio(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Current volume / rolling mean volume. 1.0 when no volume data."""
    if "volume" not in df or df["volume"].fillna(0).sum() <= 0:
        return pd.Series(1.0, index=df.index)
    avg = df["volume"].rolling(period, min_periods=5).mean().replace(0.0, np.nan)
    return df["volume"] / avg


def seasonal_rvol(df: pd.DataFrame, days: int = 14, min_obs: int = 2) -> pd.Series:
    """Time-of-day-matched relative volume (RVOL): this bar's volume vs the
    mean volume of the `days` PRIOR bars in the same time-of-day slot (same
    hour:minute of UTC). NaN when a slot has fewer than `min_obs` priors
    (strategy gates auto-pass on NaN), 1.0 when the feed has no volume.

    This is the RVOL definition from Zarattini-Barbon-Aziz (2024), where
    trading only unusually-active names (RVOL >= their own time-of-day norm)
    was the difference between Sharpe 0.48 and 2.81 — a plain rolling-average
    volume ratio mis-grades crypto's strong hour-of-day seasonality: a normal
    US-hours bar looks like a burst and a normal Asia-hours bar looks dead.
    Baseline uses only PRIOR slots (shift by one within the slot), so the
    current bar never pollutes its own norm — no lookahead by construction."""
    if "volume" not in df or df["volume"].fillna(0).sum() <= 0:
        return pd.Series(1.0, index=df.index)
    slot = df.index.hour * 60 + df.index.minute
    baseline = (df.groupby(slot)["volume"]
                  .transform(lambda s: s.rolling(days, min_periods=min_obs)
                                       .mean().shift(1)))
    return df["volume"] / baseline.replace(0.0, np.nan)


def halflife_ar1(series: pd.Series, window: int = 100) -> pd.Series:
    """Chan's half-life of mean reversion, in bars (Algorithmic Trading, ch.2).

    Fits the discrete OU / ARIMA(1,0,0) process x_t = c + phi*x_{t-1} + e_t by
    OLS over a rolling `window`, then half-life = -ln(2)/lambda with
    lambda = phi - 1 (Chan's ADF-regression form; equals -ln(2)/ln(phi) near
    the unit root, where it matters). This is the natural time scale of mean
    reversion in `series` — Chan's rule: set lookbacks/holding periods as a
    small multiple of the half-life, and don't trade reversion at all when
    the half-life is longer than your horizon.

    Policies (matching the NaN-auto-pass convention of every other gate):
    - window incomplete or degenerate (zero variance) -> NaN (gates auto-pass)
    - phi >= 1 (explosive — deviation diverging, not reverting) -> inf, which
      gates can veto explicitly
    - phi < 1 -> positive half-life; phi <= 0 collapses to fast (<1 bar)
      reversion, which simply passes a "short enough" gate.

    Finite-window honesty: OLS on a near-unit-root series is biased downward
    (the Dickey-Fuller bias, ~-3.5/window bars), so a TRUE random walk reads
    a finite half-life of roughly window/5 bars rather than inf — the "inf"
    label only fires on genuinely explosive windows. Gate thresholds must
    therefore sit well below window/5 to filter non-reverting regimes; the
    threshold, not the inf label, does the refusing.

    `series` should be a STATIONARY quantity (a deviation/spread, e.g.
    log(close/EMA)) — on raw prices the AR(1) sits at the unit root by
    construction and every window reads a huge half-life, which is the model
    correctly telling you a price level is not mean-reverting."""
    x = series.astype(float)
    b = x.shift(1)
    mean_a = x.rolling(window, min_periods=window).mean()
    mean_b = b.rolling(window, min_periods=window).mean()
    # OLS slope of x_t on x_{t-1} within each window, vectorized:
    # phi = E[x*b] - E[x]E[b]) / Var[b]
    cov = (x * b).rolling(window, min_periods=window).mean() - mean_a * mean_b
    var_b = (b * b).rolling(window, min_periods=window).mean() - mean_b * mean_b
    phi = cov / var_b.where(var_b > 0.0)   # also kills tiny-negative FP-cancellation variance
    lam = phi - 1.0
    hl = -np.log(2.0) / lam                      # NaN-safe elementwise
    hl = pd.Series(hl, index=series.index)
    hl[lam.notna() & (lam >= 0)] = np.inf        # phi >= 1: no mean reversion
    hl[lam.isna()] = np.nan                      # warmup / degenerate window
    return hl


def add_all_indicators(df: pd.DataFrame, params=None) -> pd.DataFrame:
    """Compute every indicator column the strategies need. Idempotent."""
    from config import StrategyParams  # local import to avoid cycle
    p = params or StrategyParams()
    out = df.copy()
    out["rsi2"] = rsi(out["close"], p.mr_rsi_period)
    out["rsi14"] = rsi(out["close"], 14)
    out["rsi3"] = rsi(out["close"], 3)
    out["ema5"] = ema(out["close"], p.mr_exit_ema)
    out["ema9"] = ema(out["close"], p.scalper_ema_fast)
    out["ema21"] = ema(out["close"], p.scalper_ema_slow)
    out["ema50"] = ema(out["close"], 50)
    out["ema200"] = ema(out["close"], p.mr_trend_ema)
    out["ema20"] = ema(out["close"], 20)
    out["atr"] = atr(out, p.turtle_atr_period)
    out["adx"] = adx(out, 14)
    out["don_up20"], out["don_low20"] = donchian(out, p.turtle_entry_period)
    out["don_exit_up"], out["don_exit_low"] = donchian(out, p.turtle_exit_period)
    out["vwap_roll"] = rolling_vwap(out, 96)
    out["vol_ratio"] = volume_ratio(out, 20)
    out["rvol"] = seasonal_rvol(out, days=14)
    # Chan half-life of the swing deviation (EMA20 on the strategy's own bars):
    # how fast pullbacks have historically reverted. EMA20 on 4h bars (~3 days)
    # matches the horizon connors' 12-bar time stop protects; on raw price the
    # AR(1) sits at its unit root and every window reads inf — which is the
    # model correctly reporting "price levels are not mean-reverting".
    out["halflife"] = halflife_ar1(np.log(out["close"] / out["ema20"]), window=100)
    return out
