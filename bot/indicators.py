"""
Technical indicator library, hand-rolled on pandas/numpy.

Implemented with the standard definitions quants expect:
- RSI, ATR use Wilder's smoothing (RMA), matching TradingView/Connors conventions.
- EMA uses span-based weighting.
- VWAP is session-anchored (UTC day) for crypto and falls back to a rolling
  typical-price average when the feed has no volume (forex from yfinance).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (RMA): alpha = 1/period, seeded with a simple MA."""
    return series.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


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
    # All gains and no losses -> RSI 100; no gains at all -> RSI 0.
    out = out.fillna(100.0).where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
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


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, min_periods=signal, adjust=False).mean()
    return line, sig, line - sig


def bollinger(close: pd.Series, period: int = 20, stds: float = 2.0):
    mid = sma(close, period)
    sd = close.rolling(period, min_periods=period).std()
    return mid + stds * sd, mid, mid - stds * sd


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


def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3):
    low_min = df["low"].rolling(k_period, min_periods=k_period).min()
    high_max = df["high"].rolling(k_period, min_periods=k_period).max()
    denom = (high_max - low_min).replace(0.0, np.nan)
    k = 100.0 * (df["close"] - low_min) / denom
    return k, k.rolling(d_period, min_periods=d_period).mean()


def vwap(df: pd.DataFrame, session_reset: bool = True) -> pd.Series:
    """
    Session-anchored VWAP (UTC day) when volume exists; rolling 48-bar
    typical-price average when the feed has no volume (forex).
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    volume = df["volume"].fillna(0.0) if "volume" in df else pd.Series(0.0, index=df.index)

    if volume.sum() <= 0:
        return typical.rolling(48, min_periods=10).mean()

    if session_reset and isinstance(df.index, pd.DatetimeIndex):
        day = df.index.tz_convert("UTC").normalize() if df.index.tz is not None else df.index.normalize()
        groups = day.values
    else:
        groups = np.zeros(len(df))

    tpv = (typical * volume).groupby(groups).cumsum()
    cumv = volume.groupby(groups).cumsum().replace(0.0, np.nan)
    return tpv / cumv


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
    out["atr"] = atr(out, p.turtle_atr_period)
    out["adx"] = adx(out, 14)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(out["close"])
    bb_up, bb_mid, bb_low = bollinger(out["close"], 20, 2.0)
    out["bb_up"], out["bb_mid"], out["bb_low"] = bb_up, bb_mid, bb_low
    out["don_up20"], out["don_low20"] = donchian(out, p.turtle_entry_period)
    out["don_up55"], out["don_low55"] = donchian(out, 55)
    out["don_exit_up"], out["don_exit_low"] = donchian(out, p.turtle_exit_period)
    out["vwap"] = vwap(out)
    out["vwap_roll"] = rolling_vwap(out, 96)
    out["vol_ratio"] = volume_ratio(out, 20)
    out["vol_sma"] = sma(out["volume"].fillna(0.0), 20) if "volume" in out else pd.Series(1.0, index=out.index)
    return out
