"""
Validation toolkit — the honest-statistics layer (skfolio CombinatorialPurgedCV).

A single backtest path can't distinguish an edge from a lucky run. This module
produces a DISTRIBUTION of out-of-sample results by combinatorial purged
cross-validation (Lopez de Prado 2018; skfolio implementation):

  - history is split into `n_folds` contiguous blocks;
  - every combination of `n_test_folds` blocks is one OOS "path";
  - the strategy's trades are partitioned across paths by ENTRY bar — a trade
    whose LABEL (entry through exit) spans a block boundary is dropped: the
    entry must sit `purge_bars` inside the block AND the exit must close
    `purge_bars` inside the SAME block (entry-proximity alone leaked: a
    long-held trade's outcome straddled the boundary = overlapping labels);
  - we report per-path return / win rate and the cross-path distribution:
    mean, std, the share of profitable paths.

Parameters are fixed in config and never fitted on data, so there is no
train/test parameter leakage by construction — the purged CV here measures
PATH DISPERSION: a real edge profits on most paths; a lucky run profits on one.

`signal_ic_report` applies the same machinery to raw SIGNAL quality: per-bar
conviction x forward `horizon`-bar return, Spearman rank IC per path — the
alpha-zoo style bench (Vibe-Trading pattern) for our own strategies, with
t-stat adjusted for overlapping-label autocorrelation.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


# --------------------------------------------------------------------- paths
def purged_cv_paths(n_bars: int, n_folds: int = 8, n_test_folds: int = 2) -> list[np.ndarray]:
    """OOS test-index arrays, one per path (skfolio CombinatorialPurgedCV).
    Deterministic by construction — no seed (the old seed= param was never
    read: the CV enumerates all fold combinations, it samples nothing)."""
    from skfolio.model_selection import CombinatorialPurgedCV
    cv = CombinatorialPurgedCV(n_folds=n_folds, n_test_folds=n_test_folds)
    paths = []
    for _, test in cv.split(np.zeros(n_bars)):
        # `test` is a list/array of index blocks (arrays of unequal length)
        raw = test if isinstance(test, (list, tuple)) else [test]
        blocks = [np.atleast_1d(np.asarray(b)).ravel() for b in raw]
        idx = np.concatenate(blocks).astype(int) if blocks else np.array([], dtype=int)
        paths.append(np.sort(idx))
    return paths


def _path_bounds(path: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous (start, end) runs inside a path's test indices."""
    if not len(path):
        return []
    bounds = []
    start = prev = int(path[0])
    for x in path[1:]:
        x = int(x)
        if x != prev + 1:
            bounds.append((start, prev))
            start = x
        prev = x
    bounds.append((start, prev))
    return bounds


def _ts_to_index(ts, df: pd.DataFrame) -> int | None:
    """Positional bar index of a timestamp; None when ts is missing/absent.

    The match tolerance scales with the frame's own cadence (2x the median
    bar step): the old fixed 7-day window misassigned fast bars (a 1m/5m/15m
    trade matched a NEIGHBOUR bar days away instead of missing), smearing
    purged-CV labels across path boundaries. Unparseable frames fall back to
    the old 7-day bound."""
    try:
        dt = pd.Timestamp(ts)
        pos = df.index.searchsorted(dt, side="right") - 1
        if pos < 0:
            return None
        bar_ts = df.index[pos]
        try:
            step = float(pd.Series(df.index).diff().dropna().median().total_seconds())
        except Exception:
            step = float("nan")
        tol = 2.0 * step if step == step and step > 0 else 7 * 86400
        if abs((bar_ts - dt).total_seconds()) > tol:  # ts not in this frame
            return None
        return pos
    except Exception:
        return None


def trade_bar_index(trade: dict, df: pd.DataFrame) -> int | None:
    """Positional bar index of a trade's entry (ts -> position). -1 ts = miss."""
    ts = trade.get("entry_ts") or trade.get("opened_ts")
    if not ts:
        return None
    return _ts_to_index(ts, df)


# ------------------------------------------------------------- trade paths
def oos_trade_distribution(trades: list[dict], df: pd.DataFrame,
                           n_folds: int = 8, n_test_folds: int = 2,
                           purge_bars: int = 24) -> dict:
    """Partition a backtest's trades across purged-CV paths and report the
    distribution of per-path results.

    A trade is kept for a path only when its whole LABEL sits inside one test
    block: entry inside the block, exit inside the SAME block, both clear of
    the block edges by `purge_bars`. Purging entry proximity alone was not
    enough — a turtle trade can hold hundreds of bars, and its outcome spanned
    path boundaries (overlapping-label leakage) no matter where the entry sat.
    Trades whose exit timestamp can't be resolved fall back to the entry
    proximity rule."""
    scored = []
    for t in trades:
        i = trade_bar_index(t, df)
        if i is None:
            continue
        exit_ts = t.get("exit_ts") or t.get("closed_ts")
        j = _ts_to_index(exit_ts, df) if exit_ts else None
        scored.append((i, j, t))
    if not scored:
        raise ValueError("no trades with entry timestamps inside the frame")

    paths = purged_cv_paths(len(df), n_folds=n_folds, n_test_folds=n_test_folds)

    per_path = []
    assigned: set[int] = set()
    for path in paths:
        bounds = _path_bounds(path)
        kept = []
        for i, j, t in scored:
            for lo, hi in bounds:
                if not (lo <= i <= hi):
                    continue
                if j is not None:
                    # label fully inside this block, purge_bars clear of edges
                    if lo <= j <= hi and (i - lo) >= purge_bars and (hi - j) >= purge_bars:
                        kept.append(t)
                        break
                elif (i - lo) >= purge_bars and (hi - i) >= purge_bars:
                    kept.append(t)   # exit unresolvable: entry proximity only
                    break
        if kept:
            assigned.update(id(t) for t in kept)
        pnl_pct = [(t.get("pnl_pct") or 0.0) for t in kept]
        comp = 1.0
        for p in sorted(pnl_pct):  # sorted only for float determinism; product is order-free
            comp *= (1.0 + p / 100.0)
        wins = sum(1 for p in pnl_pct if p > 0)
        per_path.append({
            "trades": len(kept),
            "return_pct": round((comp - 1.0) * 100.0, 2),
            "win_rate_pct": round(wins / len(kept) * 100.0, 1) if kept else 0.0,
            "total_pnl": round(sum(t.get("pnl", 0.0) for t in kept), 2),
        })

    # Only paths that actually traded carry evidence: empty paths are reported
    # but excluded from the distribution stats (a path with no trades says
    # nothing about the edge either way).
    active = [p for p in per_path if p["trades"]]
    rets = [p["return_pct"] for p in active]
    profitable = [r for r in rets if r > 0]
    if rets:
        mean = float(np.mean(rets))
        std = float(np.std(rets, ddof=1)) if len(rets) > 1 else 0.0
        # t-stat: mean/std*sqrt(n) (paths share some bars so treat as a lower bound)
        tstat = (mean / std * math.sqrt(len(rets))) if std > 0 else None
    else:
        mean = std = tstat = None
    return {
        "n_paths": len(per_path),
        "n_active_paths": len(active),
        "paths": per_path,
        "mean_return_pct": round(mean, 2) if mean is not None else None,
        "std_return_pct": round(std, 2) if std is not None else None,
        "t_stat": round(tstat, 2) if tstat is not None else None,
        "pct_paths_profitable": round(len(profitable) / len(rets) * 100.0, 1) if rets else None,
        "purged_trades": len(scored) - len(assigned),
        # unique trades kept in >=1 path; per-path counts SUM to more than
        # this when a trade sits in a fold block shared by several paths
        # (overlapping OOS paths are the design, not a bug)
        "kept_trades_unique": len(assigned),
        "total_trades": len(scored),
    }


# ------------------------------------------------------------- signal IC
def signal_ic_series(df: pd.DataFrame, strategy, horizon: int,
                     warmup: int = 220, use_confidence: bool = True) -> pd.DataFrame:
    """Per-bar conviction score vs forward `horizon`-bar return. Enriches the
    frame with indicators first (idempotent) so raw candle frames are fine."""
    if "atr" not in df.columns or "adx" not in df.columns:
        from bot.indicators import add_all_indicators
        df = add_all_indicators(df)
    rows = []
    n = len(df)
    for i in range(warmup, n - horizon):
        sig = strategy.evaluate(df, i)
        direction = {"LONG": 1.0, "SHORT": -1.0}.get(sig.action, 0.0)
        score = direction * (sig.confidence if use_confidence else 1.0)
        if score == 0.0:
            continue
        fwd = float(df["close"].iloc[i + horizon]) / float(df["close"].iloc[i]) - 1.0
        rows.append({"bar": i, "score": score, "fwd_return": fwd})
    return pd.DataFrame(rows)


def signal_ic_report(df: pd.DataFrame, strategy, horizon: int = 24,
                     n_folds: int = 8, n_test_folds: int = 2,
                     warmup: int = 220) -> dict:
    """Rank-IC of a strategy's signals vs forward returns, per purged-CV path.

    Overlapping forward windows autocorrelate the per-bar ICs, so the summary
    reports an overlap-adjusted effective sample size (n / horizon)."""
    series = signal_ic_series(df, strategy, horizon, warmup=warmup)
    if len(series) < 50:
        raise ValueError(f"not enough signal observations ({len(series)})")

    paths = purged_cv_paths(len(df), n_folds=n_folds, n_test_folds=n_test_folds)
    ics = []
    for path in paths:
        if not len(path):
            continue
        sub = series[series["bar"].isin(set(int(x) for x in path))]
        if len(sub) < 10:
            continue
        ics.append(float(sub["score"].corr(sub["fwd_return"], method="spearman")))

    ics = [c for c in ics if c == c]  # drop NaN paths (degenerate ties)
    if not ics:
        raise ValueError("no path had enough observations to compute IC")
    mean = float(np.mean(ics))
    std = float(np.std(ics, ddof=1)) if len(ics) > 1 else 0.0
    # pooled per-bar IC for the overlap-adjusted t-stat
    pooled = float(series["score"].corr(series["fwd_return"], method="spearman"))
    n_eff = len(series) / max(1, horizon)
    pooled_std = 1.0 / math.sqrt(max(1.0, n_eff))  # rank-IC std ~ 1/sqrt(n) rule of thumb
    return {
        "n_signal_bars": len(series),
        "horizon_bars": horizon,
        "pooled_ic": round(pooled, 4),
        "ic_t_stat_adj": round(pooled / pooled_std, 2) if pooled_std > 0 else None,
        "mean_path_ic": round(mean, 4),
        "std_path_ic": round(std, 4) if len(ics) > 1 else 0.0,
        "n_paths_with_ic": len(ics),
        "pct_paths_positive_ic": round(sum(1 for c in ics if c > 0) / len(ics) * 100.0, 1),
    }


def print_purged_cv(res: dict, title: str = ""):
    if title:
        print(f"\n{title}")
    if not res.get("n_paths"):
        print("  (no paths)")
        return
    active = res.get("n_active_paths")
    print(f"  paths: {res['n_paths']} ({active} traded) | mean ret {res.get('mean_return_pct')}% "
          f"± {res.get('std_return_pct')}% | t≈{res.get('t_stat')} | "
          f"{res.get('pct_paths_profitable')}% of TRADED paths profitable | "
          f"purged {res.get('purged_trades')}/{res.get('total_trades')} boundary trades")
    if active and res.get("total_trades", 0) < active * 2:
        print(f"  (sparse: {res.get('total_trades')} trades across {active} paths — "
              "treat the distribution as indicative, not conclusive)")
    for k, p in enumerate(res["paths"]):
        if not p["trades"]:
            continue  # empty paths say nothing; skip the noise
        print(f"    path {k + 1:2d}: {p['trades']:3d} trades  ret {p['return_pct']:+7.2f}%  "
              f"wr {p['win_rate_pct']:5.1f}%  pnl {p['total_pnl']:+9.2f}")


# ------------------------------------------------------- honest statistics
def _norm_sf(x: float) -> float:
    """Standard normal survival function P(Z > x) without scipy."""
    return 0.5 * math.erfc(x / math.sqrt(2.0))


def deflated_sharpe(sharpes: list[float], n_obs: int, bars_per_year: float,
                    returns=None) -> dict:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014).

    We tried many configurations before shipping one; the DSR asks: given the
    BEST of `len(sharpes)` trial Sharpe ratios, each estimated on `n_obs`
    observations, what's the probability the true Sharpe is actually positive?

      SR0 = expected max Sharpe under the null (all trials zero-edge),
            from the trials' cross-sectional variance;
      DSR = P(SR* > SR0 | trials) via the Gaussian approximation.

    UNITS: the trial Sharpes are ANNUALIZED (backtest.stats annualizes by
    sqrt(bars_per_year)), so the standard error of the estimate is
    sqrt(bars_per_year / n_obs) — the SE of a per-period Sharpe is 1/sqrt(n)
    and mixing that with annualized trials inflated DSR to ~1.0 for any sane
    input (the statistic could essentially never reject).

    HIGHER MOMENTS: when the primary run's per-bar returns are supplied,
    the SE uses the Merton/OPM adjustment — the asymptotic variance of the
    SR estimate depends on skewness (gamma3) and kurtosis (gamma4):
        var(SR_p) ~= (1 - gamma3*SR_p + (gamma4-1)/4 * SR_p^2) / (n-1)
    (Mertens 2002; Christie 2005; the form Bailey & LdP use for the DSR).
    Fat-tailed crypto returns (gamma4 >> 3) with non-zero per-period SR get
    a wider SE than the normal-only 1/sqrt(n). The formula applies to the
    PER-PERIOD SR: the annualized `best` is de-annualized first
    (SR_p = best / sqrt(bars_per_year)) and the SE is re-annualized after —
    feeding the annualized SR in directly (the naive patch) mis-scales the
    correction by ~sqrt(apy).

    A DSR >= 0.95 is publishable confidence; below 0.5 the strategy's Sharpe is
    fully explained by selection over trials (p-hacking, quantified)."""
    sr = [s for s in sharpes if s == s]
    if len(sr) < 2 or n_obs < 10:
        return {"deflated_sharpe": None, "reason": "need >=2 trial Sharpes and >=10 obs"}
    if bars_per_year <= 0:
        return {"deflated_sharpe": None, "reason": "bars_per_year must be positive"}
    best = max(sr)
    n_trials = len(sr)
    var = float(np.var(sr, ddof=1))
    if var <= 0:
        return {"deflated_sharpe": None, "reason": "zero variance across trials"}
    # E[max] of n iid N(0, var): gamma-based expectation (Bailey/LdP eq.)
    gamma = 0.5772156649015329
    e_max = (1.0 - gamma) * _norm_inv_cdf(1.0 - 1.0 / n_trials) \
        + gamma * _norm_inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    sr0 = e_max * math.sqrt(var)

    # SE of the ANNUALIZED Sharpe estimated over n_obs bars. Default: the
    # normal-only 1/sqrt(n) per-period SE scaled by sqrt(apy). With a returns
    # series: the Merton/OPM moment adjustment on the per-period SR.
    se_model = "normal"
    se = math.sqrt(bars_per_year / max(1, n_obs))
    if returns is not None:
        r = pd.Series(returns).dropna()
        if len(r) >= 20 and float(r.std()) > 0:
            # the trial Sharpes are annualized; the moment formula needs the
            # PER-PERIOD SR of the best trial
            sr_p = best / math.sqrt(bars_per_year)
            skew = float(r.skew())
            # pandas kurtosis is EXCESS kurtosis; the formula wants raw
            kurt = float(r.kurt()) + 3.0
            var_p = (1.0 - skew * sr_p + ((kurt - 1.0) / 4.0) * sr_p ** 2) \
                / max(1, len(r) - 1)
            if var_p > 0:
                se = math.sqrt(bars_per_year * var_p)
                se_model = "moments"
    dsr = 1.0 - _norm_sf((best - sr0) / se)
    return {
        "best_sharpe": round(best, 3),
        "n_trials": n_trials,
        "sr0_expected_max": round(sr0, 3),
        "se_model": se_model,
        "deflated_sharpe": round(dsr, 3),
        "verdict": "selection-aware confidence" if dsr >= 0.95 else
                   ("suggestive" if dsr >= 0.8 else "Sharpe explained by trial count"),
    }


def _norm_inv_cdf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation)."""
    if not 0.0 < p < 1.0:
        return 0.0
    a = [-3.969683028665376e+01, 2.209460984245187e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539347334e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    p_low, p_high = 0.02425, 1 - 0.02425
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > p_high:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def pbo_cscv(path_returns_by_config: dict[str, list[float]],
             n_sims: int = 400, seed: int = 7) -> dict:
    """Probability of Backtest Overfitting (Lopez de Prado 2018, CSCV rank
    logic) over a FAMILY of configurations run on the same aligned paths.

    `path_returns_by_config` maps config name -> per-path returns (all runs on
    the same data and the same purged-CV path layout, so column k of every
    config's list is the same out-of-sample period). For each random IS/OOS
    split of the paths: pick the config with the best in-sample mean, then
    check whether it lands in the BOTTOM half out of sample. PBO is the
    frequency of that failure.

      PBO >= 0.5  — the selection process is worse than a coin flip: the
                    family's "best" config is IS-luck, not edge;
      PBO <  0.5  — the IS winner keeps its rank out of sample.

    With ONE config this is undefined (needs a family); the shipped use is the
    four shipped strategies on the same symbol/timeframe as the trial family.
    """
    names = list(path_returns_by_config)
    k = len(names)
    m = min(len(v) for v in path_returns_by_config.values())
    if k < 2:
        return {"pbo": None, "reason": "PBO needs >=2 configurations (a family)"}
    if m < 8:
        return {"pbo": None, "reason": "need >=8 aligned traded paths per config"}
    mat = np.array([[float(x) for x in path_returns_by_config[n][:m]] for n in names])
    rng = np.random.default_rng(seed)
    bottom = 0
    for _ in range(n_sims):
        perm = rng.permutation(m)
        is_idx, oos_idx = perm[: m // 2], perm[m // 2:]
        is_perf = mat[:, is_idx].mean(axis=1)
        oos_perf = mat[:, oos_idx].mean(axis=1)
        best_is = int(np.argmax(is_perf))
        # 0 = worst OOS ... k-1 = best OOS; bottom half = below-median rank
        oos_rank = int(np.argsort(np.argsort(oos_perf))[best_is])
        if oos_rank < k / 2.0:
            bottom += 1
    pbo = bottom / n_sims
    return {
        "pbo": round(pbo, 3),
        "n_configs": k,
        "n_paths": m,
        "n_sims": n_sims,
        "configs": names,
        "verdict": "selection process overfit" if pbo >= 0.5 else
                   ("borderline" if pbo >= 0.35 else "selection holds OOS"),
    }


def monte_carlo_paths(trades: list[dict], starting_capital: float = 10_000.0,
                      n_sims: int = 2000, seed: int = 11) -> dict:
    """Resample the trade sequence (with replacement) to get the DISTRIBUTION
    of equity outcomes the realized order was one draw from. Reports the 5th
    percentile terminal equity (the honest 'bad luck' bound), the probability
    of ending below start, and a resampled drawdown distribution."""
    pnls = [t.get("pnl", 0.0) for t in trades if t.get("pnl") is not None]
    if len(pnls) < 5:
        return {"n_sims": 0, "reason": "need >=5 closed trades"}
    rng = np.random.default_rng(seed)
    arr = np.asarray(pnls, dtype=float)
    finals = np.empty(n_sims)
    max_dds = np.empty(n_sims)
    for k in range(n_sims):
        seq = rng.choice(arr, size=len(arr), replace=True)
        eq = starting_capital + np.cumsum(seq)
        peak = np.maximum.accumulate(np.concatenate(([starting_capital], eq)))[1:]
        dd = (eq - peak) / peak
        finals[k] = eq[-1]
        max_dds[k] = dd.min() if len(dd) else 0.0
    return {
        "n_sims": n_sims,
        "terminal_p5": round(float(np.percentile(finals, 5)), 2),
        "terminal_p50": round(float(np.percentile(finals, 50)), 2),
        "terminal_p95": round(float(np.percentile(finals, 95)), 2),
        "p_lose_money": round(float(np.mean(finals < starting_capital)) * 100.0, 1),
        "max_dd_p95": round(float(np.percentile(max_dds, 5)) * 100.0, 2),
        "p_dd_beyond_10pct": round(float(np.mean(max_dds < -0.10)) * 100.0, 1),
    }


def min_trl(sharpe: float, bars_per_year: float) -> dict:
    """Minimum Track Record Length (Bailey & Lopez de Prado 2012): how many
    bars of LIVE (out-of-sample) performance would we need to observe before
    believing the claimed Sharpe is positive at 95% confidence? A live demo
    paper-account is a short track record; this frames exactly how short."""
    if sharpe is None or sharpe <= 0 or bars_per_year <= 0:
        return {"min_bars": None, "reason": "needs a positive Sharpe"}
    z = 1.6449  # one-sided 95%
    min_years = (z / sharpe) ** 2
    min_bars = min_years * bars_per_year
    return {
        "min_bars": int(math.ceil(min_bars)),
        "min_years": round(min_years, 2),
        "bars_per_year": int(bars_per_year),
        "note": "bars of OOS track record needed for 95% confidence SR>0",
    }
