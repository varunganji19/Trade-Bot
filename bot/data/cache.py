"""Backtest data cache: per symbol/timeframe/day parquet files, the
provenance manifest (bars, checksum, source, caliber), freshness-bounded
reuse of rolling windows and exact-name reuse of pinned windows."""
from __future__ import annotations

import glob
import json
import os
import time
from datetime import datetime, timezone

import pandas as pd

import bot.data as base   # every data function is looked up on the package
                          # at call time, so tests can patch bot.data.<name>
from config import CONFIG, MarketSpec


def _disk_cache_path(spec: MarketSpec, days: int | None, start: str | None = None,
                     end: str | None = None) -> str:
    # kind-prefixed names: without the kind, crypto BTCUSDT/1h and forex or
    # stems that normalize identically collide and a forex frame can be
    # served for a crypto request (or vice versa). The kind prefix keeps the
    # three books' caches disjoint; the rolling glob must use the same prefix.
    safe = base._safe_name(spec.symbol)
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
            fb = base._manifest_lookup_attrs(path, spec)
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
    if age_hours > base._cache_freshness_hours():
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
        return base._load_cached(path, spec)
    except TypeError:
        try:
            return base._load_cached(path)  # type: ignore[call-arg]
        except TypeError:
            return None


def _validate_cache_hit(df: pd.DataFrame, spec: MarketSpec, path: str) -> pd.DataFrame | None:
    """Re-run validation + min-bars on a cache hit. Returns the validated
    frame, or None when the hit must be ignored (corrupt, frozen, or too
    small) so the caller refetches. Never raises for a stale hit."""
    try:
        caliber = df.attrs.get("caliber") or (
            "spot-fx" if spec.kind == "forex" else "raw")
        checked = base._validate_ohlcv(
            df, f"cache:{spec.kind}:{spec.symbol}:{spec.timeframe}",
            caliber=caliber, kind=spec.kind, timeframe=spec.timeframe)
    except Exception as exc:
        print(f"[data] ignoring corrupt cache {os.path.basename(path)}: {exc}")
        return None
    if len(checked) < base._CACHE_MIN_BARS:
        print(f"[data] ignoring undersized cache {os.path.basename(path)}: "
              f"{len(checked)} bars < {base._CACHE_MIN_BARS}")
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
        # re-read right before the write and merge, so two processes fetching
        # at once keep each other's entries; the per-pid tmp stops both
        # writers clobbering through one shared .tmp path.
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
