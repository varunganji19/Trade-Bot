"""Portfolio view of out-of-sample evidence: reported beside the promotion
gate, never used by it.

WHY. The gate pools trades from separate single-market accounts and
bootstraps profit factor over them. Positions on BTC, ETH and SOL that are
open at the same time are largely one bet, and a closed-trade list never
sees the drawdown a position carries before it exits. The question a trader
faces is the book's: hold every market's account at once and mark it every
day. This module builds that daily return series and measures it.

  * daily P&L per market account, marked to market from its walk-forward
    equity curve (fixed capital, the backtester's additive model);
  * the book: every market account held at once; a day's return is the
    summed P&L over the capital of the accounts trading that day (markets
    whose windows start later, or that are shut at weekends, are not
    counted as idle capital diluting the rest);
  * annualised Sharpe with a 90% circular block-bootstrap interval, on
    calendar days (365 a year: crypto never closes);
  * how many independent bets the markets amount to, from the average
    correlation of their daily P&L;
  * the Deflated Sharpe of the best book in a declaration, priced against
    the other books in it.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from bot.evidence_stats import bootstrap_interval

DAYS_PER_YEAR = 365
MIN_OVERLAP = 30        # shared days before a pair's correlation is read


def daily_pnl(equity_curve, start_equity) -> dict | None:
    """One market account's marked P&L per UTC day.

    A day with no scored bar (a walk-forward fold's warm-up, a forex
    weekend) is absent rather than zero, and the first day is measured from
    the starting equity. Returns None when there is no curve to read."""
    if not equity_curve or start_equity is None:
        return None
    eq = pd.Series([float(p["equity"]) for p in equity_curve],
                   index=pd.to_datetime([p["ts"] for p in equity_curve], utc=True))
    last = eq.groupby(eq.index.floor("1D")).last()
    pnl = last.diff()
    pnl.iloc[0] = last.iloc[0] - float(start_equity)
    return {"capital": float(start_equity),
            "days": [d.strftime("%Y-%m-%d") for d in last.index],
            "pnl": [float(x) for x in pnl]}


def book_frame(units: list, key: str = "daily") -> tuple[pd.DataFrame | None,
                                                         pd.Series | None, str]:
    """Daily P&L with one column per market account, on every day any of them
    was observed; NaN where an account was not observed.

    Returns (frame, capital per account, reason). The frame is None when any
    account lacks a daily series: a book missing a market would look calmer
    than the one the gate measured, so it is not computed at all."""
    cols, capital = {}, {}
    for u in units:
        d = u.get(key)
        if not d:
            return None, None, (f"{u.get('symbol')} {u.get('timeframe')}: "
                                f"no daily equity series")
        name = f"{u.get('symbol')} {u.get('timeframe')}"
        while name in cols:
            name += "'"
        cols[name] = pd.Series(d["pnl"], index=pd.to_datetime(d["days"], utc=True),
                               dtype=float)
        capital[name] = float(d["capital"])
    if not cols:
        return None, None, "no market runs"
    return pd.DataFrame(cols).sort_index(), pd.Series(capital), ""


def _returns(frame: pd.DataFrame, capital: pd.Series) -> pd.Series:
    deployed = frame.notna().astype(float) @ capital[frame.columns]
    return frame.fillna(0.0).sum(axis=1) / deployed


def annual_sharpe(returns) -> float | None:
    a = np.asarray(returns, dtype=float)
    if a.size < 2:
        return None
    sd = a.std(ddof=1)
    if sd <= 0:
        return None
    return float(a.mean() / sd * math.sqrt(DAYS_PER_YEAR))


def effective_bets(frame: pd.DataFrame) -> dict:
    """n / (1 + (n - 1) * mean pairwise correlation) over the accounts whose
    daily P&L ever moved, each pair correlated over the days both were
    observed (a pair sharing fewer than MIN_OVERLAP days is left out).
    Clipped to [1, n]: a negative average correlation would otherwise claim
    more independent bets than there are markets."""
    active = frame.loc[:, frame.std(ddof=1) > 0]
    n = active.shape[1]
    if n < 2:
        return {"active_markets": n, "mean_pairwise_corr": None,
                "effective_bets": float(n)}
    pairs = active.corr(min_periods=MIN_OVERLAP).to_numpy()[np.triu_indices(n, 1)]
    if np.isnan(pairs).all():
        return {"active_markets": n, "mean_pairwise_corr": None,
                "effective_bets": float(n)}
    rho = float(np.nanmean(pairs))
    denom = 1.0 + (n - 1) * rho
    n_eff = n / denom if denom > 0 else float(n)
    return {"active_markets": n, "mean_pairwise_corr": round(rho, 3),
            "effective_bets": round(min(max(n_eff, 1.0), float(n)), 2)}


def book_returns(units: list, key: str = "daily") -> pd.Series | None:
    frame, capital, _ = book_frame(units, key)
    return None if frame is None else _returns(frame, capital)


def portfolio_evidence(units: list, key: str = "daily", *, n_boot: int = 2000,
                       alpha: float = 0.10, seed: int = 0) -> dict:
    """The book's daily statistics, with a 90% interval on its Sharpe.
    Returns are additive on deployed capital, the backtester's fixed-capital
    model: `return_pct` is their sum, and drawdown is measured on it."""
    frame, capital, why = book_frame(units, key)
    if frame is None:
        return {"available": False, "why": why}
    pnl = frame.fillna(0.0).sum(axis=1)
    rets = _returns(frame, capital)
    growth = 1.0 + rets.cumsum()
    # the starting balance is a peak too: a book that only ever lost money
    # has drawn down from where it began
    peak = growth.cummax().clip(lower=1.0)
    sharpe = annual_sharpe(rets)
    lo, hi = bootstrap_interval(rets.to_numpy(), annual_sharpe, n_boot=n_boot,
                                alpha=alpha, seed=seed)
    vol = float(rets.std(ddof=1) * math.sqrt(DAYS_PER_YEAR)) if len(rets) > 1 else None
    return {
        "available": True, "markets": int(frame.shape[1]), "days": int(len(rets)),
        "first_day": rets.index[0].strftime("%Y-%m-%d"),
        "last_day": rets.index[-1].strftime("%Y-%m-%d"),
        "capital": round(float(capital.sum()), 2), "net_pnl": round(float(pnl.sum()), 2),
        "return_pct": round(float(rets.sum()) * 100, 2),
        "ann_vol_pct": None if vol is None else round(vol * 100, 2),
        "sharpe": None if sharpe is None else round(sharpe, 3),
        "sharpe_lo": lo, "sharpe_hi": hi,
        "interval_above_zero": lo is not None and lo > 0,
        "max_drawdown_pct": round(float((growth / peak - 1).min()) * 100, 2),
        **effective_bets(frame),
    }


def family_deflated_sharpe(returns_by_name: dict) -> dict:
    """Deflated Sharpe of the best book among `returns_by_name`, priced against
    all of them. Only these books are counted (earlier trials have no daily
    series), which is fewer trials than the full history. The null maximum
    also grows with the spread of the books' Sharpes, so one clear loser in
    the family raises the bar for the best: read it beside the interval,
    never alone."""
    from bot.validation import deflated_sharpe
    sharpes = {name: annual_sharpe(r) for name, r in returns_by_name.items()
               if r is not None}
    sharpes = {name: s for name, s in sharpes.items() if s is not None}
    if len(sharpes) < 2:
        return {"deflated_sharpe": None, "reason": "need >= 2 books with a Sharpe"}
    best = max(sharpes, key=sharpes.get)
    r = returns_by_name[best]
    out = deflated_sharpe(list(sharpes.values()), n_obs=len(r),
                          bars_per_year=DAYS_PER_YEAR, returns=r)
    out.update(best=best, counted=f"{len(sharpes)} books in this declaration only; "
                                  f"earlier trials are not counted")
    return out
