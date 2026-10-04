"""Chronological fills and net account statistics regression coverage."""
from dataclasses import replace
import math

import numpy as np
import pandas as pd
import pytest

import bot.backtest as bt_mod
from bot.backtest import BTResult, Backtester, _aggregate, _merge_folds, results_to_json
from bot.broker import PaperBroker
from bot.hft import build_hft_config
from bot.risk import RiskManager
from bot.strategies.base import Signal
from bot.validation import oos_trade_distribution
from config import MarketSpec, bars_per_year


SPEC = MarketSpec("crypto", "TEST/USDT", "5m", "Test")


def frame(n=250):
    return pd.DataFrame(
        {"open": 100.0, "high": 100.4, "low": 99.6, "close": 100.0, "volume": 100.0},
        index=pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC"))


class OneShot:
    name = "hft_market_maker"

    def __init__(self, side="LONG", maker=True, entry_bar=220, exit_bar=None,
                 trail_bar=None):
        self.side, self.maker, self.entry_bar = side, maker, entry_bar
        self.exit_bar, self.trail_bar = exit_bar, trail_bar

    def evaluate(self, df, i):
        if i != self.entry_bar:
            return Signal(self.name, "FLAT", 0.0)
        return Signal(self.name, self.side, 0.9, 1.0, 2.0,
                      limit_price=100.0 if self.maker else None)

    def check_exit(self, df, i, position):
        return ("signal exit" if i == self.exit_bar else None,
                99.8 if i == self.trail_bar else None)


def run(monkeypatch, df, strategy):
    monkeypatch.setattr(bt_mod, "get_strategy", lambda name, params: strategy)
    return Backtester(build_hft_config(fee_tier="perp"), book="fast").run(
        SPEC, df, strategy=strategy.name)


@pytest.mark.parametrize("side,event,column,level", [
    ("LONG", "stop loss", "low", 98.0),
    ("SHORT", "stop loss", "high", 102.0),
    ("LONG", "take profit", "high", 103.0),
    ("SHORT", "take profit", "low", 97.0),
])
def test_maker_bracket_on_immediately_following_bar(monkeypatch, side, event, column, level):
    df = frame()
    df.loc[df.index[222], column] = level
    result = run(monkeypatch, df, OneShot(side=side))
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade["entry_ts"] == str(df.index[221])
    assert trade["exit_ts"] == str(df.index[222])
    assert trade["exit_reason"] == event


@pytest.mark.parametrize("maker", [False, True])
def test_each_held_bar_is_scanned_once(monkeypatch, maker):
    scans = []

    class CountingBroker(PaperBroker):
        def scan_bar_exits(self, spec, bar):
            scans.append(bar.name)
            return super().scan_bar_exits(spec, bar)

    monkeypatch.setattr(bt_mod, "PaperBroker", CountingBroker)
    df = frame()
    result = run(monkeypatch, df, OneShot(maker=maker))
    assert result.trades[0]["exit_reason"] == "end of backtest"
    assert scans == list(df.index[221:])


def test_signal_exit_at_open_precedes_intrabar_target(monkeypatch):
    df = frame()
    df.loc[df.index[223], "high"] = 103.0
    result = run(monkeypatch, df, OneShot(maker=False, exit_bar=222))
    assert result.trades[0]["exit_reason"] == "signal exit"
    assert result.trades[0]["exit_ts"] == str(df.index[223])


class RepeatedTwoBarExit(OneShot):
    def __init__(self, *, maker=False, reason="time exit", name="hft_market_maker"):
        super().__init__(maker=maker)
        self.reason, self.name = reason, name

    def evaluate(self, df, i):
        self.entry_bar = i
        return super().evaluate(df, i)

    def check_exit(self, df, i, position):
        return (self.reason if position.bars_held >= 2 else None, None)


def test_two_bar_signal_exit_preserves_reentry_frequency(monkeypatch):
    strategy = RepeatedTwoBarExit()
    monkeypatch.setattr(bt_mod, "get_strategy", lambda *args: strategy)
    cfg = build_hft_config(fee_tier="perp")
    cfg.costs = replace(cfg.costs, fee_crypto=0.0, slippage_crypto=0.0)
    result = Backtester(cfg, book="fast").run(SPEC, frame(300), strategy=strategy.name)
    assert len(result.trades) == 27
    gaps = [pd.Timestamp(b["entry_ts"]) - pd.Timestamp(a["exit_ts"])
            for a, b in zip(result.trades, result.trades[1:])]
    assert set(gaps) == {pd.Timedelta(minutes=5)}


@pytest.mark.parametrize("maker", [False, True])
@pytest.mark.parametrize("reason", ["signal exit", "time exit"])
@pytest.mark.parametrize("cooldown", [1, 3])
def test_queued_exit_cooldown_starts_at_decision_bar(monkeypatch, maker, reason, cooldown):
    strategy = RepeatedTwoBarExit(maker=maker, reason=reason, name="vwap_scalper")
    monkeypatch.setattr(bt_mod, "get_strategy", lambda *args: strategy)
    cfg = build_hft_config(fee_tier="perp")
    cfg.costs = replace(cfg.costs, fee_crypto=0.0, slippage_crypto=0.0,
                        maker_fee_crypto=0.0)
    cfg.params.scalper_cooldown_bars = cooldown
    result = Backtester(cfg, book="fast").run(SPEC, frame(300), strategy=strategy.name)
    assert len(result.trades) > 2
    gaps = [pd.Timestamp(b["entry_ts"]) - pd.Timestamp(a["exit_ts"])
            for a, b in zip(result.trades, result.trades[1:])]
    assert set(gaps) == {pd.Timedelta(minutes=5 * cooldown)}


@pytest.mark.parametrize("reason,column,level", [
    ("stop loss", "low", 98.0), ("take profit", "high", 103.0),
])
def test_intrabar_exit_never_reenters_on_its_candle(monkeypatch, reason, column, level):
    df = frame(300)
    df.loc[df.index[221], column] = level
    result = run(monkeypatch, df, RepeatedTwoBarExit())
    assert result.trades[0]["exit_reason"] == reason
    assert pd.Timestamp(result.trades[1]["entry_ts"]) >= df.index[223]


def test_close_trail_only_applies_to_following_bar(monkeypatch):
    df = frame()
    df.loc[df.index[223], "low"] = 99.7
    result = run(monkeypatch, df, OneShot(maker=False, trail_bar=222))
    assert result.trades[0]["exit_ts"] == str(df.index[223])
    assert result.trades[0]["exit_reason"] == "stop loss"


@pytest.mark.parametrize("maker", [False, True])
def test_final_bar_entry_and_liquidation_are_recorded(monkeypatch, maker):
    df = frame()
    result = run(monkeypatch, df, OneShot(maker=maker, entry_bar=248))
    assert len(result.trades) == 1
    assert result.trades[0]["entry_ts"] == str(df.index[-1])
    assert result.trades[0]["exit_ts"] == str(df.index[-1])
    assert result.equity_curve[-1]["equity"] == result.end_equity
    assert result.end_equity < result.start_equity


def test_curve_has_start_flat_bars_and_terminal_fees(monkeypatch):
    df = frame()
    result = run(monkeypatch, df, OneShot(maker=False, entry_bar=230))
    assert [p["ts"] for p in result.equity_curve] == [str(ts) for ts in df.index[220:]]
    assert result.equity_curve[0]["equity"] == result.start_equity
    assert all(p["equity"] == result.start_equity for p in result.equity_curve[:11])
    assert result.equity_curve[-1]["equity"] == result.end_equity
    assert result.stats()["max_drawdown_pct"] < 0
    assert result.end_equity - result.start_equity == pytest.approx(
        sum(t["pnl"] for t in result.trades), abs=1e-9)


def test_subcent_trade_fees_reconcile_and_remain_net_losses(monkeypatch):
    class RepeatedEntries(OneShot):
        def evaluate(self, df, i):
            self.entry_bar = i if i in (220, 230, 240) else -1
            return super().evaluate(df, i)

        def check_exit(self, df, i, position):
            return "signal exit", None

    strategy = RepeatedEntries(maker=False)
    monkeypatch.setattr(bt_mod, "get_strategy", lambda name, params: strategy)
    cfg = build_hft_config(fee_tier="perp")
    cfg.costs = replace(cfg.costs, fee_crypto=1e-7, slippage_crypto=0.0)
    df = frame()
    result = Backtester(cfg, book="fast").run(SPEC, df, strategy=strategy.name)

    assert len(result.trades) == 3
    assert all(-0.005 < t["pnl"] < 0 for t in result.trades)
    assert all(0 < t["fees"] < 0.005 for t in result.trades)
    assert math.fsum(t["pnl"] for t in result.trades) == pytest.approx(
        result.end_equity - result.start_equity, abs=1e-9)
    assert result.stats()["win_rate_pct"] == 0.0
    assert result.stats()["profit_factor"] == 0.0

    monkeypatch.setattr("bot.validation.purged_cv_paths",
                        lambda *args, **kwargs: [np.arange(len(df))])
    validation = oos_trade_distribution(result.trades, df, purge_bars=4,
                                       starting_capital=result.start_equity)
    assert validation["paths"][0]["trades"] == 3
    assert validation["paths"][0]["win_rate_pct"] == 0.0
    assert validation["pct_paths_profitable"] == 0.0


def test_first_fill_bar_loss_is_in_drawdown(monkeypatch):
    df = frame()
    df.loc[df.index[221], "low"] = 98.0
    result = run(monkeypatch, df, OneShot(maker=False))
    assert result.trades[0]["exit_ts"] == str(df.index[221])
    expected = 100 * (result.end_equity / result.start_equity - 1)
    assert result.stats()["max_drawdown_pct"] == round(expected, 2)


def test_no_trade_curve_stays_dense_and_flat(monkeypatch):
    df = frame()
    result = run(monkeypatch, df, OneShot(entry_bar=-1))
    assert len(result.equity_curve) == 30
    assert {p["equity"] for p in result.equity_curve} == {10_000.0}
    assert result.stats()["sharpe"] is None


def test_maker_fill_reapproval_uses_refreshed_shrunk_quantity(monkeypatch):
    approvals = []

    class ShrinkingRisk(RiskManager):
        def approve(self, *args, **kwargs):
            approval = super().approve(*args, **kwargs)
            approvals.append(kwargs)
            if kwargs["qty_ceiling"] is not None:
                return replace(approval, qty=approval.qty / 2)
            return approval

    monkeypatch.setattr(bt_mod, "RiskManager", ShrinkingRisk)
    result = run(monkeypatch, frame(), OneShot())
    assert len(approvals) == 2
    assert approvals[1]["qty_ceiling"] > result.trades[0]["qty"]
    assert result.trades[0]["qty"] == approvals[1]["qty_ceiling"] / 2
    assert approvals[1]["open_gross_notional"] == 0
    assert approvals[1]["cluster_gross_notional"] == 0


@pytest.mark.parametrize("veto", ["halt", "cooldown"])
def test_stale_maker_cannot_fill_after_risk_veto(monkeypatch, veto):
    df = frame()

    class VetoRisk(RiskManager):
        def note_equity(self, equity, ts=None):
            super().note_equity(equity, ts=ts)
            if ts == str(df.index[221]):
                if veto == "halt":
                    self.halted = True
                else:
                    self.cooldowns[SPEC.symbol] = df.index[221].timestamp() + 3600

    monkeypatch.setattr(bt_mod, "RiskManager", VetoRisk)
    result = run(monkeypatch, df, OneShot())
    assert result.trades == []
    assert result.end_equity == result.start_equity


def fold(start_ts, values):
    values = list(values)
    return BTResult(SPEC, "test", start_equity=10_000.0, end_equity=values[-1],
                    equity_curve=[{"ts": str(ts), "equity": value} for ts, value in zip(
                        pd.date_range(start_ts, periods=len(values), freq="5min", tz="UTC"), values)],
                    trades=[{"status": "CLOSED", "pnl": values[-1] - 10_000., "fees": 0}])


def test_fixed_capital_folds_add_pnl_without_reset_drawdowns():
    results = [fold("2024-01-01", [10_000., 11_000.]),
               fold("2024-01-02", [10_000., 11_000.])]
    stats = _aggregate(results, SPEC)
    assert stats["total_pnl"] == 2_000.0
    assert stats["return_pct"] == 20.0
    assert stats["end_equity"] == 12_000.0
    assert stats["max_drawdown_pct"] == 0.0
    assert stats["aggregation_method"] == "fixed_capital_additive_pnl"


def test_fold_losses_and_flat_fold_reconcile():
    results = [fold("2024-01-01", [10_000., 11_000.]),
               fold("2024-01-02", [10_000., 9_500.]),
               fold("2024-01-03", [10_000., 10_000.])]
    merged = _merge_folds(results, SPEC)
    assert merged.end_equity == 10_500.
    assert merged.equity_curve[-1]["equity"] == 10_500.
    assert merged.stats()["max_drawdown_pct"] == -4.55
    assert merged.stats()["return_pct"] == 5.0


def test_fold_sharpe_excludes_warmup_transition():
    results = [fold("2024-01-01", np.linspace(10_000., 11_000., 25)),
               fold("2024-01-02", np.linspace(10_000., 9_900., 25))]
    merged = _merge_folds(results, SPEC)
    changes = [p["equity"] for p in merged.equity_curve]
    expected_returns = [(changes[i] / changes[i - 1] - 1)
                        for i in range(1, len(changes)) if i != 25]
    expected = (np.mean(expected_returns) / np.std(expected_returns, ddof=1)
                * math.sqrt(bars_per_year(SPEC.timeframe, SPEC.kind)))
    assert merged.stats()["sharpe"] == round(expected, 2)
    assert merged.segment_starts == [0, 25]


def test_walk_forward_exports_the_stitched_curve(monkeypatch, tmp_path):
    results = iter([fold("2024-01-01", [10_000., 11_000.]),
                    fold("2024-01-02", [10_000., 11_000.])])
    monkeypatch.setattr(Backtester, "run", lambda *args, **kwargs: next(results))
    result = Backtester().run_walk_forward(SPEC, frame(600), folds=2, progress=False)
    assert result["equity_curve"][-1]["equity"] == result["aggregate"]["end_equity"] == 12_000.
    results_to_json(result, str(tmp_path / "result.json"))
    import json
    exported = json.loads((tmp_path / "result.json").read_text())
    assert exported["equity_curve"] == result["equity_curve"]
    assert exported["metrics_version"] == 3


def cv_trade(df, pnl, pnl_pct=1.0):
    return {"entry_ts": str(df.index[40]), "exit_ts": str(df.index[45]),
            "pnl": pnl, "pnl_pct": pnl_pct}


def cv(monkeypatch, trades, df, capital=10_000.0):
    monkeypatch.setattr("bot.validation.purged_cv_paths", lambda *args, **kwargs: [np.arange(100)])
    return oos_trade_distribution(trades, df, purge_bars=4, starting_capital=capital)


def test_cv_gross_winner_net_loser_is_unprofitable(monkeypatch):
    df = frame()
    result = cv(monkeypatch, [cv_trade(df, -1.0, pnl_pct=0.1)], df)
    path = result["paths"][0]
    assert path["return_pct"] == -0.01
    assert path["win_rate_pct"] == 0.0
    assert result["pct_paths_profitable"] == 0.0
    assert result["return_basis"] == "net_pnl_over_starting_capital"


def test_cv_adds_net_pnl_and_respects_capital_and_sizing(monkeypatch):
    df = frame()
    trades = [cv_trade(df, 100., pnl_pct=10.), cv_trade(df, -50., pnl_pct=-1.)]
    result = cv(monkeypatch, trades, df)
    assert result["paths"][0]["return_pct"] == 0.5
    assert result["paths"][0]["win_rate_pct"] == 50.0
    assert cv(monkeypatch, trades, df, capital=20_000.)["paths"][0]["return_pct"] == 0.25


def test_cv_profitability_uses_unrounded_net_pnl(monkeypatch):
    df = frame()
    result = cv(monkeypatch, [cv_trade(df, 0.001)], df)
    assert result["paths"][0]["return_pct"] == 0.0
    assert result["pct_paths_profitable"] == 100.0


@pytest.mark.parametrize("capital", [0., -1., float("nan"), float("inf")])
def test_cv_rejects_invalid_capital(capital):
    with pytest.raises(ValueError, match="starting_capital"):
        oos_trade_distribution([], frame(), starting_capital=capital)


def test_cv_requires_explicit_capital():
    with pytest.raises(TypeError, match="starting_capital"):
        oos_trade_distribution([], frame())


@pytest.mark.parametrize("pnl", [None, float("nan"), float("inf")])
def test_cv_rejects_unknown_net_pnl(pnl):
    df = frame()
    with pytest.raises(ValueError, match="finite net pnl"):
        oos_trade_distribution([cv_trade(df, pnl)], df, starting_capital=10_000.)
