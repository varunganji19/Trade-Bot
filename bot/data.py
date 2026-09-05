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
between runs.
"""
from __future__ import annotations

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


def _crypto_client(source: str):
    if source not in _ccxt_clients:
        import ccxt
        cfg = {"enableRateLimit": True}
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
    loop and the all-sources-failed error tail used to be copy-pasted)."""
    chain = _CRYPTO_SOURCES if source == "auto" else [source]
    errors: list[str] = []
    for src in chain:
        try:
            df = fetch_one(src)
            df.attrs["source"] = src
            return df
        except Exception as exc:
            errors.append(f"{src}: {type(exc).__name__}: {exc}")
    raise RuntimeError(f"all crypto sources failed for {symbol} {timeframe}: " + "; ".join(errors))


def fetch_crypto_ohlcv(symbol: str, timeframe: str, limit: int = 400,
                       source: str = "auto") -> pd.DataFrame:
    """Latest `limit` bars with a fallback chain; raises if every source fails."""
    def fetch_one(src):
        rows = _ohlcv_rows_from(src, symbol, timeframe, None, limit)
        return _validate_ohlcv(_rows_to_df(rows), f"crypto:{src}")
    return _fetch_with_fallback(symbol, timeframe, source, fetch_one)


def fetch_crypto_history(symbol: str, timeframe: str, days: int,
                         source: str = "auto") -> pd.DataFrame:
    """Paginated fetch of `days` of history for backtesting (with fallback)."""
    tf_ms = TIMEFRAME_SECONDS[timeframe] * 1000

    def fetch_one(src):
        ex = _crypto_client(src)
        since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
        all_rows: list = []
        while True:
            rows = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
            if not rows:
                break
            all_rows.extend(rows)
            last = rows[-1][0]
            if len(rows) < 1000 or last >= ex.milliseconds() - tf_ms:
                break
            since = last + tf_ms
            time.sleep(ex.rateLimit / 1000.0)
        if not all_rows:
            raise RuntimeError(f"{src} returned no data")
        df = _rows_to_df(all_rows)
        df = df[~df.index.duplicated(keep="first")]
        return _validate_ohlcv(df, f"crypto:{src}")
    return _fetch_with_fallback(symbol, timeframe, source, fetch_one)


# --------------------------------------------------------------------------- forex
def fetch_forex_ohlcv(symbol: str, timeframe: str) -> pd.DataFrame:
    # no `days` argument: Yahoo's period is fixed per interval (period_map
    # below) — the old days= param was silently ignored, so it's gone
    import yfinance as yf
    period_map = {"5m": "60d", "15m": "60d", "1h": "730d", "4h": "730d", "1d": "5y"}
    # Yahoo granularity constraints: 5m/15m max 60d back, 1h max 730d.
    interval = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "1h", "1d": "1d"}[timeframe]
    df = yf.download(symbol, period=period_map[timeframe], interval=interval,
                     auto_adjust=True, progress=False)
    if df is None or df.empty:
        raise RuntimeError(f"No forex data for {symbol}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df.index = pd.to_datetime(df.index, utc=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if timeframe == "4h":  # resample 1h -> 4h
        df = df.resample("4h").agg({"open": "first", "high": "max", "low": "min",
                                    "close": "last", "volume": "sum"}).dropna()
    df = _validate_ohlcv(df, f"forex:yahoo(auto_adjust=True)")
    df.attrs["caliber"] = "adjusted"
    df.attrs["source"] = "yahoo"
    return df


# --------------------------------------------------------------------------- quality
def _validate_ohlcv(df: pd.DataFrame, origin: str) -> pd.DataFrame:
    """Fail loudly on corrupt/mixed-caliber bars instead of poisoning signals."""
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
    df.attrs["caliber"] = "raw"
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


def _disk_cache_path(spec: MarketSpec, days: int) -> str:
    safe = spec.symbol.replace("/", "").replace("=X", "")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return os.path.join(CONFIG.data_cache_dir, f"{safe}_{spec.timeframe}_{days}d_{stamp}.parquet")


def _load_cached(spec: MarketSpec, days: int) -> pd.DataFrame | None:
    path = _disk_cache_path(spec, days)
    if os.path.exists(path):
        try:
            df = pd.read_parquet(path)
            return df
        except Exception:
            return None
    return None


def _store_cached(spec: MarketSpec, days: int, df: pd.DataFrame):
    try:
        os.makedirs(CONFIG.data_cache_dir, exist_ok=True)
        df.to_parquet(_disk_cache_path(spec, days))
    except Exception:
        pass  # caching is best-effort; never block a fetch


# --------------------------------------------------------------------------- unified
def _rows_to_df(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").astype(float)
    return df


def fetch_history(spec: MarketSpec, days: int, use_cache: bool = True) -> pd.DataFrame:
    """History for backtests. Same-day results are served from disk cache so
    repeated research runs are byte-identical and don't hammer public APIs."""
    if use_cache:
        cached = _load_cached(spec, days)
        if cached is not None:
            return cached
    if spec.kind == "crypto":
        df = fetch_crypto_history(spec.symbol, spec.timeframe, days)
        df = _drop_forming_bar(df, spec.timeframe)
    else:
        df = fetch_forex_ohlcv(spec.symbol, spec.timeframe)
    if use_cache:
        _store_cached(spec, days, df)
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


def fetch_news(max_items: int | None = None) -> list:
    """Fetch RSS headlines from the configured feeds. Cached, failure-tolerant."""
    max_items = max_items or CONFIG.news_max_items
    now = time.time()
    if now - _NEWS_CACHE["ts"] < CONFIG.news_ttl_seconds and _NEWS_CACHE["items"]:
        return _NEWS_CACHE["items"][:max_items]

    items: list = []
    for url in CONFIG.news_feeds:
        try:
            resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0 (algo-bot)"},
                                allow_redirects=False)
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
