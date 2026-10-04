"""Fill-time risk and continuous, durable candle recovery regressions."""
from copy import deepcopy
from types import SimpleNamespace
import sqlite3

import pandas as pd
import pytest

from bot.engine import TradingEngine
from bot.journal import Journal
from bot.risk import RiskManager
from config import CONFIG, MarketSpec


def decision(**changes):
    values = dict(action="LONG", price=100., stop_distance=5., confidence=1.,
                  target_rr=2., strategy_name="turtle_trend", rationale="regression")
    values.update(changes)
    return SimpleNamespace(**values)


def setup(tmp_path, monkeypatch, symbols=("TEST/USDT",)):
    cfg = deepcopy(CONFIG)
    cfg.db_path = str(tmp_path / "book.db")
    cfg.portfolio.enabled = False
    cfg.watchlist = [MarketSpec("crypto", symbol, "5m") for symbol in symbols]
    journal = Journal(cfg.db_path)
    eng = TradingEngine(cfg=cfg, mode="hft", quiet=True, journal=journal)
    frame = pd.DataFrame(dict(open=[100.] * 100, high=[101.] * 100,
                              low=[99.] * 100, close=[100.] * 100, volume=[100.] * 100),
                         index=pd.date_range("2026-01-01", periods=100, freq="5min", tz="UTC"))
    monkeypatch.setattr("bot.positions.get_strategy", lambda *args: SimpleNamespace(
        check_exit=lambda *args: (None, None)))
    eng.orchestrator.decide = lambda *args: decision(action="HOLD")
    return eng, journal, cfg.watchlist, frame


def summary():
    return dict(opened=[], closed=[], errors=[], holds=0)


def rest(eng, spec, frame, qty=25.):
    eng._pending[(spec.symbol, spec.timeframe)] = dict(
        decision=decision(), qty=qty, limit=100., side="LONG", waited=0,
        decision_bar_ts=frame.index[-2].timestamp())


def open_at(eng, spec, frame, index=65):
    eng._fill_entry(spec, decision(), 1., 100., False,
                    frame.index[index].timestamp(), summary())
    return eng.broker.positions[(spec.symbol, spec.timeframe)]


@pytest.mark.parametrize("ceiling", [1., 10.])
def test_quantity_ceiling_applied_before_exposure_checks(ceiling):
    cfg = deepcopy(CONFIG)
    cfg.risk.max_gross_leverage = .2
    cfg.risk.max_cluster_leverage = .2
    rm = RiskManager(cfg)
    approval = rm.approve(decision(), MarketSpec("crypto", "TEST/USDT", "5m"),
                          10_000., 0, False, open_gross_notional=500.,
                          cluster_gross_notional=500., qty_ceiling=ceiling)
    assert approval.approved
    assert approval.qty == ceiling


@pytest.mark.parametrize("ceiling", [0., -1., float("nan"), float("inf"), .01])
def test_untradable_quantity_ceiling_rejected(ceiling):
    approval = RiskManager(deepcopy(CONFIG)).approve(
        decision(), MarketSpec("crypto", "TEST/USDT", "5m"), 10_000., 0, False,
        qty_ceiling=ceiling)
    assert not approval.approved


@pytest.mark.parametrize("gate", ["positions", "cluster", "gross"])
def test_simultaneous_resting_fills_observe_prior_fill(tmp_path, monkeypatch, gate):
    eng, journal, specs, frame = setup(tmp_path, monkeypatch, ("AAA/USDT", "BBB/USDT"))
    if gate == "positions":
        eng.cfg.risk.max_open_positions = 1
    elif gate == "cluster":
        eng.cfg.risk.max_cluster_leverage = .3
    else:
        eng.cfg.risk.max_gross_leverage = .3
    eng.market_data.latest = lambda *args, **kwargs: frame
    for spec in specs:
        rest(eng, spec, frame)
    result = eng.run_cycle()
    assert not result["errors"]
    assert len(result["opened"]) == 1
    assert len(journal.open_trades(mode="hft")) == 1
    assert not eng._pending


@pytest.mark.parametrize("cash,ceiling", [(1_000., 25.), (20_000., 1.)])
def test_resting_fill_shrinks_but_never_enlarges(tmp_path, monkeypatch, cash, ceiling):
    eng, journal, specs, frame = setup(tmp_path, monkeypatch)
    eng.broker.cash = cash
    eng.market_data.latest = lambda *args, **kwargs: frame
    rest(eng, specs[0], frame, ceiling)
    expected = min(ceiling, eng.risk.size_position(cash, 100., 5., "crypto"))
    result = eng.run_cycle()
    assert not result["errors"]
    assert result["opened"][0]["qty"] == expected
    assert journal.open_trades(mode="hft")[0]["qty"] == expected


@pytest.mark.parametrize("gate", ["pause", "cooldown"])
def test_resting_fill_rechecks_pause_and_cooldown(tmp_path, monkeypatch, gate):
    eng, journal, specs, frame = setup(tmp_path, monkeypatch)
    eng.market_data.latest = lambda *args, **kwargs: frame
    rest(eng, specs[0], frame)
    if gate == "pause":
        monkeypatch.setattr("bot.positions.is_paused", lambda: (True, "regression"))
    else:
        eng.risk.set_cooldown(specs[0].symbol, frame.index[-1].timestamp() + 600)
    result = eng.run_cycle()
    assert not result["opened"]
    assert not eng._pending
    assert not journal.open_trades(mode="hft")


@pytest.mark.parametrize("breach", ["stop loss", "take profit"])
def test_running_engine_replays_missed_breach(tmp_path, monkeypatch, breach):
    eng, journal, specs, frame = setup(tmp_path, monkeypatch)
    spec = specs[0]
    pos = open_at(eng, spec, frame)
    eng.market_data.latest = lambda *args, **kwargs: frame.iloc[:71]
    assert not eng.run_cycle()["closed"]
    assert pos.bracket_checked_through_ts == frame.index[70].timestamp()
    frame.loc[frame.index[73], "low" if breach == "stop loss" else "high"] = (
        90. if breach == "stop loss" else 111.)
    eng.market_data.latest = lambda *args, **kwargs: frame.iloc[:82]
    result = eng.run_cycle()
    assert result["closed"][0]["reason"] == breach
    assert not eng.broker.positions
    assert journal.recent_trades()[0]["status"] == "CLOSED"


def test_scan_checkpoint_failure_remains_retryable(tmp_path, monkeypatch):
    eng, journal, specs, frame = setup(tmp_path, monkeypatch)
    pos = open_at(eng, specs[0], frame)
    eng.market_data.latest = lambda *args, **kwargs: frame.iloc[:71]
    original = journal.update_trade_stops
    journal.update_trade_stops = lambda *args, **kwargs: (_ for _ in ()).throw(
        sqlite3.OperationalError("injected checkpoint failure"))
    first = eng.run_cycle()
    assert first["errors"]
    assert pos.bracket_checked_through_ts is None
    assert journal.open_trades(mode="hft")[0]["bracket_checked_through_ts"] is None
    journal.update_trade_stops = original
    assert not eng.run_cycle()["errors"]
    epoch = frame.index[70].timestamp()
    assert pos.bracket_checked_through_ts == epoch
    restarted = TradingEngine(cfg=eng.cfg, mode="hft", quiet=True, journal=journal)
    assert restarted.broker.positions[(specs[0].symbol, "5m")].bracket_checked_through_ts == epoch


def test_maker_replay_starts_at_actual_fill_candle(tmp_path, monkeypatch):
    eng, journal, specs, frame = setup(tmp_path, monkeypatch)
    spec = specs[0]
    frame.loc[frame.index[73], "low"] = 90.
    rest(eng, spec, frame.iloc[:81], qty=1.)
    eng._pending[(spec.symbol, "5m")]["decision_bar_ts"] = frame.index[70].timestamp()
    eng.market_data.latest = lambda *args, **kwargs: frame.iloc[:81]
    first = eng.run_cycle()
    assert first["opened"] and not first["closed"]
    row = journal.open_trades(mode="hft")[0]
    assert row["fill_bar_ts"] == frame.index[80].timestamp()
    eng.market_data.latest = lambda *args, **kwargs: frame.iloc[:82]
    assert not eng.run_cycle()["closed"]


def test_missing_history_keeps_cursor_and_blocks_other_entries(tmp_path, monkeypatch):
    eng, journal, specs, frame = setup(tmp_path, monkeypatch, ("NEW/USDT", "HELD/USDT"))
    pos = open_at(eng, specs[1], frame)
    rest(eng, specs[0], frame, qty=1.)
    eng.market_data.latest = lambda *args, **kwargs: frame.iloc[75:]
    result = eng.run_cycle()
    assert any("cannot cover cursor" in error for error in result["errors"])
    assert "candle recovery" in eng.health_note
    assert not result["opened"]
    assert pos.bracket_checked_through_ts is None
    assert journal.open_trades(mode="hft")[0]["bracket_checked_through_ts"] is None
    # Even an incomplete window still closes an available protective breach.
    frame.loc[frame.index[90], "low"] = 90.
    eng._last_bar_ts.clear()
    result = eng.run_cycle()
    assert result["closed"][0]["reason"] == "stop loss"


def test_short_history_still_checks_and_persists_protection(tmp_path, monkeypatch):
    eng, journal, specs, frame = setup(tmp_path, monkeypatch)
    pos = open_at(eng, specs[0], frame, index=95)
    eng.market_data.latest = lambda *args, **kwargs: frame.iloc[94:]
    result = eng.run_cycle()
    assert not result["errors"]
    assert pos.bracket_checked_through_ts == frame.index[-1].timestamp()
    assert journal.open_trades(mode="hft")[0]["bracket_checked_through_ts"] == pos.bracket_checked_through_ts


def test_restoration_and_position_responses_preserve_quantity_and_bar_clock(tmp_path, monkeypatch):
    from bot.dashboard import _journal_position_dict, _position_dict
    eng, journal, specs, frame = setup(tmp_path, monkeypatch)
    pos = open_at(eng, specs[0], frame)
    row = journal.open_trades(mode="hft")[0]
    row.update(qty=2., remaining_qty=.5)
    restored = eng.broker.restore_position(row)
    assert restored.entry_bar_ts == frame.index[65].timestamp()
    assert restored.fill_bar_ts == frame.index[66].timestamp()
    assert restored.qty == .5 and restored.entry_qty == 2.
    assert _position_dict(restored)["remaining_qty"] == .5
    assert _position_dict(restored)["entry_qty"] == 2.
    assert _journal_position_dict(row)["qty"] == 2.
    assert _journal_position_dict(row)["remaining_qty"] == .5
    assert pos.entry_qty == 1.


@pytest.mark.parametrize("outage", ["none", "empty", "exception"])
def test_unavailable_held_history_blocks_sibling_fills_until_recovery(tmp_path, monkeypatch, outage):
    eng, journal, specs, frame = setup(tmp_path, monkeypatch, ("NEW/USDT", "HELD/USDT"))
    pos = open_at(eng, specs[1], frame)
    eng._process_market(specs[1], summary(), frame.iloc[:71])
    cursor = pos.bracket_checked_through_ts
    rest(eng, specs[0], frame, qty=1.)

    def latest(spec, **kwargs):
        if spec == specs[0]:
            return frame
        if outage == "exception":
            raise RuntimeError("injected held feed outage")
        return None if outage == "none" else frame.iloc[:0]

    eng.market_data.latest = latest
    first = eng.run_cycle()
    assert not first["opened"]
    assert any("history unavailable" in error for error in first["errors"])
    assert pos.bracket_checked_through_ts == cursor
    assert "history unavailable" in eng.health_note
    eng.market_data.latest = lambda *args, **kwargs: frame
    second = eng.run_cycle()
    assert not second["errors"]
    assert not eng._recovery_gaps
    assert pos.bracket_checked_through_ts == frame.index[-1].timestamp()
    assert journal.open_trades(mode="hft")[0]["bracket_checked_through_ts"] == pos.bracket_checked_through_ts


def test_held_replay_settles_before_sibling_fill_approval(tmp_path, monkeypatch):
    eng, journal, specs, frame = setup(tmp_path, monkeypatch, ("NEW/USDT", "HELD/USDT"))
    open_at(eng, specs[1], frame)
    eng.cfg.risk.max_open_positions = 1
    rest(eng, specs[0], frame, qty=1.)
    frame.loc[frame.index[73], "low"] = 90.
    eng.market_data.latest = lambda *args, **kwargs: frame
    result = eng.run_cycle()
    assert not result["errors"]
    assert result["closed"][0]["symbol"] == "HELD/USDT"
    assert result["opened"][0]["symbol"] == "NEW/USDT"
    assert len(journal.open_trades(mode="hft")) == 1
