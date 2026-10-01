"""Statistics behind the promotion gate: intervals and market regimes.

A median profit factor over a few folds says nothing about how sure we are.
These helpers turn a list of out-of-sample trade P&Ls into a bootstrap
interval, and label each trade with the market regime it was opened in, so
the gate can ask two questions a point estimate cannot answer:

  * is the LOWER end of a 90% interval on profit factor still >= 1.0?
  * did the strategy see more than one kind of market?

Everything here is deterministic (fixed seed) so verdicts are reproducible.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

# A resample with no losing trade has an infinite profit factor. Capping it
# keeps intervals finite and JSON-safe; any value this large already says
# "no losses in the sample", which is all the cap hides.
PF_CAP = 99.0

TREND_UP, TREND_DOWN, RANGE = "trend_up", "trend_down", "range"
REGIMES = (TREND_UP, TREND_DOWN, RANGE)


def profit_factor(pnls) -> float | None:
    """Gross profit / gross loss, capped at PF_CAP. None for no trades."""
    a = np.asarray(pnls, dtype=float)
    if a.size == 0:
        return None
    gain = a[a > 0].sum()
    loss = -a[a < 0].sum()
    if loss <= 0:
        return PF_CAP if gain > 0 else None
    return float(min(gain / loss, PF_CAP))


def sharpe_per_trade(pnls) -> float | None:
    """Mean / standard deviation of per-trade P&L (not annualised: trades are
    irregular in time, so an annualisation factor would be invented)."""
    a = np.asarray(pnls, dtype=float)
    if a.size < 2:
        return None
    sd = a.std(ddof=1)
    if sd <= 0:
        return None
    return float(a.mean() / sd)


def _block_indices(n: int, block: int, rng: np.random.Generator) -> np.ndarray:
    """One circular block-bootstrap resample of range(n)."""
    starts = rng.integers(0, n, size=math.ceil(n / block))
    idx = (starts[:, None] + np.arange(block)[None, :]).ravel() % n
    return idx[:n]


def bootstrap_interval(pnls, stat, *, n_boot: int = 2000, alpha: float = 0.10,
                       block: int | None = None, seed: int = 0) -> tuple:
    """Percentile interval of `stat` under a circular block bootstrap.

    Trades are resampled in contiguous blocks (default length n^(1/3)) so
    that streaks — a strategy losing for a week in one regime — stay intact
    instead of being shuffled into independence, which would make the
    interval too narrow. Pass P&Ls in time order. Returns (lo, hi), or
    (None, None) when there are too few trades to resample."""
    a = np.asarray(pnls, dtype=float)
    n = a.size
    if n < 5:
        return None, None
    block = block or max(1, round(n ** (1 / 3)))
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n_boot):
        v = stat(a[_block_indices(n, block, rng)])
        if v is not None:
            vals.append(v)
    if len(vals) < n_boot // 2:
        return None, None
    lo, hi = np.quantile(vals, [alpha / 2, 1 - alpha / 2])
    return round(float(lo), 3), round(float(hi), 3)


def trade_evidence(pnls, **kwargs) -> dict:
    """Point estimates and 90% intervals for profit factor and Sharpe."""
    pf_lo, pf_hi = bootstrap_interval(pnls, profit_factor, **kwargs)
    sh_lo, sh_hi = bootstrap_interval(pnls, sharpe_per_trade, **kwargs)
    pf = profit_factor(pnls)
    sh = sharpe_per_trade(pnls)
    return {"trades": int(len(pnls)),
            "pf": None if pf is None else round(pf, 3), "pf_lo": pf_lo, "pf_hi": pf_hi,
            "sharpe": None if sh is None else round(sh, 4),
            "sharpe_lo": sh_lo, "sharpe_hi": sh_hi}


def label_regimes(daily: pd.DataFrame, *, sma_days: int = 200, slope_days: int = 20,
                  adx_min: float = 20.0) -> pd.Series:
    """Label each day trend_up / trend_down / range from DAILY bars.

    trend_up:   ADX(14) >= adx_min and the 200-day SMA rose over 20 days
    trend_down: ADX(14) >= adx_min and the SMA fell
    range:      ADX(14) below adx_min

    Causal: the label for day d uses data through day d-1 only (shifted one
    day), so labelling a trade by its entry time never peeks at the day it
    traded in. Days without enough history are NaN, not guessed."""
    from bot.indicators import adx
    close = daily["close"].astype(float)
    sma = close.rolling(sma_days, min_periods=sma_days).mean()
    slope = sma - sma.shift(slope_days)
    strength = adx(daily, 14)
    lab = pd.Series(np.nan, index=daily.index, dtype=object)
    ok = sma.notna() & slope.notna() & strength.notna()
    trending = ok & (strength >= adx_min)
    lab[trending & (slope > 0)] = TREND_UP
    lab[trending & (slope < 0)] = TREND_DOWN
    lab[ok & (strength < adx_min)] = RANGE
    return lab.shift(1)


def regime_at(labels: pd.Series, ts) -> str | None:
    """The regime in force at timestamp `ts` (last daily label at or before it)."""
    if labels is None or labels.empty:
        return None
    t = pd.Timestamp(ts)
    idx = labels.index
    if t.tzinfo is None and idx.tz is not None:
        t = t.tz_localize(idx.tz)
    elif t.tzinfo is not None and idx.tz is None:
        t = t.tz_convert(None)
    pos = idx.searchsorted(t, side="right") - 1
    if pos < 0:
        return None
    v = labels.iloc[pos]
    return v if isinstance(v, str) else None
