"""OHLCV validation: every frame is checked (columns, positive prices, high/low
bracketing the body, sorted unique UTC index, weekend-aware gap guard)
before indicators see it, and the still-forming bar is dropped."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

import bot.data as base   # every data function is looked up on the package
                          # at call time, so tests can patch bot.data.<name>
from config import TIMEFRAME_SECONDS


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


# spot FX closes Fri 22:00 UTC and reopens Sun 22:00 UTC
_FX_CLOSE_WEEKDAY, _FX_CLOSE_HOUR = 4, 22     # Friday
_FX_OPEN_WEEKDAY, _FX_OPEN_HOUR = 6, 22       # Sunday


def _weekend_seconds(start, end) -> float:
    """Seconds between `start` and `end` that fall inside the FX weekend.

    WHY: the frozen-feed guard used to compare a WALL-CLOCK gap against a
    flat 72h allowance for forex. Yahoo routinely drops the bars either side
    of the close, so an ordinary weekend shows up as e.g. Fri 15:00 -> Mon
    19:00 = 76h and the guard refused a perfectly good frame — EUR/USD and
    GBP/USD errored on every single cycle of the live standard book. For a
    24x5 market the meaningful quantity is TRADING time, so the weekend is
    subtracted before the comparison."""
    if end <= start:
        return 0.0
    total = 0.0
    # walk each calendar day the span touches; the weekend window is at most
    # one per week, so this is a handful of iterations for any sane gap
    day = (start - pd.Timedelta(days=3)).normalize()
    while day <= end:
        if day.weekday() == base._FX_CLOSE_WEEKDAY:
            w_start = day + pd.Timedelta(hours=base._FX_CLOSE_HOUR)
            w_end = day + pd.Timedelta(days=2, hours=base._FX_OPEN_HOUR)
            lo, hi = max(w_start, start), min(w_end, end)
            if hi > lo:
                total += (hi - lo).total_seconds()
        day += pd.Timedelta(days=1)
    return total


def _max_gap_seconds(kind: str, timeframe: str) -> float:
    """Allowed gap. For forex this is TRADING-time (see _weekend_seconds):
    two trading days covers a holiday plus Yahoo's ragged weekend edges,
    while a genuinely frozen feed still trips it."""
    tf_s = TIMEFRAME_SECONDS.get(timeframe, 3600)
    if kind == "forex":
        return max(5.0 * tf_s, 2.0 * 86400.0)
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
    eff_kind = kind or base._infer_kind(origin)
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
        max_gap = base._max_gap_seconds(eff_kind, eff_tf)
        if eff_kind == "forex":
            # measure TRADING time: subtract the weekend the gap spans
            trading = {}
            for idx, seconds in diffs.items():
                prev = idx - pd.Timedelta(seconds=float(seconds))
                trading[idx] = float(seconds) - base._weekend_seconds(prev, idx)
            idx = max(trading, key=trading.get)
            worst = trading[idx]
            wall = float(diffs.loc[idx])
        else:
            idx = diffs.idxmax()
            worst = wall = float(diffs.max())
        if worst > max_gap:
            raise RuntimeError(
                f"{origin}: max gap {worst / 3600.0:.1f}h of trading time "
                f"({wall / 3600.0:.1f}h wall clock) at {idx} exceeds "
                f"{max_gap / 3600.0:.1f}h for {eff_kind}/{eff_tf} (frozen feed?)")
    flat_max = base._FLAT_RUN_MAX.get(timeframe or "1h", 60)
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
