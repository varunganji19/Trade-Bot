"""
Cross-symbol capital allocation (skfolio-backed).

The RiskManager sizes each trade in isolation (1% of equity at the stop), which
is correct per-trade but wrong per-portfolio: opening a 4th "independent" 1%
risk when the other three positions are 0.9-correlated majors means the real
portfolio risk is nothing like 4%. This module divides the TOTAL risk budget
across the symbols that are about to hold positions concurrently:

  inverse_vol — budget share ~ 1/vol per symbol, each vol computed on the
                symbol's OWN bars (identical to skfolio's InverseVolatility,
                hand-rolled because the skfolio path aligned timestamps —
                dropping ~28% of crypto bars in a mixed 24/7 + 24x5 book).
                No return forecasts, nothing to overfit; the default.
  hrp         — skfolio HierarchicalRiskParity: uses the correlation structure
                (correlated BTC/ETH/SOL collapse into one cluster and share one
                budget) at the cost of a noisier covariance estimate. Genuinely
                needs aligned observations, so it keeps the aligned matrix and
                its documented weekend thinning.
  equal       — 1/N across candidate symbols (the naive baseline, for
                A/B-testing the allocation edge).

Weights are clipped to [min_sym_weight, max_sym_weight] per symbol and the
RiskManager consumes them as a multiplier on `risk_per_trade`. The result:
total concurrent portfolio risk stays bounded no matter how correlated the
watchlist is.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from config import CONFIG, MarketSpec


def returns_matrix(specs: list[MarketSpec], histories: dict[str, pd.DataFrame],
                   lookback: int | None = None) -> pd.DataFrame:
    """Aligned per-bar simple returns for the given symbols (inner-join on ts).

    `histories[symbol]` holds that symbol's closed-bar frame. Missing symbols
    are skipped; if nothing aligns an empty frame is returned and the caller
    should fall back to equal weights.

    NOTE: alignment inner-joins on timestamps. Crypto trades 24/7 while forex
    closes over weekends — a mixed crypto+forex matrix loses every weekend
    row for ALL symbols (~28% of crypto observations in a 24/7+24x5 book).
    This matrix is therefore ONLY for estimators that genuinely need aligned
    observations (HRP's correlation structure). Inverse-vol — the shipped
    default — uses per_symbol_vols on each symbol's OWN bars instead.
    """
    lookback = lookback or CONFIG.portfolio.lookback_bars
    rets: dict[str, pd.Series] = {}
    for spec in specs:
        df = histories.get(spec.symbol)
        if df is None or len(df) < 2:
            continue
        r = df["close"].tail(lookback).pct_change().dropna()
        if len(r):
            rets[spec.symbol] = r
    if len(rets) < 2:
        return pd.DataFrame()
    return pd.DataFrame(rets).dropna(how="any")


def per_symbol_vols(specs: list[MarketSpec], histories: dict[str, pd.DataFrame],
                    lookback: int | None = None) -> dict[str, float]:
    """Per-symbol return volatility, each computed on that symbol's OWN bars.

    No cross-symbol timestamp alignment: a 24/7 crypto symbol keeps its
    weekend bars even when a forex sibling in the same book closes over
    weekends (the aligned matrix drops ~28% of crypto rows in a mixed
    watchlist, biasing inverse-vol weights toward weekday-only vol)."""
    lookback = lookback or CONFIG.portfolio.lookback_bars
    vols: dict[str, float] = {}
    for spec in specs:
        df = histories.get(spec.symbol)
        if df is None or len(df) < 2:
            continue
        r = df["close"].tail(lookback).pct_change().dropna()
        if len(r) >= 2 and float(r.std()) > 0:
            vols[spec.symbol] = float(r.std())
    return vols


def allocation_weights(specs: list[MarketSpec], histories: dict[str, pd.DataFrame],
                       method: str | None = None) -> dict[str, float]:
    """Risk-budget share per symbol, summing to 1 across `specs` (missing
    history -> equal share within the constraint band).

    inverse_vol (the shipped default) computes each symbol's vol on its OWN
    bars — no alignment, so 24/7 crypto keeps its weekend observations next
    to weekday-only forex. hrp genuinely needs a correlation matrix and keeps
    the aligned matrix (with its documented weekend thinning) because
    correlations require common observations."""
    cfg = CONFIG.portfolio
    method = method or cfg.method
    symbols = [s.symbol for s in specs]
    if not symbols:
        return {}

    weights: dict[str, float] = {}

    if method == "inverse_vol":
        # 1/vol on each symbol's own returns; no timestamp alignment, so
        # crypto weekend bars survive next to weekday-only forex
        vols = per_symbol_vols(specs, histories)
        have = {s: v for s, v in vols.items() if s in symbols}
        if have:
            inv = {s: 1.0 / v for s, v in have.items()}
            total = sum(inv.values())
            weights = {s: x / total for s, x in inv.items()}
        if not weights:
            share = 1.0 / len(symbols)
            weights = {s: share for s in symbols}
    elif method == "hrp":
        # only HRP needs the aligned returns matrix (it drops ~28% of crypto
        # rows in a mixed book — building it for the default inverse_vol path
        # was wasted work every engine cycle / backtest bar)
        rets = returns_matrix(specs, histories)
        if not rets.empty:
            try:
                from skfolio.optimization import HierarchicalRiskParity
                model = HierarchicalRiskParity().fit(rets)
                w = np.asarray(model.weights_, dtype=float)
                cols = list(rets.columns)
                weights = {c: float(x) for c, x in zip(cols, w)}
            except Exception:
                share = 1.0 / len(symbols)
                weights = {s: share for s in symbols}
        else:
            share = 1.0 / len(symbols)
            weights = {s: share for s in symbols}
    else:
        share = 1.0 / len(symbols)
        weights = {s: share for s in symbols}

    # symbols with no usable history still get their floor share (applies to
    # the inverse_vol and hrp paths; the equal path already covers everyone)
    if method in ("inverse_vol", "hrp"):
        n_missing = len([s for s in symbols if s not in weights])
        if n_missing:
            floor = cfg.min_sym_weight
            total = 1.0 - floor * n_missing
            weights = {k: v * total for k, v in weights.items()}
            for s in symbols:
                if s not in weights:
                    weights[s] = floor

    # clip to [min, max] per symbol, renormalize to 1
    lo, hi = cfg.min_sym_weight, cfg.max_sym_weight
    n = len(weights)
    lo = min(lo, 1.0 / n)
    hi = max(hi, 1.0 / n)
    clipped = {k: float(np.clip(v, lo, hi)) for k, v in weights.items()}
    total = sum(clipped.values())
    return {k: v / total for k, v in clipped.items()}
