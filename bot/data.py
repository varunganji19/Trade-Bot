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
from datetime import datetime, timedelta, timezone

import pandas as pd

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
    Sources sitting out a failure cooldown are skipped in BOTH the automatic
    chain and explicit-source requests (a dead exchange must not be hammered
    just because the operator named it); one probe re-earns a bench once it
    expires."""
    chain = _CRYPTO_SOURCES if source == "auto" else [source]
    errors: list[str] = []
    benched = [s for s in chain if not _source_healthy(s)]
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
        return _validate_ohlcv(_rows_to_df(rows), f"crypto:{src}",
                               kind="crypto", timeframe=timeframe)
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
        df = _rows_to_df(all_rows)
        df = df[~df.index.duplicated(keep="first")]
        return _validate_ohlcv(df, f"crypto:{src}", kind="crypto", timeframe=timeframe)
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
    cap = _YAHOO_DAYS_CAP.get(timeframe)
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
    _enforce_yahoo_cap(timeframe, days, start, end, origin="forex")
    interval = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "1h", "1d": "1d"}[timeframe]
    period_map = {"1m": "7d", "5m": "60d", "15m": "60d", "1h": "730d", "4h": "730d", "1d": "5y"}
    df = _yahoo_ohlcv(symbol, interval, start, end,
                      period_map[timeframe], origin="forex")
    if timeframe == "4h":  # resample 1h -> 4h (UTC bins suit 24x5 spot FX)
        df = _resample_1h_to_4h(df)
    # spot-FX caliber: auto_adjust=True is a no-op for FX (no splits/divs) —
    # the series is raw spot, NOT dividend/split-adjusted equity. Stamped
    # distinctly from a raw series so downstream code never mixes
    # the two bases.
    df = _validate_ohlcv(df, "forex:yahoo(auto_adjust=True)", caliber="spot-fx",
                         kind="forex", timeframe=timeframe)
    df.attrs["source"] = "yahoo"
    return df


# --------------------------------------------------------------------------- quality
# Quarantine floor: after dropping corrupt bars, fewer than this many bars
# left means the frame cannot support the indicator warmup (ema200 needs
# ~200 bars; the standard nutric 220-bar warmup is the floor). Small CLEAN
# frames (e.g. limit=50 latest-bars fetches) are NOT subject to this floor —
# it applies only when quarantine actually dropped bars.
_QUARANTINE_MIN_BARS = 220
# Frozen-feed flat-close-run ceiling per timeframe (consecutive identical
# closes). 1h allows 60 so the 48-bar synthetic fixtures stay valid while a
# genuinely stuck feed (days of one price) still trips.
_FLAT_RUN_MAX = {"1m": 120, "5m": 120, "15m": 100, "1h": 60, "4h": 30, "1d": 10}


def _max_gap_seconds(kind: str, timeframe: str) -> float:
    tf_s = TIMEFRAME_SECONDS.get(timeframe, 3600)
    if kind == "forex":
        # 24x5: allow the Fri->Sun weekend gap (~48h) plus margin
        return max(5.0 * tf_s, 3.0 * 86400.0)
    return 5.0 * tf_s  # crypto trades 24/7: any 5-bar hole is suspicious


def _infer_kind(origin: str) -> str:
    o = (origin or "").lower()
    if "forex" in o:
        return "forex"
    return "crypto"


def _validate_ohlcv(df: pd.DataFrame, origin: str, caliber: str = "raw",
                    kind: str | None = None, timeframe: str | None = None,
                    quarantine_min_bars: int = _QUARANTINE_MIN_BARS) -> pd.DataFrame:
    """Quarantine corrupt bars instead of failing the whole frame.

    Bad PRICE bars (non-positive/NaN prices, high/low not bracketing the
    body) are DROPPED + logged; the frame fails only when fewer than
    `quarantine_min_bars` (default 220, the indicator warmup) remain. Clean
    frames of any size pass (a limit=50 latest-bars fetch is valid — the
    floor applies only when quarantine dropped bars). A corrupt index
    (unsorted/duplicates) still fails loudly: reordering silently would
    rewrite causality. `caliber` states the price basis ('raw' exchange
    trades vs 'adjusted-equity' Yahoo equities vs 'spot-fx' Yahoo FX) —
    stamped here so EVERY validated frame carries the truth. Frozen-feed
    guards (max-gap per kind/timeframe + flat-close-run) raise: a stuck feed
    must never silently become signals.
    """
    required = ["open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"{origin}: missing columns {missing}")
    if df.empty:
        raise RuntimeError(f"{origin}: empty frame")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise RuntimeError(f"{origin}: index not sorted/unique")
    eff_kind = kind or _infer_kind(origin)
    df = df.copy()
    quarantined = 0
    notes: list[str] = []
    prices = df[["open", "high", "low", "close"]]
    bad_px_mask = (prices <= 0).any(axis=1) | prices.isna().any(axis=1)
    if bad_px_mask.any():
        n_bad = int(bad_px_mask.sum())
        first_bad = df.index[bad_px_mask][0]
        df = df[~bad_px_mask]
        quarantined += n_bad
        notes.append(f"{n_bad} non-positive/NaN prices (first at {first_bad})")
    if len(df):
        bad_hilo_mask = (df["high"] < df[["open", "close"]].max(axis=1)) | \
                        (df["low"] > df[["open", "close"]].min(axis=1))
        if bad_hilo_mask.any():
            n_bad = int(bad_hilo_mask.sum())
            first_bad = df.index[bad_hilo_mask][0]
            df = df[~bad_hilo_mask]
            quarantined += n_bad
            notes.append(f"{n_bad} high/low not bracketing the body "
                         f"(first at {first_bad})")
    if quarantined:
        print(f"[data] {origin}: quarantined {quarantined} bad bars "
              f"({'; '.join(notes)}), {len(df)} remain")
    if df.empty:
        raise RuntimeError(
            f"{origin}: empty frame after quarantining {quarantined} bad bars "
            f"({'; '.join(notes)})")
    if quarantined and len(df) < quarantine_min_bars:
        raise RuntimeError(
            f"{origin}: quarantined {quarantined} bad bars ({'; '.join(notes)}): "
            f"only {len(df)} remain (need >= {quarantine_min_bars})")
    # frozen-feed guards: max-gap per kind/timeframe + flat-close-run
    try:
        diffs = df.index.to_series().diff().dt.total_seconds().dropna()
    except Exception:
        diffs = pd.Series(dtype=float)
    if len(diffs):
        eff_tf = timeframe or "1h"
        max_gap = _max_gap_seconds(eff_kind, eff_tf)
        worst = float(diffs.max())
        if worst > max_gap:
            idx = diffs.idxmax()
            raise RuntimeError(
                f"{origin}: max gap {worst / 3600.0:.1f}h at {idx} exceeds "
                f"{max_gap / 3600.0:.1f}h for {eff_kind}/{eff_tf} (frozen feed?)")
    flat_max = _FLAT_RUN_MAX.get(timeframe or "1h", 60)
    try:
        closes = df["close"].to_numpy()
        longest = cur = 1
        for a, b in zip(closes[:-1], closes[1:]):
            if a == b:
                cur += 1
                longest = max(longest, cur)
            else:
                cur = 1
        if longest >= flat_max:
            raise RuntimeError(
                f"{origin}: flat close run of {longest} bars "
                f"(>= {flat_max} for {timeframe or '1h'}; frozen feed?)")
    except RuntimeError:
        raise
    except Exception:
        pass
    df.attrs["caliber"] = caliber
    return df


def _drop_forming_bar(df: pd.DataFrame, timeframe: str, kind: str = "crypto",
                      now: datetime | None = None) -> pd.DataFrame:
    """Remove the still-open candle if it is the last bar (live semantics:
    only CLOSED bars are tradable). The forming bar's close moves; acting on
    it would be trading a price that doesn't exist yet.

    Session-aware: when the market is CLOSED there is no forming bar — the
    last stored bar is closed by definition. Forex keeps the last bar over
    the weekend close; crypto trades 24/7 so wall-clock always applies.
    `now` is injectable (UTC-aware) for deterministic tests.
    """
    if not len(df):
        return df
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if kind == "forex":
        # spot FX closes Fri 22:00 UTC -> Sun 22:00 UTC; no forming bar then
        if now.weekday() >= 5:
            return df
    now_ms = now.timestamp() * 1000
    tf_ms = TIMEFRAME_SECONDS.get(timeframe, 60) * 1000
    try:
        last_ms = df.index[-1].timestamp() * 1000
    except Exception:
        return df
    if now_ms < last_ms + tf_ms:  # last bar's period hasn't closed yet
        return df.iloc[:-1]
    return df


def _disk_cache_path(spec: MarketSpec, days: int | None, start: str | None = None,
                     end: str | None = None) -> str:
    # kind-prefixed names: without the kind, crypto BTCUSDT/1h and forex or
    # stems that normalize identically collide and a forex frame can be
    # served for a crypto request (or vice versa). The kind prefix keeps the
    # three books' caches disjoint; the rolling glob must use the same prefix.
    safe = _safe_name(spec.symbol)
    kind = spec.kind
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
                                f"{kind}_{safe}_{spec.timeframe}_{start}_{end}.parquet")
        # start-without-end: the window ROLLS (fetches up to now), so a
        # constant name would freeze day 1's frame forever and never prune.
        # Embed the fetch date: each day is its own cache entry, pruned by
        # the rolling rule like any other.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        return os.path.join(CONFIG.data_cache_dir,
                            f"{kind}_{safe}_{spec.timeframe}_{start}_now_{stamp}.parquet")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return os.path.join(CONFIG.data_cache_dir,
                        f"{kind}_{safe}_{spec.timeframe}_{days}d_{stamp}.parquet")


def _manifest_lookup_attrs(cache_file: str, spec: MarketSpec | None = None) -> dict:
    """Best-effort attrs recovery from data/manifest.json for a cache file.

    Matches by cache_file basename first (exact provenance), then by the
    spec key. Returns {} when the manifest is missing/unreadable.
    """
    try:
        manifest_path = os.path.join(os.path.dirname(CONFIG.db_path), "manifest.json")
        if not os.path.exists(manifest_path):
            return {}
        with open(manifest_path) as fh:
            manifest = json.load(fh)
        if not isinstance(manifest, dict):
            return {}
        base = os.path.basename(cache_file)
        for entry in manifest.values():
            if isinstance(entry, dict) and entry.get("cache_file") == base:
                out = {}
                if entry.get("source"):
                    out["source"] = str(entry["source"])
                if entry.get("caliber"):
                    out["caliber"] = str(entry["caliber"])
                return out
        if spec is not None:
            entry = manifest.get(f"{spec.kind}:{spec.symbol}:{spec.timeframe}")
            if isinstance(entry, dict):
                out = {}
                if entry.get("source"):
                    out["source"] = str(entry["source"])
                if entry.get("caliber"):
                    out["caliber"] = str(entry["caliber"])
                return out
    except Exception:
        pass
    return {}


def _load_cached(path: str, spec: MarketSpec | None = None) -> pd.DataFrame | None:
    """Read a cached parquet and re-attach source/caliber attrs.

    df.attrs do not survive a plain parquet round-trip, so the writer stores
    them in the parquet schema metadata (pyarrow) and the manifest records
    them as fallback. Without re-attachment a cache hit returns a frame with
    empty attrs — downstream caliber checks then misread it.
    """
    if not os.path.exists(path):
        return None
    try:
        df: pd.DataFrame | None = None
        meta: dict = {}
        try:
            import pyarrow.parquet as _pq
            table = _pq.read_table(path)
            raw = table.schema.metadata or {}
            for k, v in raw.items():
                try:
                    ks = k.decode() if isinstance(k, bytes) else str(k)
                    vs = v.decode() if isinstance(v, bytes) else str(v)
                    meta[ks] = vs
                except Exception:
                    continue
            df = table.to_pandas()
            if getattr(df.index, "tz", None) is None:
                try:
                    df.index = pd.to_datetime(df.index, utc=True)
                except Exception:
                    pass
        except Exception:
            df = pd.read_parquet(path)
        if df is None:
            return None
        # re-attach from file metadata, else from the manifest fallback
        for key in ("source", "caliber", "kind", "symbol", "timeframe"):
            if key in meta and meta[key]:
                df.attrs[key] = meta[key]
        if ("source" not in df.attrs or "caliber" not in df.attrs) and spec is not None:
            fb = _manifest_lookup_attrs(path, spec)
            for k, v in fb.items():
                df.attrs.setdefault(k, v)
            if "kind" not in df.attrs:
                df.attrs["kind"] = spec.kind
            if "symbol" not in df.attrs:
                df.attrs["symbol"] = spec.symbol
            if "timeframe" not in df.attrs:
                df.attrs["timeframe"] = spec.timeframe
            if "caliber" not in df.attrs:
                df.attrs["caliber"] = "spot-fx" if spec.kind == "forex" else "raw"
            if "source" not in df.attrs:
                df.attrs["source"] = "yahoo" if spec.kind == "forex" else "unknown"
        return df
    except Exception:
        return None


def _store_cached(path: str, df: pd.DataFrame) -> bool:
    """Persist a frame with source/caliber in the parquet schema metadata.

    Returns True on success, False + a log line on failure (callers surface
    the failure instead of silently skipping the cache write).
    """
    try:
        os.makedirs(CONFIG.data_cache_dir, exist_ok=True)
        meta = {k: str(df.attrs.get(k, "")) for k in
                ("source", "caliber", "kind", "symbol", "timeframe") if df.attrs.get(k)}
        # ensure kind/symbol/timeframe travel even when callers only stamped
        # source/caliber: empty strings carry no signal, so drop them
        meta = {k: v for k, v in meta.items() if v}
        try:
            import pyarrow as _pa
            import pyarrow.parquet as _pq
            table = _pa.Table.from_pandas(df, preserve_index=True)
            merged = dict(table.schema.metadata or {})
            for k, v in meta.items():
                merged[k.encode()] = v.encode()
            table = table.replace_schema_metadata(merged)
            _pq.write_table(table, path)
        except Exception:
            df.to_parquet(path)
        return True
    except Exception as exc:
        print(f"[data] cache write failed for {os.path.basename(path)}: "
              f"{type(exc).__name__}: {exc}")
        return False


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


# Rolling-cache-hit sanity floor: a cache hit with fewer bars than this is
# treated as corrupt/stale and refetched (the indicator warmup itself is
# enforced by lab/backtest at 220 bars — this floor only rejects degenerate
# cache files, never a legitimate small fetch).
_CACHE_MIN_BARS = 30


def _load_cached_compat(path: str, spec: MarketSpec | None = None) -> pd.DataFrame | None:
    """Call the module-level _load_cached tolerating 1-arg monkeypatches.

    Tests (and external callers) patch _load_cached with `lambda p: ...`;
    calling it with (path, spec) would TypeError. Try the 2-arg form first,
    fall back to 1-arg so old patches keep working (without manifest
    re-attachment).
    """
    try:
        return _load_cached(path, spec)
    except TypeError:
        try:
            return _load_cached(path)  # type: ignore[call-arg]
        except TypeError:
            return None


def _validate_cache_hit(df: pd.DataFrame, spec: MarketSpec, path: str) -> pd.DataFrame | None:
    """Re-run validation + min-bars on a cache hit. Returns the validated
    frame, or None when the hit must be ignored (corrupt, frozen, or too
    small) so the caller refetches. Never raises for a stale hit."""
    try:
        caliber = df.attrs.get("caliber") or (
            "spot-fx" if spec.kind == "forex" else "raw")
        checked = _validate_ohlcv(
            df, f"cache:{spec.kind}:{spec.symbol}:{spec.timeframe}",
            caliber=caliber, kind=spec.kind, timeframe=spec.timeframe)
    except Exception as exc:
        print(f"[data] ignoring corrupt cache {os.path.basename(path)}: {exc}")
        return None
    if len(checked) < _CACHE_MIN_BARS:
        print(f"[data] ignoring undersized cache {os.path.basename(path)}: "
              f"{len(checked)} bars < {_CACHE_MIN_BARS}")
        return None
    # preserve source when validation re-stamped only caliber
    if "source" not in checked.attrs and "source" in df.attrs:
        checked.attrs["source"] = df.attrs["source"]
    return checked


def _record_manifest(spec: MarketSpec, path: str, df: pd.DataFrame) -> bool:
    """Record the fetch's provenance in data/manifest.json (window, bar count,
    sha256 of the cached parquet, source, caliber, kind, fetch date) — the artifact
    that makes 'every number reproducible from this repo' a checkable claim.
    Returns True on success, False + a log line on failure (callers surface
    the failure instead of silently skipping provenance)."""
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
            "kind": spec.kind,
            "symbol": spec.symbol,
            "timeframe": spec.timeframe,
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
        return True
    except Exception as exc:
        print(f"[data] manifest write failed for {spec.kind}:{spec.symbol}:"
              f":{spec.timeframe}: {type(exc).__name__}: {exc}")
        return False


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
    data/manifest.json (bars, checksum, source, caliber, kind) and rolling
    cache entries older than 14 days are pruned; a fresh reuse mutates
    nothing. Cache hits are re-validated (_validate_ohlcv + min-bars) so a
    corrupt/stale file refetches instead of poisoning signals."""
    # stamp kind/symbol/timeframe on fresh fetches so the parquet metadata
    # round-trip carries provenance even before the manifest fallback
    def _stamp(df: pd.DataFrame) -> pd.DataFrame:
        try:
            df.attrs.setdefault("kind", spec.kind)
            df.attrs.setdefault("symbol", spec.symbol)
            df.attrs.setdefault("timeframe", spec.timeframe)
        except Exception:
            pass
        return df

    if start and end:
        # PINNED window: EXACT-NAME, byte-identical semantics — this is the
        # reproducibility artifact behind BACKTESTS.md. This branch must
        # NEVER glob or reuse a differently-named file: a rolling file
        # (days-form or _now-form for the same symbol) covers a DIFFERENT
        # window than the pinned one being asked for; freshness-bounded reuse
        # here would quietly re-roll the pinned numbers.
        path = _disk_cache_path(spec, days, start, end)
        cached = _load_cached_compat(path, spec)
        if cached is not None:
            hit = _validate_cache_hit(cached, spec, path)
            if hit is not None:
                return hit
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
            pattern = f"{spec.kind}_{safe}_{spec.timeframe}_{norm_start}_now_*.parquet"
        else:
            pattern = f"{spec.kind}_{safe}_{spec.timeframe}_{days}d_*.parquet"
        fresh = _find_fresh_rolling_cache(pattern)
        if fresh is not None:
            fresh_path, age = fresh
            cached = _load_cached_compat(fresh_path, spec)
            if cached is not None:
                hit = _validate_cache_hit(cached, spec, fresh_path)
                if hit is not None:
                    # the reuse path must not mutate anything: no manifest record
                    # (it would claim a fetch that never happened), no prune, no
                    # re-store. Read-only, then return.
                    print(f"[data] reusing cached {os.path.basename(fresh_path)} "
                          f"(age {age:.1f}h ≤ {_cache_freshness_hours():.0f}h "
                          f"freshness window)")
                    return hit
                # corrupt/undersized hit: fall through to refetch below
        path = _disk_cache_path(spec, days, start, end)
    if spec.kind == "crypto":
        df = fetch_crypto_history(spec.symbol, spec.timeframe, days or 365,
                                  start=start, end=end)
        df = _drop_forming_bar(df, spec.timeframe, kind="crypto")
    else:
        df = fetch_forex_ohlcv(spec.symbol, spec.timeframe, start=start, end=end,
                               days=days)
        df = _drop_forming_bar(df, spec.timeframe, kind="forex")
    df = _stamp(df)
    ok_cache = _store_cached(path, df)
    ok_manifest = _record_manifest(spec, path, df)
    if not ok_cache or not ok_manifest:
        # surface cache-write failures instead of silently skipping: the fetch
        # itself succeeded (df is returned), but provenance/reuse is degraded
        print(f"[data] warning: cache persistence degraded for "
              f"{spec.kind}:{spec.symbol}:{spec.timeframe} "
              f"(stored={ok_cache} manifest={ok_manifest})")
    _prune_rolling_cache()
    return df


class MarketData:
    """TTL-cached latest-candles provider for the live/paper engine.

    Always drops the still-forming candle: the engine must only ever evaluate
    closed bars (backtest/live parity).

    `ttl_seconds` overrides the default timeframe-scaled TTL (max(tf/2, 15s)):
    the HFT book passes a small value (2s) — at 1m cadence the default 30s
    cache would be the dominant latency between a bar closing and the bot
    acting on it, and paper trading lets us poll as fast as we like.
    """

    def __init__(self, ttl_seconds: float | None = None):
        self._cache: dict = {}
        self._ttl_override = ttl_seconds

    def latest(self, spec: MarketSpec, limit: int | None = None) -> pd.DataFrame:
        limit = limit or CONFIG.lookback_bars
        # TTL scales with the timeframe: a 1d-bar fetch needs far fewer
        # refreshes than a 5m one. (The old ttl_by_tf=False -> 30s branch was
        # unreachable — no constructor ever passed it.) An explicit
        # ttl_seconds beats the scale (the HFT book's low-latency mode).
        ttl = (self._ttl_override if self._ttl_override is not None
               else max(TIMEFRAME_SECONDS[spec.timeframe] * 0.5, 15))
        # limit is part of the key: the dashboard's marks fetch with limit=2,
        # and caching that 2-bar frame under the engine's key made the next
        # cycle skip the market entirely (len(df) < 60 -> silent no-trade)
        key = (spec.kind, spec.symbol, spec.timeframe, limit)
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        if spec.kind == "crypto":
            df = fetch_crypto_ohlcv(spec.symbol, spec.timeframe, limit)
        else:
            df = fetch_forex_ohlcv(spec.symbol, spec.timeframe).tail(limit)
        df = _drop_forming_bar(df, spec.timeframe, kind=spec.kind)
        self._cache[key] = (now, df)
        return df


# --------------------------------------------------------------------------- news
