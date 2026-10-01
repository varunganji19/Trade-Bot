"""The two fast-book candidates added 2026-10-01 (docs/HFT_TRADE_FREQUENCY.md):
cross-pair spread reversion and funding-rate reversion, plus the funding feed
they read. Every frame is synthetic — no network."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import numpy as np
import pandas as pd

from bot.funding import attach_funding, funding_features, perp_symbol
from bot.indicators import add_all_indicators
from bot.orchestrator import Orchestrator
from bot.strategies import CANDIDATE_STRATEGIES, HFT_STRATEGY_NAMES
from bot.strategies.hft import HFTCrossReversion, HFTFundingReversion
from config import MarketSpec


def _bars(prices, freq="5min", seed=1):
    rng = np.random.default_rng(seed)
    prices = np.asarray(prices, dtype=float)
    opens = np.roll(prices, 1)
    opens[0] = prices[0]
    wick = np.abs(rng.normal(0, 0.0004, len(prices))) * prices
    idx = pd.date_range("2026-01-01", periods=len(prices), freq=freq, tz="UTC")
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, prices) + wick,
                         "low": np.minimum(opens, prices) - wick, "close": prices,
                         "volume": 100.0 + rng.random(len(prices)) * 20}, index=idx)


def _reverting_ratio(n=700, seed=4):
    """An ETH/BTC-like ratio: a fast-reverting AR(1) spread around a level,
    with one sharp 2-3 sigma excursion near the end."""
    rng = np.random.default_rng(seed)
    x = np.zeros(n)
    for t in range(1, n):
        x[t] = 0.85 * x[t - 1] + rng.normal(0, 0.004)
    x[-12:-1] -= np.linspace(0.002, 0.03, 11)          # the stretch to fade
    return 0.05 * np.exp(x)


def test_cross_reversion_only_trades_crosses():
    s = HFTCrossReversion()
    assert s.applies_to("ETH/BTC") and s.applies_to("SOL/ETH")
    assert not s.applies_to("BTC/USDT") and not s.applies_to("EURUSD=X")


def test_cross_reversion_fades_a_reverting_spread_and_is_causal():
    df = add_all_indicators(_bars(_reverting_ratio()))
    s = HFTCrossReversion()
    i = len(df) - 2
    sig = s.evaluate(df, i)
    assert sig.action == "LONG", sig.rationale
    assert sig.limit_price == df["close"].iloc[i]           # maker entry
    assert sig.stop_distance > 0 and "half-life" in sig.rationale
    # truncating the frame at i must not change the bar-i decision
    trunc = s.evaluate(df.iloc[: i + 1], i)
    assert (trunc.action, trunc.confidence) == (sig.action, sig.confidence)


def test_cross_reversion_refuses_a_trending_spread():
    """A ratio that is genuinely re-rating (one coin trending against the
    other) must not be faded, however stretched it looks."""
    rng = np.random.default_rng(9)
    trend = 0.05 * np.exp(np.cumsum(rng.normal(-0.0012, 0.002, 700)))
    df = add_all_indicators(_bars(trend))
    s = HFTCrossReversion()
    signals = [s.evaluate(df, i) for i in range(400, len(df))]
    assert all(sig.action == "FLAT" for sig in signals)
    assert any("not reverting" in sig.rationale for sig in signals)


def test_funding_features_and_attach_are_causal():
    """A bar sees a print only once it has settled, and each z-score uses
    only that print and earlier ones."""
    t = pd.date_range("2025-11-01", periods=150, freq="8h", tz="UTC")
    prints = pd.Series(0.0001, index=t)
    prints.iloc[-1] = -0.0002                                # shorts crowd in
    feats = funding_features(prints)
    assert np.isnan(feats["funding_z"].iloc[10])             # not warm yet
    assert feats["funding_z"].iloc[-1] < -2.0
    # the last print must not leak into earlier z-scores
    assert feats["funding_z"].iloc[:-1].equals(funding_features(prints.iloc[:-1])["funding_z"])

    bars = _bars(np.full(40, 100.0), freq="1h")
    bars.index = pd.date_range(t[-2], periods=40, freq="1h", tz="UTC")
    out = attach_funding(bars, "BTC/USDT", history=lambda sym, start: prints[prints.index >= start])
    settle = t[-1]
    assert (out.loc[out.index < settle, "funding_rate"] == 0.0001).all()
    assert (out.loc[out.index >= settle, "funding_rate"] == -0.0002).all()


def test_attach_funding_never_raises_and_skips_non_perps():
    bars = _bars(np.full(30, 1.0))
    def boom(sym, start):
        raise TimeoutError("exchange down")
    assert "funding_z" not in attach_funding(bars, "BTC/USDT", history=boom).columns
    assert attach_funding(bars, "ETH/BTC", history=boom) is bars
    assert perp_symbol("ETH/BTC") is None and perp_symbol("EURUSD=X") is None
    assert perp_symbol("SOL/USDT") == "SOL/USDT:USDT"


def _crowded_longs_frame():
    """Price stretched up, last bar closing weak, funding far above normal."""
    rng = np.random.default_rng(2)
    prices = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, 600)))
    prices[-15:] *= np.linspace(1.0, 1.03, 15)
    df = _bars(prices)
    df.iloc[-1, df.columns.get_loc("close")] = df["low"].iloc[-1] + 0.1 * (
        df["high"].iloc[-1] - df["low"].iloc[-1])                 # weak close
    df = add_all_indicators(df)
    df["funding_rate"] = 0.0004
    df["funding_z"] = 3.0
    return df


def test_funding_reversion_fades_crowded_longs_and_is_causal():
    df = _crowded_longs_frame()
    s = HFTFundingReversion()
    i = len(df) - 1
    sig = s.evaluate(df, i)
    assert sig.action == "SHORT", sig.rationale
    assert sig.target_rr == s.p.hft_fund_target_rr and sig.limit_price is None   # taker
    trunc = s.evaluate(df.iloc[: i + 1], i)
    assert (trunc.action, trunc.confidence) == (sig.action, sig.confidence)
    # normal funding: the same price action is not a setup
    calm = df.assign(funding_z=0.0)
    assert s.evaluate(calm, i).action == "FLAT"


def test_funding_reversion_without_funding_data_stays_flat():
    df = add_all_indicators(_bars(np.linspace(100, 110, 500)))
    sig = HFTFundingReversion().evaluate(df, len(df) - 1)
    assert sig.action == "FLAT" and sig.rationale == "no funding data"
    assert not HFTFundingReversion().applies_to("ETH/BTC")


def test_new_candidates_are_measured_but_never_vote():
    for name in ("hft_cross_reversion", "hft_funding_reversion"):
        assert name in HFT_STRATEGY_NAMES          # the battery measures them
        assert name in CANDIDATE_STRATEGIES        # the live vote skips them


def test_funding_strategy_cannot_vote_before_the_engine_has_funding_data():
    """The live engine does not fetch funding (only the battery and the Lab
    do). Promoting the strategy without wiring attach_funding into the
    engine would leave it permanently 'no funding data' — a voter that can
    never vote, which looks exactly like a quiet market."""
    import bot.engine as engine_mod
    wired = "attach_funding" in inspect.getsource(engine_mod)
    assert wired or "hft_funding_reversion" in CANDIDATE_STRATEGIES


def test_orchestrator_skips_strategies_that_do_not_cover_the_market():
    calls = []

    class OnlyCrosses:
        preferred_timeframes = ("5m",)
        book = "fast"

        def applies_to(self, symbol):
            return "/BTC" in symbol

        def evaluate(self, df, i):
            calls.append(i)
            return SimpleNamespace(action="FLAT", confidence=0.0, strategy="x")

    df = add_all_indicators(_bars(np.linspace(100, 101, 300)))
    orch = Orchestrator(book="fast")
    orch.strategies = {"only_crosses": OnlyCrosses()}
    orch.decide(df, len(df) - 1, MarketSpec("crypto", "BTC/USDT", "5m"))
    assert calls == []
