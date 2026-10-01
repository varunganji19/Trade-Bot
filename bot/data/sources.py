"""Market data sources: crypto OHLCV through ccxt with a per-symbol fallback
chain (Binance -> Bybit -> OKX, ordered by IP-ban risk; a failing source is
benched after repeated strikes) and forex OHLCV from Yahoo."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pandas as pd

import bot.data as base   # every data function is looked up on the package
                          # at call time, so tests can patch bot.data.<name>
from config import TIMEFRAME_SECONDS


# --------------------------------------------------------------------------- crypto
# Fallback chain ordered by IP-ban risk: never-banned-by-default public feeds
# first (Binance), key-gated or more aggressive rate-limiters last. All support
# public OHLCV without authentication.
_CRYPTO_SOURCES = ["binance", "bybit", "okx"]

_ccxt_clients: dict = {}

# consecutive-failure cooldown per source: a geo-blocked/dead exchange re-paid
# its timeout on EVERY fetch of every symbol, every cycle (27 dead calls in a
# blackout cycle — enough to outrun the engine's 300s stop-join). After N
# consecutive failures the source sits out M minutes, then gets one retry.
_SOURCE_COOLDOWN_S = 300
_SOURCE_FAIL_STRIKES = 3
_source_fails: dict[str, tuple[int, float]] = {}   # source -> (strikes, benched_until)


def _source_healthy(src: str) -> bool:
    import time as _time
    rec = base._source_fails.get(src)
    if rec is None:
        return True
    strikes, until = rec
    if strikes < base._SOURCE_FAIL_STRIKES:
        return True
    if _time.time() >= until:      # bench expired: allow ONE probe to re-earn
        return True
    return False


def _note_source_result(src: str, ok: bool):
    import time as _time
    if ok:
        base._source_fails.pop(src, None)
        return
    strikes = base._source_fails.get(src, (0, 0.0))[0] + 1
    if strikes >= base._SOURCE_FAIL_STRIKES:
        base._source_fails[src] = (strikes, _time.time() + base._SOURCE_COOLDOWN_S)
    else:
        base._source_fails[src] = (strikes, 0.0)


def _crypto_client(source: str):
    if source not in base._ccxt_clients:
        import ccxt
        # timeout: ccxt defaults to 10s per request; an explicit 15s bound
        # (still generous) keeps a hung exchange from stretching a cycle
        cfg = {"enableRateLimit": True, "timeout": 15000}
        if source == "binance":
            cfg["options"] = {"defaultType": "spot"}
        base._ccxt_clients[source] = getattr(ccxt, source)(cfg)
    return base._ccxt_clients[source]


def _ohlcv_rows_from(source: str, symbol: str, timeframe: str, since_ms: int | None,
                     limit: int) -> list:
    """Fetch one OHLCV page (or `limit` latest bars) from one exchange."""
    ex = base._crypto_client(source)
    if since_ms is None:
        return ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    return ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=1000)


def _fetch_with_fallback(symbol, timeframe, source, fetch_one):
    """Fallback-chain scaffold shared by the two crypto fetchers (the chain
    loop and the all-sources-failed error tail used to be copy-pasted).
    Sources sitting out a failure cooldown are skipped in BOTH the automatic
    chain and explicit-source requests (a dead exchange must not be hammered
    just because the operator named it); one probe re-earns a bench once it
    expires."""
    chain = base._CRYPTO_SOURCES if source == "auto" else [source]
    errors: list[str] = []
    benched = [s for s in chain if not base._source_healthy(s)]
    for src in chain:
        if src in benched:
            errors.append(f"{src}: benched after "
                          f"{base._source_fails.get(src, (0, 0))[0]} consecutive failures")
            continue
        try:
            df = fetch_one(src)
            df.attrs["source"] = src
            base._note_source_result(src, ok=True)
            return df
        except Exception as exc:
            base._note_source_result(src, ok=False)
            errors.append(f"{src}: {type(exc).__name__}: {exc}")
    if benched and all(s in benched for s in chain) and len(chain) > 1:
        # every source benched: drop the bench so the next call retries (a
        # forever-dead fetch is worse than a throttled one)
        for s in chain:
            base._note_source_result(s, ok=True)
    raise RuntimeError(f"all crypto sources failed for {symbol} {timeframe}: " + "; ".join(errors))


def fetch_crypto_ohlcv(symbol: str, timeframe: str, limit: int = 400,
                       source: str = "auto") -> pd.DataFrame:
    """Latest `limit` bars with a fallback chain; raises if every source fails."""
    def fetch_one(src):
        rows = base._ohlcv_rows_from(src, symbol, timeframe, None, limit)
        return base._validate_ohlcv(base._rows_to_df(rows), f"crypto:{src}",
                               kind="crypto", timeframe=timeframe)
    return base._fetch_with_fallback(symbol, timeframe, source, fetch_one)


def fetch_crypto_history(symbol: str, timeframe: str, days: int,
                         source: str = "auto", start: str | None = None,
                         end: str | None = None) -> pd.DataFrame:
    """Paginated fetch of history for backtesting (with fallback).

    Either `days` (rolling window ending now) or a pinned `start`/`end`
    (YYYY-MM-DD) — the pinned form fetches the SAME window every run, which is
    what makes docs/archive/BACKTESTS.md numbers reproducible rather than re-rollable."""
    tf_ms = TIMEFRAME_SECONDS[timeframe] * 1000
    if start:
        start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    else:
        start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000) if end else None

    def fetch_one(src):
        ex = base._crypto_client(src)
        since = start_ms
        all_rows: list = []
        prev_last: int | None = None
        while True:
            rows = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
            if not rows:
                break
            all_rows.extend(rows)
            last = rows[-1][0]
            if len(rows) < 1000 or last >= (end_ms if end_ms else ex.milliseconds() - tf_ms):
                break
            if prev_last is not None and last <= prev_last:
                # no forward progress: avoid an infinite pagination loop
                break
            prev_last = last
            # since is INCLUSIVE: re-request from `last` and dedupe below so
            # gappy bars between pages are never skipped (last+tf_ms jumps a
            # full timeframe past `last` and drops any bar the exchange holds
            # in (last, last+tf_ms)).
            since = last
            time.sleep(ex.rateLimit / 1000.0)
        if end_ms:
            all_rows = [r for r in all_rows if r[0] < end_ms]
        if not all_rows:
            raise RuntimeError(f"{src} returned no data")
        df = base._rows_to_df(all_rows)
        df = df[~df.index.duplicated(keep="first")]
        return base._validate_ohlcv(df, f"crypto:{src}", kind="crypto", timeframe=timeframe)
    return base._fetch_with_fallback(symbol, timeframe, source, fetch_one)


# --------------------------------------------------------------------------- forex
def _safe_name(symbol: str) -> str:
    """Deterministic, collision-free cache-file stem from a ticker: crypto
    'BTC/USDT' -> 'BTCUSDT', forex 'EURUSD=X' -> 'EURUSD' (a leading index
    caret is stripped too). One shared transform — the cache writer and the rolling-cache glob must agree or
    freshness reuse silently misses every file."""
    return symbol.replace("/", "").replace("=X", "").replace("^", "")


def _yahoo_ohlcv(symbol: str, interval: str, start: str | None, end: str | None,
                 period: str | None, origin: str) -> pd.DataFrame:
    """Shared yfinance download + clean-up for the two Yahoo-backed kinds
    (forex): pinned start/end window when given, else the caller's
    period; column flattening, UTC index, dedupe/sort; 1h->4h resample is the
    CALLER's concern (same interval map, different calendars would resample
    identically but the origin label differs per kind)."""
    import yfinance as yf
    if start:
        # pinned window (reproducible); Yahoo's end is inclusive-exclusive
        df = yf.download(symbol, start=start, end=end, interval=interval,
                         auto_adjust=True, progress=False)
    else:
        df = yf.download(symbol, period=period, interval=interval,
                         auto_adjust=True, progress=False)
    if df is None or df.empty:
        raise RuntimeError(f"No {origin} data for {symbol}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df.index = pd.to_datetime(df.index, utc=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


# Yahoo's own history caps per interval (days). Mirrors bot/lab.py's
# _YAHOO_DAYS_CAP (kept local: lab imports data, so data cannot import lab).
# Enforced pre-fetch in the Yahoo-backed fetchers — Yahoo silently truncates
# beyond the cap, so requesting more must raise instead of returning a short
# frame callers mistake for the full window.
_YAHOO_DAYS_CAP = {"1m": 7, "5m": 60, "15m": 60, "1h": 730, "4h": 730, "1d": 1825}


def _enforce_yahoo_cap(timeframe: str, days: int | None, start: str | None,
                       end: str | None, origin: str) -> None:
    cap = base._YAHOO_DAYS_CAP.get(timeframe)
    if cap is None:
        return
    if days is not None and days > cap:
        raise RuntimeError(
            f"{origin}: requested {days}d exceeds the yfinance {cap}d cap "
            f"for {timeframe} (would silently truncate)")
    if start and end:
        try:
            span_days = (pd.Timestamp(end, tz="UTC") - pd.Timestamp(start, tz="UTC")).days
        except Exception:
            return
        if span_days > cap:
            raise RuntimeError(
                f"{origin}: pinned window {start}..{end} spans ~{span_days}d, "
                f"exceeds the yfinance {cap}d cap for {timeframe} "
                f"(would silently truncate)")


def _resample_1h_to_4h(df: pd.DataFrame) -> pd.DataFrame:
    return df.resample("4h").agg({"open": "first", "high": "max", "low": "min",
                                  "close": "last", "volume": "sum"}).dropna()


def fetch_forex_ohlcv(symbol: str, timeframe: str, start: str | None = None,
                      end: str | None = None, days: int | None = None) -> pd.DataFrame:
    # Yahoo granularity constraints: 5m/15m max 60d back, 1h max 730d.
    # `days` is enforced (not silently ignored): beyond the Yahoo cap Yahoo
    # truncates, so raise instead of returning a short frame.
    base._enforce_yahoo_cap(timeframe, days, start, end, origin="forex")
    interval = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "1h", "1d": "1d"}[timeframe]
    period_map = {"1m": "7d", "5m": "60d", "15m": "60d", "1h": "730d", "4h": "730d", "1d": "5y"}
    df = base._yahoo_ohlcv(symbol, interval, start, end,
                      period_map[timeframe], origin="forex")
    if timeframe == "4h":  # resample 1h -> 4h (UTC bins suit 24x5 spot FX)
        df = base._resample_1h_to_4h(df)
    # spot-FX caliber: auto_adjust=True is a no-op for FX (no splits/divs) —
    # the series is raw spot, NOT dividend/split-adjusted equity. Stamped
    # distinctly from a raw series so downstream code never mixes
    # the two bases.
    df = base._validate_ohlcv(df, "forex:yahoo(auto_adjust=True)", caliber="spot-fx",
                         kind="forex", timeframe=timeframe)
    df.attrs["source"] = "yahoo"
    return df


def _rows_to_df(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").astype(float)
    return df
