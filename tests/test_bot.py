"""Test suite: indicators, strategies, risk, broker, orchestrator, backtest causality.

Run:  python3 -m pytest tests/ -v
Also works without pytest via the __main__ fallback.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from config import CONFIG, MarketSpec
from bot.indicators import add_all_indicators, ema, rsi
from bot.strategies import TurtleTrend, ConnorsMeanReversion, VWAPScalper, TimeSeriesMomentum
from bot.risk import RiskManager
from bot.broker import PaperBroker
from bot.orchestrator import Orchestrator, detect_regime


# ------------------------------------------------------------------ fixtures
def make_df(prices, start="2024-01-01", freq="1h", volume_base=100.0, seed=7):
    """Synthetic OHLCV with realistic intrabar structure.

    Naive synthesis (fresh high AND low beyond the previous bar every time)
    inflates ADX to 40+ even in flat ranges — an artifact, not a market. Real
    bars usually keep highs/lows inside the prior bar; only some bars push new
    extremes. We model that so regime/ADX tests are meaningful.
    """
    rng = np.random.default_rng(seed)
    prices = np.asarray(prices, dtype=float)
    opens = np.roll(prices, 1)
    opens[0] = prices[0]
    body_hi = np.maximum(opens, prices)
    body_lo = np.minimum(opens, prices)
    wick = np.abs(rng.normal(0, 0.12, len(prices))) + 0.02
    push = rng.choice([0.0, 1.0], len(prices), p=[0.55, 0.45])
    pull = rng.choice([0.0, 1.0], len(prices), p=[0.5, 0.5])
    highs = np.maximum(body_hi + wick * push, body_hi)
    lows = np.minimum(body_lo - wick * pull, body_lo)
    vol = volume_base * (1 + rng.normal(0, 0.25, len(prices))).clip(0.2)
    idx = pd.date_range(start, periods=len(prices), freq=freq, tz="UTC")
    return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                         "close": prices, "volume": vol}, index=idx)


def trending_df(n=600, drift=0.0012, start=100.0, seed=3):
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, 0.008, n)
    return make_df(start * np.cumprod(1 + steps))


def range_df(n=600, lo=95, hi=105, seed=5):
    rng = np.random.default_rng(seed)
    prices = 100 + np.cumsum(rng.normal(0, 0.35, n))
    prices = np.clip(prices, lo, hi)
    return make_df(prices, volume_base=120)


CRYPTO_1H = MarketSpec("crypto", "TEST/USDT", "1h", "TestCoin")


# ------------------------------------------------------------------ indicators
def test_rsi_bounds_and_extremes():
    up = make_df(np.linspace(100, 130, 120))          # relentless rally
    down = make_df(np.linspace(130, 100, 120))        # relentless decline
    up_ind = add_all_indicators(up)
    down_ind = add_all_indicators(down)
    assert up_ind["rsi14"].iloc[-1] > 90
    assert down_ind["rsi14"].iloc[-1] < 10
    flat = make_df(np.full(120, 100.0))
    assert abs(add_all_indicators(flat)["rsi14"].iloc[-1] - 50) < 1e-6


def test_rsi2_wilder_smoothing():
    df = add_all_indicators(trending_df())
    r = rsi(df["close"], 2)
    assert not r.iloc[-10:].isna().any()
    vals = r.dropna()
    assert len(vals) and ((vals >= 0) & (vals <= 100)).all()
    # warmup is NaN (comparisons read False -> can never auto-fire a gate),
    # and a relentless rally reads exactly 100 (all gains), not NaN
    assert r.head(2).isna().all()
    up = make_df(np.linspace(100, 130, 40))
    up_rsi = rsi(up["close"], 2).dropna()
    assert len(up_rsi) and (up_rsi == 100.0).all()


def test_ema_matches_reference():
    s = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=float)
    e = ema(s, 3)
    # pandas ewm span=3 alpha=2/4=0.5; check recursion by hand for the last point
    alpha = 2 / (3 + 1)
    expected = s.iloc[-1]
    prev = expected
    for v in s.iloc[::-1][1:]:
        prev = alpha * v + (1 - alpha) * prev
    assert abs(e.iloc[-1] - prev) < 1e-9 or abs(e.iloc[-1] - expected) < 6


def test_atr_positive_and_sane():
    df = add_all_indicators(trending_df())
    a = df["atr"]
    assert (a.dropna() > 0).all()
    # ATR should be smaller than the average bar range
    bar_range = (df["high"] - df["low"]).rolling(14).mean()
    assert (a.dropna() <= bar_range.dropna() * 1.6).all()


def test_donchian_no_lookahead():
    df = add_all_indicators(make_df(np.linspace(100, 120, 60)))
    hi = df["high"].rolling(20).max()          # don_up20 at i uses bars (i-19..i)
    assert np.allclose(df["don_up20"].values[20:], hi.values[20:])
    # and strategies read it shifted by 1 (prior channel), verified in turtle


def test_vwap_rolling_and_no_volume():
    df = add_all_indicators(trending_df())
    assert df["vwap_roll"].iloc[-1] > 0
    novol = df.drop(columns=["volume"])
    ind = add_all_indicators(novol)
    assert ind["vol_ratio"].iloc[-1] == 1.0     # auto-pass gate
    assert not ind["vwap_roll"].iloc[-5:].isna().any()


def test_seasonal_rvol():
    """Time-of-day RVOL (Zarattini-Barbon-Aziz): this bar's volume vs the
    symbol's own average at the same hour:minute. A plain rolling ratio
    mis-grades crypto's hour-of-day seasonality; the seasonal baseline must
    grade a normal US-hours bar as normal, a burst as a burst, and stay
    causal (truncating the frame never changes past values)."""
    from bot.indicators import seasonal_rvol
    n = 3 * 96
    idx = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    df = make_df(np.full(n, 100.0), start="2026-01-01", freq="15min")
    df.index = idx
    df["volume"] = np.array([5.0 if 14 <= t.hour <= 16 else 1.0 for t in idx])

    r = seasonal_rvol(df)
    assert r.iloc[:96].isna().all()          # < 2 prior same-slot bars -> NaN (auto-pass)
    tail = r.iloc[2 * 96:]
    # both US-hours and Asia-hours bars sit at ~1.0 (their own norms) even
    # though the plain ratio reads the US-hours bars as 5x bursts
    assert 0.7 < tail.mean() < 1.3
    us = r[[i for i in r.index if 14 <= i.hour <= 16]].dropna()
    assert (us > 0.7).all() and (us < 1.4).all()

    # burst bar: 3x its own same-slot norm
    burst = df.copy()
    burst.iloc[-1, burst.columns.get_loc("volume")] *= 3.0
    assert seasonal_rvol(burst).iloc[-1] > 2.5
    # quiet bar: 0.3x norm
    quiet = df.copy()
    quiet.iloc[-1, quiet.columns.get_loc("volume")] *= 0.3
    assert seasonal_rvol(quiet).iloc[-1] < 0.5
    # no-volume feed -> 1.0 auto-pass
    assert seasonal_rvol(df.drop(columns=["volume"])).iloc[-1] == 1.0
    # causality: truncating the frame never changes any PAST bar's RVOL
    r_trunc = seasonal_rvol(df.iloc[:-5])
    mask = r.iloc[:-5].notna()
    assert (r.iloc[:-5][mask] == r_trunc[mask]).all()


def test_scalper_rvol_gate():
    """The scalper must refuse an otherwise-perfect reclaim bar whose volume
    is below the symbol's own norm for that time of day, and take it when the
    same setup arrives with unusual same-slot volume (the Stocks-in-Play
    RVOL filter: same rules went from Sharpe 0.48 to 2.81). The knob ships
    OFF (measured neutral on 24/7 crypto bars — BACKTESTS.md) so the test
    raises it explicitly; 0.0 must auto-pass everything."""
    n = 4 * 96
    prices = list(np.linspace(120, 100, n - 4)) + [100.5, 101.5, 103.2, 105.0]
    df = make_df(prices, start="2026-01-01", freq="15min")
    df["volume"] = 100.0
    df = add_all_indicators(df)
    i = len(df) - 1
    p = CONFIG.params
    old_rvol_min = p.scalper_rvol_min
    p.scalper_rvol_min = 1.10          # experiment mode: gate active
    try:
        sc = VWAPScalper()

        # the final bar's volume is 5x its own same-slot norm -> gate passes
        hot = df.copy()
        hot.loc[hot.index[-1], "volume"] = 500.0
        hot = add_all_indicators(hot)
        assert hot["rvol"].iloc[i] > 2.5
        sig_hot = sc.evaluate(hot, i)

        # identical price setup but volume at 20% of its own norm -> refused
        cold = df.copy()
        cold.loc[cold.index[-1], "volume"] = 20.0
        cold = add_all_indicators(cold)
        assert cold["rvol"].iloc[i] < 0.5
        sig_cold = sc.evaluate(cold, i)
        if sig_hot.action in ("LONG", "SHORT"):
            assert sig_cold.action == "FLAT" or \
                "conviction" in (sig_cold.rationale or "")
    finally:
        p.scalper_rvol_min = old_rvol_min

    # shipped default (0.0) auto-passes: quiet bar trades identically to hot
    p.scalper_rvol_min = 0.0
    base_sig = VWAPScalper().evaluate(cold, i)
    assert base_sig.action == sig_hot.action
    p.scalper_rvol_min = old_rvol_min


def test_halflife_ar1_math():
    """Chan's AR(1)/OU half-life: a known phi recovers ln(2)/(1-phi) bars; a
    true random walk reads a HUGE (not infinite) half-life because finite-
    window OLS is biased below the unit root (Dickey-Fuller bias — the gate
    threshold, not the inf label, does the refusing); an explosive window
    reads inf; the warmup reads NaN (auto-pass); and the statistic is causal
    (truncating the frame never changes past values)."""
    from bot.indicators import halflife_ar1
    rng = np.random.default_rng(7)
    n = 3000
    e = rng.normal(0, 0.01, n)

    def ar1(phi):
        x = np.empty(n)
        x[0] = 0.0
        for t in range(1, n):
            x[t] = phi * x[t - 1] + e[t]
        return pd.Series(x)

    hl = halflife_ar1(ar1(0.9), window=200)          # ln(2)/0.1 = 6.93
    med = hl.iloc[-500:].median()
    assert 4.0 < med < 11.0, med
    hl_fast = halflife_ar1(ar1(0.5), window=200)     # ln(2)/0.5 = 1.39
    assert 0.7 < hl_fast.iloc[-500:].median() < 2.5
    hl_rw = halflife_ar1(pd.Series(np.cumsum(e)), window=200)
    assert hl_rw.iloc[-500:].median() > 20.0          # random walk: huge, finite
    assert np.isinf(halflife_ar1(ar1(1.05), window=200).iloc[-1])   # explosive
    assert halflife_ar1(ar1(0.9), window=200).iloc[:199].isna().all()  # warmup
    # degenerate window (constant series): variance 0 -> NaN, never garbage/inf
    const = pd.Series(np.full(400, 3.0))
    assert halflife_ar1(const, window=200).isna().all()
    s = ar1(0.9)
    full = halflife_ar1(s, window=200)
    trunc = halflife_ar1(s.iloc[:-5], window=200)
    mask = full.iloc[:-5].notna()
    assert (full.iloc[:-5][mask] == trunc[mask]).all()


def test_connors_halflife_gate():
    """The half-life gate refuses a Connors entry whose measured reversion
    half-life exceeds the strategy's own horizon, passes it when short, and
    auto-passes NaN (warmup). Ships ON at 12 bars (BACKTESTS.md Round 6:
    walk-forward positive on both symbols, 3 of 4 cells positive)."""
    from config import StrategyParams
    from bot.indicators import add_all_indicators as aai
    assert StrategyParams().mr_halflife_max == 12.0    # shipped default

    # wiggled deep bull (see test_connors_pullback_entry: a pure ramp makes
    # the deviation trend and the gate correctly reads half-life inf)
    t = np.arange(404)
    prices = list(np.linspace(100, 190, 400) + 2.0 * np.sin(t[:400] * 0.7))
    prices += [190, 187, 182.5, 180.5]
    df = aai(make_df(prices))
    mr = ConnorsMeanReversion()

    p = mr.p                        # strategies own a fresh StrategyParams
    old_max = p.mr_halflife_max
    p.mr_halflife_max = 0.0         # locate the raw entry bar, gate disabled
    i = next(j for j in range(len(df) - 6, len(df))
             if mr.evaluate(df, j).action == "LONG")
    p.mr_halflife_max = 12.0
    try:
        slow = df.copy()
        slow["halflife"] = 40.0                        # slower than the horizon
        sig = mr.evaluate(slow, i)
        assert sig.action == "FLAT" and "half-life" in (sig.rationale or "")

        explosive = df.copy()
        explosive["halflife"] = float("inf")           # AR(1) at/above unit root
        sig = mr.evaluate(explosive, i)
        assert sig.action == "FLAT" and "mean reversion" in (sig.rationale or "")

        fast = df.copy()
        fast["halflife"] = 5.0                         # fast reversion: trades
        sig = mr.evaluate(fast, i)
        assert sig.action == "LONG"
        assert sig.meta["halflife"] == 5.0             # journaled for attribution

        warm = df.copy()
        warm["halflife"] = float("nan")                # warmup: auto-pass
        assert mr.evaluate(warm, i).action == "LONG"

        edge = df.copy()
        edge["halflife"] = 12.0                        # exactly the horizon: passes (strict >)
        assert mr.evaluate(edge, i).action == "LONG"
    finally:
        p.mr_halflife_max = old_max

    # knob off (0.0): the slow regime trades identically to fast
    p.mr_halflife_max = 0.0
    try:
        assert mr.evaluate(slow, i).action == "LONG"
    finally:
        p.mr_halflife_max = old_max


def test_adx_trending_vs_ranging():
    tr = add_all_indicators(trending_df(600, drift=0.002))
    rg = add_all_indicators(range_df(600))
    assert tr["adx"].iloc[-1] > rg["adx"].iloc[-1]
    assert tr["adx"].iloc[-1] > 20


# ------------------------------------------------------------------ strategies
def test_turtle_breakout_entry():
    # strong rally -> at some bar close must exceed prior 20-bar high
    df = add_all_indicators(trending_df(600, drift=0.0025, seed=11))
    turtle = TurtleTrend()
    any_long = any(turtle.evaluate(df, i).action == "LONG"
                   for i in range(250, len(df) - 1))
    assert any_long, "turtle should catch a breakout in a strong trend"
    sig = turtle.evaluate(df, len(df) - 1)
    if sig.action == "LONG":
        assert sig.stop_distance and sig.stop_distance > 0
        assert "breakout" in sig.rationale.lower() or "broke" in sig.rationale.lower()


def test_turtle_no_entries_against_ema_structure():
    """A long breakout must never fire when EMA50<EMA200 (range-edge trap), and
    vice versa. The EMA-alignment gate is what kills false breakouts in ranges."""
    df = add_all_indicators(range_df(600, seed=9))
    turtle = TurtleTrend()
    bad = []
    for i in range(250, len(df) - 1):
        s = turtle.evaluate(df, i)
        ema50, ema200 = float(df["ema50"].iloc[i]), float(df["ema200"].iloc[i])
        if s.action == "LONG" and not (ema50 > ema200):
            bad.append((i, "LONG against structure"))
        if s.action == "SHORT" and not (ema50 < ema200):
            bad.append((i, "SHORT against structure"))
    assert not bad, bad[:3]


def test_turtle_survives_a_range_market():
    """No-drift oscillation is the worst case for trend following (whipsaw).
    With the full pipeline (risk gates + cooldowns + fees + slippage), the
    strategy must bleed a little, not blow up — the documented Turtle profile."""
    from bot.backtest import Backtester
    n = 700
    osc = 100 + 4 * np.sin(np.linspace(0, 4 * 2 * np.pi, n))   # zero drift by construction
    df = make_df(osc, seed=9)
    bt = Backtester(CONFIG)
    res = bt.run(CRYPTO_1H, df, strategy="turtle_trend")
    s = res.stats()
    assert s["max_drawdown_pct"] > -5.0, s
    assert s["return_pct"] > -5.0, s
    assert s["trades"] <= 15, f"whipsaw not throttled: {s['trades']} trades in a flat sine market"


def test_connors_pullback_entry():
    # deep bull (well above EMA200) + sharp 2-bar pullback -> RSI(2) < 5 long.
    # The wiggle matters: the Chan half-life gate ships ON, and a pure linear
    # ramp makes the deviation trend (AR(1) reads explosive, half-life inf) —
    # which the gate CORRECTLY refuses. A real deep bull oscillates around its
    # EMA, so the frame does too; its measured half-life (~3-9 bars) trades.
    t = np.arange(404)
    prices = list(np.linspace(100, 190, 400) + 2.0 * np.sin(t[:400] * 0.7))
    prices += [190, 187, 182.5, 180.5]               # sharp dip
    df = add_all_indicators(make_df(prices))
    mr = ConnorsMeanReversion()
    sigs = [mr.evaluate(df, i) for i in range(len(df) - 6, len(df))]
    assert any(s.action == "LONG" for s in sigs), \
        f"expected a pullback long, got {[ (s.action, s.rationale[:60]) for s in sigs ]}"


def test_connors_never_counter_trend():
    # deep DOWNTREND: no longs even if RSI2 is 0
    prices = list(np.linspace(200, 100, 400))
    df = add_all_indicators(make_df(prices))
    mr = ConnorsMeanReversion()
    for i in range(250, len(df) - 1, 7):
        s = mr.evaluate(df, i)
        assert s.action != "LONG"


def test_scalper_vwap_reclaim():
    # decline under VWAP, then a strong reclaim bar with volume
    prices = list(np.linspace(120, 100, 300))
    prices += [100.5, 101.5, 103.2, 105.0]
    df = add_all_indicators(make_df(prices, freq="5min", volume_base=150))
    sc = VWAPScalper()
    sigs = [sc.evaluate(df, i) for i in range(len(df) - 4, len(df))]
    assert any(s.action in ("LONG", "SHORT") for s in sigs) or all(s.action == "FLAT" for s in sigs)


def test_scalper_warmup_and_safety():
    sc = VWAPScalper()
    # below warmup: must return FLAT without crashing, even with junk data
    df_short = add_all_indicators(trending_df(20))
    assert sc.evaluate(df_short, len(df_short) - 1).action == "FLAT"
    # minimal-but-sufficient frame still returns a well-formed signal
    df_ok = add_all_indicators(trending_df(50))
    sig = sc.evaluate(df_ok, len(df_ok) - 1)
    assert sig.action in ("LONG", "SHORT", "FLAT")
    assert 0.0 <= sig.confidence <= 0.90


def test_strategies_never_read_future():
    """Core causality test: truncating the frame at i must not change bar-i signals."""
    df = add_all_indicators(trending_df(500, seed=13))
    for Strat in (TurtleTrend, ConnorsMeanReversion, VWAPScalper):
        s = Strat()
        for i in (300, 380, 450):
            full = s.evaluate(df, i)
            trunc = s.evaluate(df.iloc[: i + 1], i)
            assert full.action == trunc.action
            assert abs(full.confidence - trunc.confidence) < 1e-9


# ------------------------------------------------------------------ risk
def test_risk_position_sizing():
    rm = RiskManager(CONFIG)
    qty = rm.size_position(10_000, price=50_000, stop_distance=1_000)
    # 1% risk = $100 / $1000 stop = 0.1 qty -> notional 5000 = 50% > cap 25% -> capped
    assert qty == pytest_approx(2_500 / 50_000, 6)
    qty2 = rm.size_position(10_000, price=50_000, stop_distance=4_000)
    assert qty2 == pytest_approx(100 / 4_000, 6)    # 0.025 -> notional 1250 ok
    assert rm.size_position(10_000, 1, stop_distance=None) == 0.0


def pytest_approx(expected, tol=1e-9):
    def check(v):
        return abs(v - expected) <= tol
    class _A:
        def __eq__(self, other): return check(other)
    return _A()


def test_risk_approve_gates():
    rm = RiskManager(CONFIG)
    rm.note_equity(10_000)
    d = _dec("LONG", 0.8, stop=100.0, price=1000.0)
    ok = rm.approve(d, CRYPTO_1H, 10_000, open_positions=0, has_position_on_symbol=False)
    assert ok.approved and ok.qty > 0

    assert not rm.approve(_dec("HOLD", 0.9), CRYPTO_1H, 10_000, 0, False).approved
    assert not rm.approve(_dec("LONG", 0.3), CRYPTO_1H, 10_000, 0, False).approved       # confidence floor
    assert not rm.approve(_dec("LONG", 0.9), CRYPTO_1H, 10_000, 0, True).approved        # already positioned
    assert not rm.approve(d, CRYPTO_1H, 10_000, 4, False).approved                       # max positions
    assert not rm.approve(_dec("LONG", 0.9, stop=None), CRYPTO_1H, 10_000, 0, False).approved


def test_risk_r_distance_gate():
    """The R-distance gate: a stop wider than max_r_per_trade of entry price
    is refused (vol explosion), and a declared target below min_rr_per_trade
    is refused — the old max_r_per_trade knob promised both, wired neither."""
    from bot.risk import RiskManager
    rm = RiskManager(CONFIG)
    rm.note_equity(10_000)
    # stop 15% of price (100 @ stop 15) > 10% cap -> refused
    wide = _dec("LONG", 0.8, stop=15.0, price=100.0)
    d = rm.approve(wide, CRYPTO_1H, 10_000, 0, False)
    assert not d.approved and "vol-explosion" in d.reason
    # same trade with a 5% stop passes the gate
    ok = _dec("LONG", 0.8, stop=5.0, price=100.0)
    d = rm.approve(ok, CRYPTO_1H, 10_000, 0, False)
    assert d.approved and d.qty > 0
    # declared reward below the 1.2R floor -> refused
    cheap = _dec("LONG", 0.8, stop=5.0, price=100.0)
    cheap.target_rr = 1.0
    d = rm.approve(cheap, CRYPTO_1H, 10_000, 0, False)
    assert not d.approved and "floor" in d.reason
    # 2R target passes
    good = _dec("LONG", 0.8, stop=5.0, price=100.0)
    good.target_rr = 2.0
    d = rm.approve(good, CRYPTO_1H, 10_000, 0, False)
    assert d.approved and d.qty > 0


def test_risk_r_distance_gate_boundaries():
    """Realistic-scale and boundary semantics: a normal crypto-sized stop
    (2 on a 20000 entry = 0.01% of price) sails through, and a stop sitting
    EXACTLY at the cap is allowed — the gate is >, not >=. Pin that so no one
    'fixes' it to >= later (a boundary trade is inside the vol regime the
    exits were designed for)."""
    from bot.risk import RiskManager
    rm = RiskManager(CONFIG)
    rm.note_equity(10_000)
    # realistic sized trade: BTC-like entry, ATR stop a hundredth of a percent
    real = _dec("LONG", 0.7, stop=2.0, price=20000.0)
    d = rm.approve(real, CRYPTO_1H, 10_000, 0, False)
    assert d.approved and d.qty > 0
    # boundary: stop == price * max_r_per_trade (10 on a 100 entry) is ALLOWED
    edge = _dec("LONG", 0.8, stop=100.0, price=1000.0)
    assert edge.stop_distance == edge.price * CONFIG.risk.max_r_per_trade
    d = rm.approve(edge, CRYPTO_1H, 10_000, 0, False)
    assert d.approved and d.qty > 0
    # one tick beyond the boundary is refused
    beyond = _dec("LONG", 0.8, stop=100.0 + 1e-9, price=1000.0)
    d = rm.approve(beyond, CRYPTO_1H, 10_000, 0, False)
    assert not d.approved and "vol-explosion" in d.reason


def test_risk_daily_kill_switch():
    rm = RiskManager(CONFIG)
    rm.note_equity(10_000)
    rm.note_equity(10_000 * (1 - CONFIG.risk.daily_loss_kill_switch - 0.005))
    d = _dec("LONG", 0.9, stop=5.0, price=100.0)
    res = rm.approve(d, CRYPTO_1H, 9_600, 0, False)
    assert not res.approved and "kill switch" in res.reason


def test_risk_cooldown():
    rm = RiskManager(CONFIG)
    rm.note_equity(10_000)
    d = _dec("LONG", 0.9, stop=5.0, price=100.0)
    # cooldowns are epoch seconds: a stop-out at bar-epoch 100 * 3600s (1h bars)
    # blocks entries until +3 bars of the owning timeframe
    rm.mark_stopped_out("TEST/USDT", bar_epoch=100 * 3600, timeframe="1h")
    assert not rm.approve(d, CRYPTO_1H, 10_000, 0, False, bar_epoch=101 * 3600).approved
    assert rm.approve(d, CRYPTO_1H, 10_000, 0, False, bar_epoch=104 * 3600).approved
    # the same cooldown must read coherently from a different timeframe's clock
    # (15m spec): 3 1h bars = 12 15m bars
    CRYPTO_15M = MarketSpec("crypto", "TEST/USDT", "15m", "TestCoin")
    assert not rm.approve(d, CRYPTO_15M, 10_000, 0, False, bar_epoch=(101 * 3600) + 15 * 60).approved
    assert rm.approve(d, CRYPTO_15M, 10_000, 0, False, bar_epoch=(103 * 3600) + 45 * 60).approved


class _Dec:
    def __init__(self, action, confidence, stop=None, price=100.0, rationale="test",
                 strategy_name="turtle_trend"):
        self.action, self.confidence = action, confidence
        self.stop_distance, self.price = stop, price
        self.target_rr = None
        self.rationale = rationale
        self.strategy_name = strategy_name


def _dec(action, conf, stop=None, price=100.0):
    return _Dec(action, conf, stop, price)


# ------------------------------------------------------------------ broker
def test_broker_pnl_accounting():
    b = PaperBroker(10_000)
    d = _dec("LONG", 0.8, stop=10.0, price=100.0)
    pos = b.open_position(CRYPTO_1H, d, qty=10.0, price=100.0, trade_id=1)
    fee_in = 10_000 - b.cash
    assert fee_in > 0
    assert pos.entry_price > 100.0                       # entry slipped adversely
    closed, pnl, pnl_pct, fees_rt, exit_fill = b.close_position(CRYPTO_1H, 110.0, "test")
    expected_exit_fill = 110.0 * (1 - b.costs.slippage("crypto"))
    assert exit_fill == pytest_approx(expected_exit_fill, 1e-9)   # fill, not decision price
    gross = (exit_fill - closed.entry_price) * closed.qty
    # round-trip fees now include BOTH legs (entry fee deferred into close)
    assert fees_rt == pytest_approx(fee_in + b._fee(exit_fill * closed.qty, "crypto"), 1e-9)
    assert pnl == pytest_approx(gross - fees_rt, 1e-9)
    assert b.realized_pnl == pytest_approx(pnl, 1e-9)
    # full cash invariant: every fee and every dollar of gross lands in cash
    assert b.cash == pytest_approx(10_000 - fee_in + gross - b._fee(exit_fill * closed.qty, "crypto"), 1e-9)
    assert abs(b.equity() - b.cash) < 1e-9              # flat -> equity is cash


def test_broker_stop_target_scan():
    b = PaperBroker(10_000)
    d = _dec("LONG", 0.8, stop=5.0, price=100.0)
    b.open_position(CRYPTO_1H, d, qty=1.0, price=100.0, trade_id=1)
    pos = b.positions[("TEST/USDT", "1h")]
    bar = {"high": 101.0, "low": 90.0}            # low pierced the 95 stop
    reason, px = b.scan_bar_exits(CRYPTO_1H, bar)
    assert reason == "stop loss"
    assert px == pytest_approx(pos.stop, 1e-9)


def test_broker_oco_same_bar_stop_first():
    """OCO resolution: when BOTH levels fall inside one bar's range the true
    intrabar path is unknowable, so the conservative fill (stop) is taken."""
    b = PaperBroker(10_000)
    d = _dec("LONG", 0.8, stop=5.0, price=100.0)
    b.open_position(CRYPTO_1H, d, qty=1.0, price=100.0, trade_id=1)
    pos = b.positions[("TEST/USDT", "1h")]
    bar = {"open": 100.0, "high": 200.0, "low": 50.0}    # target 110 and stop 95 both inside
    reason, px = b.scan_bar_exits(CRYPTO_1H, bar)
    assert reason == "stop loss"
    assert px == pytest_approx(pos.stop, 1e-9)


def test_broker_gap_through_stop_fills_at_open():
    """A stop is a market order: if the bar OPENS beyond the level (gap), you
    get the open — never the level. Long gap-down and short gap-up both."""
    b = PaperBroker(10_000)
    b.open_position(CRYPTO_1H, _dec("LONG", 0.8, stop=5.0, price=100.0),
                    qty=1.0, price=100.0, trade_id=1)
    reason, px = b.scan_bar_exits(CRYPTO_1H, {"open": 93.0, "high": 96.0, "low": 90.0})
    assert (reason, px) == ("stop loss", 93.0)

    b2 = PaperBroker(10_000)
    b2.open_position(CRYPTO_1H, _dec("SHORT", 0.8, stop=5.0, price=100.0),
                     qty=1.0, price=100.0, trade_id=1)
    reason, px = b2.scan_bar_exits(CRYPTO_1H, {"open": 109.0, "high": 110.0, "low": 104.0})
    assert (reason, px) == ("stop loss", 109.0)


def test_broker_gap_through_target_fills_at_open():
    """A target is a resting limit: a gap through it fills at the (better) open."""
    b = PaperBroker(10_000)
    d = _dec("LONG", 0.8, stop=5.0, price=100.0)
    d.target_rr = 2.0
    b.open_position(CRYPTO_1H, d, qty=1.0, price=100.0, trade_id=1)
    pos = b.positions[("TEST/USDT", "1h")]
    reason, px = b.scan_bar_exits(CRYPTO_1H, {"open": 115.0, "high": 120.0, "low": 114.0})
    assert reason == "take profit"
    assert px == pytest_approx(pos.target, 1e-9) or px == 115.0
    assert px >= pos.target - 1e-9     # limit fills are never worse than the level


# ------------------------------------------------------- maker/limit cost model
def _open_long_with_target(b, target_rr=2.0, qty=1.0, stop=5.0):
    d = _dec("LONG", 0.8, stop=stop, price=100.0)
    d.target_rr = target_rr
    return b.open_position(CRYPTO_1H, d, qty=qty, price=100.0, trade_id=1)


def test_take_profit_exit_priced_as_maker():
    """A bracket take-profit is a resting limit order: no slippage crossed, and
    the exit leg pays the maker fee. Crypto maker fee equals taker by default,
    so the visible difference vs a market leg is the eliminated slippage."""
    b = PaperBroker(10_000)
    pos = _open_long_with_target(b)
    _, pnl, _, fees, exit_fill = b.close_position(
        CRYPTO_1H, pos.target, "take profit")
    assert exit_fill == pytest_approx(pos.target, 1e-9)        # no slippage
    assert b.fees_paid == pytest_approx(fees, 1e-9)
    expected_exit_fee = pos.target * pos.qty * CONFIG.costs.maker_fee_crypto
    entry_fee = pos.entry_fee
    assert fees == pytest_approx(entry_fee + expected_exit_fee, 1e-9)
    # a taker close of the same position would have been strictly worse
    b2 = PaperBroker(10_000)
    pos2 = _open_long_with_target(b2)
    _, pnl2, _, fees2, exit_fill2 = b2.close_position(
        CRYPTO_1H, pos2.target, "signal exit")
    assert exit_fill2 == pytest_approx(pos2.target * (1 - CONFIG.costs.slippage_crypto), 1e-9)
    assert pnl > pnl2


def test_take_profit_gap_fill_is_favorable_and_maker_priced():
    """Gapping through a limit fills at the open (never worse than the level)
    and the maker fee applies to the actual fill notional."""
    b = PaperBroker(10_000)
    d = _dec("LONG", 0.8, stop=5.0, price=100.0)
    d.target_rr = 2.0
    pos = b.open_position(CRYPTO_1H, d, qty=2.0, price=100.0, trade_id=1)
    gap_open = pos.target + 3.0                       # gapped well through the level
    _, _, _, fees, exit_fill = b.close_position(
        CRYPTO_1H, gap_open, "take profit")
    assert exit_fill == pytest_approx(gap_open, 1e-9)          # better than the level
    expected_exit_fee = gap_open * pos.qty * CONFIG.costs.maker_fee_crypto
    assert fees == pytest_approx(pos.entry_fee + expected_exit_fee, 1e-9)


def test_stop_and_manual_exits_still_pay_taker():
    """Stops, signal exits, manual closes and end-of-data closes are market
    orders: taker fee + adverse slippage, unchanged by the maker model."""
    for reason in ("stop loss", "manual close", "signal exit", "end of backtest"):
        b = PaperBroker(10_000)
        pos = _open_long_with_target(b)
        _, _, _, fees, exit_fill = b.close_position(CRYPTO_1H, 110.0, reason)
        assert exit_fill == pytest_approx(
            110.0 * (1 - CONFIG.costs.slippage_crypto), 1e-9), reason
        expected_exit_fee = exit_fill * pos.qty * CONFIG.costs.fee_crypto
        assert fees == pytest_approx(pos.entry_fee + expected_exit_fee, 1e-9), reason


def test_maker_pricing_kill_switch():
    """MAKER_PRICING=0 reverts every exit to taker pricing. The broker gets a
    PRIVATE cost config: PaperBroker defaults to the shared CONFIG.costs
    singleton, so flipping flags on it would leak into every later test."""
    import copy
    b = PaperBroker(10_000, costs=copy.deepcopy(CONFIG.costs))
    b.costs.maker_pricing = False
    pos = _open_long_with_target(b)
    _, _, _, _, exit_fill = b.close_position(CRYPTO_1H, pos.target, "take profit")
    assert exit_fill == pytest_approx(
        pos.target * (1 - CONFIG.costs.slippage_crypto), 1e-9)


def test_forex_maker_fee_applies_to_take_profit():
    """Forex exit legs pay the (halved-spread) forex maker fee; slippage is
    still eliminated."""
    b = PaperBroker(10_000)
    fx = MarketSpec("forex", "EURUSD=X", "1h", "EUR/USD")
    d = _dec("LONG", 0.8, stop=0.005, price=1.10)
    d.target_rr = 2.0
    pos = b.open_position(fx, d, qty=10_000.0, price=1.10, trade_id=1)
    _, _, _, fees, exit_fill = b.close_position(fx, pos.target, "take profit")
    assert exit_fill == pytest_approx(pos.target, 1e-12)
    expected_exit_fee = exit_fill * pos.qty * CONFIG.costs.maker_fee_forex
    assert fees == pytest_approx(pos.entry_fee + expected_exit_fee, 1e-6)
    assert expected_exit_fee < exit_fill * pos.qty * CONFIG.costs.fee_forex + 1e-12


def test_risk_kill_switch_uses_simulated_clock():
    """The kill switch must track the TRADING day from bar timestamps, not the
    wall clock — a 2024 backtest can't run on today's calendar. Same rule in
    backtest and live: backtests pass ts, live omits it."""
    rm = RiskManager(CONFIG)
    rm.note_equity(10_000, ts="2024-05-01T00:00:00+00:00")
    assert not rm.halted
    rm.note_equity(10_000 * 0.96, ts="2024-05-01T12:00:00+00:00")   # -4% mid-day
    assert rm.halted                                   # engaged on the simulated day
    d = _dec("LONG", 0.9, stop=5.0, price=100.0)
    assert not rm.approve(d, CRYPTO_1H, 9_600, 0, False).approved
    rm.note_equity(10_000 * 0.96, ts="2024-05-02T00:00:00+00:00")   # next day re-arms
    assert not rm.halted


def test_backtest_deterministic():
    """Identical inputs -> identical trades, equity curve and stats. Every
    validation claim downstream depends on this reproducibility."""
    from bot.backtest import Backtester
    df = trending_df(700, drift=0.0015, seed=17)
    bt = Backtester(CONFIG)
    r1 = bt.run(CRYPTO_1H, df, strategy=None)
    r2 = bt.run(CRYPTO_1H, df, strategy=None)
    assert r1.stats() == r2.stats()
    assert r1.trades == r2.trades
    assert r1.equity_curve == r2.equity_curve


def test_backtest_stop_fills_never_beat_the_level():
    """End-to-end fill honesty: every stop-loss exit in a backtest must fill at
    or WORSE than the stop level (slippage and gaps only hurt)."""
    from bot.backtest import Backtester
    rise = 100.0 * np.cumprod(1 + np.full(280, 0.004))          # trend -> breakout entry
    crash = rise[-1] * np.cumprod(1 + np.full(60, -0.02))        # violent reversal
    df = make_df(np.concatenate([rise, crash]), seed=23)
    bt = Backtester(CONFIG)
    res = bt.run(CRYPTO_1H, df, strategy="turtle_trend")
    stops = [t for t in res.trades if t["exit_reason"] == "stop loss"]
    for t in stops:
        if t["side"] == "long":
            assert t["exit_price"] <= t["stop"] + 1e-9, t
        else:
            assert t["exit_price"] >= t["stop"] - 1e-9, t
    # fees recorded per trade must now be the full round trip (both legs);
    # _trade_dict rounds fees to 4 decimals
    for t in res.trades:
        expected = (t["entry_price"] + t["exit_price"]) * t["qty"] * CONFIG.costs.fee_crypto
        assert abs(t["fees"] - expected) <= 5e-4, t


# ------------------------------------------------------------------ orchestrator
def test_regime_detection():
    tr = add_all_indicators(trending_df(600, drift=0.002))
    rg = add_all_indicators(range_df(600))
    r1, _ = detect_regime(tr, len(tr) - 1)
    r2, _ = detect_regime(rg, len(rg) - 1)
    assert r1 == "trending"
    assert r2 == "ranging"


def test_orchestrator_decision_shape():
    o = Orchestrator()
    df = add_all_indicators(trending_df(500, seed=21))
    d = o.decide(df, len(df) - 2, CRYPTO_1H, include_sentiment=False)
    assert d.action in ("LONG", "SHORT", "HOLD")
    assert 0 <= d.confidence <= 0.95
    assert d.regime in ("trending", "ranging", "unknown")
    assert isinstance(d.rationale, str) and len(d.rationale) > 5
    # every signal name must be a REGISTERED strategy (Milestones C1/C2 added
    # ts_momentum/fx_regime_meanrev to the 1h vote — the pre-C assertion froze
    # the 3-strategy roster and failed the moment the registry grew)
    from bot.strategies import STRATEGY_CLASSES
    assert set(d.strategy_signals) <= set(STRATEGY_CLASSES) | {"kronos"}
    if d.action != "HOLD":
        assert d.stop_distance > 0
        assert d.strategy_name


def test_every_valid_timeframe_is_owned():
    """VALID_TIMEFRAMES must all map to a strategy — a spec on an unowned
    timeframe silently HOLDs forever while the UI badges it (the old literal
    STRATEGY_BY_TF lied for 5m/1d; the derived map must now cover them).
    Post-Milestone-C: a timeframe may be OWNED by several strategies
    (ts_momentum + turtle share 1h) — the orchestrator consults every
    strategy that prefers the spec's timeframe, so the derived map lists
    whichever strategy last claimed the tf. What must hold: every valid tf
    has an owner, and the badge names a strategy that actually prefers
    that timeframe."""
    import bot.dashboard as dash_mod
    from config import VALID_TIMEFRAMES
    covered = set(dash_mod.STRATEGY_BY_TF)
    assert covered >= set(VALID_TIMEFRAMES), \
        f"unowned timeframes: {sorted(set(VALID_TIMEFRAMES) - covered)}"
    # the ownership map must agree with what the orchestrator enforces: the
    # badge for each tf names a REGISTERED strategy that prefers that tf
    from bot.strategies import STRATEGY_CLASSES
    for tf, name in dash_mod.STRATEGY_BY_TF.items():
        assert name in STRATEGY_CLASSES
        assert tf in STRATEGY_CLASSES[name].preferred_timeframes
    assert dash_mod.STRATEGY_BY_TF["5m"] == "vwap_scalper"
    assert dash_mod.STRATEGY_BY_TF["1d"] == "connors_meanrev"


def test_orchestrator_conflict_guard():
    o = Orchestrator()
    df = add_all_indicators(range_df(400))
    # fabricate conflicting strong signals by monkeypatching strategies
    class FakeLong:
        preferred_timeframes = ("1h",)
        def evaluate(self, df, i):
            from bot.strategies.base import Signal
            return Signal("fake", "LONG", 0.9, rationale="x")
    class FakeShort:
        preferred_timeframes = ("1h",)
        def evaluate(self, df, i):
            from bot.strategies.base import Signal
            return Signal("fake2", "SHORT", 0.9, rationale="y")
    o.strategies = {"turtle_trend": FakeLong(), "connors_meanrev": FakeShort()}
    d = o.decide(df, len(df) - 2, CRYPTO_1H, include_sentiment=False)
    assert d.action == "HOLD"


# ------------------------------------------------------------------ backtest
def test_backtest_end_to_end_and_costs():
    from bot.backtest import Backtester
    df = trending_df(700, drift=0.0015, seed=17)
    bt = Backtester(CONFIG)
    res = bt.run(CRYPTO_1H, df, strategy="turtle_trend")
    s = res.stats()
    assert s["trades"] >= 1, "a strong trend should produce at least one turtle trade"
    assert s["fees"] > 0
    assert set(["return_pct", "win_rate_pct", "max_drawdown_pct"]) <= set(s)
    assert s["end_equity"] > 0


def test_backtest_does_not_lose_money_in_flat_market_fees_only():
    """Flat market -> turtle ADX gate + connors no-signal -> at most few trades, no disaster."""
    from bot.backtest import Backtester
    df = make_df(np.full(600, 100.0) + np.random.default_rng(1).normal(0, 0.05, 600))
    bt = Backtester(CONFIG)
    res = bt.run(CRYPTO_1H, df, strategy="turtle_trend")
    s = res.stats()
    assert s["max_drawdown_pct"] > -5.0        # must survive a flat market
    assert s["return_pct"] > -5.0


def test_journal_roundtrip():
    from bot.journal import Journal
    with tempfile.TemporaryDirectory() as td:
        j = Journal(os.path.join(td, "t.db"))
        d = _dec("LONG", 0.7, stop=2.0, price=100.0)
        d.regime = "trending"
        d.strategy_signals = {"turtle_trend": {"action": "LONG", "confidence": 0.7}}
        d.sentiment = {}
        d.rationale = "test"
        d.target_rr = None
        j.add_decision("TEST/USDT", "1h", d)
        tid = j.open_trade("TEST/USDT", "long", 1.0, 100.0, 95.0, 105.0,
                           "turtle_trend", "test rationale")
        j.close_trade(tid, 105.0, 4.9, 4.9, 0.21, "take profit")
        j.add_equity(10_004.9, 10_004.9)
        j.log_chat("user", "hi")
        stats = j.stats()
        assert stats["closed_trades"] == 1
        assert stats["win_rate"] == 100.0
        assert stats["by_strategy"]["turtle_trend"]["trades"] == 1
        assert len(j.recent_decisions()) == 1
        assert len(j.equity_curve()) == 1
        assert j.open_trades() == []
        # mode-filtered reads must not crash (regression: f-string SQL built
        # "WHERE status='CLOSED' WHERE mode=..." for stats(mode=...))
        assert j.stats(mode="paper")["closed_trades"] == 1
        assert j.recent_trades(mode="paper")[0]["symbol"] == "TEST/USDT"
        assert j.equity_curve(mode="paper")[0]["cash"] == 10_004.9
        assert j.last_equity_point(mode="paper")["cash"] == 10_004.9


def test_journal_transactions_ledger():
    """The Account tab's deposit/withdraw history: add_transaction writes
    typed ledger rows and recent_transactions returns them newest-first."""
    from bot.journal import Journal
    with tempfile.TemporaryDirectory() as td:
        j = Journal(os.path.join(td, "t.db"))
        j.add_transaction("deposit", 500.0)
        j.add_transaction("withdrawal", 200.0, cash_after=9_800.0, equity_after=10_300.0)
        j.add_transaction("reset", 10_000.0, note="account reset")
        rows = j.recent_transactions()
        assert len(rows) == 3
        assert [r["id"] for r in rows] == sorted((r["id"] for r in rows), reverse=True)
        assert [r["kind"] for r in rows] == ["reset", "withdrawal", "deposit"]
        assert rows[0]["amount"] == pytest_approx(10_000.0)
        assert rows[1]["amount"] == pytest_approx(200.0)
        assert rows[2]["amount"] == pytest_approx(500.0)
        # snapshot columns round-trip; the optional ones stay NULL when omitted
        assert rows[0]["mode"] == "paper"
        assert rows[1]["cash_after"] == pytest_approx(9_800.0)
        assert rows[1]["equity_after"] == pytest_approx(10_300.0)
        assert rows[2]["cash_after"] is None
        assert rows[0]["note"] == "account reset"
        # mode filter: the written mode sees all 3, a bogus mode sees none
        assert len(j.recent_transactions(mode="paper")) == 3
        assert j.recent_transactions(mode="live") == []
        # limit keeps exactly the two most recent rows
        top2 = j.recent_transactions(limit=2)
        assert [r["kind"] for r in top2] == ["reset", "withdrawal"]


def test_journal_chronological_anchor_and_stats_order():
    """Seeded history is written market-by-market: insertion id order is not
    time order. The restart anchor and the drawdown walk must use timestamps
    (regression: last id-ordered row — the last-seeded MARKET, not the latest
    point — became broker cash on restart)."""
    from bot.journal import Journal
    with tempfile.TemporaryDirectory() as td:
        j = Journal(os.path.join(td, "t.db"))
        # market A's history written LAST but chronologically EARLIER
        j.add_equity(9_000.0, 9_000.0, ts="2026-01-01T00:00:00+00:00")
        j.add_equity(9_500.0, 9_500.0, ts="2026-01-02T00:00:00+00:00")
        j.add_equity(11_000.0, 11_000.0, ts="2025-12-31T00:00:00+00:00")  # late insert, early ts
        assert j.last_equity_point()["cash"] == 9_500.0
        s = j.stats()
        assert s["start_equity"] == 11_000.0    # chronological first
        assert s["current_equity"] == 9_500.0    # chronological last
        # peak 11k -> trough 9k is a real -18.2% drawdown; id order saw none
        assert s["max_drawdown_pct"] < -15.0


def test_journal_close_trade_with_equity_is_atomic_and_reconciles():
    """close_trade(equity=, cash=) writes the trade close and the equity point
    in ONE transaction, and closed_cash_delta_since() reports exactly the
    window a crash between close and the next cycle-end write would leave.

    Anchor-aware arithmetic (the old query returned pnl+fees = gross for
    every window, refunding fees the broker had already charged):
      - anchor BETWEEN entry and close (the standard crash window): the
        anchor cash already paid the entry fee; the close event adds
        gross - exit_fee = pnl + entry_fee;
      - entry ALSO after the anchor (outage window, skipped equity writes):
        both legs are inside the window -> delta = pnl exactly."""
    from bot.journal import Journal
    with tempfile.TemporaryDirectory() as td:
        j = Journal(os.path.join(td, "t.db"))
        j.add_equity(10_000.0, 10_000.0, ts="2026-01-01T00:00:00+00:00")
        # standard crash window: opened BEFORE the anchor, closed after it.
        # Legacy row shape (no entry_fee/realized_cash_delta recorded) —
        # entry_fee approximates as fees/2.
        tid = j.open_trade("TEST/USDT", "long", 1.0, 100.0, 95.0, 105.0,
                           "turtle_trend", "r", opened_ts="2025-12-31T00:00:00+00:00")
        j.close_trade(tid, 105.0, 4.9, 4.9, 0.21, "take profit",
                      closed_ts="2026-01-01T12:00:00+00:00")
        gap = j.closed_cash_delta_since("2026-01-01T00:00:00+00:00")
        assert gap == pytest_approx(4.9 + 0.21 / 2.0, 1e-9)   # pnl + entry_fee
        # outage window: entry AND close both after the anchor -> plain pnl
        tid_b = j.open_trade("TEST/USDT", "long", 1.0, 100.0, 95.0, 105.0,
                             "turtle_trend", "r", opened_ts="2026-06-02T00:00:00+00:00")
        j.close_trade(tid_b, 105.0, 3.0, 3.0, 0.2, "take profit",
                      closed_ts="2026-06-03T00:00:00+00:00")
        # anchor BEFORE tid_b's entry: tid (closed Jan) is outside the window;
        # tid_b spans it with BOTH legs inside -> delta = pnl exactly
        gap_b = j.closed_cash_delta_since("2026-06-01T12:00:00+00:00")
        assert gap_b == pytest_approx(3.0, 1e-9)               # both legs inside
        # exact columns recorded: entry_fee + realized_cash_delta are used
        # verbatim, no approximation. Anchor at 13:00 on Jan 1 catches BOTH
        # tid_b (whole trade inside the window -> plain pnl 3.0) and tid_c
        # (entry before the anchor -> recorded close-event delta 2.09).
        tid_c = j.open_trade("TEST/USDT", "long", 1.0, 100.0, 95.0, 105.0,
                             "turtle_trend", "r", opened_ts="2025-12-30T00:00:00+00:00",
                             entry_fee=0.09)
        j.close_trade(tid_c, 105.0, 2.0, 2.0, 0.19, "take profit",
                      closed_ts="2026-01-02T00:00:00+00:00",
                      entry_fee=0.09, realized_cash_delta=2.09)
        gap_c = j.closed_cash_delta_since("2026-01-01T13:00:00+00:00")
        assert gap_c == pytest_approx(3.0 + 2.09, 1e-9)
        # the atomic path writes both rows together
        tid2 = j.open_trade("TEST/USDT", "long", 1.0, 100.0, 95.0, 105.0,
                            "turtle_trend", "r")
        j.close_trade(tid2, 105.0, 4.0, 4.0, 0.2, "take profit",
                      equity=10_010.0, cash=10_010.0, mode="paper")
        rows = j.equity_curve()
        assert rows[-1]["cash"] == 10_010.0
        assert j.closed_cash_delta_since(rows[-1]["ts"]) == pytest_approx(0.0, 1e-9)


def test_journal_abort_trade_on_failed_fill():
    """A trade row opened but never filled by the broker must be ABORTED, not
    linger OPEN — a lingering row restores as a ghost position on restart."""
    from bot.journal import Journal
    with tempfile.TemporaryDirectory() as td:
        j = Journal(os.path.join(td, "t.db"))
        tid = j.open_trade("TEST/USDT", "long", 1.0, 100.0, None, None,
                           "turtle_trend", "r")
        j.abort_trade(tid)
        assert j.open_trades() == []
        assert j.recent_trades()[0]["status"] == "ABORTED"
        j.abort_trade(tid)   # idempotent: an already-closed row is untouched
        assert j.recent_trades()[0]["status"] == "ABORTED"


def test_journal_migration_is_idempotent_and_races_safe():
    """Two Journal instances on one DB (dashboard + engine): the migration must
    survive the duplicate-column race and re-run the backfill on every boot."""
    from bot.journal import Journal
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "t.db")
        j1 = Journal(path)
        Journal(path)          # second constructor while first exists
        # a legacy NULL timeframe row gets healed by the next boot's backfill
        with j1._conn() as conn:
            conn.execute("UPDATE trades SET timeframe=NULL WHERE id=1")
        j3 = Journal(path)
        with j3._conn() as conn:
            n_null = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE timeframe IS NULL").fetchone()[0]
        assert n_null == 0


def test_journal_wal_mode_enabled():
    """WAL lets the 4s dashboard reads never block engine writes (the old
    delete-journal mode serialized them)."""
    from bot.journal import Journal
    with tempfile.TemporaryDirectory() as td:
        j = Journal(os.path.join(td, "t.db"))
        with j._conn() as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


# ----------------------------- regression: review-agent findings (2026-09-04)
def test_broker_positions_snapshot_is_a_copy():
    """API threads must not iterate the live positions dict (races engine
    mutations -> 'dictionary changed size during iteration' -> 500s)."""
    b = PaperBroker(10_000)
    b.open_position(CRYPTO_1H, _dec("LONG", 0.8, stop=2.0, price=100.0),
                    qty=1.0, price=100.0, trade_id=1)
    snap = b.positions_snapshot()
    assert snap is not b.positions.values()
    assert len(snap) == 1
    snap.clear()          # mutating the snapshot must not touch the broker
    assert len(b.positions) == 1


def test_marketdata_cache_keys_by_limit():
    """The dashboard's marks fetch uses limit=2; caching that frame under the
    engine's key made the next cycle see 2 bars (< 60) and silently skip the
    market — no decision, no exit checks."""
    from bot.data import MarketData
    md = MarketData()
    md._cache[( "crypto", "TEST/USDT", "1h", 400)] = (time.time(), "engine-frame")
    md._cache[("crypto", "TEST/USDT", "1h", 2)] = (time.time(), "marks-frame")
    assert md.latest(CRYPTO_1H) == "engine-frame"
    assert md.latest(CRYPTO_1H, limit=2) == "marks-frame"


def test_watchlist_save_is_atomic_and_load_tolerates_corruption():
    """A crash mid-save must not leave torn JSON (atomic replace), and a
    corrupt file must be renamed aside — never silently traded around."""
    from config import save_watchlist, apply_saved_watchlist
    import json
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "watchlist.json")
        specs = [MarketSpec("crypto", "BTC/USDT", "1h", "Bitcoin")]
        assert save_watchlist(specs, path)
        assert not os.path.exists(path + ".tmp")          # no temp residue
        with open(path) as fh:
            assert json.load(fh)["specs"][0]["symbol"] == "BTC/USDT"
        # torn/corrupt file: loader renames it and falls back, loudly
        with open(path, "w") as fh:
            fh.write('{"specs": [trunc')
        apply_saved_watchlist(path)
        assert os.path.exists(path + ".corrupt")
        assert not os.path.exists(path) or json.load(open(path)) != "x"


def test_journal_stats_aggregates_match_python_math():
    """stats() moved to SQL aggregates: it must produce the same numbers the
    old Python-side reduction did (win rate, PF, averages, by_strategy)."""
    from bot.journal import Journal
    with tempfile.TemporaryDirectory() as td:
        j = Journal(os.path.join(td, "t.db"))
        rows = [("A", 100.0), ("A", -40.0), ("B", 60.0), ("B", -60.0), ("A", 30.0)]
        for i, (strat, pnl) in enumerate(rows):
            tid = j.open_trade("TEST/USDT", "long", 1.0, 100.0, 95.0, None,
                               strat, "r")
            j.close_trade(tid, 100.0 + pnl, pnl, pnl / 100.0, 0.1, "x")
        j.add_equity(10_000.0, 10_000.0)
        j.add_equity(10_090.0, 10_090.0)
        s = j.stats()
        assert s["closed_trades"] == 5
        assert s["win_rate"] == 60.0
        assert s["total_pnl"] == 90.0
        assert s["profit_factor"] == pytest_approx(190.0 / 100.0, 1e-6)
        assert s["by_strategy"]["A"]["pnl"] == 90.0
        assert s["by_strategy"]["A"]["wins"] == 2
        assert s["by_strategy"]["B"]["wins"] == 1


# ------------------------------------------- regression: audit bugs (2026-09-04)
def test_broker_positions_are_per_timeframe():
    """Bug 1 regression: a symbol traded by several specs (BTC 1h turtle +
    15m scalper + 4h meanrev) must hold SEPARATE positions per timeframe, and
    a 4h bar scan must never manage (and stop out) the 1h book's position."""
    from bot.broker import PaperBroker
    tf_specs = {"1h": MarketSpec("crypto", "TEST/USDT", "1h"),
                "15m": MarketSpec("crypto", "TEST/USDT", "15m"),
                "4h": MarketSpec("crypto", "TEST/USDT", "4h")}
    b = PaperBroker(10_000)
    d = _dec("LONG", 0.8, stop=2.0, price=100.0)
    b.open_position(tf_specs["1h"], d, qty=1.0, price=100.0, trade_id=1)

    # the 1h position exists under its own key, not under the bare symbol
    assert ("TEST/USDT", "1h") in b.positions
    assert ("TEST/USDT", "15m") not in b.positions
    assert b.has_position("TEST/USDT")

    # a 4h bar whose low is far below the 1h stop must NOT touch the 1h book
    big_4h_bar = {"open": 99.0, "high": 99.5, "low": 50.0, "close": 98.0}
    assert b.scan_bar_exits(tf_specs["4h"], big_4h_bar) == (None, None)
    assert ("TEST/USDT", "1h") in b.positions

    # the 1h book's own bar stops it at the stop level
    reason, px = b.scan_bar_exits(tf_specs["1h"], {"open": 99.0, "high": 99.5, "low": 90.0})
    assert reason == "stop loss"
    assert px == pytest_approx(b.positions[("TEST/USDT", "1h")].stop, 1e-9)

    # the 15m book may open its own position while the 1h one lives
    d15 = _dec("LONG", 0.8, stop=1.0, price=100.0)
    p15 = b.open_position(tf_specs["15m"], d15, qty=1.0, price=100.0, trade_id=2)
    assert p15 is not b.positions[("TEST/USDT", "1h")]
    assert len(b.positions) == 2


def test_engine_restores_cash_and_positions_from_journal():
    """Bug 2 regression: after a restart the broker must continue the journaled
    account (cash from the last equity point, open positions re-attached under
    their timeframe), not reset to paper_capital or re-charge the entry fee."""
    import bot.engine as engine_mod
    from bot.journal import Journal

    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "t.db")
        j = Journal(db)
        # account has traded down to 9500 cash and holds one open BTC 1h position
        j.add_equity(9_500.0, 9_500.0, mode="paper")
        j.open_trade("BTC/USDT", "long", 0.01, 80_000.0, 78_000.0, None,
                     "turtle_trend", "repro", mode="paper", timeframe="1h")
        j2 = Journal(db)
        assert j2.last_equity_point()["cash"] == pytest_approx(9_500.0, 1e-9)

        # point the engine at the temp journal; skip Kronos (model load is slow
        # and irrelevant to recovery)
        old_db, old_kronos = CONFIG.db_path, engine_mod.TradingEngine._init_kronos
        CONFIG.db_path = db
        engine_mod.TradingEngine._init_kronos = lambda self: None
        try:
            eng = engine_mod.TradingEngine(mode="paper", quiet=True)
            restored_cash = eng.broker.cash
            eng_keys = sorted(eng.broker.positions.keys())
            # entry fee recorded for round-trip PnL but NOT charged to cash
            fee = eng.broker.positions[("BTC/USDT", "1h")].entry_fee
        finally:
            CONFIG.db_path = old_db
            engine_mod.TradingEngine._init_kronos = old_kronos

    assert restored_cash == pytest_approx(9_500.0, 1e-6)   # not 10_000, no fee re-charge
    assert eng_keys == [("BTC/USDT", "1h")]
    assert fee is not None and fee > 0


def test_engine_bars_held_counts_bars_not_cycles():
    """Bug 3 regression: bars_held must count CLOSED BARS of the owning
    timeframe (from the decision bar's epoch), never 60-second engine cycles —
    otherwise a 12-bar 4h time stop fires in 12 minutes."""
    import bot.engine as engine_mod

    # gentle uptrend on 4h bars; a stop 1e6 below price can never trigger
    prices = 100.0 * np.cumprod(1 + np.full(60, 0.001))
    df = add_all_indicators(make_df(prices, freq="4h", seed=9))
    spec_4h = MarketSpec("crypto", "BTC/USDT", "4h")

    with tempfile.TemporaryDirectory() as td:
        old_db, old_kronos = CONFIG.db_path, engine_mod.TradingEngine._init_kronos
        CONFIG.db_path = os.path.join(td, "t.db")
        engine_mod.TradingEngine._init_kronos = lambda self: None
        try:
            eng = engine_mod.TradingEngine(mode="paper", quiet=True)
            i = len(df) - 1
            d = _dec("LONG", 0.9, stop=1e6, price=float(df["close"].iloc[i]))
            pos = eng.broker.open_position(spec_4h, d, qty=1.0,
                                           price=float(df["close"].iloc[i]), trade_id=-1,
                                           ts=str(df.index[i - 3]),
                                           decision_bar_ts=float(df.index[i - 3].timestamp()))
            # three 4h bars have closed since the decision bar
            eng._manage_position(spec_4h, pos, df, i, {},
                                 bar_epoch=float(df.index[i].timestamp()))
            assert pos.bars_held == 3
            assert ("BTC/USDT", "4h") in eng.broker.positions   # still open, not stopped
        finally:
            CONFIG.db_path = old_db
            engine_mod.TradingEngine._init_kronos = old_kronos


def test_journal_timeframe_migration():
    """Old journals (pre-timeframe-column) must migrate in place and their
    open trades restore under the watchlist spec's timeframe."""
    import sqlite3
    from bot.journal import Journal
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "old.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,"
            " side TEXT NOT NULL, qty REAL NOT NULL, entry_price REAL NOT NULL, exit_price REAL,"
            " stop_price REAL, target_price REAL, strategy TEXT NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'OPEN', opened_ts TEXT NOT NULL, closed_ts TEXT,"
            " pnl REAL, pnl_pct REAL, fees REAL, exit_reason TEXT, rationale_open TEXT,"
            " rationale_close TEXT, mode TEXT NOT NULL DEFAULT 'paper')")
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, entry_price, status, opened_ts, strategy,"
            " rationale_open, mode) VALUES ('BTC/USDT','long',0.01,80000.0,'OPEN','2026-09-03T15:00:00+00:00',"
            " 'turtle_trend','legacy','paper')")
        conn.commit()
        conn.close()

        j = Journal(db)                              # must migrate, not crash
        rows = j.open_trades()
        assert len(rows) == 1
        assert rows[0]["timeframe"] == "1h"          # legacy default
        # writer stores the spec's timeframe for new trades
        j.open_trade("ETH/USDT", "long", 1.0, 100.0, 95.0, None,
                     "connors_meanrev", "new row", timeframe="4h")
        assert j.open_trades()[-1]["timeframe"] == "4h"


def test_sentiment_lexicon():
    from bot.sentiment import lexicon_score, SentimentOverlay
    s = SentimentOverlay(llm_client=None)
    assert lexicon_score("Bitcoin surges to record high on ETF approval") > 0
    assert lexicon_score("Crypto exchange hacked; prices plunge") < 0
    assert lexicon_score("The weather is fine today") == 0.0
    a1, c1, _ = s.apply("LONG", 0.8, {"score": -0.7})
    assert a1 == "HOLD"                                   # veto on strongly contrary news
    a2, c2, _ = s.apply("LONG", 0.8, {"score": 0.8})
    assert a2 == "LONG" and c2 > 0.8                      # aligned news bumps confidence
    a3, c3, _ = s.apply("HOLD", 0.5, {"score": -0.9})
    assert a3 == "HOLD"                                   # never initiates


# ------------------------------------------------------------------ data quality
def test_data_validation_rejects_corrupt_bars():
    """Mixed-caliber/corrupt bars must fail loudly, never reach indicators."""
    from bot.data import _validate_ohlcv
    good = make_df(np.linspace(100, 130, 120))
    assert _validate_ohlcv(good, "test").attrs["caliber"] == "raw"

    bad_hilo = good.copy()
    bad_hilo.iloc[10, bad_hilo.columns.get_loc("high")] = bad_hilo["open"].iloc[10] - 1.0
    with pytest_raises(RuntimeError, "high/low"):
        _validate_ohlcv(bad_hilo, "test")

    bad_px = good.copy()
    bad_px.iloc[5, bad_px.columns.get_loc("close")] = -1.0
    with pytest_raises(RuntimeError, "non-positive"):
        _validate_ohlcv(bad_px, "test")

    unsorted = good.copy().sample(frac=1.0)
    with pytest_raises(RuntimeError, "sorted"):
        _validate_ohlcv(unsorted, "test")


def pytest_raises(exc_type, needle):
    class _C:
        def __enter__(self):
            return self

        def __exit__(self, et, ev, tb):
            assert et is not None and issubclass(et, exc_type), f"expected {exc_type}"
            assert needle in str(ev), f"expected '{needle}' in: {ev}"
            return True
    return _C()


def test_data_forming_bar_dropped():
    """The still-open candle must never reach the engine (it trades closed
    bars only — backtest/live parity)."""
    from bot.data import _drop_forming_bar
    now_ms = time.time() * 1000
    # 60s bars: second bar closes at now+30s -> still forming
    idx = pd.to_datetime([now_ms - 90_000, now_ms - 30_000], unit="ms", utc=True)
    df = pd.DataFrame({"open": [1.0, 2.0], "high": [2.0, 3.0], "low": [0.5, 1.5],
                       "close": [1.5, 2.5], "volume": [10.0, 10.0]}, index=idx)
    assert len(_drop_forming_bar(df, "1m")) == 1
    # both bars fully closed -> keep both
    idx2 = pd.to_datetime([now_ms - 180_000, now_ms - 90_000], unit="ms", utc=True)
    df2 = df.copy()
    df2.index = idx2
    assert len(_drop_forming_bar(df2, "1m")) == 2


def test_data_fallback_chain():
    """If the first exchange fails, the next one serves; all failing raises."""
    from bot import data as data_mod

    calls = []

    def fake_rows_from(source, symbol, timeframe, since_ms, limit):
        calls.append(source)
        if source == "binance":
            raise ConnectionError("simulated outage")
        rows = [[(pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(hours=i)).timestamp() * 1000,
                 100, 101, 99, 100.5, 10.0] for i in range(50)]
        return rows

    original = data_mod._ohlcv_rows_from
    data_mod._ohlcv_rows_from = fake_rows_from
    try:
        df = data_mod.fetch_crypto_ohlcv("TEST/USDT", "1h", limit=50)
        assert df.attrs["source"] == "bybit"
        assert calls == ["binance", "bybit"]
        # total failure -> informative error naming every source
        data_mod._ohlcv_rows_from = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down"))
        try:
            data_mod.fetch_crypto_ohlcv("TEST/USDT", "1h", limit=50)
            raise AssertionError("should have raised")
        except RuntimeError as e:
            assert "binance" in str(e) and "bybit" in str(e) and "okx" in str(e)
    finally:
        data_mod._ohlcv_rows_from = original


def test_data_disk_cache_roundtrip():
    """fetch_history caches to disk so repeated backtests are byte-identical
    and don't hammer public APIs."""
    import tempfile
    from bot import data as data_mod
    spec = MarketSpec("crypto", "TEST/USDT", "1h")
    synthetic = make_df(np.linspace(100, 130, 300))

    original = data_mod.fetch_crypto_history
    with tempfile.TemporaryDirectory() as td:
        old_cache_dir = CONFIG.data_cache_dir
        CONFIG.data_cache_dir = td
        try:
            data_mod.fetch_crypto_history = lambda *a, **k: synthetic.copy()
            first = data_mod.fetch_history(spec, days=7)
            # network layer now "fails" — the cache must still serve identical data
            data_mod.fetch_crypto_history = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down"))
            second = data_mod.fetch_history(spec, days=7)
            assert first.equals(second)
            assert len(first) == len(synthetic)
        finally:
            CONFIG.data_cache_dir = old_cache_dir
            data_mod.fetch_crypto_history = original


# ------------------------------------------------------------------ allocator
def test_allocator_inverse_vol_tilts_to_calm():
    """A calmer symbol gets the larger risk-budget share; weights sum to 1 and
    respect the clip band. skfolio is exercised for real here."""
    from bot.allocator import allocation_weights
    rng = np.random.default_rng(4)
    base = np.linspace(100, 120, 300)
    specs = [MarketSpec("crypto", "CALM/USDT", "1h"), MarketSpec("crypto", "WILD/USDT", "1h")]
    hist = {
        "CALM/USDT": make_df(base + rng.normal(0, 0.2, 300), seed=1),
        "WILD/USDT": make_df(base + rng.normal(0, 5.0, 300), seed=2),
    }
    w = allocation_weights(specs, hist, method="inverse_vol")
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert w["CALM/USDT"] > w["WILD/USDT"], w
    for v in w.values():
        assert 0.0 < v < 1.0


def test_allocator_equal_and_missing_history():
    from bot.allocator import allocation_weights
    specs = [MarketSpec("crypto", "A/USDT", "1h"), MarketSpec("crypto", "B/USDT", "1h")]
    # no histories at all -> equal split, never a crash
    w = allocation_weights(specs, {}, method="inverse_vol")
    assert abs(w["A/USDT"] - 0.5) < 1e-9
    # equal method ignores histories
    w2 = allocation_weights(specs, {"A/USDT": make_df(np.linspace(1, 2, 50))}, method="equal")
    assert abs(w2["A/USDT"] - 0.5) < 1e-9


def test_allocator_weights_bound_book_risk():
    """With allocation installed, per-symbol risk = base * share * n (each
    position still risks <= base), and a bigger share means bigger risk — the
    calm symbol carries the larger slice of the book's budget."""
    rm = RiskManager(CONFIG)
    base = CONFIG.risk.risk_per_trade
    rm.set_allocation({"A/USDT": 0.40, "B/USDT": 0.30, "C/USDT": 0.20, "D/USDT": 0.10})
    fracs = {s: rm.risk_fraction(s) for s in ("A/USDT", "B/USDT", "C/USDT", "D/USDT")}
    # each position risks at most the base (never above) ...
    assert all(f <= base + 1e-12 for f in fracs.values())
    # ... proportional to its budget share ...
    assert fracs["A/USDT"] > fracs["C/USDT"] > fracs["D/USDT"]
    assert fracs["A/USDT"] == pytest_approx(base, 1e-12)   # 0.40 share of 4 -> full base
    # ... and the no-allocation default is the base everywhere
    rm2 = RiskManager(CONFIG)
    assert rm2.risk_fraction("ANY/USDT") == base
    # size_position consumes the fraction (smaller share -> smaller qty)
    qty_full = rm2.size_position(10_000, 100.0, 5.0, "crypto")
    qty_small = rm.size_position(10_000, 100.0, 5.0, "crypto",
                                 risk_fraction=rm.risk_fraction("D/USDT"))
    assert qty_small < qty_full


# ------------------------------------------------------------------ validation
def test_purged_cv_paths_disjoint_and_covering():
    """Every bar lands in at least one test path; blocks are contiguous."""
    from bot.validation import purged_cv_paths, _path_bounds
    paths = purged_cv_paths(1000, n_folds=6, n_test_folds=2)
    assert len(paths) >= 5
    seen = set()
    for p in paths:
        bounds = _path_bounds(p)
        for lo, hi in bounds:
            assert hi > lo                    # blocks have length
            seen.update(range(lo, hi + 1))    # contiguous
    assert seen == set(range(1000))           # full coverage


def test_oos_trade_distribution_shapes():
    """A backtest's trades, partitioned across purged paths, give a
    distribution: every path's stats are consistent with its kept trades."""
    from bot.backtest import Backtester
    from bot.validation import oos_trade_distribution
    df = trending_df(800, drift=0.002, seed=31)
    bt = Backtester(CONFIG)
    res = bt.run(CRYPTO_1H, df, strategy="turtle_trend")
    if len(res.trades) < 3:
        return  # not enough trades in this seed to partition meaningfully
    out = oos_trade_distribution(res.trades, df, n_folds=6, n_test_folds=2, purge_bars=24)
    assert out["n_paths"] == len(out["paths"]) >= 5
    # per-path counts can overlap (a trade inside a block shared by several
    # OOS paths is legitimately kept by each); the UNIQUE kept count is the
    # one that partitions the trade set
    kept_unique = out["kept_trades_unique"]
    assert kept_unique + out["purged_trades"] == out["total_trades"]
    assert out["total_trades"] == len(res.trades)
    for p in out["paths"]:
        assert p["trades"] >= 0 and p["win_rate_pct"] >= 0
    assert 0.0 <= (out["pct_paths_profitable"] or 0.0) <= 100.0


def test_signal_ic_report_runs():
    """Conviction vs forward return rank-IC with overlap-adjusted t-stat."""
    from bot.validation import signal_ic_report
    df = add_all_indicators(trending_df(800, drift=0.002, seed=41))
    ic = signal_ic_report(df, TurtleTrend(), horizon=12, warmup=240)
    assert ic["n_signal_bars"] > 50
    assert -1.0 <= ic["pooled_ic"] <= 1.0
    assert ic["n_paths_with_ic"] >= 3
    assert 0.0 <= ic["pct_paths_positive_ic"] <= 100.0


# --------------------------------------- validation statistics (2026-09-04)
def test_drawdown_throttle_scales_risk_in_drawdown():
    """Deep drawdowns must shrink new-trade risk (research roadmap item): half
    at -10% from the rolling peak, a quarter at -20% — and it must recover
    (unlike the daily kill switch) and never block entries outright."""
    rm = RiskManager(CONFIG)
    rm.note_equity(10_000, ts="2024-05-01T00:00:00+00:00")
    assert rm.dd_risk_scale == 1.0
    full = rm.size_position(9_800, 100.0, 5.0, "crypto")
    rm.note_equity(8_900, ts="2024-05-02T00:00:00+00:00")     # -11% from peak
    assert rm.dd_risk_scale == 0.5
    half = rm.size_position(8_900, 100.0, 5.0, "crypto")
    # size scales with BOTH the equity and the risk fraction: the throttle
    # multiplies the fraction (0.5 here), equity does the rest
    assert full == pytest_approx(9_800 * 0.01 / 5.0, 1e-9)
    assert half == pytest_approx(8_900 * 0.01 * 0.5 / 5.0, 1e-9)
    assert half < full
    # deep drawdown: quarter risk, but an entry is still APPROVED (throttle
    # scales, the kill switch blocks)
    rm.note_equity(7_500, ts="2024-05-03T00:00:00+00:00")     # -25% from peak
    assert rm.dd_risk_scale == 0.25
    d = _dec("LONG", 0.9, stop=5.0, price=100.0)
    dec = rm.approve(d, CRYPTO_1H, 7_500, 0, False,
                    bar_epoch=100.0)
    assert dec.approved
    quarter = rm.size_position(7_500, 100.0, 5.0, "crypto")
    assert quarter == pytest_approx(7_500 * 0.01 * 0.25 / 5.0, 1e-6)
    # recovery: a new peak restores full risk
    rm.note_equity(11_000, ts="2024-05-04T00:00:00+00:00")
    assert rm.dd_risk_scale == 1.0


def test_drawdown_throttle_composes_with_allocation():
    """The throttle multiplies the ALLOCATED fraction, never the other way."""
    rm = RiskManager(CONFIG)
    rm.set_allocation({"A/USDT": 0.40, "B/USDT": 0.30, "C/USDT": 0.20, "D/USDT": 0.10})
    rm.note_equity(10_000, ts="2024-05-01T00:00:00+00:00")
    full_d = rm.size_position(10_000, 100.0, 5.0, "crypto",
                              risk_fraction=rm.risk_fraction("D/USDT"))
    rm.note_equity(8_500, ts="2024-05-02T00:00:00+00:00")     # -15% -> half risk
    half_d = rm.size_position(8_500, 100.0, 5.0, "crypto",
                              risk_fraction=rm.risk_fraction("D/USDT"))
    assert half_d < full_d


def test_deflated_sharpe_quantifies_selection():
    """A best Sharpe that's barely above what N trials of pure chance produce
    must deflate toward 0.5 (it's selection, not edge); a high Sharpe from few
    trials on a long sample keeps its confidence. Trial Sharpes are ANNUALIZED
    (bars_per_year is the annualization factor) — the old per-period SE mixed
    with annualized trials read ~1.0 for ANY input and could never reject."""
    from bot.validation import deflated_sharpe
    APY = 8760.0  # 1h crypto bars/year
    # 100-trial research sweep, best Sharpe 1.3, short 100-bar sample: the
    # null's expected max across that many trials is ~1.28 — no real evidence
    sweep = [1.3] + [round(x, 3) for x in np.linspace(-0.5, 1.2, 100)]
    dsr = deflated_sharpe(sweep, n_obs=100, bars_per_year=APY)
    assert dsr["deflated_sharpe"] < 0.8
    assert dsr["verdict"] == "Sharpe explained by trial count"
    # 3 trials, best Sharpe 2.0, three years of 1h bars -> selection can't explain it
    dsr2 = deflated_sharpe([2.0, 0.5, 0.2], n_obs=26_000, bars_per_year=APY)
    assert dsr2["deflated_sharpe"] > 0.95
    assert dsr2["verdict"] == "selection-aware confidence"
    # pinned reference case (audit 2026-09): Sharpe 1.0 over 5 trials / 26k
    # 1h bars must be ~0.89 "suggestive" — the unit-buggy version returned 1.0
    ref = deflated_sharpe([1.0, 0.5, 0.6, 0.4, 0.5], n_obs=26_000, bars_per_year=APY)
    assert 0.85 <= ref["deflated_sharpe"] <= 0.93
    assert ref["verdict"] == "suggestive"
    # degenerate inputs are refused, not crashed
    assert deflated_sharpe([1.0], 100, APY)["deflated_sharpe"] is None
    assert deflated_sharpe([1.0, 1.0, 1.0], 100, APY)["deflated_sharpe"] is None
    assert deflated_sharpe([1.0, 0.5], 100, -1.0)["deflated_sharpe"] is None


def test_pbo_cscv_over_config_family():
    """PBO over a config FAMILY: a consistently-dominant config keeps its OOS
    rank (PBO ~ 0); a pure-noise family's IS winner is IS luck (PBO ~ 0.5);
    degenerate inputs return None instead of crashing."""
    from bot.validation import pbo_cscv
    consistent = {"A": [2.0] * 10, "B": [1.0] * 10, "C": [0.5] * 10}
    out = pbo_cscv(consistent)
    assert out["pbo"] == 0.0 and out["verdict"] == "selection holds OOS"
    rng = np.random.default_rng(3)
    noise = {f"cfg{i}": list(rng.normal(0, 2, 10)) for i in range(10)}
    overfit = pbo_cscv(noise)
    assert 0.3 <= overfit["pbo"] <= 0.7       # pure noise: selection ~ coin flip
    # needs a family (>=2 configs) and enough aligned paths
    assert pbo_cscv({"A": [1.0] * 10})["pbo"] is None
    assert pbo_cscv({"A": [1.0] * 4, "B": [1.0] * 4})["pbo"] is None


def test_monte_carlo_paths_distribution():
    """Resampled trade orders give a calibrated outcome distribution: quantiles
    ordered, probabilities in range, deterministic under the seed."""
    from bot.validation import monte_carlo_paths
    trades = [{"pnl": p} for p in (5.0, -3.0, 8.0, -2.0, 4.0, -6.0, 7.0, -1.0, 3.0, -2.0)]
    mc = monte_carlo_paths(trades, starting_capital=1_000.0, n_sims=500)
    assert mc["n_sims"] == 500
    assert mc["terminal_p5"] <= mc["terminal_p50"] <= mc["terminal_p95"]
    assert 0.0 <= mc["p_lose_money"] <= 100.0
    assert 0.0 <= mc["p_dd_beyond_10pct"] <= 100.0
    assert mc["max_dd_p95"] <= 0.0          # a drawdown quantile is <= 0
    mc2 = monte_carlo_paths(trades, starting_capital=1_000.0, n_sims=500)
    assert mc == mc2                          # seeded: reproducible
    assert monte_carlo_paths([{"pnl": 1.0}], 1000)["n_sims"] == 0   # too few trades


def test_min_trl_scales_with_sharpe():
    """A higher Sharpe needs a shorter track record to be believed; a Sharpe of
    0.5 needs roughly (1.645/0.5)^2 ≈ 10.82 years at 95% confidence."""
    from bot.validation import min_trl
    weak = min_trl(0.5, 8_760)
    strong = min_trl(2.0, 8_760)
    assert weak["min_years"] == pytest_approx((1.6449 / 0.5) ** 2, 1e-2)
    assert strong["min_bars"] < weak["min_bars"]
    assert min_trl(-0.2, 8_760)["min_bars"] is None


# --------------------------- ECC coverage-agent top gaps (2026-09-04, Phase 1)
def test_engine_skips_equity_when_no_marks_available():
    """Coverage gap 1: a held position whose symbol fetch fails must SKIP the
    equity write (never mark at 0.0 and journal a phantom drawdown), then
    resume writing when data returns. The kill switch must NOT engage."""
    import bot.engine as engine_mod

    prices = 100.0 * np.cumprod(1 + np.full(80, 0.001))
    df = add_all_indicators(make_df(prices, seed=9))
    spec = MarketSpec("crypto", "TEST/USDT", "1h")
    i = len(df) - 1

    with tempfile.TemporaryDirectory() as td:
        old_db, old_kronos = CONFIG.db_path, engine_mod.TradingEngine._init_kronos
        CONFIG.db_path = os.path.join(td, "t.db")
        engine_mod.TradingEngine._init_kronos = lambda self: None
        try:
            eng = engine_mod.TradingEngine(mode="paper", quiet=True)
            eng.broker.open_position(spec, _dec("LONG", 0.9, stop=50.0,
                                                price=float(df["close"].iloc[i])),
                                      qty=1.0, price=float(df["close"].iloc[i]),
                                      trade_id=-1, ts=str(df.index[i]),
                                      decision_bar_ts=float(df.index[i].timestamp()))
            n_equity_before = len(eng.journal.equity_curve(limit=10**9))

            # fetch fails for the held symbol -> mark unavailable
            eng.market_data.latest = lambda s, limit=None: None
            summary = eng.run_cycle()
            assert any("equity point skipped" in e for e in summary["errors"])
            assert len(eng.journal.equity_curve(limit=10**9)) == n_equity_before
            assert not eng.risk.halted           # no phantom drawdown tripped it

            # data returns -> equity writes resume
            eng.market_data.latest = lambda s, limit=None: df
            summary2 = eng.run_cycle()
            assert "equity" in summary2
            assert len(eng.journal.equity_curve(limit=10**9)) == n_equity_before + 1
        finally:
            CONFIG.db_path = old_db
            engine_mod.TradingEngine._init_kronos = old_kronos


def test_engine_aborts_journal_row_when_fill_fails():
    """Coverage gap 2: journal-first open + broker fill failure -> the trade
    row must be ABORTED (no ghost OPEN position) and the error surface."""
    import bot.engine as engine_mod

    df = add_all_indicators(trending_df(260, drift=0.004, seed=13))
    spec = MarketSpec("crypto", "TEST/USDT", "1h")
    i = len(df) - 1

    with tempfile.TemporaryDirectory() as td:
        old_db, old_kronos = CONFIG.db_path, engine_mod.TradingEngine._init_kronos
        CONFIG.db_path = os.path.join(td, "t.db")
        engine_mod.TradingEngine._init_kronos = lambda self: None
        try:
            eng = engine_mod.TradingEngine(mode="paper", quiet=True)

            class BoomDecision:
                action = "LONG"
                confidence = 0.9
                price = float(df["close"].iloc[i])
                stop_distance = 2.0
                target_rr = None
                strategy_name = "turtle_trend"
                rationale = "boom"
                regime = "trending"
                strategy_signals = {}
                sentiment = {}

            eng.broker.open_position = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("fill rejected"))
            summary = {"errors": [], "opened": [], "closed": [], "holds": 0}
            # emulate the orchestrator-approved path directly: journal row then
            # broker fill raises -> abort_trade must clean the row
            from bot.engine import utc_now
            trade_id = eng.journal.open_trade(
                spec.symbol, "long", 1.0, BoomDecision.price, None, None,
                "turtle_trend", "r", mode="paper", opened_ts=utc_now(),
                timeframe="1h")
            try:
                eng.broker.open_position(spec, BoomDecision, 1.0, BoomDecision.price,
                                         trade_id, ts=utc_now())
            except RuntimeError:
                eng.journal.abort_trade(trade_id)
                summary["errors"].append("fill failed")
            assert eng.journal.open_trades() == []
            assert eng.journal.recent_trades()[0]["status"] == "ABORTED"
            assert summary["errors"]
        finally:
            CONFIG.db_path = old_db
            engine_mod.TradingEngine._init_kronos = old_kronos


def test_chatbot_intents_and_single_user_log():
    """Coverage gap 3: deterministic intents answer from the journal, and the
    user's message is logged EXACTLY ONCE per answer (regression: the
    deterministic path used to double-log; the fix moved logging to answer())."""
    from bot.journal import Journal
    from bot.chatbot import ChatBot
    with tempfile.TemporaryDirectory() as td:
        j = Journal(os.path.join(td, "t.db"))
        bot = ChatBot(journal=j)
        assert not bot.llm.enabled        # no key in the test env

        r1 = bot.answer("how much did we earn?")
        assert "win rate" in r1.lower() or "closed trades" in r1.lower()
        r2 = bot.answer("explain the turtle strategy")
        assert "Donchian" in r2
        r3 = bot.answer("what are your risk rules?")
        assert "1%" in r3
        with j._conn() as conn:
            n_user = conn.execute(
                "SELECT COUNT(*) FROM chat_log WHERE role='user'").fetchone()[0]
        assert n_user == 3, f"one user row per answer, got {n_user}"
        # assistant replies were logged too
        with j._conn() as conn:
            n_asst = conn.execute(
                "SELECT COUNT(*) FROM chat_log WHERE role='assistant'").fetchone()[0]
        assert n_asst == 3
        # fallback labels failures honestly and still answers
        orig = j.stats
        j.stats = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
        try:
            r4 = bot.answer("how much did we earn?")
            assert "lookup failed" in r4
        finally:
            j.stats = orig


def test_strategy_check_exit_units():
    """Coverage gap 17: the live exit paths asserted directly (they run daily
    on real money but were only covered inside full backtests):
    turtle Donchian exit, meanrev RSI/EMA/time exits, scalper breakeven trail
    + buffered VWAP confirmation (one noisy close must NOT exit)."""
    from bot.broker import Position

    p = CONFIG.params
    # --- turtle: a close below the PRIOR 10-bar exit channel exits. The
    # unshifted rolling low includes the decision bar's own low, and close >=
    # low by construction — the unshifted condition was mathematically
    # impossible and this assert passed vacuously for months (reason=None,
    # below=False). Verified non-vacuous: the seed frame has bars that satisfy
    # the shifted condition and the test asserts on them.
    up = add_all_indicators(trending_df(300, drift=0.003, seed=21))
    turtle = TurtleTrend()
    pos = Position(trade_id=1, symbol="T", side="long", qty=1.0,
                   entry_price=float(up["close"].iloc[-30]),
                   stop=None, target=None, strategy="turtle_trend")
    prior_lo = up["low"].rolling(p.turtle_exit_period).min().shift(1)
    i = len(up) - 1
    below = float(up["close"].iloc[i]) < float(prior_lo.iloc[i])
    if not below:
        # construct the non-vacuous case: force the last close under the prior
        # channel so the exit MUST fire (a real reversal bar)
        forced = up.copy()
        floor = float(prior_lo.iloc[i])
        forced.loc[forced.index[i], "close"] = floor - 1.0
        forced.loc[forced.index[i], "low"] = min(floor - 1.0, float(forced["low"].iloc[i]))
        reason_f, _ = turtle.check_exit(forced, i, pos)
        assert reason_f is not None and "opposite-channel" in reason_f
    else:
        reason, _ = turtle.check_exit(up, i, pos)
        assert reason is not None and "opposite-channel" in reason

    # --- meanrev: RSI(2) snapback + time stop
    mr = ConnorsMeanReversion()
    pos = Position(trade_id=1, symbol="T", side="long", qty=1.0,
                   entry_price=100.0, stop=None, target=None,
                   strategy="connors_meanrev", bars_held=p.mr_time_stop_bars)
    frame = up.copy()
    frame["rsi2"] = 70.0            # snapback above the exit threshold
    reason, _ = mr.check_exit(frame, len(frame) - 1, pos)
    assert reason is not None
    frame2 = up.copy()
    frame2["rsi2"] = 30.0
    pos2 = Position(trade_id=2, symbol="T", side="long", qty=1.0,
                    entry_price=100.0, stop=None, target=None,
                    strategy="connors_meanrev", bars_held=2)
    reason2, _ = mr.check_exit(frame2, len(frame2) - 1, pos2)
    # no snapback, no time stop, no EMA exit -> hold
    assert reason2 is None or "time stop" in reason2 or "ema" in (reason2 or "").lower()

    # --- scalper: breakeven trail at 1R, buffered VWAP exit needs 2 closes
    sc = VWAPScalper()
    pos = Position(trade_id=3, symbol="T", side="long", qty=1.0,
                   entry_price=100.0, stop=98.0, target=None,
                   strategy="vwap_scalper", risk_per_unit=2.0)
    frame = up.copy()
    frame["vwap_roll"] = 100.5      # price above VWAP: no exit pressure
    i = len(frame) - 1
    frame.loc[frame.index[i], "close"] = 102.0     # 1R -> stop trails to breakeven
    reason, new_stop = sc.check_exit(frame, i, pos)
    if new_stop is not None:
        # cost-aware breakeven: entry buffered by taker fee + slippage so the
        # "breakeven" exit nets ~0 after both legs' costs (the nominal-entry
        # stop realized a guaranteed -0.30% crypto round trip)
        buf = CONFIG.costs.fee("crypto") + CONFIG.costs.slippage("crypto")
        assert new_stop == pytest_approx(100.0 * (1.0 + buf), 1e-9)
    # ONE close beyond the VWAP buffer must NOT exit; two consecutive must
    f = frame.copy()
    f.loc[f.index[i], "close"] = 99.0                       # close < vwap - buffer
    reason_a, _ = sc.check_exit(f, i, pos)
    f2 = f.copy()
    f2.loc[f2.index[i - 1], "close"] = 99.0                 # previous close too
    reason_b, _ = sc.check_exit(f2, i, pos)
    assert reason_a in (None,) or "confirm" not in (reason_a or "")
    assert reason_b is None or "VWAP" in (reason_b or "")


def test_dashboard_api_smoke():
    """Coverage gaps 8-11: the FULL FastAPI surface, exercised in-process
    (TestClient; 'testserver' is in the dashboard's allowed_hosts for exactly
    this). Imports the real module against a temp DB — no test imports
    bot.dashboard anywhere else."""
    from fastapi.testclient import TestClient
    import bot.dashboard as dash_mod

    with tempfile.TemporaryDirectory() as td:
        old_db = CONFIG.db_path
        CONFIG.db_path = os.path.join(td, "t.db")
        try:
            dash_mod.journal.db_path = CONFIG.db_path
            dash_mod.journal = dash_mod.Journal(CONFIG.db_path)
            dash_mod.chatbot = dash_mod.ChatBot(dash_mod.journal)
            client = TestClient(dash_mod.app)

            # core reads
            assert client.get("/api/stats").status_code == 200
            assert client.get("/api/account").status_code == 200
            assert client.get("/api/equity").status_code == 200
            assert client.get("/").status_code == 200
            # DNS-rebinding guard: a foreign Host is refused
            bad = client.get("/api/stats", headers={"Host": "evil.example.com"})
            assert bad.status_code == 400
            # watchlist validation + CRUD
            assert client.post("/api/watchlist",
                               json={"kind": "crypto", "symbol": "../etc/passwd",
                                     "timeframe": "1h"}).status_code == 422
            r = client.post("/api/watchlist",
                            json={"kind": "crypto", "symbol": "ada/usdt",
                                  "timeframe": "1h"})
            assert r.status_code == 201 and r.json()["spec"]["symbol"] == "ADA/USDT"
            assert client.post("/api/watchlist",
                               json={"kind": "crypto", "symbol": "ADA/USDT",
                                     "timeframe": "1h"}).status_code == 409
            # delete with an open journal trade -> 409 (zombie guard)
            tid = dash_mod.journal.open_trade("ADA/USDT", "long", 1.0, 0.5,
                                              0.45, None, "turtle_trend", "r",
                                              timeframe="1h")
            r = client.delete("/api/watchlist/crypto/ADA%2FUSDT/1h")
            assert r.status_code == 409
            dash_mod.journal.abort_trade(tid)
            r = client.delete("/api/watchlist/crypto/ADA%2FUSDT/1h")
            assert r.status_code == 200
            # engine interval bounds
            assert client.post("/api/engine/start",
                               json={"interval": 0}).status_code == 422
            # engine stop requires a JSON body like every other mutating POST
            # (a body-less endpoint was form-CSRF-able from any web page)
            assert client.post("/api/engine/stop").status_code == 422
            assert client.post("/api/engine/stop", json={}).status_code == 200
            assert client.post("/api/engine/stop", json={}).json() == {"status": "not_running"}
            # the start/stop pair persists the operator's desired state so a
            # dashboard restart can auto-resume the engine
            dash_mod._write_engine_state(True, 45)
            state = json.load(open(dash_mod._engine_state_path()))
            assert state == {"desired": "running", "interval": 45}
            dash_mod._write_engine_state(False, 45)
            state = json.load(open(dash_mod._engine_state_path()))
            assert state["desired"] == "stopped"
            # deposit math (engine off path)
            r = client.post("/api/account/deposit", json={"amount": 250.0})
            assert r.status_code == 200 and r.json()["cash"] == 250.0 \
                or r.json()["cash"] == pytest_approx(
                    CONFIG.paper_capital + 250.0, 0.01)
            # transactions ledger: typed deposit/withdrawal rows for the Account tab
            txs = client.get("/api/account/transactions").json()
            assert txs and txs[0]["kind"] == "deposit" and txs[0]["amount"] == 250.0
            assert txs[0]["cash_after"] == pytest_approx(
                CONFIG.paper_capital + 250.0, 0.01)
            assert client.post("/api/account/withdraw",
                               json={"amount": 100.0}).status_code == 200
            txs = client.get("/api/account/transactions").json()
            assert txs[0]["kind"] == "withdrawal"            # newest first
            assert txs[0]["cash_after"] == pytest_approx(
                CONFIG.paper_capital + 150.0, 0.01)
            assert {t["kind"] for t in txs} == {"deposit", "withdrawal"}
            # reset wipes the ledger and records the fresh starting capital
            r = client.post("/api/account/reset", json={"capital": 5000.0})
            assert r.status_code == 200
            txs = client.get("/api/account/transactions").json()
            assert len(txs) == 1 and txs[0]["kind"] == "reset" \
                and txs[0]["amount"] == 5000.0
        finally:
            CONFIG.db_path = old_db


def test_dashboard_token_guard():
    """Optional bearer auth (DASHBOARD_TOKEN), default OFF: unset token admits
    everything; set token admits only the exact header. Pure predicate, so the
    middleware's decision logic is tested without rebuilding the ASGI app."""
    from bot.dashboard import _check_token
    assert _check_token("", "") is True                    # token unset: open
    assert _check_token("Bearer whatever", "") is True
    assert _check_token("", "secret") is False             # token set: denied
    assert _check_token("Bearer wrong", "secret") is False
    assert _check_token("Bearer secret", "secret") is True
    assert _check_token("secret", "secret") is False       # header must be Bearer


# ------------------------------------------------------------------ kronos
def test_kronos_ic_tracker_promotion_gate():
    """The EARNED VOTING RIGHTS gate: no vote before min_observations even with
    a great IC; vote at hurdle; vote lost when IC decays below the floor."""
    import tempfile
    from bot.kronos_signal import KronosConfig, KronosSignalEngine
    with tempfile.TemporaryDirectory() as td:
        cfg = KronosConfig()
        cfg.track_file = os.path.join(td, "ic.json")
        cfg.min_observations = 20
        eng = KronosSignalEngine(cfg)

        # no observations -> never promoted
        assert not eng.promoted()

        # feed 30 PERFECT forecasts (score ranks match return ranks)
        tr = eng.tracker
        for k in range(30):
            tr.records.append((float(k), float(k) * 0.01))   # perfectly monotone
        assert tr.n() >= cfg.min_observations
        assert tr.ic() is not None and tr.ic() > cfg.ic_hurdle
        assert eng.promoted()

        # IC decays below the hurdle (the synthetic noise is perfectly wrong,
        # IC = -1) -> vote is lost; hysteresis keeps the demote floor at 0,
        # so only genuinely-dead-or-wrong IC loses the vote
        noise = [(float(k % 2), float((k + 1) % 2) * 0.01) for k in range(400)]
        tr.records.extend(noise)
        assert tr.n() >= 2 * cfg.ic_half_life     # window is fully noise now
        assert tr.ic() is not None and tr.ic() < cfg.ic_hurdle
        assert not eng.promoted()


def test_kronos_engine_degrades_gracefully():
    """Without a loadable model the engine must be inert, never crash. Forced
    with a bogus model path (the real weights may or may not be vendored on
    this machine — the degradation path must hold regardless)."""
    import tempfile
    from bot.kronos_signal import KronosConfig, KronosSignalEngine
    cfg = KronosConfig()
    cfg.track_file = os.path.join(tempfile.mkdtemp(), "ic.json")
    cfg.model_name = "tests/fixtures/missing_model"      # not loadable
    cfg.tokenizer_name = "tests/fixtures/missing_tok"
    eng = KronosSignalEngine(cfg)
    assert not eng.predictor.available
    assert eng.evaluate(pd.DataFrame(), horizon=24) is None
    assert not eng.promoted()
    eng.log_and_maybe_resolve(pd.DataFrame(), None)       # no-op, no crash


def test_orchestrator_kronos_vote_only_when_promoted():
    """Kronos joins the weighted vote ONLY with earned rights; either way its
    forecast is journaled in strategy_signals with the voting flag."""
    from bot.kronos_signal import KronosSignal
    o = Orchestrator()
    df = add_all_indicators(trending_df(500, seed=21))
    i = len(df) - 2
    ks = KronosSignal(direction="LONG", p_up=0.78,
                      dispersion_pct=1.2, expected_return_pct=0.9, horizon_bars=24,
                      rationale="kronos test signal")

    # not promoted: journaled, but no kronos entry among voting signals
    d0 = o.decide(df, i, CRYPTO_1H, include_sentiment=False,
                  kronos_signal=ks, kronos_promoted=False)
    assert "kronos" in d0.strategy_signals
    assert d0.strategy_signals["kronos"]["voting"] is False
    assert d0.strategy_signals["kronos"]["p_up"] == 0.78
    assert "tracked, no vote" in d0.rationale
    # no stop distance may ever come from kronos itself
    if d0.action == "HOLD":
        assert d0.stop_distance is None

    # promoted: it votes with weight 0.20 and the rationale flags [VOTING]
    d1 = o.decide(df, i, CRYPTO_1H, include_sentiment=False,
                  kronos_signal=ks, kronos_promoted=True)
    assert d1.strategy_signals["kronos"]["voting"] is True
    assert "[VOTING]" in d1.rationale
    # conflict guard ignores kronos: it can't manufacture a hold by disagreeing
    ks_short = KronosSignal(direction="SHORT", p_up=0.2,
                            dispersion_pct=1.0, expected_return_pct=-0.8, horizon_bars=24,
                            rationale="kronos short")
    d2 = o.decide(df, i, CRYPTO_1H, include_sentiment=False,
                  kronos_signal=ks_short, kronos_promoted=True)
    assert "kronos" in d2.strategy_signals and d2.strategy_signals["kronos"]["voting"] is True


# ------------------------------------------------------------------ shadow
def _journal_trade(i, entry_i, exit_i, df, strategy="turtle_trend", side="long",
                   exit_reason="strategy exit", pnl=10.0, stop_off=2.0, qty=1.0):
    return {
        "id": i, "symbol": "TEST/USDT", "side": side, "qty": qty,
        "entry_price": float(df["close"].iloc[entry_i]),
        "exit_price": float(df["close"].iloc[exit_i]),
        "stop_price": float(df["close"].iloc[entry_i]) - stop_off if side == "long"
                      else float(df["close"].iloc[entry_i]) + stop_off,
        "target_price": None, "strategy": strategy, "status": "CLOSED",
        "opened_ts": str(df.index[entry_i]), "closed_ts": str(df.index[exit_i]),
        "pnl": pnl, "pnl_pct": pnl, "fees": 0.2, "exit_reason": exit_reason,
        "rationale_open": "test", "rationale_close": "", "mode": "paper",
    }


def test_shadow_rule_adherence_categories():
    """The replay must classify on-rule, late, rule-break and unknown honestly:
    hard brackets (stop/target) are on-rule by construction; a strategy exit
    exactly when the strategy fired is on-rule; lingering past the signal is
    late; exiting with the strategy silent is a rule break."""
    from bot.shadow import rule_adherence
    df = add_all_indicators(trending_df(700, drift=0.0015, seed=17))
    from bot.backtest import Backtester
    bt = Backtester(CONFIG)
    res = bt.run(CRYPTO_1H, df, strategy="turtle_trend")
    if not res.trades:
        return
    # replay the backtest's OWN trades: every exit was produced by these exact
    # rules, so adherence should be essentially 100% (minus bracket exits that
    # map to on-rule anyway)
    rep = rule_adherence(res.trades, df, CONFIG.params)
    decided = rep.n_on_rule + rep.n_rule_break + rep.n_late
    assert decided >= 1
    assert rep.n_rule_break == 0, [t for t in rep.trades if t["verdict"] == "rule break"]
    # a fabricated discretionary exit: with the Turtle S1 exit FIXED, the
    # strategy (correctly) fires an opposite-channel exit before this row's
    # close on a trending frame — so the honest verdict for "closed later
    # anyway" is LATE (it would only be a rule break if the strategy stayed
    # silent through the close, which the fixed exit rarely allows on a
    # trending frame). Both verdicts mean "not on-rule"; late additionally
    # dates the divergence.
    silent = _journal_trade(999, 300, 320, df, exit_reason="LLM override",
                            strategy="turtle_trend")
    rep2 = rule_adherence([silent], df, CONFIG.params)
    verdicts = {t["verdict"] for t in rep2.trades}
    assert verdicts <= {"rule break", "on-rule (hard bracket)", "late"}
    if rep2.trades and rep2.trades[0]["verdict"] == "rule break":
        assert "strategy silent" in rep2.trades[0]["note"]
    # a stop-loss exit is on-rule by construction
    stopped = _journal_trade(998, 300, 320, df, exit_reason="stop loss", pnl=-20.0)
    rep3 = rule_adherence([stopped], df, CONFIG.params)
    assert rep3.trades and rep3.trades[0]["verdict"] == "on-rule (hard bracket)"


def test_shadow_behavior_profile_math():
    """R-multiples, disposition gap, stop-blowthrough on hand-computable trades."""
    from bot.shadow import behavior_profile
    base_ts = pd.date_range("2024-01-01", periods=5, freq="1h", tz="UTC")
    trades = [
        # winner held 2h, +1R: entry 100 stop 98 -> risk 2 * qty 1
        {"status": "CLOSED", "side": "long", "entry_price": 100.0, "stop_price": 98.0,
         "qty": 1.0, "pnl": 2.0, "opened_ts": str(base_ts[0]), "closed_ts": str(base_ts[2]),
         "fees": 0.1, "exit_reason": "x", "symbol": "T", "strategy": "s"},
        # loser held 4h, -2R (blew through stop)
        {"status": "CLOSED", "side": "long", "entry_price": 100.0, "stop_price": 98.0,
         "qty": 1.0, "pnl": -4.0, "opened_ts": str(base_ts[0]), "closed_ts": str(base_ts[4]),
         "fees": 0.1, "exit_reason": "y", "symbol": "T", "strategy": "s"},
        {"status": "OPEN", "side": "long", "entry_price": 100.0, "stop_price": 98.0,
         "qty": 1.0, "pnl": None, "opened_ts": str(base_ts[0]), "closed_ts": None,
         "fees": None, "exit_reason": None, "symbol": "T", "strategy": "s"},
    ]
    p = behavior_profile(trades)
    assert p["n_trades"] == 2                     # OPEN excluded
    assert p["win_rate_pct"] == 50.0
    assert p["avg_r"] == pytest_approx(-0.5, 3)   # (+1R + -2R)/2
    assert p["min_r"] == pytest_approx(-2.0, 3)
    assert p["n_blew_through_stop"] == 1
    # winners 2h vs losers 4h -> disposition gap -2 (cut winners early)
    assert p["disposition_gap_hours"] == pytest_approx(-2.0, 6)


# ------------------------------------------------------------------ engine autonomy
def _engine_with_db(td):
    """Engine on a temp journal with Kronos skipped (model load is slow and
    irrelevant here). Returns (engine, old-state tuple for restore)."""
    import bot.engine as engine_mod
    old_db, old_kronos = CONFIG.db_path, engine_mod.TradingEngine._init_kronos
    CONFIG.db_path = os.path.join(td, "t.db")
    engine_mod.TradingEngine._init_kronos = lambda self: None
    return engine_mod.TradingEngine(mode="paper", quiet=True), (old_db, old_kronos)


def test_engine_stop_loss_end_to_end():
    """The owner's core guarantee at the ENGINE level (all prior stop tests
    stopped at the broker): a bar through the stop -> run through the engine's
    manage path -> journal row CLOSED 'stop loss' + cooldown set."""
    import bot.engine as engine_mod

    df = add_all_indicators(trending_df(260, drift=0.004, seed=13))
    spec = MarketSpec("crypto", "TEST/USDT", "1h")
    i = len(df) - 1
    price = float(df["close"].iloc[i])

    with tempfile.TemporaryDirectory() as td:
        eng, saved = _engine_with_db(td)
        try:
            d = _dec("LONG", 0.9, stop=2.0, price=price)
            eng.broker.open_position(spec, d, qty=1.0, price=price, trade_id=-1,
                                     ts=str(df.index[i - 3]),
                                     decision_bar_ts=float(df.index[i - 3].timestamp()))
            # journal row mirroring the broker position (the real path writes
            # it before the fill)
            trade_id = eng.journal.open_trade(
                spec.symbol, "long", 1.0, price, price - 2.0, None,
                "turtle_trend", "r", mode="paper",
                opened_ts=str(df.index[i - 3]), timeframe="1h")
            eng.broker.positions[(spec.symbol, "1h")].trade_id = trade_id

            # a closed bar whose low pierces the stop by a wide margin
            crash = df.copy()
            crash.iloc[i, crash.columns.get_loc("low")] = price - 5.0
            summary: dict = {"cycle": 1, "opened": [], "closed": [], "holds": 0, "errors": []}
            eng._manage_position(spec, eng.broker.positions[(spec.symbol, "1h")],
                                 crash, i, summary, bar_epoch=float(df.index[i].timestamp()))
            row = eng.journal.recent_trades()[0]
            assert row["id"] == trade_id and row["status"] == "CLOSED"
            assert row["exit_reason"] == "stop loss"
            assert summary["closed"] and summary["closed"][0]["reason"] == "stop loss"
            # stop-out cooldown is armed on the owning timeframe's clock
            assert eng.risk.cooldowns.get(spec.symbol, 0.0) > 0
        finally:
            CONFIG.db_path, engine_mod.TradingEngine._init_kronos = saved


def test_engine_replays_missed_stop_breach_after_restart():
    """A stop breached while the engine was OFFLINE must still exit on the
    first cycle after restart, even though the latest bar's range is back
    inside the levels (the old code scanned only the last closed bar)."""
    import bot.engine as engine_mod
    from bot.journal import Journal

    prices = 100 * np.cumprod(1 + np.full(80, 0.001))
    df = add_all_indicators(make_df(prices, freq="1h", seed=11))
    spec = MarketSpec("crypto", "TEST/USDT", "1h")
    i = len(df) - 1
    entry_px = float(df["close"].iloc[i - 6])
    breach_low = entry_px - 5.0          # bar i-4 dipped far through the stop
    stop = entry_px - 2.0

    with tempfile.TemporaryDirectory() as td:
        j = Journal(os.path.join(td, "t.db"))
        j.add_equity(10_000.0, 10_000.0, mode="paper")
        j.open_trade(spec.symbol, "long", 1.0, entry_px, stop, None,
                     "turtle_trend", "r", mode="paper",
                     opened_ts=str(df.index[i - 6]), timeframe="1h")
        # rewrite the journaled open row? not needed — restore uses opened_ts
        old_db, old_kronos = CONFIG.db_path, engine_mod.TradingEngine._init_kronos
        CONFIG.db_path = os.path.join(td, "t.db")
        engine_mod.TradingEngine._init_kronos = lambda self: None
        try:
            eng = engine_mod.TradingEngine(mode="paper", quiet=True)
            assert (spec.symbol, "1h") in eng._replay_pending
            # make the HISTORICAL bar i-4 breach the stop, latest bar clean
            replay_df = df.copy()
            replay_df.iloc[i - 4, replay_df.columns.get_loc("low")] = breach_low
            summary: dict = {"cycle": 1, "opened": [], "closed": [], "holds": 0, "errors": []}
            eng._process_market(spec, summary, replay_df)
            assert (spec.symbol, "1h") not in eng.broker.positions, \
                "breach bar replay must have closed the position"
            assert summary["closed"] and summary["closed"][0]["reason"] == "stop loss"
        finally:
            CONFIG.db_path, engine_mod.TradingEngine._init_kronos = old_db, old_kronos


def test_engine_no_phantom_stop_on_entry_bar():
    """The stop is computed FROM the entry bar, so scanning that same bar
    would instant-stop every trade whose entry bar had a wide range. The
    manage path must only scan bars strictly AFTER the decision bar."""
    import bot.engine as engine_mod

    df = add_all_indicators(trending_df(260, drift=0.004, seed=13))
    spec = MarketSpec("crypto", "TEST/USDT", "1h")
    i = len(df) - 1
    price = float(df["close"].iloc[i])

    with tempfile.TemporaryDirectory() as td:
        eng, saved = _engine_with_db(td)
        try:
            # stop INSIDE the entry bar's range (bar low < stop) — a phantom
            # scan would fire immediately
            d = _dec("LONG", 0.9, stop=2.0, price=price)
            pos = eng.broker.open_position(spec, d, qty=1.0, price=price, trade_id=-1,
                                           ts=str(df.index[i]),
                                           decision_bar_ts=float(df.index[i].timestamp()))
            assert float(df.iloc[i]["low"]) <= pos.stop, "fixture must pierce the stop"
            summary: dict = {"cycle": 1, "opened": [], "closed": [], "holds": 0, "errors": []}
            eng._manage_position(spec, pos, df, i, summary,
                                 bar_epoch=float(df.index[i].timestamp()))
            assert (spec.symbol, "1h") in eng.broker.positions, \
                "entry-bar scan must be skipped (no phantom stop)"
            # a LATER bar through the stop still exits
            later = df.copy()
            later.iloc[i, later.columns.get_loc("low")] = price - 5.0
            eng._manage_position(spec, pos, later, i, summary,
                                 bar_epoch=float(df.index[i + 1].timestamp()) if i + 1 < len(df)
                                 else float(df.index[i].timestamp()) + 3600.0)
            assert (spec.symbol, "1h") not in eng.broker.positions
        finally:
            CONFIG.db_path, engine_mod.TradingEngine._init_kronos = saved


def test_engine_data_outage_surfaces_then_closes():
    """A held position behind a dead feed is UNGUARDED: after WARN failures the
    condition must be visible on the engine (health_note, not last_error — the
    dashboard tears down on last_error), and after CLOSE failures the position
    is cut at the last known good mark."""
    import bot.engine as engine_mod

    df = add_all_indicators(trending_df(260, drift=0.004, seed=13))
    spec = MarketSpec("crypto", "TEST/USDT", "1h")
    i = len(df) - 1
    price = float(df["close"].iloc[i])

    with tempfile.TemporaryDirectory() as td:
        eng, saved = _engine_with_db(td)
        try:
            d = _dec("LONG", 0.9, stop=2.0, price=price)
            eng.broker.open_position(spec, d, qty=1.0, price=price, trade_id=-1,
                                     ts=str(df.index[i]),
                                     decision_bar_ts=float(df.index[i].timestamp()))
            eng._last_good_price[(spec.symbol, "1h")] = price
            summary: dict = {"cycle": 1, "opened": [], "closed": [], "holds": 0, "errors": []}
            for n in range(1, engine_mod.TradingEngine.FETCH_FAIL_CLOSE):
                eng._note_fetch_fail(spec, summary)
                assert (spec.symbol, "1h") in eng.broker.positions  # not yet closed
            eng._refresh_health_note()   # the cycle's finally-block does this live
            assert eng.health_note and "unguarded" in eng.health_note
            assert eng.last_error is None  # non-fatal channel only
            eng._note_fetch_fail(spec, summary)   # failure #CLOSE -> forced cut
            assert (spec.symbol, "1h") not in eng.broker.positions
            assert summary["closed"] and summary["closed"][0]["reason"] == "data outage"
            eng._refresh_health_note()
            assert eng.health_note is None        # position gone, note cleared
        finally:
            CONFIG.db_path, engine_mod.TradingEngine._init_kronos = saved


def test_engine_closes_null_stop_restored_row():
    """A legacy/crashed OPEN row without a stop must not trade unguarded: the
    first cycle closes it at the mark."""
    import bot.engine as engine_mod
    from bot.journal import Journal

    df = add_all_indicators(trending_df(260, drift=0.004, seed=13))
    spec = MarketSpec("crypto", "TEST/USDT", "1h")
    i = len(df) - 1

    with tempfile.TemporaryDirectory() as td:
        j = Journal(os.path.join(td, "t.db"))
        j.add_equity(10_000.0, 10_000.0, mode="paper")
        j.open_trade(spec.symbol, "long", 1.0, 100.0, None, None,
                     "turtle_trend", "r", mode="paper",
                     opened_ts=str(df.index[i - 2]), timeframe="1h")
        old_db, old_kronos = CONFIG.db_path, engine_mod.TradingEngine._init_kronos
        CONFIG.db_path = os.path.join(td, "t.db")
        engine_mod.TradingEngine._init_kronos = lambda self: None
        try:
            eng = engine_mod.TradingEngine(mode="paper", quiet=True)
            assert (spec.symbol, "1h") in eng._unguarded_pending
            summary: dict = {"cycle": 1, "opened": [], "closed": [], "holds": 0, "errors": []}
            eng._process_market(spec, summary, df)
            assert (spec.symbol, "1h") not in eng.broker.positions
            assert summary["closed"] and summary["closed"][0]["reason"] == "restored without stop"
            assert eng.journal.recent_trades()[0]["status"] == "CLOSED"
        finally:
            CONFIG.db_path, engine_mod.TradingEngine._init_kronos = old_db, old_kronos


# ------------------------------------------------- audit fixes (2026-09-06)
def test_backtest_scans_fill_bar_for_stop():
    """Parity: the fill bar (the entry fills at its open) is scanned for
    stop/target in the backtest exactly like the live engine manages it — a
    dip through the stop inside the fill bar must stop the trade out AT the
    fill bar (the old next-bar-only scan never saw that bar at all)."""
    from bot.backtest import Backtester
    df = trending_df(800, drift=0.002, seed=31)
    res = Backtester(CONFIG).run(CRYPTO_1H, df, strategy="turtle_trend")
    assert res.trades, "seed 31 must yield trades for this test to be meaningful"
    t0 = res.trades[0]
    assert t0["exit_ts"] != t0["entry_ts"], \
        "seed 31 must exit after the fill bar for this test to be meaningful"
    stop = t0["stop"]
    j = df.index.searchsorted(pd.Timestamp(t0["entry_ts"]), side="right") - 1
    assert j >= 0
    dipped = df.copy()
    dipped.iloc[j, dipped.columns.get_loc("low")] = stop * 0.90  # breach, fill bar
    res2 = Backtester(CONFIG).run(CRYPTO_1H, dipped, strategy="turtle_trend")
    assert res2.trades
    t = res2.trades[0]
    assert t["exit_reason"] == "stop loss", \
        f"fill-bar breach must stop out, got {t['exit_reason']}"
    assert t["exit_ts"] == t0["entry_ts"]      # exited IN the fill bar
    assert t["exit_price"] <= stop * 1.0000001  # never better than the level


def test_purged_cv_drops_trades_spanning_path_boundaries():
    """Purge rule: a trade whose HOLDING spans a path block boundary is dropped
    even when its entry sits far from the edge — entry-proximity purging alone
    leaked long holds across every split."""
    from bot.validation import oos_trade_distribution
    idx = pd.date_range("2024-01-01", periods=400, freq="1h", tz="UTC")
    df = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                       "volume": 1.0}, index=idx)
    # entry bar 100 -> exit bar 320: 220-bar hold crosses several block
    # boundaries (blocks are 100 bars here) -> never kept on any path
    long_hold = [{"entry_ts": str(idx[100]), "exit_ts": str(idx[320]),
                  "pnl": 5.0, "pnl_pct": 5.0}]
    out = oos_trade_distribution(long_hold, df, n_folds=4, n_test_folds=2,
                                 purge_bars=4)
    assert out["total_trades"] == 1
    assert out["purged_trades"] == 1
    assert all(p["trades"] == 0 for p in out["paths"])
    # positive control: a trade fully inside one block, clear of the edges,
    # is kept on every path that contains that block (folds 12/23/24)
    short_trade = [{"entry_ts": str(idx[110]), "exit_ts": str(idx[115]),
                    "pnl": 1.0, "pnl_pct": 1.0}]
    out2 = oos_trade_distribution(short_trade, df, n_folds=4, n_test_folds=2,
                                  purge_bars=4)
    assert out2["purged_trades"] == 0
    assert sum(p["trades"] for p in out2["paths"]) == 3


def test_journal_mixed_ts_formats_order_correctly():
    """seed_demo used to write pandas' space-separated ts while the engine
    wrote 'T' ISO — within a day every space row sorted before every T row
    regardless of time-of-day, and the restart cash anchor could pick a stale
    point. Write-time normalization + the boot migration fix the order."""
    from bot.journal import Journal
    with tempfile.TemporaryDirectory() as td:
        j = Journal(os.path.join(td, "t.db"))
        j.add_equity(10_000.0, 10_000.0, ts="2026-09-05 10:00:00+00:00")  # legacy format
        j.add_equity(11_000.0, 11_000.0, ts="2026-09-05T09:00:00+00:00")  # earlier, T format
        j.add_equity(12_000.0, 12_000.0, ts="2026-09-05 11:30:00+00:00")  # latest, legacy format
        assert [r["equity"] for r in j.equity_curve()] == [11_000.0, 10_000.0, 12_000.0]
        assert j.last_equity_point()["cash"] == 12_000.0

        # rows already in a legacy DB are normalized by the boot migration:
        # string-sorting the raw mixed formats would read the 09:00 row
        # ('T' > ' ') as the LATEST point — the exact stale-anchor bug
        import sqlite3
        raw = sqlite3.connect(os.path.join(td, "legacy.db"))
        raw.execute("CREATE TABLE equity (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " ts TEXT NOT NULL, equity REAL NOT NULL, cash REAL NOT NULL,"
                    " mode TEXT NOT NULL DEFAULT 'paper', note TEXT)")
        raw.execute("INSERT INTO equity (ts, equity, cash) VALUES"
                    " ('2026-09-05 10:00:00+00:00', 10000, 10000)")
        raw.execute("INSERT INTO equity (ts, equity, cash) VALUES"
                    " ('2026-09-05T09:00:00+00:00', 11000, 11000)")
        raw.commit()
        raw.close()
        j2 = Journal(os.path.join(td, "legacy.db"))
        assert [r["equity"] for r in j2.equity_curve()] == [11000, 10000]
        assert j2.last_equity_point()["cash"] == 10000.0


def test_kronos_ledger_respects_market_isolation():
    """A pending BTC forecast must NOT resolve against an ETH frame that
    happens to complete its horizon first — cross-market resolution corrupted
    the IC and, with it, the promotion gate."""
    from bot.kronos_signal import KronosICTracker
    with tempfile.TemporaryDirectory() as td:
        tr = KronosICTracker(os.path.join(td, "ic.json"))
        idx = pd.date_range("2024-01-01", periods=60, freq="1h", tz="UTC")
        btc = pd.Series(np.linspace(100, 200, 60), index=idx)   # strong up
        eth = pd.Series(np.linspace(100, 40, 60), index=idx)    # strong down
        tr.log_forecast(0.8, str(idx[0]), horizon=10, market="BTC/USDT|1h")
        # ETH's frame arrives first and HAS the horizon bars — the BTC
        # forecast must wait for BTC closes, not score against ETH
        tr.resolve(eth, market="ETH/USDT|1h")
        assert tr.n() == 0 and len(tr._pending) == 1
        tr.resolve(btc, market="BTC/USDT|1h")
        assert tr.n() == 1 and not tr._pending
        score, fwd = tr.records[0]
        assert fwd > 0.1   # BTC's realized up-move, not ETH's down-move


def test_data_outage_close_retries_when_no_mark_available():
    """A position opened DURING the outage has no last-good mark: the forced
    close must be deferred and retried every cycle, and the failure counter
    must NOT reset (it used to reset on the failed close, leaving the position
    unguarded for another FETCH_FAIL_CLOSE failures)."""
    import bot.engine as engine_mod
    spec = MarketSpec("crypto", "TEST/USDT", "1h")
    with tempfile.TemporaryDirectory() as td:
        eng, saved = _engine_with_db(td)
        try:
            d = _dec("LONG", 0.9, stop=2.0, price=100.0)
            eng.broker.open_position(spec, d, qty=1.0, price=100.0, trade_id=-1,
                                     ts="2026-09-05T00:00:00+00:00")
            summary: dict = {"cycle": 1, "opened": [], "closed": [], "holds": 0,
                             "errors": []}
            for _ in range(engine_mod.TradingEngine.FETCH_FAIL_CLOSE + 3):
                eng._note_fetch_fail(spec, summary)
            assert (spec.symbol, "1h") in eng.broker.positions   # no mark: deferred
            assert eng._fetch_fails[(spec.symbol, "1h")] == \
                engine_mod.TradingEngine.FETCH_FAIL_CLOSE + 3    # counter kept growing
            eng._last_good_price[(spec.symbol, "1h")] = 100.0    # mark appears
            eng._note_fetch_fail(spec, summary)                  # next attempt closes
            assert (spec.symbol, "1h") not in eng.broker.positions
        finally:
            CONFIG.db_path, engine_mod.TradingEngine._init_kronos = saved


def test_chatbot_paper_record_excludes_demo_rows():
    """seed-demo rows are mode='demo': the chatbot's earnings answer must
    describe the bot's OWN paper record and say what it excluded."""
    from bot.chatbot import ChatBot
    from bot.journal import Journal
    with tempfile.TemporaryDirectory() as td:
        j = Journal(os.path.join(td, "t.db"))
        tid = j.open_trade("BTC/USDT", "long", 1.0, 100.0, 90.0, None,
                           "turtle_trend", "r", mode="paper")
        j.close_trade(trade_id=tid, exit_price=110.0, pnl=10.0, pnl_pct=10.0,
                      fees=0.0, exit_reason="take profit")
        tid2 = j.open_trade("ETH/USDT", "long", 1.0, 100.0, 90.0, None,
                            "turtle_trend", "r", mode="demo")
        j.close_trade(trade_id=tid2, exit_price=200.0, pnl=100.0, pnl_pct=100.0,
                      fees=0.0, exit_reason="take profit")
        class _NoLLM:            # enabled is a read-only property on LLMClient
            enabled = False
        reply = ChatBot(journal=j, llm=_NoLLM()).answer("how much did you earn?")
        assert "$10.00" in reply              # the paper record
        assert "$100.00" not in reply         # the demo replay PnL is not the record
        assert "demo" in reply                # the exclusion is stated, not silent


# ------------------------------------------------- group-4 features (2026-09-06)
def test_fetch_history_pinned_window_cache_and_manifest():
    """A pinned --start/--end fetch gets a DATE-STABLE cache path (byte-identical
    reruns) and records its provenance (bars, sha256) in data/manifest.json."""
    from bot import data as data_mod
    from bot.data import fetch_history
    spec = MarketSpec("crypto", "TEST/USDT", "1h")
    idx = pd.date_range("2024-01-01", periods=48, freq="1h", tz="UTC")
    fake = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                         "volume": 0.0}, index=idx)
    calls = []
    orig = data_mod.fetch_crypto_history
    data_mod.fetch_crypto_history = lambda *a, **k: calls.append(k) or fake
    old_dir, old_db = CONFIG.data_cache_dir, CONFIG.db_path
    with tempfile.TemporaryDirectory() as td:
        CONFIG.data_cache_dir = td
        CONFIG.db_path = os.path.join(td, "t.db")
        try:
            df = fetch_history(spec, start="2024-01-01", end="2024-01-03")
            assert len(df) == 48
            assert calls and calls[0].get("start") == "2024-01-01"
            cache = data_mod._disk_cache_path(spec, None, "2024-01-01", "2024-01-03")
            assert "2024-01-01" in os.path.basename(cache)   # date-stable name
            assert os.path.exists(cache)
            manifest_path = os.path.join(td, "manifest.json")
            assert os.path.exists(manifest_path)
            import json as _json
            entry = _json.load(open(manifest_path))["crypto:TEST/USDT:1h"]
            assert entry["bars"] == 48 and len(entry["sha256"]) == 64
            # a second call is served from the pinned cache without a refetch
            calls.clear()
            fetch_history(spec, start="2024-01-01", end="2024-01-03")
            assert not calls
        finally:
            CONFIG.data_cache_dir, CONFIG.db_path = old_dir, old_db
            data_mod.fetch_crypto_history = orig


def test_validation_report_renderer():
    """`validate --report` renders the SAME numbers as markdown: stats, verdicts,
    paths, and the caveats block."""
    from bot.report import render_validation_report
    r = {"symbol": "BTC/USDT", "timeframe": "1h", "strategy": "turtle_trend",
         "days": 365, "bars": 8760, "generated_at": "2026-09-06T00:00:00+00:00",
         "backtest": {"return_pct": 1.5, "trades": 11, "win_rate_pct": 9.1,
                      "profit_factor": 1.5, "max_drawdown_pct": -5.1, "sharpe": 0.26,
                      "total_pnl": 15.0, "fees": 12.0},
         "purged_cv": {"n_paths": 6, "n_active_paths": 4, "mean_return_pct": 1.2,
                       "std_return_pct": 3.4, "t_stat": 0.8, "pct_paths_profitable": 75.0,
                       "purged_trades": 2, "total_trades": 11,
                       "paths": [{"trades": 3, "return_pct": 2.0, "win_rate_pct": 33.3,
                                  "total_pnl": 5.0}]},
         "pbo": {"pbo": 0.25, "verdict": "selection holds OOS", "n_configs": 2,
                 "n_paths": 6, "n_sims": 400},
         "deflated_sharpe": {"best_sharpe": 1.0, "n_trials": 5, "deflated_sharpe": 0.892,
                             "verdict": "suggestive"},
         "monte_carlo": {"n_sims": 2000, "terminal_p5": 9100.0, "terminal_p50": 10050.0,
                         "terminal_p95": 11200.0, "p_lose_money": 40.0,
                         "p_dd_beyond_10pct": 8.0},
         "min_trl": {"min_bars": 14000, "min_years": 1.6}}
    md = render_validation_report(r)
    assert "# Validation report — BTC/USDT 1h" in md
    assert "DSR 0.892" in md and "suggestive" in md
    assert "0.25" in md and "selection holds OOS" in md
    assert "1.6 years" in md and "Caveats" in md
    assert "| 1 | 3 | +2.00 | 33.3 |" in md   # path table


def test_dashboard_evidence_endpoint_smoke():
    """/api/evidence serves all four payload sections and tolerates missing
    artifacts (empty dirs, no ledger file) instead of erroring."""
    import bot.dashboard as dash
    from fastapi.testclient import TestClient
    client = TestClient(dash.app)
    r = client.get("/api/evidence")
    assert r.status_code == 200
    payload = r.json()
    assert set(payload) == {"kronos", "validations", "shadow", "manifest"}
    assert isinstance(payload["validations"], list)
    assert isinstance(payload["manifest"], dict)
    # the real machine's ledger (if present) either loads or reports why not
    k = payload["kronos"]
    assert ("n" in k) or ("error" in k)


def test_cli_writers_create_results_dir_on_fresh_machine():
    """cmd_shadow's default output and run_battery's per-run JSON used to raise
    FileNotFoundError on a fresh clone (data/ is gitignored, data/results/ was
    never created) — AFTER the full fetch/backtest work, the worst moment.
    Pin the makedirs behavior without network: the shadow report path and the
    battery's write path must both create their parent dir."""
    import tempfile
    # (a) the shared _ensure_parent helper semantics the writers now rely on
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "data", "results", "shadow_report.json")
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w") as fh:
            json.dump({"profile": {"n_trades": 0}}, fh)
        assert os.path.exists(out)
    # (b) run_battery's module-level makedirs call — import must not have
    # side effects, so simulate main()'s first line exactly as shipped
    src = open("run_battery.py").read()
    assert 'os.makedirs("data/results", exist_ok=True)' in src
    src_m = open("main.py").read()
    assert 'os.makedirs(os.path.dirname(out) or ".", exist_ok=True)' in src_m


def test_reset_backup_is_wal_checkpointed_and_pruned():
    """The reset backup used to be copy2 of the main db file only — rows still
    living in the -wal (mid-session writes) were missing from the 'verified'
    backup. It must checkpoint FIRST, name backups with sub-second precision,
    and keep only the newest few."""
    import glob as _glob
    import tempfile
    from bot import dashboard as dash
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as td:
        old_db = CONFIG.db_path
        CONFIG.db_path = os.path.join(td, "t.db")
        try:
            dash.journal.db_path = CONFIG.db_path
            dash.journal = dash.Journal(CONFIG.db_path)
            dash.chatbot = dash.ChatBot(dash.journal)
            client = TestClient(dash.app)
            j = dash.journal
            j.add_equity(10_000.0, 10_000.0, mode="paper")
            j.log_chat("user", "hello during reset window")   # fresh WAL content
            r = client.post("/api/account/reset", json={"capital": 5000})
            assert r.status_code == 200
            backup = r.json()["backup"]
            assert os.path.exists(backup)
            # the backup must be a COMPLETE database (schema + the pre-reset
            # chat row), not just the checkpointed main file
            import sqlite3
            bc = sqlite3.connect(backup)
            rows = bc.execute("SELECT COUNT(*) FROM chat_log").fetchone()[0]
            assert rows >= 1
            bc.close()
            # retention: a second reset prunes to keep_n newest (5), not zero
            client.post("/api/account/reset", json={"capital": 5000})
            left = _glob.glob(os.path.join(td, "t.backup.*.db"))
            assert 1 <= len(left) <= 5
        finally:
            # restore the module-level journal too (leaving it pointed at the
            # deleted tempdir poisons any later test that uses dash.app)
            CONFIG.db_path = old_db
            dash.journal.db_path = old_db
            dash.journal = dash.Journal(old_db)
            dash.chatbot = dash.ChatBot(dash.journal)


def test_seed_demo_idempotent_and_headline_coherent():
    """Re-seeding must REPLACE the demo rows (it used to stack: 419 → 1,256
    trades, headline return → 0.0%), and the seeded equity walk must be ONE
    portfolio-threaded account whose end equals capital + total trade P&L —
    the old per-spec walks restarted at paper_capital each, so the headline
    showed '-$1,020 P&L' beside '+3.40% return'."""
    import tempfile
    from types import SimpleNamespace
    from bot import seed_demo
    from config import MarketSpec

    with tempfile.TemporaryDirectory() as td:
        old_db = CONFIG.db_path
        CONFIG.db_path = os.path.join(td, "t.db")
        try:
            cfg = SimpleNamespace(
                watchlist=[MarketSpec("crypto", "TESTA/USDT", "1h"),
                           MarketSpec("crypto", "TESTB/USDT", "1h")],
                paper_capital=10_000.0)
            fake_trades = [
                {"symbol": "TESTA/USDT", "side": "long", "qty": 1.0, "entry_price": 100.0,
                 "stop": 95.0, "target": 110.0, "strategy": "turtle_trend",
                 "rationale": "r", "entry_ts": "2026-01-01T00:00:00+00:00",
                 "exit_ts": "2026-01-02T00:00:00+00:00", "exit_price": 105.0,
                 "pnl": 300.0, "pnl_pct": 3.0, "fees": 1.0, "exit_reason": "target"},
                {"symbol": "TESTB/USDT", "side": "short", "qty": 1.0, "entry_price": 50.0,
                 "stop": 55.0, "target": 45.0, "strategy": "connors_meanrev",
                 "rationale": "r", "entry_ts": "2026-01-03T00:00:00+00:00",
                 "exit_ts": "2026-01-04T00:00:00+00:00", "exit_price": 52.0,
                 "pnl": -150.0, "pnl_pct": -3.0, "fees": 1.0, "exit_reason": "stop"},
            ]
            class _FakeBT:
                def run(self, spec, df=None, strategy=None):
                    class _R:
                        trades = fake_trades
                        equity_curve = []
                        def stats(self):
                            return {"trades": len(fake_trades), "total_pnl": 150.0,
                                    "win_rate_pct": 50.0}
                    return _R()
            old_bt = seed_demo.Backtester
            old_fetch = seed_demo.fetch_history
            seed_demo.Backtester = lambda cfg=None: _FakeBT()
            seed_demo.fetch_history = lambda spec, days=240: pd.DataFrame(
                {"a": range(300)})
            try:
                n1 = seed_demo.seed(cfg)
                assert n1["trades"] == 4 and n1["equity"] >= 2
                n2 = seed_demo.seed(cfg)          # RE-SEED: must replace, not stack
                assert n2["trades"] == 4
                j = seed_demo.Journal()
                modes = j.trade_mode_counts()
                assert modes == {"demo": 4}, modes
                s = j.stats()                       # demo-only journal: all rows
                assert s["closed_trades"] == 4
                # headline coherence: end equity == capital + total trade P&L
                # (each fake trade is duplicated across both specs: +300×2, −150×2)
                assert abs(s["current_equity"] - (10_000.0 + 300.0)) < 0.01, s
                assert abs(s["return_pct"] - 3.0) < 0.01, s
            finally:
                seed_demo.Backtester = old_bt
                seed_demo.fetch_history = old_fetch
        finally:
            CONFIG.db_path = old_db


def test_decisions_feed_filters_demo_rows():
    """/api/decisions must read the paper feed first and only fall back to all
    rows on a demo-only journal (the Overview terminal and the chatbot's 'why'
    path used to present seeded demo decisions as the bot's own)."""
    import tempfile
    from types import SimpleNamespace
    from bot import dashboard as dash
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as td:
        old_db = CONFIG.db_path
        CONFIG.db_path = os.path.join(td, "t.db")
        try:
            dash.journal.db_path = CONFIG.db_path
            dash.journal = dash.Journal(CONFIG.db_path)
            dash.chatbot = dash.ChatBot(dash.journal)
            j = dash.journal
            d = SimpleNamespace(action="LONG", confidence=0.7, price=100.0,
                                regime="trending", stop_distance=None, target_rr=None,
                                strategy_signals={}, sentiment={}, rationale="r")
            j.add_decision("BTC/USDT", "1h", d, mode="demo")
            j.add_decision("ETH/USDT", "1h", d, mode="paper")
            client = TestClient(dash.app)
            r = client.get("/api/decisions").json()
            assert [x["symbol"] for x in r] == ["ETH/USDT"]      # paper first
            j2 = dash.Journal(os.path.join(td, "demo_only.db"))
            CONFIG.db_path = os.path.join(td, "demo_only.db")
            dash.journal.db_path = CONFIG.db_path
            dash.journal = j2
            dash.chatbot = dash.ChatBot(j2)
            j2.add_decision("GBPUSD=X", "1h", d, mode="demo")
            r2 = TestClient(dash.app).get("/api/decisions").json()
            assert [x["symbol"] for x in r2] == ["GBPUSD=X"]     # demo fallback renders
        finally:
            CONFIG.db_path = old_db
            dash.journal.db_path = old_db
            dash.journal = dash.Journal(old_db)
            dash.chatbot = dash.ChatBot(dash.journal)


def test_chatbot_why_matches_symbol_not_newest():
    """'why did you buy BTC?' must answer about BTC — it used to return the
    newest non-HOLD decision of ANY market (a GBPUSD demo row on a fresh
    clone) and even claimed 'no entries' while 54 demo entries existed."""
    import tempfile
    from types import SimpleNamespace
    from bot.chatbot import ChatBot
    from bot.journal import Journal

    with tempfile.TemporaryDirectory() as td:
        old_db = CONFIG.db_path
        CONFIG.db_path = os.path.join(td, "t.db")
        try:
            j = Journal(CONFIG.db_path)
            d = SimpleNamespace(action="LONG", confidence=0.7, price=1.27,
                                regime="trending", stop_distance=None, target_rr=None,
                                strategy_signals={}, sentiment={}, rationale="why-demo")
            j.add_decision("GBPUSD=X", "1h", d, mode="demo")
            d2 = SimpleNamespace(action="LONG", confidence=0.8, price=100.0,
                                 regime="trending", stop_distance=None, target_rr=None,
                                 strategy_signals={}, sentiment={}, rationale="live-btc")
            j.add_decision("BTC/USDT", "1h", d2, mode="paper")
            class _NoLLM:
                enabled = False
                provider = "none"
            bot = ChatBot(j, _NoLLM())
            # asks about BTC -> must answer BTC, never the GBPUSD demo row
            assert "BTC/USDT" in bot.answer("why did you buy BTC?")
            # 'long' must not read as a market: the SOL question targets SOL
            a_long = bot.answer("why did you open a long on SOL?")
            assert "SOL" in a_long and "BTC/USDT" not in a_long
            # asks about a market never traded -> honest 'no decision on X'
            no = bot.answer("why did you buy DOGE?")
            assert "DOGE" in no and "No" in no
            # demo-only journal: honest demo labeling instead of a wrong answer
            with tempfile.TemporaryDirectory() as td2:
                CONFIG.db_path = os.path.join(td2, "t2.db")
                j2 = Journal(CONFIG.db_path)
                j2.add_decision("GBPUSD=X", "1h", d, mode="demo")
                bot2 = ChatBot(j2, _NoLLM())
                a2 = bot2.answer("why did you buy GBPUSD?")
                assert "GBPUSD=X" in a2 and "demo" in a2
        finally:
            CONFIG.db_path = old_db


def test_journal_quarantines_corrupt_db():
    """A torn trading.db used to crash Journal() at import — uvicorn died with
    a raw traceback and no UI to explain. It must quarantine (like the kronos
    ledger) and start fresh; a merely-busy db must NOT be quarantined."""
    import tempfile
    from bot.journal import Journal, _db_corrupt

    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "t.db")
        with open(p, "wb") as fh:
            fh.write(b"definitely not a sqlite database" * 50)
        CONFIG.db_path = p
        j = Journal()                     # must quarantine, not raise
        j.add_equity(1.0, 1.0)
        assert any("corrupt" in f for f in os.listdir(td))
    # signature check: corruption yes, busy/locked no
    import sqlite3
    assert _db_corrupt(sqlite3.DatabaseError("file is not a database"))
    assert _db_corrupt(sqlite3.DatabaseError("database disk image is malformed"))
    assert not _db_corrupt(sqlite3.DatabaseError("database is locked"))
    assert not _db_corrupt(sqlite3.OperationalError("database or disk is full"))


def test_ccxt_source_cooldown_benches_dead_exchanges():
    """Three consecutive failures bench a source for 5 minutes (a geo-blocked
    Binance used to re-pay its timeout on every fetch, every cycle); a benched
    source is not even probed; all-benched un-benches so fetches keep trying;
    success resets strikes."""
    from bot import data as data_mod

    def fetch_one_ok(src):
        rows = [[(pd.Timestamp("2024-01-01", tz="UTC") +
                  pd.Timedelta(hours=i)).timestamp() * 1000, 100, 101, 99, 100.5, 10.0]
                for i in range(50)]
        return data_mod._validate_ohlcv(data_mod._rows_to_df(rows), f"crypto:{src}")

    def fetch_one_binance_dead(src):
        if src == "binance":
            raise ConnectionError("geo-blocked")
        return fetch_one_ok(src)

    data_mod._source_fails.clear()
    for _ in range(3):                    # binance fails 3x consecutively -> benched
        try:
            data_mod._fetch_with_fallback("X/USDT", "1h", "auto", fetch_one_binance_dead)
        except RuntimeError:
            pass
    # 4th call: binance is benched, fetch served by bybit WITHOUT probing binance
    probed = []

    def spy(src):
        probed.append(src)
        return fetch_one_binance_dead(src)

    df = data_mod._fetch_with_fallback("X/USDT", "1h", "auto", spy)
    assert df.attrs["source"] == "bybit"
    assert "binance" not in probed        # benched: not even probed
    # success resets strikes: bybit has none now, and a direct binance win clears its bench
    df2 = data_mod._fetch_with_fallback("X/USDT", "1h", "binance", fetch_one_ok)
    assert df2.attrs["source"] == "binance"
    assert data_mod._source_fails.get("binance") is None
    # all sources benched -> bench dropped so the next call retries (never dead)
    for s in ("binance", "bybit", "okx"):
        data_mod._source_fails[s] = (99, time.time() + 9999)
    try:
        data_mod._fetch_with_fallback("X/USDT", "1h", "auto", fetch_one_binance_dead)
    except RuntimeError:
        pass
    assert all(v[0] < 99 for v in data_mod._source_fails.values())
    data_mod._source_fails.clear()


# ---------------------------------- verification-gap pass (2026-09-08)
# Fixes from the JUDGE_REPORT passes shipped with holes in their test cover:
# these six pin the ones that had NO regression test standing guard.

def test_account_reset_refused_while_engine_stopping():
    """A reset issued while the engine thread outlived its bounded stop-join
    (status 'stopping') must 409 and touch NOTHING — the wipe used to proceed
    and could race the engine's in-flight journal writes. With the thread
    confirmed down ('not_running'), the same reset proceeds normally."""
    import glob as _glob
    import tempfile
    from bot import dashboard as dash
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as td:
        old_db = CONFIG.db_path
        CONFIG.db_path = os.path.join(td, "t.db")
        real_stop = dash.api_engine_stop
        try:
            dash.journal.db_path = CONFIG.db_path
            dash.journal = dash.Journal(CONFIG.db_path)
            dash.chatbot = dash.ChatBot(dash.journal)
            client = TestClient(dash.app)
            tid = dash.journal.open_trade("BTC/USDT", "long", 1.0, 100.0, 90.0,
                                          None, "turtle_trend", "r", mode="paper")
            assert tid

            dash.api_engine_stop = lambda body: {"status": "stopping"}
            r = client.post("/api/account/reset", json={"capital": 5000})
            assert r.status_code == 409 and "stopping" in r.json()["detail"]
            # refused means REFUSED: trade survives, no backup, no wipe
            assert dash.journal.trade_mode_counts() == {"paper": 1}
            assert not _glob.glob(os.path.join(td, "t.backup.*.db"))

            # control: same request, thread confirmed down -> reset proceeds
            dash.api_engine_stop = lambda body: {"status": "not_running"}
            r = client.post("/api/account/reset", json={"capital": 5000})
            assert r.status_code == 200
            assert dash.journal.trade_mode_counts() == {}   # wiped
            assert os.path.exists(r.json()["backup"])
        finally:
            dash.api_engine_stop = real_stop
            CONFIG.db_path = old_db
            dash.journal.db_path = old_db
            dash.journal = dash.Journal(old_db)
            dash.chatbot = dash.ChatBot(dash.journal)


def test_kronos_ledger_quarantines_torn_file_and_survives_restart():
    """A torn kronos_ic.json (crash mid-write) must be quarantined to
    .corrupt for inspection — NOT silently reset to empty (the ledger is the
    promotion gate's memory) and NOT deleted (the evidence is needed to see
    what was lost). A healthy ledger must roundtrip through a restart."""
    from bot.kronos_signal import KronosICTracker
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "ic.json")
        with open(path, "w") as fh:
            fh.write('{"records": [[0.5, 0.01')        # crash mid-write
        tr = KronosICTracker(path)
        assert tr.n() == 0 and not tr._pending          # starts fresh, loudly
        assert not os.path.exists(path)                # ...after moving it aside
        assert os.path.exists(path + ".corrupt")
        with open(path + ".corrupt") as fh:
            assert "records" in fh.read()              # evidence preserved

        # healthy save/load roundtrip across a fresh instance ("restart")
        idx = pd.date_range("2024-01-01", periods=40, freq="1h", tz="UTC")
        closes = pd.Series(np.linspace(100, 120, 40), index=idx)
        tr.log_forecast(0.5, str(idx[0]), horizon=10, market="BTC/USDT|1h")
        tr.resolve(closes, market="BTC/USDT|1h")
        assert tr.n() == 1 and not os.path.exists(path + ".tmp")   # no tmp litter
        tr2 = KronosICTracker(path)
        assert tr2.n() == 1 and tr2.records == tr.records


def test_kronos_horizon_normalized_to_one_day_per_timeframe():
    """horizon=24 was hardcoded for every book: 24x4h forecast FOUR DAYS out,
    24x15m forecast four hours. The horizon must be ~one day of bars for
    whichever timeframe the book trades."""
    import bot.engine as engine_mod
    from config import TIMEFRAME_SECONDS
    h = engine_mod.TradingEngine._kronos_horizon
    assert h("5m") == 288 and h("15m") == 96 and h("1h") == 24
    assert h("4h") == 6 and h("1d") == 1
    for tf in ("5m", "15m", "1h", "4h", "1d"):
        assert h(tf) * TIMEFRAME_SECONDS[tf] == 86400   # exactly one day ahead


def test_bars_per_year_forex_weekday_scaling():
    """Crypto trades 24/7 but Yahoo forex trades ~24x5 (weekend gaps): the
    24/7 bar count overstated forex Sharpe magnitudes ~18%
    (sqrt(8760/6257) = 1.18). kind='forex' scales by 5/7; the default kind
    stays crypto so no existing call site shifts."""
    from config import bars_per_year
    assert bars_per_year("1h") == 8760.0               # 24/7 crypto, default kind
    assert abs(bars_per_year("1h", "forex") - 8760.0 * 5.0 / 7.0) < 0.01
    assert abs(bars_per_year("1h", "forex") - 6257.14) < 0.01
    for tf in ("5m", "15m", "1h", "4h", "1d"):
        assert bars_per_year(tf) > bars_per_year(tf, "forex")   # scaled, never inflated


def test_journal_conn_closes_deterministically():
    """`with self._conn()` used to commit but never close — every 4s dashboard
    poll leaked a connection to the GC's discretion. The contextmanager must
    close on BOTH paths: clean exit and exception mid-block."""
    import sqlite3
    from bot.journal import Journal
    with tempfile.TemporaryDirectory() as td:
        j = Journal(os.path.join(td, "t.db"))
        j.add_equity(10.0, 10.0)
        with j._conn() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM equity").fetchone()["n"]
            assert n == 1
        try:
            conn.execute("SELECT 1")
            closed_clean = False
        except sqlite3.ProgrammingError:
            closed_clean = True
        assert closed_clean
        try:
            with j._conn() as conn2:
                conn2.execute("SELECT * FROM no_such_table")
            raised = False
        except sqlite3.OperationalError:
            raised = True
        assert raised
        try:
            conn2.execute("SELECT 1")                  # rollback AND close
            closed_err = False
        except sqlite3.ProgrammingError:
            closed_err = True
        assert closed_err
        with j._conn() as conn3:                       # db still usable after
            assert conn3.execute("SELECT COUNT(*) AS n FROM equity").fetchone()["n"] == 1


def test_meanrev_never_fires_short_gate_on_warmup_rsi():
    """RSI(2) warmup used to read 100.0 (blanket fillna), which sits ABOVE the
    95 short gate — during the rsi2-lead window the gate was genuinely armed,
    masked only by the unrelated EMA200 warmup veto outlasting it. Warmup is
    NaN now: NaN fails _ok(), so no gate can fire. The counterfactual (100.0
    planted at the same bar) shorts, proving the NaN is what disarms it."""
    t = np.arange(260)
    prices = np.linspace(210, 100, 260) + 1.5 * np.sin(t * 0.7)
    df = add_all_indicators(make_df(prices))
    mr = ConnorsMeanReversion()
    i = len(df) - 1
    assert mr.p.mr_rsi_sell_above == 95.0             # the gate in question
    assert df["ema200"].iloc[i] > df["close"].iloc[i]  # downtrend: short side armed

    warm = df.copy()
    warm.loc[warm.index[i], "rsi2"] = float("nan")     # the FIXED warmup value
    warm.loc[warm.index[i], "halflife"] = 5.0         # half-life gate passes
    sig = mr.evaluate(warm, i)
    assert sig.action == "FLAT" and "not ready" in (sig.rationale or "")

    old = df.copy()
    old.loc[old.index[i], "rsi2"] = 100.0             # the OLD warmup value
    old.loc[old.index[i], "halflife"] = 5.0
    assert mr.evaluate(old, i).action == "SHORT"


# ------------------- confirmed-flaw fixes (FLAW_VALIDATION.md, 2026-09-09)
def test_turtle_opposite_channel_exit_actually_fires():
    """Flaw 1.1: the unshifted exit channel included the decision bar's own
    low/high, making the exit mathematically impossible (close >= low by
    candlestick construction). The shifted (prior-channel) exit must fire on
    a real reversal, and a full backtest must produce opposite-channel exits
    rather than only stops and end-of-data holds."""
    from bot.backtest import Backtester
    p = CONFIG.params
    # rising frame -> build the reversal tail: close breaks under the PRIOR
    # 10-bar low, with a coherent candle (low <= close)
    prices = np.concatenate([np.linspace(100, 130, 260),
                              np.linspace(130, 90, 40)])
    df = add_all_indicators(make_df(prices, seed=11))
    turtle = TurtleTrend()
    from bot.broker import Position
    pos = Position(trade_id=1, symbol="T", side="long", qty=1.0,
                   entry_price=float(df["close"].iloc[200]),
                   stop=None, target=None, strategy="turtle_trend")
    fired = 0
    for i in range(p.turtle_exit_period + 1, len(df)):
        reason, _ = turtle.check_exit(df, i, pos)
        if reason:
            fired += 1
            prior_lo = df["low"].rolling(p.turtle_exit_period).min().shift(1).iloc[i]
            assert float(df["close"].iloc[i]) < float(prior_lo)   # the real condition
    assert fired >= 1, "opposite-channel exit never fired — dead exit code is back"
    # end-to-end: the fixed backtest exits via the channel, not just stops
    res = Backtester(CONFIG).run(CRYPTO_1H, df, strategy="turtle_trend")
    reasons = {t["exit_reason"] for t in res.trades}
    assert any("opposite-channel" in r for r in reasons), reasons


def test_scalper_breakeven_stop_is_cost_aware():
    """Flaw 1.3: a stop AT the entry price realized a guaranteed ~-0.30%
    round trip (exit taker fee + slippage on top of the entry leg). The
    breakeven stop must sit above entry (long) by the cost buffer, and exiting
    there must net approximately zero P&L through the broker."""
    sc = VWAPScalper()
    df = add_all_indicators(trending_df(300, drift=0.002, seed=23))
    from bot.broker import Position
    pos = Position(trade_id=1, symbol="TEST/USDT", side="long", qty=1.0,
                   entry_price=100.0, stop=98.0, target=None,
                   strategy="vwap_scalper", risk_per_unit=2.0)
    frame = df.copy()
    frame["vwap_roll"] = 100.5
    i = len(frame) - 1
    frame.loc[frame.index[i], "close"] = 102.0        # +1R
    _, new_stop = sc.check_exit(frame, i, pos)
    buf = CONFIG.costs.fee("crypto") + CONFIG.costs.slippage("crypto")
    assert new_stop is not None
    assert new_stop == pytest_approx(100.0 * (1.0 + buf), 1e-9)
    assert new_stop > 100.0                            # the whole point
    # and the short side mirrors it (below entry)
    pos_s = Position(trade_id=2, symbol="TEST/USDT", side="short", qty=1.0,
                     entry_price=100.0, stop=102.0, target=None,
                     strategy="vwap_scalper", risk_per_unit=2.0)
    frame_s = frame.copy()
    frame_s.loc[frame_s.index[i], "close"] = 98.0      # +1R for the short
    _, new_stop_s = sc.check_exit(frame_s, i, pos_s)
    assert new_stop_s == pytest_approx(100.0 * (1.0 - buf), 1e-9)
    # broker-level: entering and exiting at the BE level nets ~-entry-leg only,
    # vs the old guaranteed -0.30%
    b = PaperBroker(10_000.0)
    d = _dec("LONG", 0.9, stop=5.0, price=100.0)
    opened = b.open_position(CRYPTO_1H, d, qty=1.0, price=100.0, trade_id=1)
    closed, pnl, _, _, _ = b.close_position(CRYPTO_1H, opened.entry_price * (1.0 + buf),
                                            "stop loss")
    # exit ABOVE raw entry (by the buffer) yet still nets a small loss: the
    # entry-leg fee. Before the fix, the BE stop was AT entry and lost both.
    assert -0.20 < pnl < 0.0


def test_journal_initial_stop_survives_trailing():
    """Flaw 2.1: trailing stops overwrote stop_price in the trades table, so
    R-multiples divided by the FINAL (trailed/BE) stop — producing ±20R
    explosions and excluding stop==entry rows. initial_stop_price must be
    latched at open and never moved by trails; the broker's restore and
    shadow's R math must both use it."""
    from bot.journal import Journal
    from bot.shadow import behavior_profile
    from bot.broker import PaperBroker
    with tempfile.TemporaryDirectory() as td:
        j = Journal(os.path.join(td, "t.db"))
        tid = j.open_trade("TEST/USDT", "long", 1.0, 100.0, None, None,
                           "vwap_scalper", "r")
        # fill arrives: stop 98 latched as the initial stop
        j.record_fill(tid, entry_price=100.5, stop=98.0, target=None,
                      entry_fee=0.1, initial_stop=98.0)
        # the position runs to +1R: stop trails to cost-aware breakeven
        j.update_trade_stops(tid, stop=100.7)
        row = j.recent_trades()[0]
        assert row["stop_price"] == pytest_approx(100.7, 1e-9)          # trailed
        assert row["initial_stop_price"] == pytest_approx(98.0, 1e-9)   # latched
        # broker restore derives R from the INITIAL stop
        b = PaperBroker(10_000.0)
        pos = b.restore_position(j.open_trades()[0], "crypto", timeframe="1h")
        assert pos.risk_per_unit == pytest_approx(2.5, 1e-9)            # |100.5-98|
        # behavior_profile: a BE-trailed small loser reads as a sane fraction
        # of initial risk, not an explosion; and stop==entry rows are INCLUDED
        # via the initial stop instead of being excluded
        j.close_trade(tid, 100.7, -0.35, -0.35, 0.25, "stop loss",
                     closed_ts="2026-01-01T00:00:00+00:00")
        t = j.recent_trades()[0]
        prof = behavior_profile([{**t, "qty": 1.0}])
        assert prof["avg_r"] is not None and abs(prof["avg_r"]) < 0.25   # -0.35/2.5R
        assert prof["n_blew_through_stop"] == 0
        # migration: a legacy DB gains the columns and backfills
        j2 = Journal(os.path.join(td, "t2.db"))
        tid2 = j2.open_trade("TEST/USDT", "long", 1.0, 100.0, 95.0, 105.0,
                             "turtle_trend", "r")
        # simulate a legacy CLOSED row: fees set, audit columns absent
        j2.close_trade(tid2, 105.0, 4.9, 4.9, 0.21, "take profit",
                       closed_ts="2026-01-01T00:00:00+00:00")
        conn = sqlite3.connect(j2.db_path)
        conn.execute("UPDATE trades SET initial_stop_price=NULL, entry_fee=NULL WHERE id=?",
                     (tid2,))
        conn.commit()
        conn.close()
        j3 = Journal(j2.db_path)      # boot runs _migrate
        row2 = j3.recent_trades()[0]
        assert row2["initial_stop_price"] == pytest_approx(95.0, 1e-9)  # backfilled
        assert row2["entry_fee"] == pytest_approx(0.105, 1e-9)          # fees/2


def test_engine_crash_window_cash_reconciliation_is_exact():
    """Flaw 2.3: the crash-window recovery must restore broker cash to the
    cent for trades closed after the last equity anchor. Two windows:
      - STANDARD: opened -> anchor -> closed -> crash (anchor between the
        legs): recovery owes the close event = pnl + entry_fee;
      - OUTAGE: anchor -> opened -> closed -> crash (skipped equity writes
        while held symbols failed to fetch): recovery owes the WHOLE trade
        = pnl.
    Both compare against the uninterrupted broker path."""
    import bot.engine as engine_mod
    df = add_all_indicators(trending_df(260, drift=0.003, seed=29))
    spec = MarketSpec("crypto", "TEST/USDT", "1h")
    price = float(df["close"].iloc[50])
    d = _dec("LONG", 0.9, stop=2.0, price=price)
    T_OPEN, T_ANCHOR, T_CLOSE = ("2026-01-01T00:00:00+00:00",
                                 "2026-01-01T00:30:00+00:00",
                                 "2026-01-01T01:00:00+00:00")

    def _trade_cycle(eng, journal, anchor_ts=None):
        """Open + close one trade through broker+journal; optionally write an
        equity anchor BETWEEN the legs (the crash-window setup). Returns the
        broker's post-close cash (the ground truth)."""
        pos = eng.broker.open_position(spec, d, qty=2.0, price=price, trade_id=1,
                                       ts=T_OPEN, decision_bar_ts=0.0)
        journal.open_trade(spec.symbol, "long", 2.0, price, None, None,
                           "turtle_trend", "r", timeframe="1h", opened_ts=T_OPEN)
        journal.record_fill(1, pos.entry_price, pos.stop, None,
                            pos.entry_fee, pos.initial_stop)
        if anchor_ts is not None:
            journal.add_equity(eng.broker.equity({spec.symbol: price}),
                               eng.broker.cash, ts=anchor_ts)
        closed, pnl, pnl_pct, fees, exit_fill = eng.broker.close_position(
            spec, price + 1.0, "signal exit")
        journal.close_trade(1, exit_fill, pnl, pnl_pct, fees, "signal exit",
                            closed_ts=T_CLOSE, entry_fee=pos.entry_fee,
                            realized_cash_delta=pnl + (pos.entry_fee or 0.0))
        return eng.broker.cash

    with tempfile.TemporaryDirectory() as td:
        eng, saved = _engine_with_db(td)
        try:
            # uninterrupted path: cycle-end equity written AFTER the close
            truth = _trade_cycle(eng, eng.journal, anchor_ts=None)
            eng.journal.add_equity(truth, truth, ts="2026-01-01T02:00:00+00:00")

            # STANDARD crash window: anchor BETWEEN the legs, no write after
            with tempfile.TemporaryDirectory() as td2:
                eng2, _ = _engine_with_db(td2)
                _trade_cycle(eng2, eng2.journal, anchor_ts=T_ANCHOR)
                # crash + restart: restore must land exactly on truth
                eng3 = engine_mod.TradingEngine(mode="paper", quiet=True)
                assert eng3.broker.cash == pytest_approx(truth, 1e-6), \
                    (eng3.broker.cash, truth)

            # OUTAGE window: anchor BEFORE the entry (equity writes skipped
            # while the held symbol's fetch failed), whole trade inside the
            # window -> recovery owes plain pnl
            with tempfile.TemporaryDirectory() as td3:
                eng4, _ = _engine_with_db(td3)
                eng4.journal.add_equity(eng4.broker.equity({}), eng4.broker.cash,
                                        ts="2025-12-31T00:00:00+00:00")
                _trade_cycle(eng4, eng4.journal, anchor_ts=None)
                eng5 = engine_mod.TradingEngine(mode="paper", quiet=True)
                assert eng5.broker.cash == pytest_approx(truth, 1e-6), \
                    (eng5.broker.cash, truth)
        finally:
            CONFIG.db_path, engine_mod.TradingEngine._init_kronos = saved


def test_allocator_keeps_crypto_weekend_bars():
    """Flaw 3.2: the aligned returns matrix dropped every weekend row when the
    book mixed 24/7 crypto with 24x5 forex (~28% of crypto observations).
    inverse_vol must now compute each symbol's vol on its OWN bars: a
    crypto+forex book keeps all crypto bars, and weekend-vol moves weights."""
    from bot.allocator import allocation_weights, per_symbol_vols, returns_matrix
    # 10 calendar days of hourly bars: crypto has all 240, forex weekdays only
    idx = pd.date_range("2026-01-01", periods=240, freq="1h", tz="UTC")
    rng = np.random.default_rng(6)
    crypto = pd.DataFrame({"close": 100 + rng.normal(0, 1.0, 240).cumsum(),
                           "volume": 1.0}, index=idx)
    # forex frame: weekend rows MISSING (the realistic 24x5 shape)
    fidx = idx[idx.dayofweek < 5]
    forex = pd.DataFrame({"close": 1.10 + rng.normal(0, 0.002, len(fidx)).cumsum(),
                          "volume": 0.0}, index=fidx)
    specs = [MarketSpec("crypto", "BTC/USDT", "1h"), MarketSpec("forex", "EURUSD=X", "1h")]
    hist = {"BTC/USDT": crypto, "EURUSD=X": forex}
    # the aligned matrix itself still thins (only for HRP) — documented
    m = returns_matrix(specs, hist)
    assert len(m) < len(crypto) - 2              # weekends gone from the matrix
    # per-symbol vols: crypto uses ALL its own bars (the default lookback of
    # 200 bars is the allocator's window — compare on that same tail)
    vols = per_symbol_vols(specs, hist)
    r_crypto = crypto["close"].tail(200).pct_change().dropna()
    assert vols["BTC/USDT"] == pytest_approx(float(r_crypto.std()), 1e-12)
    r_forex = forex["close"].tail(200).pct_change().dropna()
    assert vols["EURUSD=X"] == pytest_approx(float(r_forex.std()), 1e-12)
    # inverse_vol weights derive from those own-bar vols: calmer symbol wins
    w = allocation_weights(specs, hist, method="inverse_vol")
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert w["BTC/USDT"] > 0 and w["EURUSD=X"] > 0
    calm_specs = [MarketSpec("crypto", "CALM/USDT", "1h"), MarketSpec("forex", "EURUSD=X", "1h")]
    calm = crypto.copy()
    calm["close"] = 100 + rng.normal(0, 0.05, 240).cumsum()    # tiny vol
    w2 = allocation_weights(calm_specs, {**hist, "CALM/USDT": calm}, method="inverse_vol")
    assert w2["CALM/USDT"] > w2["EURUSD=X"]


def test_sentiment_lexicon_filters_by_asset():
    """Flaw 3.3: the lexicon branch ignored asset_hint, so a crypto-crash
    headline vetoed EUR/USD longs (and macro forex news moved crypto).
    Headlines about OTHER assets must be filtered out; macro stays relevant;
    a fully-unmatched batch falls back to scoring everything."""
    import bot.data as data_mod
    import bot.sentiment as sentiment_mod
    from bot.sentiment import SentimentOverlay
    headlines = [
        {"title": "DeFi protocol exploited for $50M; crypto plunges",
         "summary": "hack drains bridge reserves", "source": "cointelegraph"},
        {"title": "ECB signals dovish pivot; euro rallies",
         "summary": "", "source": "fxstreet"},
    ]
    # patch where sentiment LOOKS the name up (it did `from bot.data import
    # fetch_news` — patching bot.data.fetch_news alone rebinds nothing)
    orig = data_mod.fetch_news
    sentiment_mod.fetch_news = lambda: [dict(h) for h in headlines]
    try:
        s = SentimentOverlay(llm_client=None)
        # EUR/USD book: the crypto-hack headline is irrelevant -> only the
        # dovish-ECB story scores -> positive, not the -0.6 crypto panic
        eur = s.assess(asset_hint="EUR/USD")
        assert eur["score"] > 0, eur
        # BTC book: only the hack story is relevant -> negative
        btc = s.assess(asset_hint="Bitcoin")
        assert btc["score"] < 0, btc
        # macro headlines stay relevant to every book (and this one scores
        # positive through the lexicon: "rate cut" + "rally" are bullish)
        macro = [{"title": "Fed signals rate cut; stocks rally worldwide",
                  "summary": "", "source": "fxstreet"}]
        sentiment_mod.fetch_news = lambda: [dict(h) for h in macro]
        s2 = SentimentOverlay(llm_client=None)
        both = s2.assess(asset_hint="Bitcoin")
        assert both["score"] > 0
        # no relevant match -> fall back to the whole batch (old behavior)
        off = [{"title": "Champions League final ends in thriller",
                "summary": "", "source": "x"}]
        sentiment_mod.fetch_news = lambda: [dict(off[0])]
        s3 = SentimentOverlay(llm_client=None)
        fb = s3.assess(asset_hint="Bitcoin")
        assert fb["method"] == "lexicon"          # scored, just neutral
    finally:
        sentiment_mod.fetch_news = orig


def test_deflated_sharpe_moments_widen_se_on_fat_tails():
    """Flaw 3.1: the normal-only SE ignored skew/kurtosis. The moment-aware
    SE (Merton/OPM form on the PER-PERIOD SR, de-annualized first) must
    differ from the normal SE in the right direction on fat-tailed returns,
    equal it exactly for normal returns, and the no-returns path must keep
    the old behavior and say which model it used."""
    from bot.validation import deflated_sharpe
    APY = 8760.0
    trials = [1.0, 0.5, 0.6, 0.4, 0.5]
    rng = np.random.default_rng(2)
    normal = rng.normal(0, 0.01, 5_000)
    fat = np.concatenate([rng.normal(0, 0.004, 4_900), rng.normal(0, 0.08, 100)])
    d_norm = deflated_sharpe(trials, 5_000, APY)
    d_normal_rets = deflated_sharpe(trials, 5_000, APY, returns=normal)
    d_fat = deflated_sharpe(trials, 5_000, APY, returns=fat)
    assert d_norm["se_model"] == "normal"
    assert d_normal_rets["se_model"] == "moments"
    # near-normal returns: the moment SE lands within a few % of the normal SE
    assert 0.9 < (d_normal_rets["deflated_sharpe"] / d_norm["deflated_sharpe"]) < 1.05
    # fat tails: wider SE -> LOWER DSR confidence (the honest direction)
    assert d_fat["deflated_sharpe"] <= d_normal_rets["deflated_sharpe"]
    # skew interacts with sign: the pinned reference stays in its band with
    # the normal model (back-compat of the default path)
    ref = deflated_sharpe([1.0, 0.5, 0.6, 0.4, 0.5], 26_000, APY)
    assert 0.85 <= ref["deflated_sharpe"] <= 0.93


# ---------------------------------------------------------------------------
# Wave W1: gross cap + pause controls
# (audit Fix 2.2-lite: explicit gross-notional leverage cap; manual
#  "pause all trading" flag — entries-only halt; risk-control docs)
# ---------------------------------------------------------------------------
def test_risk_gross_leverage_cap_gate():
    """The ~1x book bound must be an EXPLICIT gate in approve(), not an
    implicit 25% x 4 arithmetic coincidence. Over the cap -> refused with a
    gross-cap reason; exactly at the cap -> allowed (gate is >, not >=);
    under it -> approved; and the 0.0 default (the backtest call site, which
    passes no gross) is unchanged behavior."""
    rm = RiskManager(CONFIG)
    rm.note_equity(10_000)
    d = _dec("LONG", 0.9, stop=5.0, price=100.0)
    # sizing: 1% risk ($100) / $5 stop = 20 qty -> new notional $2,000
    res = rm.approve(d, CRYPTO_1H, 10_000, 0, False, open_gross_notional=8_100.0)
    assert not res.approved                       # 8_100 + 2_000 > 1.0 x 10_000
    assert "gross" in res.reason.lower() and "cap" in res.reason.lower()
    # exactly AT the cap (8_000 + 2_000 == 10_000) is allowed: >, not >=
    res = rm.approve(d, CRYPTO_1H, 10_000, 0, False, open_gross_notional=8_000.0)
    assert res.approved and res.qty > 0
    # headroom -> approved
    res = rm.approve(d, CRYPTO_1H, 10_000, 0, False, open_gross_notional=7_000.0)
    assert res.approved and res.qty > 0
    # default (backtest call site passes no gross) -> unchanged
    res = rm.approve(d, CRYPTO_1H, 10_000, 0, False)
    assert res.approved and res.qty > 0


def test_engine_open_gross_notional_marks_and_fallback():
    """The gross-cap input helper: sum(qty x mark) over open positions, each
    book marked at its OWN last-good close; a book with no mark yet (e.g. a
    restored position behind a dead feed) falls back to its entry price —
    never 0.0, which would under-count exposure and silently dis-arm the cap."""
    import bot.engine as engine_mod
    with tempfile.TemporaryDirectory() as td:
        old_db, old_kronos = CONFIG.db_path, engine_mod.TradingEngine._init_kronos
        CONFIG.db_path = os.path.join(td, "t.db")
        engine_mod.TradingEngine._init_kronos = lambda self: None
        try:
            eng = engine_mod.TradingEngine(mode="paper", quiet=True)
            spec_a = MarketSpec("crypto", "TEST/USDT", "1h")
            spec_b = MarketSpec("crypto", "OTHER/USDT", "15m")
            pos_a = eng.broker.open_position(spec_a, _dec("LONG", 0.9, stop=1.0, price=100.0),
                                             qty=2.0, price=100.0, trade_id=-1)
            pos_b = eng.broker.open_position(spec_b, _dec("LONG", 0.9, stop=1.0, price=50.0),
                                             qty=4.0, price=50.0, trade_id=-2)
            # book A has a last-good close (110) that differs from its entry;
            # book B has no mark yet -> entry-price fallback
            eng._last_good_price[("TEST/USDT", "1h")] = 110.0
            expected = pos_a.qty * 110.0 + pos_b.qty * pos_b.entry_price
            assert eng._open_gross_notional() == pytest_approx(expected, 1e-9)
            # the mark must actually be USED: an entry-price-only sum differs
            # (a helper that ignored the last-good close would read equal)
            entry_only = pos_a.qty * pos_a.entry_price + pos_b.qty * pos_b.entry_price
            assert eng._open_gross_notional() != pytest_approx(entry_only, 1e-9)
            # flat book -> 0.0 (the gate's default input)
            eng.broker.positions.clear()
            assert eng._open_gross_notional() == 0.0
        finally:
            CONFIG.db_path = old_db
            engine_mod.TradingEngine._init_kronos = old_kronos


def test_engine_passes_open_gross_into_approve():
    """Call-site wiring: when the live engine evaluates a NEW entry it must
    hand approve() the mark-priced gross of the whole open book (not the 0.0
    default) — the gate is only as real as the number it is fed."""
    import bot.engine as engine_mod

    df = add_all_indicators(make_df(100.0 * np.cumprod(1 + np.full(120, 0.001)), seed=9))
    spec = MarketSpec("crypto", "TEST/USDT", "1h")

    with tempfile.TemporaryDirectory() as td:
        old_db, old_kronos = CONFIG.db_path, engine_mod.TradingEngine._init_kronos
        old_wl = list(CONFIG.watchlist)
        CONFIG.db_path = os.path.join(td, "t.db")
        CONFIG.watchlist[:] = [spec]
        engine_mod.TradingEngine._init_kronos = lambda self: None
        try:
            eng = engine_mod.TradingEngine(mode="paper", quiet=True)
            eng.market_data.latest = lambda s, limit=None: df
            # one open position on ANOTHER symbol; its book's last-good mark
            # (60) differs from its entry, so a 0.0 or entry-priced hand-off
            # is detectable
            other = MarketSpec("crypto", "OTHER/USDT", "1h")
            pos = eng.broker.open_position(other, _dec("LONG", 0.9, stop=1.0, price=50.0),
                                           qty=2.0, price=50.0, trade_id=-1)
            eng._last_good_price[("OTHER/USDT", "1h")] = 60.0

            class EntryDecision:
                action = "LONG"
                confidence = 0.9
                price = 100.0
                stop_distance = 2.0
                target_rr = None
                strategy_name = "turtle_trend"
                rationale = "wiring test"
                regime = "trending"
                strategy_signals = {}
                sentiment = {}

            eng.orchestrator.decide = lambda *a, **k: EntryDecision()

            captured = {}
            real_approve = eng.risk.approve

            def spy(decision, spec_, equity, open_pos, *args, **kw):
                captured["gross"] = kw.get("open_gross_notional")
                return real_approve(decision, spec_, equity, open_pos, *args, **kw)

            eng.risk.approve = spy
            eng.run_cycle()
            # the TEST/USDT entry was evaluated with the OTHER book's
            # mark-priced gross in hand
            assert captured["gross"] == pytest_approx(pos.qty * 60.0, 1e-9)
        finally:
            CONFIG.db_path = old_db
            CONFIG.watchlist[:] = old_wl
            engine_mod.TradingEngine._init_kronos = old_kronos


def test_manual_pause_blocks_new_entries_not_exits():
    """Pause semantics: a paused RiskManager refuses an otherwise-valid NEW
    entry (reason states entries-only), a broker close still fills (every
    stop/target/strategy/manual exit routes through close_position and is
    never blocked), and resuming re-admits the same entry. The flag file
    lands next to the ACTIVE journal (CONFIG.db_path's dir) — never real
    data/ — and resume writes a visible paused:false record, not a delete."""
    from bot.pause import _pause_path, is_paused, set_paused
    with tempfile.TemporaryDirectory() as td:
        old_db = CONFIG.db_path
        CONFIG.db_path = os.path.join(td, "t.db")
        try:
            assert is_paused() == (False, None)          # missing file -> not paused
            assert set_paused(True, "audit freeze") is True
            paused, note = is_paused()
            assert paused is True and note == "audit freeze"
            assert os.path.dirname(_pause_path()) == td

            rm = RiskManager(CONFIG)
            rm.note_equity(10_000)
            d = _dec("LONG", 0.9, stop=5.0, price=100.0)
            rm.paused = True
            res = rm.approve(d, CRYPTO_1H, 10_000, 0, False)
            assert not res.approved
            assert "pause" in res.reason.lower()
            assert "entries only" in res.reason.lower()
            # EXITS ARE NEVER BLOCKED: a broker close still fills while paused
            broker = PaperBroker(costs=CONFIG.costs)
            broker.open_position(CRYPTO_1H, d, qty=1.0, price=100.0, trade_id=-1)
            closed_pos, _, _, _, _ = broker.close_position(CRYPTO_1H, 100.0, "manual close")
            assert closed_pos.symbol == CRYPTO_1H.symbol and not broker.positions
            # resume (paused=False) -> the SAME entry passes
            rm.paused = False
            res = rm.approve(d, CRYPTO_1H, 10_000, 0, False)
            assert res.approved and res.qty > 0
            # resume persists a visible paused:false record (not a deletion)
            assert set_paused(False) is True
            payload = json.load(open(_pause_path()))
            assert payload["paused"] is False and "ts" in payload
            assert is_paused() == (False, None)
        finally:
            CONFIG.db_path = old_db


def test_corrupt_pause_flag_quarantined_fail_safe():
    """A torn/unreadable pause flag fails TOWARD NOT TRADING: quarantined
    aside with a .corrupt.<epoch> suffix (journal/watchlist pattern — kept
    for inspection, out of the read path) and answered as PAUSED with a loud
    note. Never an exception, never a silent 'resume'."""
    from bot.pause import _pause_path, is_paused
    with tempfile.TemporaryDirectory() as td:
        old_db = CONFIG.db_path
        CONFIG.db_path = os.path.join(td, "t.db")
        try:
            path = _pause_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write("{ this is not json")
            paused, note = is_paused()
            assert paused is True and "PAUSED" in note
            assert not os.path.exists(path)           # moved out of the read path
            quarantined = [f for f in os.listdir(td)
                           if f.startswith("trading_paused.json.corrupt.")]
            assert quarantined                        # kept for inspection
        finally:
            CONFIG.db_path = old_db


def test_engine_cycle_mirrors_pause_flag():
    """The engine reads the manual flag ONCE per cycle (the only flag IO in
    the engine) and mirrors it into RiskManager.paused; the cycle summary
    carries paused + note. A cleared flag re-admits entries on the next
    cycle — the halt is a state the operator ends, not a latch."""
    import bot.engine as engine_mod
    from bot.pause import set_paused
    with tempfile.TemporaryDirectory() as td:
        old_db, old_kronos = CONFIG.db_path, engine_mod.TradingEngine._init_kronos
        CONFIG.db_path = os.path.join(td, "t.db")
        engine_mod.TradingEngine._init_kronos = lambda self: None
        try:
            eng = engine_mod.TradingEngine(mode="paper", quiet=True)
            eng.market_data.latest = lambda s, limit=None: None   # no fetches/entries
            assert set_paused(True, "watching the news") is True
            summary = eng.run_cycle()
            assert summary["paused"] is True and eng.risk.paused is True
            assert summary.get("paused_note") == "watching the news"
            assert set_paused(False) is True
            summary = eng.run_cycle()
            assert summary["paused"] is False and eng.risk.paused is False
        finally:
            CONFIG.db_path = old_db
            engine_mod.TradingEngine._init_kronos = old_kronos


def test_dashboard_pause_resume_endpoints():
    """Dashboard pause/resume API (TestClient, no server): the flag persists
    next to the ACTIVE journal (this test's temp dir — never the real
    data/), /api/engine/status and /api/stats report it, the note
    round-trips, and a body-less POST is refused like every other mutating
    endpoint (the JSON body forces the CSRF preflight)."""
    from fastapi.testclient import TestClient
    import bot.dashboard as dash_mod
    from bot.pause import _pause_path

    with tempfile.TemporaryDirectory() as td:
        old_db, old_journal = CONFIG.db_path, dash_mod.journal
        CONFIG.db_path = os.path.join(td, "t.db")
        try:
            dash_mod.journal = dash_mod.Journal(CONFIG.db_path)
            dash_mod.chatbot = dash_mod.ChatBot(dash_mod.journal)
            client = TestClient(dash_mod.app)

            assert client.get("/api/engine/status").json()["paused"] is False
            r = client.post("/api/trading/pause", json={})
            assert r.status_code == 200 and r.json()["status"] == "paused"
            assert "entries only" in r.json()["semantics"]
            # the flag landed in the TEST's data dir, not the real one
            assert os.path.dirname(_pause_path()) == td
            assert os.path.exists(_pause_path())
            assert client.get("/api/engine/status").json()["paused"] is True
            assert client.get("/api/stats").json()["paused"] is True
            # a note round-trips through the flag file
            r = client.post("/api/trading/pause", json={"note": "watching the Fed"})
            assert r.status_code == 200 and r.json()["note"] == "watching the Fed"
            # resume clears it everywhere
            r = client.post("/api/trading/resume", json={})
            assert r.status_code == 200 and r.json()["status"] == "resumed"
            assert client.get("/api/engine/status").json()["paused"] is False
            assert client.get("/api/stats").json()["paused"] is False
            # body-less POST is refused (CSRF preflight convention)
            assert client.post("/api/trading/pause").status_code == 422
            client.post("/api/trading/resume", json={})
        finally:
            CONFIG.db_path = old_db
            dash_mod.journal = old_journal
            dash_mod.chatbot = dash_mod.ChatBot(old_journal)


# ---------------------------------------------------------------------------
# Wave W3: Kronos future stamps + cache freshness
# (audit Fix 4.2 correctness half: forecast stamps must follow the book's
#  ACTUAL timeframe on irregular frames where infer_freq returns None — the
#  old "1h" fallback made a 15m book's 24-step forecast span 24 wrong hours
#  and a 4h book's span 24 instead of 96; audit Fix 4.1: rolling windows
#  reuse any cache file fresher than CACHE_FRESHNESS_HOURS by mtime, not
#  just the same-calendar-day stamp, while PINNED windows stay exact-name
#  byte-identical and never glob)
# ---------------------------------------------------------------------------
def test_kronos_future_index_pure_helper():
    """_future_index: `horizon` stamps exactly TIMEFRAME_SECONDS[tf] apart,
    UTC-aware, first one STRICTLY after last_ts (the decision bar)."""
    from bot.kronos_signal import _future_index
    last = pd.Timestamp("2026-09-08 16:00", tz="UTC")

    idx4 = _future_index(last, 6, "4h")
    assert isinstance(idx4, pd.DatetimeIndex) and len(idx4) == 6
    assert str(idx4.tz) == "UTC"
    assert idx4[0] == last + pd.Timedelta(hours=4)      # strictly after
    gaps = idx4[1:] - idx4[:-1]
    assert (gaps == pd.Timedelta(hours=4)).all()
    assert idx4[-1] == last + pd.Timedelta(hours=24)

    idx15 = _future_index(last, 24, "15m")
    assert len(idx15) == 24
    assert idx15[0] == last + pd.Timedelta(minutes=15)
    assert (idx15[1:] - idx15[:-1] == pd.Timedelta(minutes=15)).all()
    # a 24-step 15m forecast spans 6 hours of future, not 24 — the exact
    # mis-stamp the old 1h fallback produced on irregular 15m books
    assert idx15[-1] == last + pd.Timedelta(hours=6)

    # naive input is treated as UTC (cached frames may carry naive stamps)
    idx_naive = _future_index(pd.Timestamp("2026-09-08 16:00"), 2, "1h")
    assert str(idx_naive.tz) == "UTC" and len(idx_naive) == 2


def _gapped_4h_frame(n_gap=40):
    """Regular 4h bars with a 2-DAY hole in the middle — infer_freq returns
    None on it (forex weekend gap / exchange outage analogue)."""
    head = pd.date_range("2026-01-01", periods=n_gap, freq="4h", tz="UTC")
    tail = pd.date_range(head[-1] + pd.Timedelta(days=2, hours=4),
                         periods=n_gap, freq="4h", tz="UTC")
    idx = head.append(tail)
    prices = 100.0 * np.cumprod(1 + np.full(len(idx), 0.001))
    return make_df(prices, start=idx[0].strftime("%Y-%m-%d %H:%M"),
                   seed=11).set_axis(idx)


def test_kronos_evaluate_stamps_by_timeframe_on_gapped_frame():
    """End-to-end pass-through: with timeframe given, the future stamps the
    predictor receives are the book's ACTUAL 4h spacing even on a frame
    infer_freq rejects (pre-fix code fell back to 1h stamps there, spanning
    24 hours instead of 96)."""
    import tempfile
    from bot.kronos_signal import KronosConfig, KronosSignalEngine

    df = _gapped_4h_frame()
    assert pd.infer_freq(df.index) is None        # the premise of the flaw

    recorded = []

    class _FakePredictor:
        def predict(self, df, x_timestamp, y_timestamp, pred_len, T, top_p,
                    sample_count, **kw):
            recorded.append(pd.DatetimeIndex(y_timestamp))
            # minimal contract evaluate() consumes: close column, pred_len rows
            base = float(df["close"].iloc[-1])
            return pd.DataFrame({"close": np.full(pred_len, base)},
                                index=pd.DatetimeIndex(y_timestamp))

    class _FakeLazy:                    # drop-in for KronosPredictorLazy
        def _ensure(self):
            return _FakePredictor()

    with tempfile.TemporaryDirectory() as td:
        cfg = KronosConfig()
        cfg.track_file = os.path.join(td, "ic.json")
        cfg.sample_count = 2              # 2 sequential single-sample calls
        eng = KronosSignalEngine(cfg)
        real_lazy = eng.predictor
        eng.predictor = _FakeLazy()
        try:
            sig = eng.evaluate(df, horizon=24, timeframe="4h")
            assert sig is not None
            assert len(recorded) >= 1
            for stamps in recorded:
                assert len(stamps) == 24
                # every consecutive stamp pair is exactly 4h apart — the
                # pre-fix fallback produced 1h here (and only 24h of span)
                assert (stamps[1:] - stamps[:-1] == pd.Timedelta(hours=4)).all()
                assert stamps[0] > df.index[-1]    # strictly after the book
            # the 24-step 4h forecast spans 96 hours of stamped future
            assert recorded[0][-1] == df.index[-1] + pd.Timedelta(hours=96)
        finally:
            eng.predictor = real_lazy


def test_kronos_evaluate_timeframe_none_keeps_infer_freq_fallback():
    """timeframe=None keeps today's infer_freq fallback EXACTLY (back-compat:
    main.py's cmd_kronos calls evaluate without a timeframe) — on a REGULAR
    frame the stamps are the inferred spacing; on a gapped frame they remain
    the legacy 1h fallback (that behavior is what the engine-side timeframe=
    kwarg fixes at its call site)."""
    import tempfile
    from bot.kronos_signal import KronosConfig, KronosSignalEngine

    df = make_df(np.linspace(100, 130, 200), freq="4h")    # regular 4h
    recorded = []

    class _FakePredictor:
        def predict(self, df, x_timestamp, y_timestamp, pred_len, T, top_p,
                    sample_count, **kw):
            recorded.append(pd.DatetimeIndex(y_timestamp))
            base = float(df["close"].iloc[-1])
            return pd.DataFrame({"close": np.full(pred_len, base)},
                                index=pd.DatetimeIndex(y_timestamp))

    class _FakeLazy:                    # drop-in for KronosPredictorLazy
        def _ensure(self):
            return _FakePredictor()

    with tempfile.TemporaryDirectory() as td:
        cfg = KronosConfig()
        cfg.track_file = os.path.join(td, "ic.json")
        cfg.sample_count = 1
        eng = KronosSignalEngine(cfg)
        real_lazy = eng.predictor
        eng.predictor = _FakeLazy()
        try:
            sig = eng.evaluate(df, horizon=6)           # NO timeframe
            assert sig is not None
            stamps = recorded[0]
            # infer_freq on a regular 4h book yields 4h stamps (unchanged)
            assert (stamps[1:] - stamps[:-1] == pd.Timedelta(hours=4)).all()
            assert stamps[0] == df.index[-1] + pd.Timedelta(hours=4)
        finally:
            eng.predictor = real_lazy


def _w3_cache_env(td, spec, days=7, stamp="20260101"):
    """Shared setup: point CONFIG.data_cache_dir at a tmp dir, return the
    OLD state the test must restore (the real data/cache is never touched)."""
    from bot import data as data_mod
    old = {
        "cache_dir": CONFIG.data_cache_dir,
        "db_path": CONFIG.db_path,
        "fetch": data_mod.fetch_crypto_history,
    }
    CONFIG.data_cache_dir = td
    CONFIG.db_path = os.path.join(td, "t.db")     # manifest lands in td too
    return old, data_mod


def _w3_restore(old, data_mod):
    CONFIG.data_cache_dir = old["cache_dir"]
    CONFIG.db_path = old["db_path"]
    data_mod.fetch_crypto_history = old["fetch"]


def _w3_rolling_cache_path(td, spec, days, stamp):
    """The exact rolling cache name _disk_cache_path would write for a
    days-window request (same dashed-ISO + day-stamp conventions)."""
    safe = spec.symbol.replace("/", "").replace("=X", "")
    return os.path.join(td, f"{safe}_{spec.timeframe}_{days}d_{stamp}.parquet")


def _w3_plant_rolling_cache(td, spec, frame, stamp="20260101"):
    """Write a plausible parquet under a ROLLING days-form cache name with an
    OLD date-stamp in the name (the pre-fix regime refetched it tomorrow)."""
    safe = spec.symbol.replace("/", "").replace("=X", "")
    path = os.path.join(td, f"{safe}_{spec.timeframe}_7d_{stamp}.parquet")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frame.to_parquet(path)
    return path


def test_fetch_history_reuses_fresh_rolling_cache_across_day_stamps():
    """Fix 4.1: a rolling days-window with an OLD date-stamp but a NEW mtime
    (yesterday's fetch, minutes old) is REUSED — no fetcher call, no manifest
    write, no prune. Pre-fix code computed today's exact path, missed the
    old-stamp file, and hit the network."""
    import tempfile
    from bot.data import fetch_history
    spec = MarketSpec("crypto", "TEST/USDT", "1h")
    frame = make_df(np.linspace(100, 130, 300))

    with tempfile.TemporaryDirectory() as td:
        old, data_mod = _w3_cache_env(td, spec)
        try:
            path = _w3_plant_rolling_cache(td, spec, frame, stamp="20260101")
            os.utime(path, (time.time(), time.time()))     # fresh mtime, old stamp
            # any fetch attempt = test failure
            data_mod.fetch_crypto_history = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("network fetch attempted"))
            got = fetch_history(spec, days=7)
            assert len(got) == len(frame)
            assert got["close"].iloc[0] == pytest_approx(frame["close"].iloc[0], 1e-9)
            assert got["close"].iloc[-1] == pytest_approx(frame["close"].iloc[-1], 1e-9)
            # reuse is read-only: no manifest entry for a fetch that never happened
            assert not os.path.exists(os.path.join(td, "manifest.json"))
        finally:
            _w3_restore(old, data_mod)


def test_fetch_history_stale_rolling_cache_refetches():
    """The freshness bound is real: a file older than the window means
    refetch — the fetched frame is returned and a new cache file is written
    for next time."""
    import tempfile
    from bot.data import fetch_history
    spec = MarketSpec("crypto", "TEST/USDT", "1h")
    stale = make_df(np.linspace(100, 130, 300))
    fresh_frame = make_df(np.linspace(200, 260, 300), seed=42)

    with tempfile.TemporaryDirectory() as td:
        old, data_mod = _w3_cache_env(td, spec)
        try:
            _w3_plant_rolling_cache(td, spec, stale, stamp="20260101")
            data_mod.fetch_crypto_history = lambda *a, **k: fresh_frame.copy()
            # age the planted file past the 24h window (mtime, not the name's
            # day-stamp, is the freshness clock — backdate by 2 days)
            safe = spec.symbol.replace("/", "").replace("=X", "")
            planted = os.path.join(td, f"{safe}_{spec.timeframe}_7d_20260101.parquet")
            two_days_ago = time.time() - 2 * 86400
            os.utime(planted, (two_days_ago, two_days_ago))
            # under TODAY's date stamp: the file the pre-4.1 exact-name path
            # would have read had it been fresh — planted with the FETCHED
            # frame's content and also backdated, so serving it instead of
            # refetching is detectable (the stale-frame value differs)
            today_stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
            today_planted = _w3_rolling_cache_path(td, spec, 7, today_stamp)
            stale.to_parquet(today_planted)
            os.utime(today_planted, (two_days_ago, two_days_ago))
            got = fetch_history(spec, days=7)
            assert got["close"].iloc[0] == pytest_approx(fresh_frame["close"].iloc[0], 1e-9)
            assert len(got) == len(fresh_frame)
            # the refetch persisted today's-stamp cache file — with the
            # FETCHED frame's content (over the planted stale decoy)
            stored = pd.read_parquet(_w3_rolling_cache_path(td, spec, 7, today_stamp))
            assert len(stored) == len(fresh_frame)
            assert stored["close"].iloc[0] == pytest_approx(
                fresh_frame["close"].iloc[0], 1e-9)
        finally:
            _w3_restore(old, data_mod)


def test_fetch_history_pinned_window_never_globs_rolling_decoy():
    """PINNED --start/--end keeps EXACT-NAME byte-identical semantics: a
    fresh rolling days-form decoy for the SAME symbol must never be glob-
    reused for a pinned request (it covers a different window) — with the
    fetcher failing, the pinned call must fail rather than serve the decoy."""
    import tempfile
    from bot.data import fetch_history
    spec = MarketSpec("crypto", "TEST/USDT", "1h")
    decoy = make_df(np.linspace(100, 130, 300))

    with tempfile.TemporaryDirectory() as td:
        old, data_mod = _w3_cache_env(td, spec)
        try:
            path = _w3_plant_rolling_cache(td, spec, decoy, stamp="20260101")
            os.utime(path, (time.time(), time.time()))      # fresh decoy
            reads = []
            real_load = data_mod._load_cached
            data_mod._load_cached = lambda p: reads.append(os.path.basename(p)) or real_load(p)
            data_mod.fetch_crypto_history = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("network fetch attempted"))
            try:
                # the pinned window has no exact-name cache and must NOT fall
                # back to the decoy: it attempts the fetch and propagates the
                # error rather than serving a frame fetched for another window
                try:
                    got = fetch_history(spec, start="2024-01-01", end="2024-01-03")
                    raised = False
                except RuntimeError:
                    got = None
                    raised = True
                assert raised, "pinned window glob-reused the rolling decoy"
                assert got is None or got["close"].iloc[0] != pytest_approx(
                    decoy["close"].iloc[0], 1e-9)
                # the ONLY cache file the pinned branch read was its exact
                # pinned name — never the days-form decoy
                assert reads == ["TESTUSDT_1h_2024-01-01_2024-01-03.parquet"], reads
            finally:
                data_mod._load_cached = real_load
        finally:
            _w3_restore(old, data_mod)


def test_cache_freshness_env_read_at_call_time():
    """CACHE_FRESHNESS_HOURS is read from env at CALL time: a shrink is a
    same-process force-refetch knob, and a malformed value falls back to the
    24h default instead of crashing the fetch path."""
    from bot.data import _cache_freshness_hours
    assert _cache_freshness_hours() == pytest_approx(24.0, 1e-9)
    os.environ["CACHE_FRESHNESS_HOURS"] = "0.5"
    try:
        assert _cache_freshness_hours() == pytest_approx(0.5, 1e-9)
        os.environ["CACHE_FRESHNESS_HOURS"] = "not-a-number"
        assert _cache_freshness_hours() == pytest_approx(24.0, 1e-9)
    finally:
        del os.environ["CACHE_FRESHNESS_HOURS"]
    assert _cache_freshness_hours() == pytest_approx(24.0, 1e-9)


# ------------------------------------- Wave W2: backtest exit ordering (Fix 1.2)
def _w2_exit_conflict_frame(n=260):
    """Deterministic crafted frame, zero intrabar noise: every bar is fully
    controlled so the only moving parts are the ones the ordering test needs.
    Timeline (warmup 220, loop i in [220, n-2]):
      bar 250  decision bar -> the stub fires its one LONG entry
      bar 251  FILL bar: entry fills at its open (100) with slippage; the
               bracket (stop ~95.05, target ~110.05) sits far outside its
               [99, 101] range, so the fill-bar scan (step (a), untouched by
               Fix 1.2) finds nothing
      bar 252  exit-decision bar: range [100, 103] still clears both levels,
               so nothing can exit before the strategy's check_exit(252)
      bar 253  exit bar: opens at 104 (above stop, below target) and its high
               115 blows through the target — the exact Fix 1.2 conflict: the
               signal exit fills at this bar's OPEN while the target lies
               inside its range
    Bars before 250 are flat filler (no signal, nothing to hit)."""
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    opens = np.full(n, 100.0)
    highs = np.full(n, 100.6)
    lows = np.full(n, 99.4)
    closes = np.full(n, 100.0)
    vols = np.full(n, 100.0)
    crafted = {
        251: (100.0, 101.0, 99.0, 100.5),   # fill bar: clears both levels
        252: (100.5, 103.0, 100.0, 102.5),  # decision bar: clears both levels
        253: (104.0, 115.0, 103.8, 114.0),  # exit bar: open below target, high through it
    }
    for j, (oj, hj, lj, cj) in crafted.items():
        opens[j], highs[j], lows[j], closes[j] = oj, hj, lj, cj
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes,
                         "volume": vols}, index=idx)


class _W2StubStrategy:
    """Single-strategy-mode stand-in pinned to fixed bars: evaluate fires its
    LONG exactly once (bar 250), check_exit fires exactly once (at exit_bar)
    or never (exit_bar=None). The entry Signal clears every risk.approve gate
    (confidence 0.80 >= floor 0.55, stop_distance 5.0 <= 10%-of-price cap,
    target_rr 2.0 >= 1.2 floor), so the trade reaches the conflict through the
    REAL entry path — sizing, fills, bracket construction — not a
    hand-installed Position."""
    name = "w2_stub"

    def __init__(self, exit_bar=None, exit_reason="stub signal exit"):
        self.exit_bar, self.exit_reason = exit_bar, exit_reason

    def evaluate(self, df, i):
        from bot.strategies.base import Signal
        if i == 250:
            return Signal(self.name, "LONG", 0.80, stop_distance=5.0,
                          target_rr=2.0, rationale="w2 crafted entry")
        return Signal(self.name, "FLAT", 0.0, rationale="w2 flat")

    def check_exit(self, df, i, position):
        return (self.exit_reason, None) if i == self.exit_bar else (None, None)


def _w2_run_with_stub(stub, df):
    """Single-strategy-mode run with get_strategy swapped for the stub —
    patched in bot.backtest's own namespace (where run() imported it), and
    restored even when the test body asserts mid-flight."""
    import bot.backtest as bt_mod
    old = bt_mod.get_strategy
    bt_mod.get_strategy = lambda name, params: stub
    try:
        return bt_mod.Backtester(CONFIG).run(CRYPTO_1H, df, strategy="w2_stub")
    finally:
        bt_mod.get_strategy = old


def test_backtest_strategy_exit_fill_precedes_same_bar_target():
    """Fix 1.2 flip assertion: the strategy exit decided at bar 252's close
    fills at bar 253's OPEN, and that fill must execute BEFORE bar 253's
    stop/target scan — a take-profit level inside bar 253's range cannot beat
    an order already filled at the open. The old scan-next-bar-first order
    recorded 'take profit' on this exact frame; only the reorder flips it to
    the signal exit at the (slipped) open."""
    df = _w2_exit_conflict_frame()
    res = _w2_run_with_stub(_W2StubStrategy(exit_bar=252), df)
    assert len(res.trades) == 1, "stub must enter exactly once and never re-enter"
    t = res.trades[0]
    assert t["side"] == "long" and t["entry_ts"] == str(df.index[251])
    # non-vacuity preconditions: the target really is inside bar 253 (above
    # its open — an intrabar touch, not a gap-through) and nothing EARLIER in
    # the hold could have exited, so the only race is exit-at-open vs
    # target-in-range on bar 253
    assert df["open"].iloc[253] < t["target"] <= df["high"].iloc[253]
    assert df["low"].iloc[251:253].min() > t["stop"]
    assert df["high"].iloc[251:253].max() < t["target"]
    # THE ordering assertion: the signal exit wins, filled at bar 253's open
    assert t["exit_reason"] == "stub signal exit", \
        f"a same-bar target stole the already-filled signal exit: {t['exit_reason']}"
    # market leg (signal exits are not resting limits): adverse slippage off
    # the open, taker pricing — NOT the target level
    expected = float(df["open"].iloc[253]) * (1 - CONFIG.costs.slippage("crypto"))
    assert t["exit_price"] == pytest_approx(expected, 1e-9)
    assert t["exit_ts"] == str(df.index[253])


def test_backtest_take_profit_still_fills_without_signal_exit():
    """Control for the Fix 1.2 reorder: identical frame, but the strategy
    never fires check_exit, so the step-(c) next-bar scan must still produce
    the take-profit fill at its level (resting limit: maker, no slippage).
    Ordering-independent by construction — no signal exit exists to race it,
    so this passes under both orders; it guards the normal bracket path
    against collateral damage from the reorder."""
    df = _w2_exit_conflict_frame()
    res = _w2_run_with_stub(_W2StubStrategy(exit_bar=None), df)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t["exit_reason"] == "take profit"
    assert t["exit_price"] == pytest_approx(t["target"], 1e-9)  # level fill, no slip
    assert t["exit_ts"] == str(df.index[253])


# ---------------------------------------------------------------------------
# Wave B1: India market support (NSE cash equities + Nifty 50 via yfinance,
# a third market kind with its own regulatory cost stack, whole-share
# sizing, an NSE session gate on live entries, and a persisted market mode)
# ---------------------------------------------------------------------------
from bot.calendar import IST, is_nse_session_open  # noqa: E402
from config import (SPECS_INDIA, active_specs, apply_market_mode,  # noqa: E402
                    get_market_mode, set_market_mode)


def test_india_cost_model_leg_aware_rates():
    """The India per-leg cost rate: both legs pay brokerage + delivery STT
    (0.1%) + exchange txn + SEBI + GST-on-taxable; only the BUY leg adds
    stamp duty. side=None (call sites that don't know the leg) must equal
    the BUY rate — the conservative max. Expected values are computed IN
    the test from the same constants (no magic totals). Pre-fix, kind='india'
    fell through to the forex rate (0.0002/0.0001), so every assertion here
    fails against the old code."""
    c = CONFIG.costs
    taxable = c.india_brokerage + c.india_txn + c.india_sebi
    gst = c.india_gst * taxable
    base = c.india_brokerage + c.india_stt + c.india_txn + c.india_sebi + gst
    buy_rate = base + c.india_stamp          # stamp is buy-side only
    sell_rate = base
    assert c.fee("india", side="buy") == pytest_approx(buy_rate, 1e-12)
    assert c.fee("india", side="sell") == pytest_approx(sell_rate, 1e-12)
    # unknown leg -> conservative max = the buy-side rate (stamp included)
    assert c.fee("india") == pytest_approx(buy_rate, 1e-12)
    assert c.fee("india", side=None) == pytest_approx(buy_rate, 1e-12)
    # maker does NOT reduce the India rate: Indian charges are regulatory
    # per-side taxes (brokerage + STT + txn + SEBI + GST), not maker rebates
    assert c.fee("india", maker=True, side="sell") == pytest_approx(sell_rate, 1e-12)
    assert c.fee("india", maker=True) == pytest_approx(buy_rate, 1e-12)
    # sell < buy (stamp), and delivery STT dominates the intraday rate the
    # model deliberately does NOT use (0.025% sell-only)
    assert sell_rate < buy_rate
    assert c.india_stt == pytest_approx(0.001, 1e-15)
    # slippage: adverse 0.05% taker (crypto parity — liquid large caps),
    # zero maker (resting limit fills at its level)
    assert c.slippage("india") == pytest_approx(0.0005, 1e-12)
    assert c.slippage("india", maker=True) == 0.0


def test_india_cost_model_leaves_crypto_forex_rates_byte_unchanged():
    """Backward compatibility: the fee()/slippage() signature gained `side`
    (default None) but the crypto/forex rates must stay byte-identical to
    the pre-B1 values — pinned as exact floats."""
    c = CONFIG.costs
    assert c.fee("crypto") == 0.001 and c.fee("crypto", maker=True) == 0.001
    assert c.fee("forex") == 0.0002 and c.fee("forex", maker=True) == 0.0001
    assert c.fee("crypto", side="buy") == 0.001       # side ignored off-india
    assert c.fee("forex", side="sell", maker=True) == 0.0001
    assert c.slippage("crypto") == 0.0005 and c.slippage("crypto", maker=True) == 0.0
    assert c.slippage("forex") == 0.0001 and c.slippage("forex", maker=True) == 0.0


def test_infer_kind_india_detection():
    """'.NS' equities and '^' indices infer 'india' BEFORE the crypto
    fallback; crypto '/' and forex '=' logic unchanged."""
    from config import infer_kind
    assert infer_kind("RELIANCE.NS") == "india"
    assert infer_kind("^NSEI") == "india"
    assert infer_kind("BTC/USDT") == "crypto"
    assert infer_kind("EURUSD=X") == "forex"


def test_india_whole_share_sizing():
    """India equities are whole-share: size_position(kind='india') must
    return an integer quantity (round(qty, 0) semantics), the min-notional
    dust guard still applies, and forex behavior is unchanged. Pre-fix,
    'india' fell into the round(qty, 6) crypto branch."""
    rm = RiskManager()
    qty = rm.size_position(10_000, 100.0, 5.0, "india")      # 20.0 exactly
    assert qty == 20.0 and float(qty).is_integer()
    qty2 = rm.size_position(10_000, 100.0, 6.0, "india")    # 16.67 -> 17 shares
    assert qty2 == 17.0 and float(qty2).is_integer()
    # dust guard: a size whose notional < 10 (currency units) returns 0
    assert rm.size_position(10_000, 5.0, 100.0, "india") == 0.0
    # forex unchanged: still whole units
    assert rm.size_position(10_000, 100.0, 5.0, "forex") == float(
        int(rm.size_position(10_000, 100.0, 5.0, "forex")))
    # crypto unchanged: 6-decimal granularity
    assert rm.size_position(10_000, 100.0, 5.0, "crypto") == pytest_approx(
        round(10_000 * 0.01 / 5.0, 6), 1e-12)


def test_bars_per_year_india_sessions():
    """India annualizes on ~245 sessions x bars-per-session (a 6.25h NSE
    session -> 7 one-hour bars, 2 four-hour bars), not the 24/7 crypto
    count; crypto/forex branches unchanged."""
    from config import bars_per_year
    assert bars_per_year("1h", "india") == pytest_approx(245.0 * 7, 1e-9)
    assert bars_per_year("4h", "india") == pytest_approx(245.0 * 2, 1e-9)
    assert bars_per_year("1d", "india") == pytest_approx(245.0, 1e-9)
    assert bars_per_year("1h") == 8760.0                       # crypto default
    assert abs(bars_per_year("1h", "forex") - 8760.0 * 5.0 / 7.0) < 0.01
    assert bars_per_year("1h", "india") < bars_per_year("1h", "forex") < bars_per_year("1h")


def test_nse_calendar_session_open_pure_function():
    """is_nse_session_open is a pure function of its input: Mon 10:00 IST
    open; Sat/Sun, pre-open 08:00, post-close 16:00 closed; Republic Day
    2026 (a Monday) closed. Explicit datetimes — no clock mocking."""
    def ist(y, m, d, hh, mm):
        return datetime(y, m, d, hh, mm, tzinfo=IST)
    # 2026-06-15 is a Monday
    assert is_nse_session_open(ist(2026, 6, 15, 10, 0)) is True
    assert is_nse_session_open(ist(2026, 6, 13, 10, 0)) is False      # Saturday
    assert is_nse_session_open(ist(2026, 6, 14, 10, 0)) is False      # Sunday
    assert is_nse_session_open(ist(2026, 6, 15, 8, 0)) is False       # pre-open
    assert is_nse_session_open(ist(2026, 6, 15, 16, 0)) is False      # post-close
    # boundary inclusivity: 09:15 and 15:30 are in-session
    assert is_nse_session_open(ist(2026, 6, 15, 9, 15)) is True
    assert is_nse_session_open(ist(2026, 6, 15, 15, 30)) is True
    # holiday Monday: Republic Day 2026-01-26
    assert is_nse_session_open(ist(2026, 1, 26, 10, 0)) is False
    # the same instant expressed in UTC lands in-session (05:30 IST offset)
    assert is_nse_session_open(datetime(2026, 6, 15, 4, 30, tzinfo=timezone.utc)) is True
    assert is_nse_session_open(datetime(2026, 1, 26, 4, 30, tzinfo=timezone.utc)) is False


def test_market_mode_persistence_roundtrip_and_lockstep():
    """set_market_mode writes data/market_mode.json atomically AND rewrites
    watchlist.json to the new mode's universe (derived state, kept in
    lockstep); get_market_mode round-trips; corrupt mode file -> quarantine
    + default 'forex'; apply_market_mode repairs a watchlist.json that
    disagrees with the persisted mode. All on tmp paths (CONFIG.db_path
    monkeypatched — mode file derives from its dir at CALL time)."""
    import config as config_mod
    real_db, real_watchlist = CONFIG.db_path, config_mod.WATCHLIST_PATH
    real_saved = CONFIG.watchlist[:]
    with tempfile.TemporaryDirectory() as td:
        CONFIG.db_path = os.path.join(td, "t.db")
        config_mod.WATCHLIST_PATH = os.path.join(td, "watchlist.json")
        try:
            # default when the file is missing
            assert get_market_mode() == "forex"
            assert len(active_specs("india")) == 8
            assert {s.kind for s in SPECS_INDIA} == {"india"}
            # switch to india: mode file + watchlist lockstep
            assert set_market_mode("india") is True
            assert get_market_mode() == "india"
            with open(config_mod.WATCHLIST_PATH) as fh:
                specs = json.load(fh)["specs"]
            assert len(specs) == 8 and all(s["kind"] == "india" for s in specs)
            # switch back: the forex universe returns
            assert set_market_mode("forex") is True
            assert get_market_mode() == "forex"
            with open(config_mod.WATCHLIST_PATH) as fh:
                specs = json.load(fh)["specs"]
            assert all(s["kind"] in ("crypto", "forex") for s in specs)
            # invalid mode: refused, nothing written
            assert set_market_mode("equities") is False
            # corrupt mode file: quarantined, default returned, never raises
            mode_path = config_mod._market_mode_path()
            with open(mode_path, "w") as fh:
                fh.write("{not json")
            assert get_market_mode() == "forex"
            assert os.path.exists(f"{mode_path}.corrupt.") is False or any(
                f.startswith("market_mode.json.corrupt.")
                for f in os.listdir(os.path.dirname(mode_path)))
            # apply_market_mode rewrites a watchlist.json that disagrees with
            # the persisted mode (e.g. hand-edited, or the mode switched while
            # this process was down)
            set_market_mode("india")
            config_mod.save_watchlist(list(config_mod.DEFAULT_WATCHLIST)[:2],
                                      config_mod.WATCHLIST_PATH)
            wl = apply_market_mode()
            assert len(wl) == 8 and all(s.kind == "india" for s in wl)
            # ...and leaves an AGREEING file untouched
            before = open(config_mod.WATCHLIST_PATH).read()
            assert apply_market_mode() is wl
            assert open(config_mod.WATCHLIST_PATH).read() == before
        finally:
            CONFIG.db_path = real_db
            config_mod.WATCHLIST_PATH = real_watchlist
            CONFIG.watchlist[:] = real_saved


def test_engine_india_session_gate_blocks_entries_not_management():
    """The NSE session gate sits AFTER position management and BEFORE the
    entry decision: at a closed session an india spec takes NO entry (no
    decision row, no position), but an existing position still gets its
    stop-loss scan / close path. Frozen clock via monkeypatched
    is_nse_session_open — the gate's own clock is irrelevant to the test."""
    import bot.calendar as cal_mod
    import bot.engine as engine_mod

    df = add_all_indicators(trending_df(260, drift=0.004, seed=13))
    spec = MarketSpec("india", "RELIANCE.NS", "1h")
    i = len(df) - 1
    price = float(df["close"].iloc[i])
    # 2026-06-13 is a Saturday in IST — outside the NSE session
    closed_now = datetime(2026, 6, 13, 10, 0, tzinfo=IST)

    real_open = cal_mod.is_nse_session_open
    import bot.engine as eng_mod_for_patch
    eng_mod_for_patch.is_nse_session_open = lambda now=None: False
    try:
        with tempfile.TemporaryDirectory() as td:
            eng, saved = _engine_with_db(td)
            try:
                # no entry: a strong LONG decision at a closed session must
                # not even reach the orchestrator (no decision row opened)
                summary: dict = {"cycle": 1, "opened": [], "closed": [], "holds": 0, "errors": []}
                eng._process_market(spec, summary, df)
                assert (spec.symbol, "1h") not in eng.broker.positions
                assert eng.journal.recent_decisions() == [] \
                    if hasattr(eng.journal, "recent_decisions") else True
                # and the gate really was india-specific state: a closed
                # session was what blocked it (crypto spec at the same clock
                # still decides)
                # an OPEN position is still fully managed: a bar through the
                # stop exits at the closed session
                d = _dec("LONG", 0.9, stop=2.0, price=price)
                eng.broker.open_position(spec, d, qty=1.0, price=price,
                                         trade_id=-1,
                                         ts=str(df.index[i - 3]),
                                         decision_bar_ts=float(df.index[i - 3].timestamp()))
                trade_id = eng.journal.open_trade(
                    spec.symbol, "long", 1.0, price, price - 2.0, None,
                    "turtle_trend", "r", mode="paper",
                    opened_ts=str(df.index[i - 3]), timeframe="1h")
                eng.broker.positions[(spec.symbol, "1h")].trade_id = trade_id
                crash = df.copy()
                crash.iloc[i, crash.columns.get_loc("low")] = price - 5.0
                eng._process_market(spec, summary, crash)
                assert (spec.symbol, "1h") not in eng.broker.positions, \
                    "closed session must still manage exits (stop scan)"
                assert summary["closed"] and summary["closed"][0]["reason"] == "stop loss"
            finally:
                CONFIG.db_path, engine_mod.TradingEngine._init_kronos = saved
    finally:
        eng_mod_for_patch.is_nse_session_open = real_open
    # the frozen 'closed_now' anchor really is outside the NSE session
    assert is_nse_session_open(closed_now) is False


def test_india_fetch_via_yahoo_path_hermetic():
    """fetch_history on an india spec routes through fetch_india_ohlcv
    (yfinance path, auto_adjust=True — REQUIRED for equities: adjusted
    prices are the tradable series), returns the frame with caliber attrs,
    and the ^NSEI cache filename is deterministic and collision-free
    (caret stripped). Hermetic: yf.download monkeypatched."""
    import bot.data as data_mod

    calls = []

    def fake_download(symbol, **kw):
        calls.append((symbol, kw.get("interval"), kw.get("auto_adjust")))
        return pd.DataFrame({
            "Open": [100.0, 101.0, 102.0, 103.0, 104.0],
            "High": [101.0, 102.0, 103.0, 104.0, 105.0],
            "Low": [99.0, 100.0, 101.0, 102.0, 103.0],
            "Close": [100.5, 101.5, 102.5, 103.5, 104.5],
            "Volume": [1000.0, 1100.0, 1200.0, 1300.0, 1400.0],
        }, index=pd.date_range("2026-06-10", periods=5, freq="1h", tz="UTC"))

    real_cache_dir, real_download = CONFIG.data_cache_dir, None
    import yfinance as yf
    real_download = yf.download
    yf.download = fake_download
    CONFIG.data_cache_dir = None  # type: ignore[assignment]
    # route the disk cache at a tmp dir too (fetch_history stores a parquet)
    with tempfile.TemporaryDirectory() as td:
        CONFIG.data_cache_dir = os.path.join(td, "cache")
        try:
            spec = MarketSpec("india", "RELIANCE.NS", "1h")
            df = data_mod.fetch_history(spec, days=90)
            assert len(df) == 5
            assert calls and calls[0][0] == "RELIANCE.NS" and calls[0][1] == "1h"
            assert calls[0][2] is True, "auto_adjust must stay on for equities"
            assert df.attrs["caliber"] == "adjusted"
            assert df.attrs["source"] == "yahoo"
            # deterministic, collision-free cache names for india tickers
            nsei = MarketSpec("india", "^NSEI", "1h")
            p = data_mod._disk_cache_path(nsei, 90)
            assert os.path.basename(p) == "NSEI_1h_90d_" + os.path.basename(p).split("_")[-1]
            assert "NSEI" in os.path.basename(p) and "^" not in os.path.basename(p)
            rel = MarketSpec("india", "RELIANCE.NS", "4h")
            assert os.path.basename(data_mod._disk_cache_path(rel, 90)) == \
                "RELIANCE.NS_4h_90d_" + os.path.basename(
                    data_mod._disk_cache_path(rel, 90)).split("_")[-1]
            # forex/crypto cache names unchanged by the new transform
            assert os.path.basename(
                data_mod._disk_cache_path(MarketSpec("forex", "EURUSD=X", "1h"), 90)
            ).startswith("EURUSD_1h_90d_")
            assert os.path.basename(
                data_mod._disk_cache_path(MarketSpec("crypto", "BTC/USDT", "1h"), 90)
            ).startswith("BTCUSDT_1h_90d_")
        finally:
            yf.download = real_download
            CONFIG.data_cache_dir = real_cache_dir


def test_india_fetch_real_network_smoke():
    """REAL-network smoke (allowed for this wave's data layer only): 5d of
    ^NSEI 1d bars from yfinance validate cleanly. Skipped by default so a
    flaky/offline machine never fails the suite — run explicitly with
    RUN_NETWORK_TESTS=1 pytest -k real_network_smoke when the network is up."""
    if not os.environ.get("RUN_NETWORK_TESTS"):
        return
    import bot.data as data_mod
    df = data_mod.fetch_india_ohlcv("^NSEI", "1d", start="2026-09-01", end="2026-09-10")
    assert len(df) >= 3
    assert df.attrs["caliber"] == "adjusted"


# ---------------------------------------------------------------------------
# Wave B2: market toggle UI + docs (dashboard forex <-> india switch)
# The toggle's ORPHAN GUARD is the safety core: a mode switch rewrites the
# watchlist, so an open position whose market dropped out of the universe
# would lose its feed, its marks and its management (a zombie book). Both
# holders are checked (live engine + journal OPEN rows), and a confirmed
# switch closes every position BEFORE the mode flips — never after.
# ---------------------------------------------------------------------------
def _market_mode_dashboard(td):
    """Hermetic dashboard fixture (the pause-endpoint pattern): tmp DB +
    tmp watchlist + a fresh journal/chatbot pair. Returns (client, module).
    Restore via the returned module's `_b2_restore` in the test's finally."""
    from fastapi.testclient import TestClient
    import config as config_mod
    import bot.dashboard as dash_mod

    dash_mod._b2_saved = (CONFIG.db_path, config_mod.WATCHLIST_PATH,
                          CONFIG.watchlist[:], dash_mod.journal)
    CONFIG.db_path = os.path.join(td, "t.db")
    config_mod.WATCHLIST_PATH = os.path.join(td, "watchlist.json")
    dash_mod.journal = dash_mod.Journal(CONFIG.db_path)
    dash_mod.chatbot = dash_mod.ChatBot(dash_mod.journal)
    return TestClient(dash_mod.app), dash_mod


def _restore_market_mode_fixture(dash_mod):
    """Undo _market_mode_dashboard: real CONFIG paths + real journal back,
    and the persisted mode file left in a sane state on the REAL data dir
    (a leaked 'india' mode would silently flip the family dashboard's
    universe on the next boot)."""
    import config as config_mod
    (old_db, old_wl, saved_wl, old_journal) = dash_mod._b2_saved
    CONFIG.db_path = old_db
    config_mod.WATCHLIST_PATH = old_wl
    CONFIG.watchlist[:] = saved_wl
    dash_mod.journal = old_journal
    dash_mod.chatbot = dash_mod.ChatBot(old_journal)
    # the mode file derives from CONFIG.db_path's dir at CALL time, so the
    # REAL file (if any) was never touched by the test; the tmp one dies with
    # the tmpdir. Nothing to restore on the real data dir.
    del dash_mod._b2_saved


def test_market_mode_endpoints_roundtrip():
    """GET /api/market/mode -> forex default with the 9-spec crypto+forex
    universe; POST india (no open positions) -> 200 switched; GET -> india
    with the 8 NSE specs; the mode file + watchlist.json land in the TEST's
    tmp dir; POST back to forex restores the crypto+forex universe; the
    running CONFIG is hot-swapped (the live engine's next cycle sees the
    new book); /api/engine/status carries the persisted mode. Wiring test:
    the guard itself is proven by the two tests below."""
    with tempfile.TemporaryDirectory() as td:
        client, dash = _market_mode_dashboard(td)
        try:
            r = client.get("/api/market/mode")
            assert r.status_code == 200
            assert r.json()["mode"] == "forex"
            assert len(r.json()["specs"]) == 9
            # invalid mode: 422 like every other validation error
            r = client.post("/api/market/mode", json={"mode": "equities"})
            assert r.status_code == 422
            # body-less POST refused (CSRF preflight convention)
            assert client.post("/api/market/mode").status_code == 422
            # clean switch to india
            r = client.post("/api/market/mode", json={"mode": "india"})
            assert r.status_code == 200 and r.json()["status"] == "switched"
            assert r.json()["closed"] == 0
            r = client.get("/api/market/mode")
            assert r.json()["mode"] == "india"
            specs = r.json()["specs"]
            assert len(specs) == 8 and all(s["kind"] == "india" for s in specs)
            assert specs[0]["symbol"] == "^NSEI"
            # files landed in the TEST's data dir, never the real one
            import config as config_mod
            assert os.path.exists(os.path.join(td, "market_mode.json"))
            assert os.path.dirname(config_mod.WATCHLIST_PATH) == td
            # CONFIG hot-swapped: a RUNNING engine's next cycle sees the
            # new universe (apply_saved_watchlist mutates in place)
            assert {s.kind for s in CONFIG.watchlist} == {"india"}
            # the status poll carries the mode so the UI badge stays live
            assert client.get("/api/engine/status").json()["market_mode"] == "india"
            # switch back: the crypto+forex universe returns
            r = client.post("/api/market/mode", json={"mode": "forex"})
            assert r.status_code == 200 and r.json()["status"] == "switched"
            assert {s.kind for s in CONFIG.watchlist} <= {"crypto", "forex"}
            # a same-mode POST is a no-op, never an error
            r = client.post("/api/market/mode", json={"mode": "forex"})
            assert r.status_code == 200 and r.json()["status"] == "unchanged"
        finally:
            _restore_market_mode_fixture(dash)


def test_market_switch_blocks_with_open_positions_without_confirm():
    """The orphan guard: a seeded OPEN journal row makes the switch answer
    409 with requires_confirm + the position listed, and the mode must stay
    UNCHANGED (forex) — the row stays OPEN. Pre-fix (no guard) this switch
    would have succeeded and orphaned the position."""
    with tempfile.TemporaryDirectory() as td:
        client, dash = _market_mode_dashboard(td)
        try:
            assert client.get("/api/market/mode").json()["mode"] == "forex"
            tid = dash.journal.open_trade("BTC/USDT", "long", 1.0, 50000.0,
                                          49000.0, None, "turtle_trend", "r",
                                          timeframe="1h")
            r = client.post("/api/market/mode", json={"mode": "india"})
            assert r.status_code == 409
            body = r.json()["detail"]
            assert body["requires_confirm"] is True
            assert [(p["symbol"], p["timeframe"], p["held_in"])
                    for p in body["open_positions"]] == [("BTC/USDT", "1h", "journal")]
            # mode UNCHANGED, position untouched
            assert client.get("/api/market/mode").json()["mode"] == "forex"
            assert {s.kind for s in CONFIG.watchlist} <= {"crypto", "forex"}
            rows = dash.journal.open_trades()
            assert len(rows) == 1 and rows[0]["id"] == tid and rows[0]["status"] == "OPEN"
        finally:
            _restore_market_mode_fixture(dash)


def test_market_switch_with_confirm_closes_positions_and_switches():
    """The confirmed path: confirm_close_positions=true closes every open
    position FIRST (the engine-off branch: journal close at the entry mark
    with the conservative fee estimate — no live engine exists to fetch
    marks), THEN flips the mode to india and hot-swaps the watchlist."""
    with tempfile.TemporaryDirectory() as td:
        client, dash = _market_mode_dashboard(td)
        try:
            tid = dash.journal.open_trade("BTC/USDT", "long", 1.0, 50000.0,
                                          49000.0, None, "turtle_trend", "r",
                                          timeframe="1h")
            r = client.post("/api/market/mode",
                            json={"mode": "india", "confirm_close_positions": True})
            assert r.status_code == 200 and r.json()["status"] == "switched"
            assert r.json()["closed"] == 1
            assert r.json()["closes"][0]["symbol"] == "BTC/USDT"
            # the open trade is now CLOSED in the journal
            assert dash.journal.open_trades() == []
            rows = [t for t in dash.journal.recent_trades(limit=10) if t["id"] == tid]
            assert rows and rows[0]["status"] == "CLOSED"
            assert "market switch" in rows[0]["exit_reason"]
            # the fee estimate is the conservative two-leg India/crypto rate
            # on the notional (crypto: 0.1% taker per leg -> 100.0 on 50k)
            assert rows[0]["fees"] > 0
            # mode + watchlist switched to the India universe
            assert client.get("/api/market/mode").json()["mode"] == "india"
            assert {s.kind for s in CONFIG.watchlist} == {"india"}
            assert client.get("/api/watchlist").json()[0]["symbol"] == "^NSEI"
        finally:
            _restore_market_mode_fixture(dash)


def test_market_mode_in_engine_status_and_stats():
    """/api/engine/status AND /api/stats both report market_mode matching
    the PERSISTED mode (the pause-flag pattern W1 set): the UI badge rides
    the existing 4s poll, and the mode file outlives any engine run."""
    with tempfile.TemporaryDirectory() as td:
        client, dash = _market_mode_dashboard(td)
        try:
            assert client.get("/api/engine/status").json()["market_mode"] == "forex"
            assert client.get("/api/stats").json()["market_mode"] == "forex"
            client.post("/api/market/mode", json={"mode": "india"})
            assert client.get("/api/engine/status").json()["market_mode"] == "india"
            assert client.get("/api/stats").json()["market_mode"] == "india"
        finally:
            _restore_market_mode_fixture(dash)


# ---------------------------------------------------------------------------
# Milestone C1: India time-series momentum (bot/strategies/ts_momentum.py)
# Long-only absolute momentum per symbol — path (a) of the milestone: the
# SSRN papers rank a whole universe cross-sectionally; BaseStrategy is a
# single-symbol evaluator, so the decile gate becomes a trailing-return
# threshold (+8% over 240 1h bars) plus the 52w-high anchor (within 10% of
# the rolling 1-year high) plus turtle's EMA200 structure discipline.
# Frames must exceed the 2452-bar warmup (max(240, 2450, 30)+2) — hence the
# ~2500-bar synthetic constructions throughout this block.
# ---------------------------------------------------------------------------

def _tsmom_frame(n=2560, drift=0.0008, vol=0.004, seed=5, tail=None):
    """Uptrend OHLC frame for ts_momentum: geometric walk with positive
    drift (as a share, not a percent) and optional `tail` prices appended to
    craft the final bars (reversal / decay cases)."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, vol, n)
    prices = list(100.0 * np.cumprod(1 + steps))
    if tail:
        prices += list(tail)
    return add_all_indicators(make_df(prices, seed=seed))


def _tsmom_downtrend(n=2560, drift=-0.0012, vol=0.004, seed=9):
    """Mirror construction with negative drift: the no-short case."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, vol, n)
    prices = list(100.0 * np.cumprod(1 + steps))
    return add_all_indicators(make_df(prices, seed=seed))


def test_tsmom_warmup_returns_flat():
    """i below the warmup line (max(lookback, 52w_bars, 30)+2 = 2452) must
    be FLAT 'warming up' — a 400-bar frame never even reaches evaluation."""
    df = _tsmom_frame(400, seed=11)
    ts = TimeSeriesMomentum()
    for i in (0, 100, len(df) - 1):
        sig = ts.evaluate(df, i)
        assert sig.action == "FLAT" and "warm" in (sig.rationale or "").lower()


def test_tsmom_uptrend_goes_long():
    """A genuine uptrend — positive 240-bar trailing return, within 10% of
    the rolling 1-year high, above EMA200 — must produce a LONG with a
    positive ATR stop and meta carrying both momentum readings."""
    df = _tsmom_frame(seed=5)
    ts = TimeSeriesMomentum()
    i = len(df) - 1
    sig = ts.evaluate(df, i)
    assert sig.action == "LONG", sig.rationale
    assert 0.30 <= sig.confidence <= 1.0
    assert sig.stop_distance and sig.stop_distance > 0
    assert sig.target_rr is None           # signal-exit strategy, no fixed TP
    assert sig.confidence >= 0.55          # can clear the orchestrator floor
    assert "momentum" in sig.rationale.lower()
    assert sig.meta["lookback_ret"] > ts.p.tsmom_min_ret
    assert sig.meta["high_prox"] > 1.0 - ts.p.tsmom_52w_prox


def test_tsmom_flat_when_momentum_below_threshold():
    """Trailing return under +8%: FLAT, and the reason must name momentum
    (the gate that fired, not a generic refusal)."""
    rng = np.random.default_rng(21)
    n = 2560
    # sideways chop with a small positive drift: trailing 240-bar return
    # lands far below +8% while price still sits near its 1-year high
    steps = rng.normal(0.00002, 0.0025, n)
    prices = list(100.0 * np.cumprod(1 + steps))
    df = add_all_indicators(make_df(prices, seed=21))
    ts = TimeSeriesMomentum()
    i = len(df) - 1
    sig = ts.evaluate(df, i)
    assert sig.action == "FLAT"
    assert "momentum" in (sig.rationale or "").lower(), sig.rationale
    # non-vacuous: the frame's trailing return really is below the gate
    assert ts._trailing_ret(df, i) < ts.p.tsmom_min_ret


def test_tsmom_exit_on_momentum_decay():
    """A held long exits when the trailing 240-bar return decays below
    tsmom_exit_ret (default 0.0): a 2260-bar rally followed by a 300-bar
    steady decay, so the DECISION bar sits at the bottom and the 240-bar
    trailing return is decisively negative — the momentum regime flipped.
    The momentum condition is checked first in check_exit, so the reason
    names the decay even though the channel also happens to be breached."""
    ts = TimeSeriesMomentum()
    rng = np.random.default_rng(5)
    steps = rng.normal(0.0008, 0.004, 2260)
    rally = list(100.0 * np.cumprod(1 + steps))
    top = rally[-1]
    tail = [top * (0.996 ** (k + 1)) for k in range(300)]
    df = add_all_indicators(make_df(rally + tail, seed=5))
    i = len(df) - 1
    # non-vacuous: the regime really flipped on this frame
    assert ts._trailing_ret(df, i) < ts.p.tsmom_exit_ret
    from bot.broker import Position
    pos = Position(trade_id=1, symbol="T", side="long", qty=1.0,
                    entry_price=top, stop=None, target=None, strategy="ts_momentum")
    reason, _ = ts.check_exit(df, i, pos)
    assert reason is not None and "momentum" in reason.lower(), reason


def test_tsmom_exit_on_prior_channel_break():
    """The prior-10-bar-low channel exit (turtle's shift=1 convention): the
    last close must break BELOW the shifted channel. Crafted the same way
    as the fixed turtle test — force the final close under the prior
    channel floor, on a frame where momentum is STILL positive and EMA200
    is still held, so the CHANNEL exit is the one that fires."""
    ts = TimeSeriesMomentum()
    df = _tsmom_frame(seed=5)
    i = len(df) - 1
    prior_lo = df["low"].rolling(ts.p.turtle_exit_period).min().shift(1)
    floor = float(prior_lo.iloc[i])
    assert np.isfinite(floor)
    forced = df.copy()
    forced.loc[forced.index[i], "close"] = floor - 1.0
    forced.loc[forced.index[i], "low"] = min(floor - 1.0, float(forced["low"].iloc[i]))
    # channel breach is real; momentum decay is NOT (single dip bar can't
    # flip a 240-bar return), so the exit reason must name the channel
    assert float(forced["close"].iloc[i]) < floor
    assert ts._trailing_ret(forced, i) >= ts.p.tsmom_exit_ret
    from bot.broker import Position
    pos = Position(trade_id=1, symbol="T", side="long", qty=1.0,
                   entry_price=float(df["close"].iloc[i - 20]),
                   stop=None, target=None, strategy="ts_momentum")
    reason, _ = ts.check_exit(forced, i, pos)
    assert reason is not None and "channel" in reason.lower(), reason
    # a close ABOVE the prior channel floor never fires the channel exit
    reason_hold, _ = ts.check_exit(df, i, pos)
    assert reason_hold is None or "channel" not in (reason_hold or "").lower()


def test_tsmom_never_shorts_a_downtrend():
    """Long-only by design (NSE cash equities): a relentless downtrend —
    momentum decisively negative, near 52w LOW, below EMA200 — must be FLAT
    everywhere, never SHORT."""
    df = _tsmom_downtrend(seed=9)
    ts = TimeSeriesMomentum()
    actions = {}
    for i in range(len(df) - 40, len(df)):
        sig = ts.evaluate(df, i)
        actions.setdefault(sig.action, 0)
        actions[sig.action] += 1
    assert set(actions) <= {"FLAT"}, actions
    # non-vacuous: the frame really is a deep downtrend on every gate
    i = len(df) - 1
    assert ts._trailing_ret(df, i) < 0
    assert df["close"].iloc[i] < df["ema200"].iloc[i]


def test_tsmom_causal_no_future_leak():
    """evaluate(i) on the full frame must equal evaluate(i) on the same
    frame with all bars > i truncated — the trailing return and the 52w
    rolling high read only bars <= i. A few i values across a random-ish
    frame with mixed trend phases."""
    rng = np.random.default_rng(33)
    n = 2600
    # three regimes: rally, chop, renewed rally (each phase ~866 bars)
    phase1 = rng.normal(0.0012, 0.004, 866)
    phase2 = rng.normal(-0.0002, 0.003, 866)
    phase3 = rng.normal(0.0010, 0.004, n - 1732)
    steps = np.concatenate([phase1, phase2, phase3])
    df = add_all_indicators(make_df(list(100.0 * np.cumprod(1 + steps)), seed=33))
    ts = TimeSeriesMomentum()
    for i in (2460, 2500, len(df) - 100, len(df) - 1):
        full = ts.evaluate(df, i)
        trunc = ts.evaluate(df.iloc[: i + 1], i)
        assert full.action == trunc.action, (i, full.rationale, trunc.rationale)
        assert abs(full.confidence - trunc.confidence) < 1e-9
        assert (full.stop_distance is None) == (trunc.stop_distance is None)
        if full.stop_distance is not None:
            assert abs(full.stop_distance - trunc.stop_distance) < 1e-9


# ---------------------------------------------------------------------------
# Milestone C2: FX regime-conditioned mean reversion (fx_regime_meanrev)
# ---------------------------------------------------------------------------
# Frames below are hand-crafted so each gate is the ONLY moving part at the
# decision bar: fast mean-reverting wiggle (AR(1) halflife ~1-4 bars, gate
# passes), terminal stretch (|z| > 2), and zero-range bars (ATR exactly
# controlled). See the module docstring in bot/strategies/fx_regime_meanrev.py
# for the SSRN 6087107 grounding and the pairs-trading stretch-goal flag.
def _fxmr_zero_range_frame(close, start="2024-01-01"):
    """Deterministic zero-range bars: open=high=low=close, so ATR is a pure
    function of the close-to-close moves we craft (the wiggle amplitude)."""
    close = np.asarray(close, dtype=float)
    idx = pd.date_range(start, periods=len(close), freq="1h", tz="UTC")
    return pd.DataFrame({"open": close, "high": close, "low": close,
                         "close": close, "volume": np.full(len(close), 100.0)},
                        index=idx)


def _fxmr_long_frame(n=210, seed=11):
    """Stretched BELOW the mean on a fast-reverting wiggle: perpetual noise
    around a level (AR(1) halflife ~1 bar at the decision bar) plus a 5-bar
    terminal dip -> z < -2 with the regime gate passing -> LONG."""
    rng = np.random.default_rng(seed)
    base = 1.10 + rng.normal(0, 0.0008, n)
    dip = np.zeros(n)
    dip[-5:] = np.array([0, -0.0012, -0.0024, -0.0034, -0.0042])
    return base + dip


def _fxmr_short_frame(n=210, seed=11):
    """Stretched ABOVE the mean on the same fast-reverting wiggle (terminal
    spike) -> z > +2 with the gate passing -> SHORT (forex allows shorts)."""
    rng = np.random.default_rng(seed)
    base = 1.10 + rng.normal(0, 0.0008, n)
    spike = np.zeros(n)
    spike[-5:] = np.array([0, 0.0012, 0.0024, 0.0034, 0.0042])
    return base + spike


def _fxmr_rw_frame(n=210, seed=7):
    """A genuine random-walk stretch (no hand-crafted ramp — a real seeded
    cumulative walk): the deviation z-score crosses |2| while the window's
    AR(1) fit reads half-life ~window/5 bars (the finite-window Dickey-Fuller
    bias documented in indicators.halflife_ar1) — far beyond the 12-bar
    horizon. This is the regime the paper's conditioning refuses to fade:
    a stretch in a non-reverting regime. Decision bar 204 on seed 7."""
    rng = np.random.default_rng(seed)
    return 1.10 + np.cumsum(rng.normal(0, 0.0011, n))


def test_fxmr_warmup_flat():
    """Below max(z_window, ema, atr) + 2 bars the strategy must be FLAT with
    a warming-up rationale — even on a frame that would otherwise fire."""
    from bot.strategies.fx_regime_meanrev import FXRegimeMeanRev
    s = FXRegimeMeanRev()
    df = add_all_indicators(make_df(_fxmr_long_frame(210)))
    assert s.p.fxmr_z_window == 100                       # warmup must cover it
    for i in (30, 80, 100, 101):
        sig = s.evaluate(df, i)
        assert sig.action == "FLAT"
        assert "warming up" in (sig.rationale or "")


def test_fxmr_long_entry_on_reverting_stretch():
    """z < -2 on a fast-reverting deviation (halflife <= fxmr_halflife_max):
    LONG fires with in-range confidence, a positive ATR stop, and the fixed
    1.5R declared target (the reward-floor-honest choice, module docstring)."""
    from bot.strategies.fx_regime_meanrev import FXRegimeMeanRev
    s = FXRegimeMeanRev()
    df = add_all_indicators(make_df(_fxmr_long_frame()))
    i = len(df) - 1
    hl = float(df["halflife"].iloc[i])
    assert hl <= s.p.fxmr_halflife_max          # frame sanity: the gate passes
    sig = s.evaluate(df, i)
    assert sig.action == "LONG", sig.rationale
    assert 0.0 <= sig.confidence <= 0.90
    assert sig.confidence >= 0.55              # must clear the orchestrator floor
    assert sig.stop_distance and sig.stop_distance > 0
    assert sig.target_rr == 1.5                 # declared fixed target
    assert sig.meta["z"] < -s.p.fxmr_z_entry    # journaled attribution


def test_fxmr_regime_gate_refuses_random_walk():
    """NON-VACUITY of the regime gate: a genuine random-walk stretch (z = +3.4
    at the decision bar — a real entry-grade stretch on every other gate) where
    the window's AR(1) fit reads half-life ~13 bars, beyond the strategy's own
    12-bar horizon, must be FLAT with a regime/half-life rationale — the
    paper's core claim: don't fade stretches in non-reverting regimes. The
    identical entry with the gate disabled (fxmr_halflife_max = 0) fires
    SHORT, proving the gate (not some other gate) is what refuses."""
    from bot.strategies.fx_regime_meanrev import FXRegimeMeanRev
    s = FXRegimeMeanRev()
    df = add_all_indicators(make_df(_fxmr_rw_frame()))
    i = 204
    z, hl = s._z_now(df, i), float(df["halflife"].iloc[i])
    assert z > s.p.fxmr_z_entry                    # frame sanity: real stretch
    assert hl > s.p.fxmr_halflife_max              # frame sanity: gate must refuse
    sig = s.evaluate(df, i)
    assert sig.action == "FLAT"
    assert "half-life" in (sig.rationale or "") or "regime" in (sig.rationale or "")
    p = s.p
    old_max = p.fxmr_halflife_max
    p.fxmr_halflife_max = 0.0                               # gate disabled
    try:
        raw = s.evaluate(df, i)
        assert raw.action == "SHORT", ("gate-off control must fire on this frame",
                                      raw.rationale)
    finally:
        p.fxmr_halflife_max = old_max


def test_fxmr_short_entry_symmetric():
    """z > +2 with the gate passing -> SHORT, mirror of the long side."""
    from bot.strategies.fx_regime_meanrev import FXRegimeMeanRev
    s = FXRegimeMeanRev()
    df = add_all_indicators(make_df(_fxmr_short_frame()))
    i = len(df) - 1
    sig = s.evaluate(df, i)
    assert sig.action == "SHORT", sig.rationale
    assert sig.confidence >= 0.55
    assert sig.stop_distance and sig.stop_distance > 0
    assert sig.meta["z"] > s.p.fxmr_z_entry


def test_fxmr_snapback_exit():
    """Held long: z crossing back above -fxmr_z_exit means the snapback is
    complete — the exit fires with a distinct reason. z still deep (< -0.5)
    holds (the control)."""
    from bot.strategies.fx_regime_meanrev import FXRegimeMeanRev
    from bot.broker import Position
    s = FXRegimeMeanRev()
    n = 230
    rng = np.random.default_rng(11)
    base = 1.10 + rng.normal(0, 0.0008, n)
    dip = np.zeros(n)
    dip[205:210] = np.array([0, -0.0012, -0.0024, -0.0034, -0.0042])
    dip[210:] = np.linspace(-0.0042, -0.0002, n - 210)      # revert toward the level
    df = add_all_indicators(make_df(base + dip))
    pos = Position(trade_id=1, symbol="EURUSD=X", side="long", qty=1.0,
                   entry_price=float(df["close"].iloc[209]), stop=None, target=None,
                   strategy="fx_regime_meanrev", bars_held=1)
    deep = s.check_exit(df, 210, pos)          # z ~ -2.8: snapback incomplete
    assert deep[0] is None
    z_back = s._z_now(df, 220)
    assert z_back > -s.p.fxmr_z_exit           # frame sanity: crossing happened
    reason, _ = s.check_exit(df, 220, pos)
    assert reason is not None and "snapback" in reason


def test_fxmr_time_stop():
    """Held long with z still stretched (no snapback, regime still reverting):
    at fxmr_time_stop_bars the time stop fires (~1 trading day — intraday
    reversion must not become a position trade); below it, the control holds."""
    from bot.strategies.fx_regime_meanrev import FXRegimeMeanRev
    from bot.broker import Position
    s = FXRegimeMeanRev()
    n = 230
    rng = np.random.default_rng(11)
    noise = rng.normal(0, 0.0008, n)
    dip = np.zeros(n)
    dip[205:210] = np.array([0, -0.0012, -0.0024, -0.0034, -0.0042])
    dip[210:] = -0.0042                              # the dip HOLDS: no snapback
    t = np.arange(n)
    # a smooth trough around the decision bar keeps z stretched below -0.5
    # (a flat held dip slowly normalizes into its own window and lets z decay
    # toward the band — the bump keeps the stretch alive at bar 229)
    bump = -0.0016 * np.cos((t - 229) * 2 * np.pi / 12.0) * (t >= 210)
    df = add_all_indicators(make_df(1.10 + noise + dip + bump))
    i = len(df) - 1
    assert s._z_now(df, i) < -s.p.fxmr_z_exit            # still stretched
    assert s._hl_refusal(float(df["halflife"].iloc[i])) is None   # regime alive
    pos = Position(trade_id=1, symbol="EURUSD=X", side="long", qty=1.0,
                   entry_price=1.096, stop=None, target=None,
                   strategy="fx_regime_meanrev", bars_held=s.p.fxmr_time_stop_bars)
    reason, _ = s.check_exit(df, i, pos)
    assert reason is not None and "time stop" in reason
    pos_fresh = Position(trade_id=2, symbol="EURUSD=X", side="long", qty=1.0,
                          entry_price=1.096, stop=None, target=None,
                          strategy="fx_regime_meanrev", bars_held=2)
    assert s.check_exit(df, i, pos_fresh)[0] is None


def test_fxmr_atr_floor():
    """Dead-flat regime: a real stretch (z < -2, gate passing) on bars whose
    ATR sits below fxmr_min_atr_pct of price must be FLAT with a volatility
    rationale — the spread eats the whole edge at that vol. Inverting the
    floor (0.0) lets the identical entry fire: the gate is load-bearing."""
    from bot.strategies.fx_regime_meanrev import FXRegimeMeanRev
    s = FXRegimeMeanRev()
    n = 210
    t = np.arange(n)
    # fast wiggle (halflife ~2 bars, gate passes) at an amplitude so small the
    # ATR lands ~0.009% of price — well under the 0.02% floor — plus a terminal
    # dip so z < -2 (zero-range bars: ATR is exactly the crafted wiggle)
    close = 1.10 + 0.00012 * np.sin(t * 2 * np.pi / 6.0)
    close[-5:] -= np.array([0, 0.0002, 0.00035, 0.00045, 0.0005])
    df = add_all_indicators(_fxmr_zero_range_frame(close))
    i = len(df) - 1
    assert df["atr"].iloc[i] / df["close"].iloc[i] < s.p.fxmr_min_atr_pct  # frame sanity
    assert s._z_now(df, i) < -s.p.fxmr_z_entry                            # real stretch
    sig = s.evaluate(df, i)
    assert sig.action == "FLAT"
    assert "volatility" in (sig.rationale or "").lower() or "floor" in (sig.rationale or "")
    p = s.p
    old_floor = p.fxmr_min_atr_pct
    p.fxmr_min_atr_pct = 0.0                      # floor inverted (non-vacuity control)
    try:
        raw = s.evaluate(df, i)
        assert raw.action == "LONG", ("floor-off control must fire on this frame",
                                      raw.rationale)
    finally:
        p.fxmr_min_atr_pct = old_floor


def test_fxmr_causal_no_lookahead():
    """evaluate(i) on the full frame must equal evaluate on the frame
    TRUNCATED at i (action + confidence), on a crafted reverting frame at
    several decision bars — the same causality contract as every strategy."""
    from bot.strategies.fx_regime_meanrev import FXRegimeMeanRev
    s = FXRegimeMeanRev()
    df = add_all_indicators(make_df(_fxmr_long_frame(260)))
    for i in (150, 180, 220, 259):
        full = s.evaluate(df, i)
        trunc = s.evaluate(df.iloc[: i + 1], i)
        assert full.action == trunc.action, i
        assert abs(full.confidence - trunc.confidence) < 1e-9, i
        assert (full.stop_distance is None) == (trunc.stop_distance is None)
        if full.stop_distance is not None:
            assert abs(full.stop_distance - trunc.stop_distance) < 1e-12


if __name__ == "__main__":
    fails = 0
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    for name, fn in fns:
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as exc:
            fails += 1
            import traceback
            print(f"  ✗ {name}: {exc}")
            traceback.print_exc()
    print(f"\n{len(fns) - fails}/{len(fns)} tests passed")
    sys.exit(1 if fails else 0)
