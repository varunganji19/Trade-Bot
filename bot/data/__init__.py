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

import os
import time

import pandas as pd

from config import CONFIG, MarketSpec, TIMEFRAME_SECONDS

# The package's public and test-reachable names. Each submodule looks them
# up here at call time, so patching bot.data.<name> reaches every caller.
from bot.data.sources import (  # noqa: E402,F401
    _CRYPTO_SOURCES,
    _ccxt_clients,
    _SOURCE_COOLDOWN_S,
    _SOURCE_FAIL_STRIKES,
    _source_fails,
    _source_healthy,
    _note_source_result,
    _crypto_client,
    _ohlcv_rows_from,
    _fetch_with_fallback,
    fetch_crypto_ohlcv,
    fetch_crypto_history,
    _safe_name,
    _yahoo_ohlcv,
    _YAHOO_DAYS_CAP,
    _enforce_yahoo_cap,
    _resample_1h_to_4h,
    fetch_forex_ohlcv,
    _rows_to_df)
from bot.data.validate import (  # noqa: E402,F401
    _FX_CLOSE_HOUR, _FX_CLOSE_WEEKDAY, _FX_OPEN_HOUR, _FX_OPEN_WEEKDAY,
    _QUARANTINE_MIN_BARS,
    _FLAT_RUN_MAX,
    _weekend_seconds,
    _max_gap_seconds,
    _infer_kind,
    _validate_ohlcv,
    _drop_forming_bar)
from bot.data.cache import (  # noqa: E402,F401
    _disk_cache_path,
    _manifest_lookup_attrs,
    _load_cached,
    _store_cached,
    _cache_freshness_hours,
    _find_fresh_rolling_cache,
    _prune_rolling_cache,
    _CACHE_MIN_BARS,
    _load_cached_compat,
    _validate_cache_hit,
    _record_manifest)


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
        # reproducibility artifact behind docs/archive/BACKTESTS.md. This branch must
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
        # old. (docs/archive/HISTORY.md Fix 4.1: freshness is the honest bound.) The
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
