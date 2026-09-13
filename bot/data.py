"""
Market data + news ingestion.

- Crypto OHLCV: ccxt public APIs with a per-symbol fallback chain (Binance ->
  Bybit -> OKX) ordered by IP-ban risk, so one exchange being down or
  geo-blocking never stops the engine. No API keys required.
- Forex OHLCV: yfinance (Yahoo). Volume is often zero for FX; indicators degrade
  gracefully (see indicators.vwap / volume_ratio).
- News: RSS headlines parsed with the stdlib; failures never break the engine.

Data quality (the "price caliber" discipline from the research notes):
  - every OHLCV frame passes `_validate_ohlcv` (required columns, positive
    prices, high >= max(open, close), low <= min(open, close), sorted unique
    UTC index) before it reaches indicators — mixed-caliber or corrupt bars
    fail loudly instead of silently poisoning signals;
  - crypto bars are RAW (exchange trades, no corporate-action adjustment);
    forex bars from Yahoo are DIVIDEND/SPLIT-ADJUSTED (auto_adjust=True) —
    stamped per frame via `df.attrs["caliber"]` so down-stream code and
    journaling can tell them apart;
  - `_drop_forming_bar` removes the still-open candle exchanges return before
    the LIVE engine evaluates (it must only ever trade closed bars); the
    backtester drops it too, for identical data semantics.

Backtests also persist frames to data/cache/*.parquet (per symbol+timeframe+day)
so repeated research runs don't hammer public APIs and stay byte-identical
between runs. ROLLING windows reuse any cache file fresher than
CACHE_FRESHNESS_HOURS (mtime-bounded, default 24h) — the day-stamp in the name
records the fetch day, not a freshness boundary; PINNED --start/--end windows
keep exact-name byte-identical semantics forever.
"""
from __future__ import annotations

import glob
import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from config import CONFIG, MarketSpec, TIMEFRAME_SECONDS


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
    rec = _source_fails.get(src)
    if rec is None:
        return True
    strikes, until = rec
    if strikes < _SOURCE_FAIL_STRIKES:
        return True
    if _time.time() >= until:      # bench expired: allow ONE probe to re-earn
        return True
    return False


def _note_source_result(src: str, ok: bool):
    import time as _time
    if ok:
        _source_fails.pop(src, None)
        return
    strikes = _source_fails.get(src, (0, 0.0))[0] + 1
    if strikes >= _SOURCE_FAIL_STRIKES:
        _source_fails[src] = (strikes, _time.time() + _SOURCE_COOLDOWN_S)
    else:
        _source_fails[src] = (strikes, 0.0)


def _crypto_client(source: str):
    if source not in _ccxt_clients:
        import ccxt
        # timeout: ccxt defaults to 10s per request; an explicit 15s bound
        # (still generous) keeps a hung exchange from stretching a cycle
        cfg = {"enableRateLimit": True, "timeout": 15000}
        if source == "binance":
            cfg["options"] = {"defaultType": "spot"}
        _ccxt_clients[source] = getattr(ccxt, source)(cfg)
    return _ccxt_clients[source]


def _ohlcv_rows_from(source: str, symbol: str, timeframe: str, since_ms: int | None,
                     limit: int) -> list:
    """Fetch one OHLCV page (or `limit` latest bars) from one exchange."""
    ex = _crypto_client(source)
    if since_ms is None:
        return ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    return ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=1000)


def _fetch_with_fallback(symbol, timeframe, source, fetch_one):
    """Fallback-chain scaffold shared by the two crypto fetchers (the chain
    loop and the all-sources-failed error tail used to be copy-pasted).
    Sources sitting out a failure cooldown are skipped in the AUTOMATIC chain
    (an explicit source= request is a deliberate operator choice and bypasses
    the bench); one probe re-earns a bench once it expires."""
    chain = _CRYPTO_SOURCES if source == "auto" else [source]
    errors: list[str] = []
    benched = [s for s in chain if source == "auto" and not _source_healthy(s)]
    for src in chain:
        if src in benched:
            errors.append(f"{src}: benched after "
                          f"{_source_fails.get(src, (0, 0))[0]} consecutive failures")
            continue
        try:
            df = fetch_one(src)
            df.attrs["source"] = src
            _note_source_result(src, ok=True)
            return df
        except Exception as exc:
            _note_source_result(src, ok=False)
            errors.append(f"{src}: {type(exc).__name__}: {exc}")
    if benched and all(s in benched for s in chain) and len(chain) > 1:
        # every source benched: drop the bench so the next call retries (a
        # forever-dead fetch is worse than a throttled one)
        for s in chain:
            _note_source_result(s, ok=True)
    raise RuntimeError(f"all crypto sources failed for {symbol} {timeframe}: " + "; ".join(errors))


def fetch_crypto_ohlcv(symbol: str, timeframe: str, limit: int = 400,
                       source: str = "auto") -> pd.DataFrame:
    """Latest `limit` bars with a fallback chain; raises if every source fails."""
    def fetch_one(src):
        rows = _ohlcv_rows_from(src, symbol, timeframe, None, limit)
        return _validate_ohlcv(_rows_to_df(rows), f"crypto:{src}")
    return _fetch_with_fallback(symbol, timeframe, source, fetch_one)


def fetch_crypto_history(symbol: str, timeframe: str, days: int,
                         source: str = "auto", start: str | None = None,
                         end: str | None = None) -> pd.DataFrame:
    """Paginated fetch of history for backtesting (with fallback).

    Either `days` (rolling window ending now) or a pinned `start`/`end`
    (YYYY-MM-DD) — the pinned form fetches the SAME window every run, which is
    what makes BACKTESTS.md numbers reproducible rather than re-rollable."""
    tf_ms = TIMEFRAME_SECONDS[timeframe] * 1000
    if start:
        start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    else:
        start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000) if end else None

    def fetch_one(src):
        ex = _crypto_client(src)
        since = start_ms
        all_rows: list = []
        while True:
            rows = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
            if not rows:
                break
            all_rows.extend(rows)
            last = rows[-1][0]
            if len(rows) < 1000 or last >= (end_ms if end_ms else ex.milliseconds() - tf_ms):
                break
            since = last + tf_ms
            time.sleep(ex.rateLimit / 1000.0)
        if end_ms:
            all_rows = [r for r in all_rows if r[0] < end_ms]
        if not all_rows:
            raise RuntimeError(f"{src} returned no data")
        df = _rows_to_df(all_rows)
        df = df[~df.index.duplicated(keep="first")]
        return _validate_ohlcv(df, f"crypto:{src}")
    return _fetch_with_fallback(symbol, timeframe, source, fetch_one)


# --------------------------------------------------------------------------- forex
def _safe_name(symbol: str) -> str:
    """Deterministic, collision-free cache-file stem from a ticker: crypto
    'BTC/USDT' -> 'BTCUSDT', forex 'EURUSD=X' -> 'EURUSD', India '^NSEI' ->
    'NSEI' (caret stripped), 'RELIANCE.NS' kept verbatim ('.NS' is the NSE
    suffix — collision-free: no other kind's stem can end '.NS'). One shared
    transform — the cache writer and the rolling-cache glob must agree or
    freshness reuse silently misses every file."""
    return symbol.replace("/", "").replace("=X", "").replace("^", "")


def _yahoo_ohlcv(symbol: str, interval: str, start: str | None, end: str | None,
                 period: str | None, origin: str) -> pd.DataFrame:
    """Shared yfinance download + clean-up for the two Yahoo-backed kinds
    (forex, india): pinned start/end window when given, else the caller's
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


def _resample_1h_to_4h(df: pd.DataFrame) -> pd.DataFrame:
    return df.resample("4h").agg({"open": "first", "high": "max", "low": "min",
                                  "close": "last", "volume": "sum"}).dropna()


def fetch_forex_ohlcv(symbol: str, timeframe: str, start: str | None = None,
                      end: str | None = None) -> pd.DataFrame:
    # no `days` argument: Yahoo's period is fixed per interval (period_map
    # below) — the old days= param was silently ignored, so it's gone
    # Yahoo granularity constraints: 5m/15m max 60d back, 1h max 730d.
    interval = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "1h", "1d": "1d"}[timeframe]
    period_map = {"5m": "60d", "15m": "60d", "1h": "730d", "4h": "730d", "1d": "5y"}
    df = _yahoo_ohlcv(symbol, interval, start, end,
                      period_map[timeframe], origin="forex")
    if timeframe == "4h":  # resample 1h -> 4h
        df = _resample_1h_to_4h(df)
    df = _validate_ohlcv(df, "forex:yahoo(auto_adjust=True)", caliber="adjusted")
    df.attrs["source"] = "yahoo"
    return df


# --------------------------------------------------------------------------- india
# India (NSE cash equities + indices) rides the SAME Yahoo path as forex
# ('.NS' equity tickers, '^'-prefixed index tickers), with one caliber note:
# auto_adjust=True is REQUIRED for equities — split/dividend-adjusted prices
# are the tradable series (a raw unadjusted series breaks across every ex-
# date; the adjusted series is continuous and is what any backtest must act
# on). 15m is not fetched for India (yfinance caps 15m intraday at 60d; the
# 90-day acceptance window needs the history) — the India universe is 1h/4h/1d.
def fetch_india_ohlcv(symbol: str, timeframe: str, start: str | None = None,
                      end: str | None = None) -> pd.DataFrame:
    interval = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "1h", "1d": "1d"}[timeframe]
    period_map = {"5m": "60d", "15m": "60d", "1h": "730d", "4h": "730d", "1d": "5y"}
    df = _yahoo_ohlcv(symbol, interval, start, end,
                      period_map[timeframe], origin="india")
    if timeframe == "4h":  # resample 1h -> 4h (same as forex)
        df = _resample_1h_to_4h(df)
    df = _validate_ohlcv(df, "india:yahoo(auto_adjust=True)", caliber="adjusted")
    df.attrs["source"] = "yahoo"
    return df


# --------------------------------------------------------------------------- quality
def _validate_ohlcv(df: pd.DataFrame, origin: str, caliber: str = "raw") -> pd.DataFrame:
    """Fail loudly on corrupt/mixed-caliber bars instead of poisoning signals.
    `caliber` states the price basis ('raw' exchange trades vs 'adjusted'
    Yahoo bars) — stamped here so EVERY validated frame carries the truth
    (the old pattern of stamping 'raw' unconditionally and overwriting in
    some callers mislabeled any frame that skipped the overwrite)."""
    required = ["open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"{origin}: missing columns {missing}")
    if df.empty:
        raise RuntimeError(f"{origin}: empty frame")
    prices = df[["open", "high", "low", "close"]]
    if (prices <= 0).any().any() or prices.isna().any().any():
        bad = df[(prices <= 0).any(axis=1) | prices.isna().any(axis=1)]
        raise RuntimeError(f"{origin}: {len(bad)} bars with non-positive/NaN prices "
                            f"(first at {bad.index[0]})")
    bad_hilo = df[(df["high"] < df[["open", "close"]].max(axis=1)) |
                  (df["low"] > df[["open", "close"]].min(axis=1))]
    if len(bad_hilo):
        raise RuntimeError(f"{origin}: {len(bad_hilo)} bars with high/low not bracketing "
                            f"the body (mixed caliber or corrupt feed; first at {bad_hilo.index[0]})")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise RuntimeError(f"{origin}: index not sorted/unique")
    df = df.copy()
    df.attrs["caliber"] = caliber
    return df


def _drop_forming_bar(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Remove the still-open candle if it is the last bar (live semantics:
    only CLOSED bars are tradable). The forming bar's close moves; acting on
    it would be trading a price that doesn't exist yet."""
    if not len(df):
        return df
    now_ms = time.time() * 1000
    tf_ms = TIMEFRAME_SECONDS.get(timeframe, 60) * 1000
    last_ms = df.index[-1].timestamp() * 1000
    if now_ms < last_ms + tf_ms:  # last bar's period hasn't closed yet
        return df.iloc[:-1]
    return df


def _disk_cache_path(spec: MarketSpec, days: int | None, start: str | None = None,
                     end: str | None = None) -> str:
    safe = _safe_name(spec.symbol)
    if start:
        # normalize to dashed ISO: pd.Timestamp happily accepts '20240601',
        # and an undashed pinned name would END in 8 digits — the rolling-prune
        # signature — so a "never pruned" pinned window could silently vanish
        # after 14 days. Dashed names can never collide with that signature.
        start = pd.Timestamp(start).strftime("%Y-%m-%d")
        if end:
            end = pd.Timestamp(end).strftime("%Y-%m-%d")
            # pinned window: the cache name is date-stable, so reruns are
            # byte-identical forever — not just on the same day
            return os.path.join(CONFIG.data_cache_dir,
                                f"{safe}_{spec.timeframe}_{start}_{end}.parquet")
        # start-without-end: the window ROLLS (fetches up to now), so a
        # constant name would freeze day 1's frame forever and never prune.
        # Embed the fetch date: each day is its own cache entry, pruned by
        # the rolling rule like any other.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        return os.path.join(CONFIG.data_cache_dir,
                            f"{safe}_{spec.timeframe}_{start}_now_{stamp}.parquet")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return os.path.join(CONFIG.data_cache_dir, f"{safe}_{spec.timeframe}_{days}d_{stamp}.parquet")


def _load_cached(path: str) -> pd.DataFrame | None:
    if os.path.exists(path):
        try:
            return pd.read_parquet(path)
        except Exception:
            return None
    return None


def _store_cached(path: str, df: pd.DataFrame):
    try:
        os.makedirs(CONFIG.data_cache_dir, exist_ok=True)
        df.to_parquet(path)
    except Exception:
        pass  # caching is best-effort; never block a fetch


def _cache_freshness_hours() -> float:
    """Rolling-cache freshness bound in HOURS, read from env at CALL time.

    Module-level read at import time would freeze tests and shell sessions to
    the value present at process start; reading at call time lets a one-off
    `CACHE_FRESHNESS_HOURS=0 python3 main.py backtest ...` force refetches
    without editing anything. The plain `os.environ.get(...)` default is
    Python's standard "missing key / malformed value" fallback pattern."""
    try:
        return float(os.environ.get("CACHE_FRESHNESS_HOURS", 24.0))
    except (TypeError, ValueError):
        return 24.0


def _find_fresh_rolling_cache(glob_pattern: str) -> tuple[str, float] | None:
    """Newest (by mtime) file matching the rolling-cache glob, when it is
    fresher than the CACHE_FRESHNESS_HOURS window; else None.

    mtime, not the name's date-stamp, decides freshness: the stamp records the
    fetch DAY (yesterday's file fetched at 23:59 is 1 minute old), and with
    several fresh hits the newest wins — the most recently fetched frame is
    the best available approximation of 'now'."""
    try:
        matches = glob.glob(os.path.join(CONFIG.data_cache_dir, glob_pattern))
    except OSError:
        return None
    if not matches:
        return None
    newest = max(matches, key=os.path.getmtime)
    age_hours = (time.time() - os.path.getmtime(newest)) / 3600.0
    if age_hours > _cache_freshness_hours():
        return None
    return newest, age_hours


def _prune_rolling_cache():
    """Delete ROLLING (date-stamped) parquet entries older than 14 days —
    they re-fetch on demand. Pinned-window caches are never pruned: they are
    the reproducibility artifacts."""
    try:
        cutoff = time.time() - 14 * 86400
        for f in os.listdir(CONFIG.data_cache_dir):
            if not f.endswith(".parquet"):
                continue
            stem = f[:-len(".parquet")]
            if len(stem) >= 8 and stem[-8:].isdigit():        # rolling stamp
                p = os.path.join(CONFIG.data_cache_dir, f)
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
    except OSError:
        pass


def _record_manifest(spec: MarketSpec, path: str, df: pd.DataFrame) -> None:
    """Record the fetch's provenance in data/manifest.json (window, bar count,
    sha256 of the cached parquet, source, caliber, fetch date) — the artifact
    that makes 'every number reproducible from this repo' a checkable claim."""
    import hashlib
    try:
        manifest_path = os.path.join(os.path.dirname(CONFIG.db_path), "manifest.json")
        manifest: dict = {}
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path) as fh:
                    manifest = json.load(fh)
            except Exception:
                manifest = {}
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        manifest[f"{spec.kind}:{spec.symbol}:{spec.timeframe}"] = {
            "bars": int(len(df)),
            "first_ts": str(df.index[0]),
            "last_ts": str(df.index[-1]),
            "sha256": h.hexdigest(),
            "source": str(df.attrs.get("source", "unknown")),
            "caliber": str(df.attrs.get("caliber", "unknown")),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cache_file": os.path.basename(path),
        }
        # re-read right before the write and merge: two CLI processes fetching
        # at once used to lose each other's entries (read-modify-write with a
        # whole manifest in flight). The per-pid tmp also stops both writers
        # clobbering through one shared .tmp path.
        try:
            with open(manifest_path) as fh:
                fresh = json.load(fh)
            if isinstance(fresh, dict):
                fresh.update(manifest)
                manifest = fresh
        except Exception:
            pass
        tmp = f"{manifest_path}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(manifest, fh, indent=1, sort_keys=True)
        os.replace(tmp, manifest_path)
    except Exception:
        pass  # provenance is best-effort; never block a fetch


# --------------------------------------------------------------------------- unified
def _rows_to_df(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").astype(float)
    return df


def fetch_history(spec: MarketSpec, days: int | None = None,
                  start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """History for backtests. Either `days` (rolling window ending now —
    reuses any disk cache fresher than CACHE_FRESHNESS_HOURS, whatever the
    fetch-day stamp in its name) or pinned `start`/`end` (date-stable cache —
    byte-identical reruns). Every successful FETCH is recorded in
    data/manifest.json (bars, checksum, source) and rolling cache entries
    older than 14 days are pruned; a fresh reuse mutates nothing."""
    if start and end:
        # PINNED window: EXACT-NAME, byte-identical semantics — this is the
        # reproducibility artifact behind BACKTESTS.md. This branch must
        # NEVER glob or reuse a differently-named file: a rolling file
        # (days-form or _now-form for the same symbol) covers a DIFFERENT
        # window than the pinned one being asked for; freshness-bounded reuse
        # here would quietly re-roll the pinned numbers.
        path = _disk_cache_path(spec, days, start, end)
        cached = _load_cached(path)
        if cached is not None:
            return cached
    else:
        # ROLLING window (days-form, or start-without-end): the window slides
        # with now anyway, so reuse is bounded by FRESHNESS, not by the fetch
        # day embedded in the file name — the old exact-day-stamp lookup
        # refetched every calendar day even when yesterday's file was minutes
        # old. (FLAW_VALIDATION Fix 4.1: freshness is the honest bound.) The
        # glob is the ONLY rolling reuse path on purpose: keeping a same-day
        # exact-name fallback would let a day-stamped file that aged past the
        # window sneak back in, defeating CACHE_FRESHNESS_HOURS=0 as a
        # force-refetch knob. A same-day file is always glob-fresh under the
        # default 24h window, so nothing is lost.
        safe = _safe_name(spec.symbol)
        if start:
            # normalize through the same dashed-ISO rule _disk_cache_path
            # applies, so the glob matches exactly the names it writes
            norm_start = pd.Timestamp(start).strftime("%Y-%m-%d")
            pattern = f"{safe}_{spec.timeframe}_{norm_start}_now_*.parquet"
        else:
            pattern = f"{safe}_{spec.timeframe}_{days}d_*.parquet"
        fresh = _find_fresh_rolling_cache(pattern)
        if fresh is not None:
            fresh_path, age = fresh
            cached = _load_cached(fresh_path)
            if cached is not None:
                # the reuse path must not mutate anything: no manifest record
                # (it would claim a fetch that never happened), no prune, no
                # re-store. Read-only, then return.
                print(f"[data] reusing cached {os.path.basename(fresh_path)} "
                      f"(age {age:.1f}h ≤ {_cache_freshness_hours():.0f}h "
                      f"freshness window)")
                return cached
        path = _disk_cache_path(spec, days, start, end)
    if spec.kind == "crypto":
        df = fetch_crypto_history(spec.symbol, spec.timeframe, days or 365,
                                  start=start, end=end)
        df = _drop_forming_bar(df, spec.timeframe)
    elif spec.kind == "india":
        df = fetch_india_ohlcv(spec.symbol, spec.timeframe, start=start, end=end)
    else:
        df = fetch_forex_ohlcv(spec.symbol, spec.timeframe, start=start, end=end)
    _store_cached(path, df)
    _record_manifest(spec, path, df)
    _prune_rolling_cache()
    return df


class MarketData:
    """TTL-cached latest-candles provider for the live/paper engine.

    Always drops the still-forming candle: the engine must only ever evaluate
    closed bars (backtest/live parity).
    """

    def __init__(self):
        self._cache: dict = {}

    def latest(self, spec: MarketSpec, limit: int | None = None) -> pd.DataFrame:
        limit = limit or CONFIG.lookback_bars
        # TTL scales with the timeframe: a 1d-bar fetch needs far fewer
        # refreshes than a 5m one. (The old ttl_by_tf=False -> 30s branch was
        # unreachable — no constructor ever passed it.)
        ttl = TIMEFRAME_SECONDS[spec.timeframe]
        # limit is part of the key: the dashboard's marks fetch with limit=2,
        # and caching that 2-bar frame under the engine's key made the next
        # cycle skip the market entirely (len(df) < 60 -> silent no-trade)
        key = (spec.kind, spec.symbol, spec.timeframe, limit)
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < max(ttl * 0.5, 15):
            return hit[1]
        if spec.kind == "crypto":
            df = fetch_crypto_ohlcv(spec.symbol, spec.timeframe, limit)
        elif spec.kind == "india":
            df = fetch_india_ohlcv(spec.symbol, spec.timeframe).tail(limit)
        else:
            df = fetch_forex_ohlcv(spec.symbol, spec.timeframe).tail(limit)
        df = _drop_forming_bar(df, spec.timeframe)
        self._cache[key] = (now, df)
        return df


# --------------------------------------------------------------------------- news
_NEWS_CACHE = {"ts": 0.0, "items": []}


def _parse_rss(xml_text: str, source: str) -> list:
    items = []
    try:
        # size cap before parsing: stdlib ET expands internal entities without
        # a limit, so a crafted "billion laughs" feed allocates unbounded memory
        if len(xml_text) > 2_000_000:
            return items
        root = ET.fromstring(xml_text)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            desc = (item.findtext("description") or "").strip()
            if title:
                items.append({"title": title, "source": source, "published": pub,
                              "summary": desc[:280]})
    except ET.ParseError:
        pass
    return items


def fetch_news() -> list:
    """Fetch RSS headlines from the configured feeds. Cached, failure-tolerant."""
    max_items = CONFIG.news_max_items
    now = time.time()
    if now - _NEWS_CACHE["ts"] < CONFIG.news_ttl_seconds and _NEWS_CACHE["items"]:
        return _NEWS_CACHE["items"][:max_items]

    items: list = []
    for url in CONFIG.news_feeds:
        try:
            resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0 (algo-bot)"},
                                allow_redirects=True)
            # redirects FOLLOWED: public feeds 301 on www->apex moves, and the
            # old allow_redirects=False silently yielded zero headlines for
            # them. The 2MB parse cap below still bounds the final body.
            if resp.status_code == 200:
                source = url.split("/")[2].replace("www.", "")
                items.extend(_parse_rss(resp.text, source))
        except Exception:
            continue
    if items:
        _NEWS_CACHE["ts"] = now
        _NEWS_CACHE["items"] = items
        try:
            os.makedirs(CONFIG.data_cache_dir, exist_ok=True)
            with open(os.path.join(CONFIG.data_cache_dir, "news_cache.json"), "w") as f:
                json.dump(items[:50], f, indent=1)
        except Exception:
            pass
    return items[:max_items]
