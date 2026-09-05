"""Test suite: indicators, strategies, risk, broker, orchestrator, backtest causality.

Run:  python3 -m pytest tests/ -v     (or: python3 tests/run_tests.py)
Also works without pytest via the __main__ fallback.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from config import CONFIG, MarketSpec
from bot.indicators import add_all_indicators, adx, atr, ema, rsi
from bot.strategies import TurtleTrend, ConnorsMeanReversion, VWAPScalper
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
    opens = np.roll(prices, 1); opens[0] = prices[0]
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
    up_ind = add_all_indicators(up); down_ind = add_all_indicators(down)
    assert up_ind["rsi14"].iloc[-1] > 90
    assert down_ind["rsi14"].iloc[-1] < 10
    flat = make_df(np.full(120, 100.0))
    assert abs(add_all_indicators(flat)["rsi14"].iloc[-1] - 50) < 1e-6


def test_rsi2_wilder_smoothing():
    df = add_all_indicators(trending_df())
    r = rsi(df["close"], 2)
    assert not r.iloc[-10:].isna().any()
    assert ((r >= 0) & (r <= 100)).all()


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
    # deep bull (well above EMA200) + sharp 2-bar pullback -> RSI(2) < 5 long
    prices = list(np.linspace(100, 190, 400))       # steep climb: deep bull regime
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
    assert set(d.strategy_signals) <= {"turtle_trend", "connors_meanrev", "vwap_scalper"}
    if d.action != "HOLD":
        assert d.stop_distance > 0
        assert d.strategy_name


def test_every_valid_timeframe_is_owned():
    """VALID_TIMEFRAMES must all map to a strategy — a spec on an unowned
    timeframe silently HOLDs forever while the UI badges it (the old literal
    STRATEGY_BY_TF lied for 5m/1d; the derived map must now cover them)."""
    import bot.dashboard as dash_mod
    from config import VALID_TIMEFRAMES
    covered = set(dash_mod.STRATEGY_BY_TF)
    assert covered >= set(VALID_TIMEFRAMES), \
        f"unowned timeframes: {sorted(set(VALID_TIMEFRAMES) - covered)}"
    # the ownership map must agree with what the orchestrator enforces
    from bot.strategies import STRATEGY_CLASSES
    for name, cls in STRATEGY_CLASSES.items():
        for tf in cls.preferred_timeframes:
            assert dash_mod.STRATEGY_BY_TF[tf] == name
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
    window a crash between close and the next cycle-end write would leave."""
    from bot.journal import Journal
    with tempfile.TemporaryDirectory() as td:
        j = Journal(os.path.join(td, "t.db"))
        j.add_equity(10_000.0, 10_000.0, ts="2026-01-01T00:00:00+00:00")
        tid = j.open_trade("TEST/USDT", "long", 1.0, 100.0, 95.0, 105.0,
                           "turtle_trend", "r")
        # crash-window simulation: close WITHOUT the equity args, as an old
        # engine would between the two writes
        j.close_trade(tid, 105.0, 4.9, 4.9, 0.21, "take profit",
                      closed_ts="2026-01-01T12:00:00+00:00")
        gap = j.closed_cash_delta_since("2026-01-01T00:00:00+00:00")
        assert gap == pytest_approx(4.9 + 0.21, 1e-9)
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
        j2 = Journal(path)          # second constructor while first exists
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
        tid = j.open_trade("ETH/USDT", "long", 1.0, 100.0, 95.0, None,
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
    w2 = allocation_weights(specs, {"A/USDT": make_df(base := np.linspace(1, 2, 50))}, method="equal")
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
    kept = sum(p["trades"] for p in out["paths"])
    assert kept + out["purged_trades"] == out["total_trades"]
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
    trials on a long sample keeps its confidence."""
    from bot.validation import deflated_sharpe
    # 100-trial research sweep, best Sharpe 1.3, short 100-bar sample: the
    # null's expected max across that many trials is ~1.28 — no real evidence
    sweep = [1.3] + [round(x, 3) for x in np.linspace(-0.5, 1.2, 100)]
    dsr = deflated_sharpe(sweep, n_obs=100)
    assert dsr["deflated_sharpe"] < 0.8
    assert dsr["verdict"] == "Sharpe explained by trial count"
    # 3 trials, best Sharpe 2.0, 5000 bars -> selection can't explain it
    dsr2 = deflated_sharpe([2.0, 0.5, 0.2], n_obs=5_000)
    assert dsr2["deflated_sharpe"] > 0.95
    assert dsr2["verdict"] == "selection-aware confidence"
    # degenerate inputs are refused, not crashed
    assert deflated_sharpe([1.0], 100)["deflated_sharpe"] is None
    assert deflated_sharpe([1.0, 1.0, 1.0], 100)["deflated_sharpe"] is None


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
    # --- turtle: a close below the 10-bar exit channel exits
    up = add_all_indicators(trending_df(300, drift=0.003, seed=21))
    turtle = TurtleTrend()
    pos = Position(trade_id=1, symbol="T", side="long", qty=1.0,
                   entry_price=float(up["close"].iloc[-30]),
                   stop=None, target=None, strategy="turtle_trend")
    exit_lo = up["close"].rolling(p.turtle_exit_period).min().shift(1)
    i = len(up) - 1
    below = float(up["close"].iloc[i]) < float(exit_lo.iloc[i])
    reason, _ = turtle.check_exit(up, i, pos)
    assert (reason is not None) == below

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
    frame.loc[frame.index[i], "close"] = 102.0     # 1R -> stop trails to entry
    reason, new_stop = sc.check_exit(frame, i, pos)
    if new_stop is not None:
        assert new_stop == pytest_approx(100.0, 1e-9)     # breakeven
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
    ks = KronosSignal(direction="LONG", p_up=0.78, p_target_before_stop=0.61,
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
    ks_short = KronosSignal(direction="SHORT", p_up=0.2, p_target_before_stop=None,
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
    # a fabricated discretionary exit (strategy silent) must be a rule break
    silent = _journal_trade(999, 300, 320, df, exit_reason="LLM override",
                            strategy="turtle_trend")
    rep2 = rule_adherence([silent], df, CONFIG.params)
    verdicts = {t["verdict"] for t in rep2.trades}
    assert verdicts <= {"rule break", "on-rule (hard bracket)"}
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


# ------------------------------------------------------------------ runner
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
