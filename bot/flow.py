"""Real taker order flow from Binance klines, for the fast book.

Every Binance kline carries the volume bought by takers (aggressive buyers)
alongside the total volume; ccxt's OHLCV drops that column, which is why
`hft_ofi_momentum` infers direction from where a bar closed in its range (a
CLV x volume guess). This module fetches the real figure for
`hft_taker_flow`.

What is attached: `taker_buy_frac` = taker-buy volume / total volume for
each bar, from Binance spot klines. A fraction rather than a volume, so it
stays meaningful when the bars themselves came from a fallback exchange.
Signed aggressor flow for a bar is then (2 * frac - 1) * volume.

Causality: a bar's taker volume is final when the bar closes, exactly like
its volume, so a strategy deciding on a closed bar may use it.

Scope: backtests only (the experiment runner and the Lab). `hft_taker_flow`
is a candidate, which the live engine never evaluates; wiring this into the
live engine is the step that has to come with promoting it (a guard test
pins that, as for funding).
"""
from __future__ import annotations

import os

import pandas as pd

from config import CONFIG, TIMEFRAME_SECONDS

_LIMIT = 1000                     # klines per request (Binance maximum)


def binance_symbol(symbol: str) -> str | None:
    """'BTC/USDT' -> 'BTCUSDT'; None for anything that is not a crypto pair."""
    base, sep, quote = symbol.partition("/")
    return f"{base}{quote}" if sep and base and quote and "=" not in symbol else None


def _cache_path(symbol: str, timeframe: str) -> str:
    return os.path.join(CONFIG.data_cache_dir,
                        f"takerflow_{symbol.replace('/', '')}_{timeframe}.parquet")


def _fetch_klines(sym: str, timeframe: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Closed klines between start and end: open time -> volume, taker buys."""
    import ccxt
    ex = ccxt.binance({"enableRateLimit": True, "timeout": 10000})
    step = TIMEFRAME_SECONDS[timeframe] * 1000
    rows: list = []
    cursor = start_ms
    while cursor < end_ms:
        batch = ex.publicGetKlines({"symbol": sym, "interval": timeframe,
                                    "startTime": cursor, "endTime": end_ms,
                                    "limit": _LIMIT})
        if not batch:
            break
        rows.extend(batch)
        last_open = int(batch[-1][0])
        if len(batch) < _LIMIT or last_open + step <= cursor:
            break
        cursor = last_open + step
    if not rows:
        return pd.DataFrame(columns=["volume", "taker_buy_volume"])
    idx = pd.to_datetime([int(r[0]) for r in rows], unit="ms", utc=True)
    out = pd.DataFrame({"volume": [float(r[5]) for r in rows],
                        "taker_buy_volume": [float(r[9]) for r in rows]}, index=idx)
    # the still-forming kline would change; keep closed bars only
    now_ms = pd.Timestamp.now(tz="UTC").value // 1_000_000
    closed = [int(r[6]) < now_ms for r in rows]
    out = out[closed]
    return out[~out.index.duplicated(keep="last")].sort_index()


def taker_flow_history(symbol: str, timeframe: str, start: pd.Timestamp,
                       end: pd.Timestamp) -> pd.DataFrame:
    """Taker flow for [start, end], disk-cached per symbol and timeframe.

    Closed klines never change, so the cache only grows: a refetch asks
    only for what is missing before the first or after the last cached bar."""
    sym = binance_symbol(symbol)
    if sym is None:
        return pd.DataFrame(columns=["volume", "taker_buy_volume"])
    path = _cache_path(symbol, timeframe)
    cached = pd.read_parquet(path) if os.path.exists(path) else None
    parts = [] if cached is None else [cached]
    ms = lambda t: int(pd.Timestamp(t).value // 1_000_000)  # noqa: E731
    step = pd.Timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    if cached is None or not len(cached):
        parts.append(_fetch_klines(sym, timeframe, ms(start), ms(end)))
    else:
        if start < cached.index[0]:
            parts.append(_fetch_klines(sym, timeframe, ms(start), ms(cached.index[0])))
        if end > cached.index[-1] + step:
            parts.append(_fetch_klines(sym, timeframe, ms(cached.index[-1] + step), ms(end)))
    merged = pd.concat([p for p in parts if len(p)]) if any(len(p) for p in parts) \
        else pd.DataFrame(columns=["volume", "taker_buy_volume"])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    if len(merged) and (cached is None or len(merged) != len(cached)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        merged.to_parquet(path)
    return merged[(merged.index >= start) & (merged.index <= end)]


def attach_taker_flow(df: pd.DataFrame, symbol: str, timeframe: str,
                      history=taker_flow_history) -> pd.DataFrame:
    """`df` with a `taker_buy_frac` column, matched on bar open time.

    Never raises: a non-crypto symbol, a failed fetch or a bar Binance did
    not report leaves NaN, and the strategy says 'no taker-flow data'."""
    if df is None or not len(df) or binance_symbol(symbol) is None:
        return df
    try:
        idx = pd.DatetimeIndex(df.index).tz_convert("UTC")
        flow = history(symbol, timeframe, idx[0], idx[-1])
    except Exception as exc:
        print(f"[flow] {symbol} {timeframe}: unavailable ({type(exc).__name__}: {exc})")
        return df
    out = df.copy()
    if flow is None or not len(flow):
        out["taker_buy_frac"] = float("nan")
        return out
    vol = flow["volume"].where(flow["volume"] > 0)
    frac = (flow["taker_buy_volume"] / vol).clip(0.0, 1.0)
    frac.index = pd.DatetimeIndex(frac.index).tz_convert("UTC").as_unit("ns")
    out["taker_buy_frac"] = frac.reindex(idx.as_unit("ns")).to_numpy()
    return out
