"""The portfolio view of out-of-sample evidence (bot/portfolio_evidence.py):
a daily, marked-to-market book reported beside the gate, never used by it."""
import json

import numpy as np
import pandas as pd
import pytest

import bot.experiments as ex
import bot.promotion as promo
from bot.evidence_stats import trade_evidence
from bot.portfolio_evidence import (book_frame, book_returns, daily_pnl, effective_bets,
                                    family_deflated_sharpe, portfolio_evidence)
from tests.test_experiments import _fake_world, _write

CAPITAL = 10_000.0


def _curve(start, equities, freq="6h"):
    idx = pd.date_range(start, periods=len(equities), freq=freq, tz="UTC")
    return [{"ts": str(t), "equity": float(e)} for t, e in zip(idx, equities)]


def _unit(symbol, daily_pnls, start="2025-01-01", tf="1h"):
    """A market account whose marked equity moves by `daily_pnls`, one per day."""
    equity = CAPITAL + np.cumsum(daily_pnls)
    curve = _curve(start, equity, freq="1D")
    return {"symbol": symbol, "timeframe": tf, "daily": daily_pnl(curve, CAPITAL)}


# --------------------------------------------------------------- daily P&L
def test_daily_pnl_marks_each_observed_day_and_reconciles():
    # two days of 6-hourly marks, a three-day gap (a fold's warm-up), one more day
    curve = (_curve("2025-01-01", [10_010, 10_020, 9_990, 10_005,
                                    10_030, 10_040, 10_050, 10_060])
             + _curve("2025-01-06", [10_060, 10_070, 10_080, 10_100]))
    d = daily_pnl(curve, CAPITAL)
    assert d["days"] == ["2025-01-01", "2025-01-02", "2025-01-06"]
    assert d["pnl"] == pytest.approx([5.0, 55.0, 40.0])     # day 1 from the start
    assert sum(d["pnl"]) == pytest.approx(10_100 - CAPITAL)  # nothing lost in the gap
    assert daily_pnl([], CAPITAL) is None


def test_a_market_without_a_daily_series_makes_the_book_unavailable():
    units = [_unit("BTC/USDT", [5.0, -2.0, 3.0]),
             {"symbol": "ETH/USDT", "timeframe": "1h", "daily": None}]
    frame, _, why = book_frame(units)
    assert frame is None and "ETH/USDT" in why
    book = portfolio_evidence(units)
    assert book == {"available": False, "why": why}


# ------------------------------------------------------- independent bets
def test_correlated_markets_count_as_one_bet():
    rng = np.random.default_rng(1)
    common = rng.normal(0, 10, 300)
    same = pd.DataFrame({"BTC": common, "ETH": common * 2.0, "SOL": common + 1e-9})
    assert effective_bets(same)["effective_bets"] == pytest.approx(1.0)

    apart = pd.DataFrame(rng.normal(0, 10, (300, 3)), columns=["a", "b", "c"])
    assert 2.5 < effective_bets(apart)["effective_bets"] <= 3.0

    opposite = pd.DataFrame({"a": common, "b": -common})
    assert effective_bets(opposite)["effective_bets"] == 2.0      # clipped to n

    flat = pd.DataFrame({"a": common, "idle": np.zeros(300)})
    assert effective_bets(flat)["active_markets"] == 1


def test_copies_of_one_market_narrow_the_trade_interval_but_not_the_book():
    """The reason this view exists: pooling trades from markets that move
    together multiplies the trade count and shrinks the profit-factor
    interval, though nothing new was learned. The book's Sharpe and its
    interval do not move, and the copies count as one bet."""
    rng = np.random.default_rng(7)
    pnls = rng.normal(2.0, 30.0, 400)
    one = trade_evidence(list(pnls))
    five = trade_evidence(list(np.repeat(pnls, 5)))    # five markets, same trades
    assert five["pf"] == pytest.approx(one["pf"])
    assert five["pf_hi"] - five["pf_lo"] < one["pf_hi"] - one["pf_lo"]

    days = rng.normal(3.0, 40.0, 500)
    single = portfolio_evidence([_unit("BTC/USDT", days)])
    copies = portfolio_evidence([_unit(f"C{k}/USDT", days) for k in range(5)])
    assert copies["sharpe"] == pytest.approx(single["sharpe"])
    assert (copies["sharpe_lo"], copies["sharpe_hi"]) == (single["sharpe_lo"],
                                                          single["sharpe_hi"])
    assert copies["effective_bets"] == pytest.approx(1.0)
    assert copies["markets"] == 5 and copies["capital"] == 5 * CAPITAL


# ------------------------------------------------------------- the book
def test_portfolio_statistics_reconcile_with_the_accounts():
    rng = np.random.default_rng(3)
    a = rng.normal(4.0, 25.0, 400)
    b = rng.normal(4.0, 25.0, 300)
    units = [_unit("BTC/USDT", a), _unit("EURUSD=X", b, start="2025-03-01")]
    book = portfolio_evidence(units)
    assert book["available"] and book["markets"] == 2
    assert book["net_pnl"] == pytest.approx(a.sum() + b.sum(), abs=0.01)
    assert book["capital"] == 2 * CAPITAL
    assert book["days"] == 400                       # union of observed days
    assert book["sharpe_lo"] <= book["sharpe"] <= book["sharpe_hi"]
    assert book["interval_above_zero"] == (book["sharpe_lo"] > 0)


def test_a_market_that_starts_later_is_not_idle_capital_before_it_starts():
    """Forex windows in the standard gate begin months before the crypto
    ones; until a market starts, its capital is not in the denominator."""
    early = _unit("EURUSD=X", np.full(100, 10.0), start="2025-01-01")
    late = _unit("BTC/USDT", np.full(50, 30.0), start="2025-02-20")   # day 51 on
    rets = book_returns([early, late])
    assert rets.iloc[:50].tolist() == pytest.approx([10.0 / CAPITAL] * 50)
    assert rets.iloc[50:100].tolist() == pytest.approx([40.0 / (2 * CAPITAL)] * 50)
    book = portfolio_evidence([early, late])
    assert book["net_pnl"] == pytest.approx(100 * 10.0 + 50 * 30.0)
    assert book["return_pct"] == pytest.approx((50 * 0.001 + 50 * 0.002) * 100)


def test_a_steady_edge_clears_zero_and_a_steady_loss_draws_down_from_the_start():
    rng = np.random.default_rng(11)
    win = portfolio_evidence([_unit("BTC/USDT", rng.normal(20.0, 10.0, 365))])
    assert win["interval_above_zero"] is True

    lose = portfolio_evidence([_unit("BTC/USDT", np.full(100, -10.0))])
    assert lose["max_drawdown_pct"] == pytest.approx(-10.0)    # 1,000 of 10,000
    assert lose["interval_above_zero"] is False


def test_family_deflated_sharpe_prices_the_best_book_against_the_rest():
    rng = np.random.default_rng(5)
    books = {f"s{k}": pd.Series(rng.normal(0.0002 * k, 0.01, 700)) for k in range(5)}
    out = family_deflated_sharpe(books)
    assert out["best"] == max(books, key=lambda k: books[k].mean() / books[k].std())
    assert out["n_trials"] == 5 and 0.0 <= out["deflated_sharpe"] <= 1.0
    assert "not counted" in out["counted"]
    assert family_deflated_sharpe({"only": books["s1"]})["deflated_sharpe"] is None


# --------------------------------------------------- through the runner
def _with_equity_curves(monkeypatch):
    """The fake backtester from test_experiments, plus the marked equity curve
    and starting capital the real one returns."""
    import bot.backtest as bt_mod
    base = bt_mod.Backtester

    class CurveBT(base):
        starting_capital = CAPITAL

        def run_walk_forward(self, *a, **k):
            wf = super().run_walk_forward(*a, **k)
            balance, curve = CAPITAL, []
            for t in sorted(wf["trades"], key=lambda t: t["exit_ts"]):
                balance += t["pnl"]
                curve.append({"ts": t["exit_ts"], "equity": balance})
            wf["equity_curve"] = curve
            return wf

    monkeypatch.setattr(bt_mod, "Backtester", CurveBT)


def test_the_runner_reports_the_book_and_the_gate_ignores_it(tmp_path, monkeypatch):
    _fake_world(tmp_path, monkeypatch)
    _with_equity_curves(monkeypatch)
    path = _write(tmp_path, strategies='["turtle_trend", "connors_meanrev", "ts_momentum"]')
    res = ex.run_declaration(path, workers=1, quiet=True, require_preregistered=False)
    by = {e["strategy"]: e for e in res["summary"]}
    for e in by.values():
        book = e["portfolio"]
        assert book["available"] and book["markets"] == e["markets"]
        assert book["net_pnl"] == pytest.approx(e["oos"]["net_pnl"], abs=0.05)
    assert by["ts_momentum"]["portfolio"]["markets"] == 4      # 2 symbols x 1h, 4h
    assert res["portfolio_family"]["n_trials"] == 3

    # the verdicts read the trade-pooled evidence only: the same run without
    # the daily series writes the same verdicts
    verdicts = json.loads((tmp_path / "db" / "results" / "promotions_standard.json")
                          .read_text())["strategies"]
    _fake_world(tmp_path, monkeypatch)
    bare = ex.run_declaration(path, workers=1, quiet=True, require_preregistered=False)
    assert all(not e["portfolio"]["available"] for e in bare["summary"])
    again = json.loads((tmp_path / "db" / "results" / "promotions_standard.json")
                       .read_text())["strategies"]
    assert {k: v["status"] for k, v in again.items()} == \
        {k: v["status"] for k, v in verdicts.items()}

    _with_equity_curves(monkeypatch)
    ex.run_declaration(path, workers=1, quiet=True, require_preregistered=False)
    rows = {r["strategy"]: r for r in ex.experiment_log() if r["experiment"] == "g"}
    assert "portfolio Sharpe" in rows["turtle_trend"]["result"]
    assert "independent bets" in rows["turtle_trend"]["result"]
    promo._CACHE.update(path=None, mtime=None, verdicts={})
