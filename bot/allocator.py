"""
Cross-symbol capital allocation (skfolio-backed).

The RiskManager sizes each trade in isolation (1% of equity at the stop), which
is correct per-trade but wrong per-portfolio: opening a 4th "independent" 1%
risk when the other three positions are 0.9-correlated majors means the real
portfolio risk is nothing like 4%. This module divides the TOTAL risk budget
across the symbols that are about to hold positions concurrently:

  inverse_vol — skfolio InverseVolatility: budget share ~ 1/vol per symbol.
                No return forecasts, nothing to overfit; the default.
  hrp         — skfolio HierarchicalRiskParity: uses the correlation structure
                (correlated BTC/ETH/SOL collapse into one cluster and share one
                budget) at the cost of a noisier covariance estimate.
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


def allocation_weights(specs: list[MarketSpec], histories: dict[str, pd.DataFrame],
                       method: str | None = None) -> dict[str, float]:
    """Risk-budget share per symbol, summing to 1 across `specs` (missing
    history -> equal share within the constraint band)."""
    cfg = CONFIG.portfolio
    method = method or cfg.method
    symbols = [s.symbol for s in specs]
    if not symbols:
        return {}

    rets = returns_matrix(specs, histories)
    weights: dict[str, float] = {}

    if method == "equal" or rets.empty:
        share = 1.0 / len(symbols)
        weights = {s: share for s in symbols}
    else:
        try:
            if method == "hrp":
                from skfolio.optimization import HierarchicalRiskParity
                model = HierarchicalRiskParity().fit(rets)
            else:  # inverse_vol (default)
                from skfolio.optimization import InverseVolatility
                model = InverseVolatility().fit(rets)
            w = np.asarray(model.weights_, dtype=float)
            cols = list(rets.columns)
            weights = {c: float(x) for c, x in zip(cols, w)}
            # symbols with no aligned history still get their floor share
            n_missing = len([s for s in symbols if s not in weights])
            if n_missing:
                floor = cfg.min_sym_weight
                total = 1.0 - floor * n_missing
                weights = {k: v * total for k, v in weights.items()}
                for s in symbols:
                    if s not in weights:
                        weights[s] = floor
        except Exception:
            share = 1.0 / len(symbols)
            weights = {s: share for s in symbols}

    # clip to [min, max] per symbol, renormalize to 1
    lo, hi = cfg.min_sym_weight, cfg.max_sym_weight
    n = len(weights)
    lo = min(lo, 1.0 / n)
    hi = max(hi, 1.0 / n)
    clipped = {k: float(np.clip(v, lo, hi)) for k, v in weights.items()}
    total = sum(clipped.values())
    return {k: v / total for k, v in clipped.items()}
