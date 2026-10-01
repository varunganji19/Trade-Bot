"""Perpetual-futures funding rates for the fast book's funding strategy.

Binance USDⓈ-M perpetuals settle funding every 8h (00/08/16 UTC). The rate is
the market's own price for leverage: when longs crowd in they pay shorts, and
when shorts crowd in they pay longs. `hft_funding_reversion` reads it as a
crowding signal (bot/strategies/hft.py).

Causality: a print is attached only to bars whose OPEN time is at or after
its settlement time, so a bar never sees a rate that had not settled yet.
The z-score is computed over the prints themselves (each one uses only that
print and the ones before it), then forward-filled onto the bars.

Scope: backtests only (the battery and the Lab). The strategy is a candidate,
which the live engine never evaluates; wiring funding into the live engine
is the step that has to come with promoting it (see the guard test).
"""
from __future__ import annotations

import os

import pandas as pd

from config import CONFIG

FUNDING_Z_WINDOW = 90           # prints (30 days at 3 per day)
FUNDING_Z_MIN_PRINTS = 30       # 10 days before a z-score means anything
# Binance pins quiet-market funding near its 1bp/8h default, so the rolling
# spread of the rate can be almost zero; without a floor a 0.1bp wiggle
# would read as a many-sigma "extreme"
FUNDING_STD_FLOOR = 0.00002     # 0.2bp
HISTORY_PAD_DAYS = 40           # fetched before the frame so the z-score is warm


def perp_symbol(symbol: str) -> str | None:
    """'BTC/USDT' -> 'BTC/USDT:USDT' (the linear perpetual); None for
    anything without a USDT perpetual (crosses like ETH/BTC, forex)."""
    base, _, quote = symbol.partition("/")
    return f"{base}/{quote}:{quote}" if base and quote == "USDT" else None


def _cache_path(symbol: str) -> str:
    return os.path.join(CONFIG.data_cache_dir,
                        f"funding_{symbol.replace('/', '')}.parquet")


def _fetch_prints(perp: str, since_ms: int) -> pd.Series:
    """Every settled print from `since_ms` to now, paginated."""
    import ccxt
    ex = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 10000})
    rows: list = []
    cursor = since_ms
    while True:
        batch = ex.fetch_funding_rate_history(perp, since=cursor, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        last = batch[-1]["timestamp"]
        if len(batch) < 1000 or last <= cursor:
            break
        cursor = last + 1
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series([float(r["fundingRate"]) for r in rows],
                  index=pd.to_datetime([r["timestamp"] for r in rows], unit="ms", utc=True))
    return s[~s.index.duplicated(keep="last")].sort_index()


def funding_history(symbol: str, start: pd.Timestamp) -> pd.Series:
    """Settled funding prints from `start` to now, disk-cached per symbol.

    The cache only ever grows: settled prints never change, so a refetch
    starts from the last cached print."""
    perp = perp_symbol(symbol)
    if perp is None:
        return pd.Series(dtype=float)
    path = _cache_path(symbol)
    cached = pd.Series(dtype=float)
    if os.path.exists(path):
        cached = pd.read_parquet(path)["funding_rate"]
    need_from = start
    if len(cached) and cached.index[0] <= start:
        need_from = cached.index[-1] + pd.Timedelta(milliseconds=1)
    fresh = _fetch_prints(perp, int(need_from.timestamp() * 1000))
    merged = pd.concat([cached, fresh])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    if len(merged):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        merged.to_frame("funding_rate").to_parquet(path)
    return merged[merged.index >= start]


def funding_features(prints: pd.Series) -> pd.DataFrame:
    """funding_rate and its rolling z-score, indexed by settlement time."""
    mean = prints.rolling(FUNDING_Z_WINDOW, min_periods=FUNDING_Z_MIN_PRINTS).mean()
    std = prints.rolling(FUNDING_Z_WINDOW, min_periods=FUNDING_Z_MIN_PRINTS).std()
    z = (prints - mean) / std.clip(lower=FUNDING_STD_FLOOR)
    return pd.DataFrame({"funding_rate": prints, "funding_z": z})


def attach_funding(df: pd.DataFrame, symbol: str,
                   history=funding_history) -> pd.DataFrame:
    """`df` with funding_rate / funding_z columns as of each bar's open.

    Never raises: a symbol with no perpetual, or a failed fetch, returns the
    frame unchanged and the strategy reports 'no funding data'."""
    if df is None or not len(df) or perp_symbol(symbol) is None:
        return df
    try:
        start = df.index[0] - pd.Timedelta(days=HISTORY_PAD_DAYS)
        prints = history(symbol, start)
    except Exception as exc:
        print(f"[funding] {symbol}: unavailable ({type(exc).__name__}: {exc})")
        return df
    if prints is None or not len(prints):
        return df
    feats = funding_features(prints)
    feats.index = pd.DatetimeIndex(feats.index).tz_convert("UTC").as_unit("ns")
    bars = pd.DataFrame(index=pd.DatetimeIndex(df.index).tz_convert("UTC").as_unit("ns"))
    joined = pd.merge_asof(bars, feats, left_index=True, right_index=True,
                           direction="backward")
    out = df.copy()
    out["funding_rate"] = joined["funding_rate"].to_numpy()
    out["funding_z"] = joined["funding_z"].to_numpy()
    return out


__all__ = ["perp_symbol", "funding_history", "funding_features", "attach_funding"]
