"""Real taker order flow (bot/flow.py) and the strategy built on it."""
import inspect

import numpy as np
import pandas as pd

import bot.flow as flow
from bot.strategies import CANDIDATE_STRATEGIES, get_strategy


def _bars(n=400, start="2026-01-01", freq="5min", drift=0.0, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.001, n)))
    c = pd.Series(close, index=idx)
    return pd.DataFrame({"open": c.shift(1).fillna(c.iloc[0]), "high": c * 1.001,
                         "low": c * 0.999, "close": c, "volume": 100.0})


def test_attach_aligns_the_taker_fraction_on_bar_open_time():
    df = _bars(10)
    hist = pd.DataFrame({"volume": [50.0] * 10, "taker_buy_volume": [10.0 * k for k in range(10)]},
                        index=df.index)
    hist.loc[df.index[3], "volume"] = 0.0             # no trades: fraction unknown
    out = flow.attach_taker_flow(df, "BTC/USDT", "5m", history=lambda *a: hist.drop(df.index[5]))
    frac = out["taker_buy_frac"]
    assert frac.iloc[2] == 20.0 / 50.0
    assert np.isnan(frac.iloc[3]) and np.isnan(frac.iloc[5])   # unknown, missing
    assert frac.iloc[9] == 1.0                                   # 90/50 clipped
    assert "taker_buy_frac" not in df.columns                   # input untouched


def test_attach_never_raises_and_skips_non_crypto():
    df = _bars(10)
    assert "taker_buy_frac" not in flow.attach_taker_flow(df, "EURUSD=X", "5m").columns
    boom = lambda *a: (_ for _ in ()).throw(ConnectionError("down"))  # noqa: E731
    assert flow.attach_taker_flow(df, "BTC/USDT", "5m", history=boom) is df


def test_cache_fetches_only_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(flow.CONFIG, "data_cache_dir", str(tmp_path))
    calls = []

    def fake(sym, tf, start_ms, end_ms):
        calls.append((start_ms, end_ms))
        idx = pd.date_range(pd.Timestamp(start_ms, unit="ms", tz="UTC"),
                            pd.Timestamp(end_ms, unit="ms", tz="UTC"), freq="5min")
        return pd.DataFrame({"volume": 1.0, "taker_buy_volume": 0.5}, index=idx)

    monkeypatch.setattr(flow, "_fetch_klines", fake)
    t0 = pd.Timestamp("2026-01-01", tz="UTC")
    a = flow.taker_flow_history("BTC/USDT", "5m", t0, t0 + pd.Timedelta(hours=10))
    b = flow.taker_flow_history("BTC/USDT", "5m", t0, t0 + pd.Timedelta(hours=12))
    assert len(calls) == 2
    assert calls[1][0] > calls[0][0]                 # second call: only the new tail
    assert len(b) == len(a) + 24


def _with_flow(df, frac):
    from bot.indicators import add_all_indicators
    out = add_all_indicators(df)
    out["taker_buy_frac"] = frac
    return out


def test_strategy_stays_flat_without_flow_data():
    from bot.indicators import add_all_indicators
    s = get_strategy("hft_taker_flow")
    sig = s.evaluate(add_all_indicators(_bars()), 399)
    assert sig.action == "FLAT" and "no taker-flow data" in sig.rationale


def test_buying_pressure_with_the_trend_goes_long():
    df = _bars(400, drift=0.0004, seed=3)
    df["volume"] = 200.0                              # ATR wide enough to clear costs
    df["high"], df["low"] = df["close"] * 1.004, df["close"] * 0.996
    frac = np.full(len(df), 0.5)
    frac[-3:] = 0.95                                  # takers lift the offer, hard
    s = get_strategy("hft_taker_flow")
    sig = s.evaluate(_with_flow(df, frac), len(df) - 1)
    assert sig.action == "LONG", sig.rationale


def test_signal_is_causal():
    """Truncating history at bar i cannot change the bar-i signal."""
    df = _bars(400, seed=7)
    df["high"], df["low"] = df["close"] * 1.004, df["close"] * 0.996
    frac = np.random.default_rng(7).uniform(0.2, 0.8, len(df))
    full = _with_flow(df, frac)
    s = get_strategy("hft_taker_flow")
    for i in (250, 320, 399):
        part = _with_flow(df.iloc[:i + 1], frac[:i + 1])
        assert s.evaluate(full, i).action == s.evaluate(part, i).action


def test_taker_flow_cannot_vote_before_the_engine_fetches_it():
    """The live engine does not fetch taker flow (only the experiment runner
    and the Lab do). Promoting the strategy without wiring attach_taker_flow
    into the engine would leave it permanently flat."""
    import bot.engine as engine_mod
    import bot.positions as positions_mod
    wired = any("attach_taker_flow" in inspect.getsource(m) for m in (engine_mod, positions_mod))
    assert wired or "hft_taker_flow" in CANDIDATE_STRATEGIES
