"""Bootstrap intervals and regime labels behind the stricter promotion gate."""
import numpy as np
import pandas as pd

from bot import evidence_stats as es


def _daily(closes, start="2023-01-01"):
    idx = pd.date_range(start, periods=len(closes), freq="1D", tz="UTC")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c.shift(1).fillna(c.iloc[0]), "high": c * 1.01,
                         "low": c * 0.99, "close": c, "volume": 1.0})


def test_profit_factor_edges():
    assert es.profit_factor([]) is None
    assert es.profit_factor([10, -5]) == 2.0
    assert es.profit_factor([3, 4]) == es.PF_CAP        # no losses: capped, not inf
    assert es.profit_factor([-1, -2]) == 0.0


def test_bootstrap_is_deterministic_and_brackets_the_point():
    rng = np.random.default_rng(1)
    pnls = rng.normal(5, 20, 200)
    a = es.trade_evidence(pnls)
    b = es.trade_evidence(pnls)
    assert a == b                                         # fixed seed
    assert a["pf_lo"] <= a["pf"] <= a["pf_hi"]
    assert a["sharpe_lo"] <= a["sharpe"] <= a["sharpe_hi"]


def test_interval_separates_clear_winner_loser_and_noise():
    rng = np.random.default_rng(2)
    win = es.trade_evidence(rng.normal(8, 20, 300))
    lose = es.trade_evidence(rng.normal(-8, 20, 300))
    noise = es.trade_evidence(rng.normal(0, 20, 40))
    assert win["pf_lo"] > 1.0
    assert lose["pf_hi"] < 1.0
    assert noise["pf_lo"] < 1.0 < noise["pf_hi"]          # small sample: undecided


def test_too_few_trades_give_no_interval():
    ev = es.trade_evidence([5, -3, 2])
    assert ev["pf_lo"] is None and ev["pf_hi"] is None


def test_regimes_follow_trend_and_range():
    up = _daily(np.linspace(100, 400, 400))
    down = _daily(np.linspace(400, 100, 400))
    flat = _daily(100 + np.sin(np.arange(400) / 2.0))
    assert es.label_regimes(up).iloc[-1] == es.TREND_UP
    assert es.label_regimes(down).iloc[-1] == es.TREND_DOWN
    assert es.label_regimes(flat).iloc[-1] == es.RANGE
    assert pd.isna(es.label_regimes(up).iloc[150])        # not enough history: no guess


def test_regime_label_is_causal():
    """The label for day d may not use day d's own bar."""
    base = np.linspace(100, 400, 400)
    a = es.label_regimes(_daily(base))
    shocked = base.copy()
    shocked[-1] = 1.0                                     # crash on the last day
    b = es.label_regimes(_daily(shocked))
    assert a.iloc[-1] == b.iloc[-1]


def test_regime_at_uses_last_label_before_timestamp():
    labels = es.label_regimes(_daily(np.linspace(100, 400, 400)))
    ts = labels.index[-1] + pd.Timedelta(hours=5)
    assert es.regime_at(labels, ts) == labels.iloc[-1]
    assert es.regime_at(labels, labels.index[0] - pd.Timedelta(days=1)) is None
    assert es.regime_at(labels, "2024-02-01 10:00:00") in es.REGIMES   # naive ts ok
