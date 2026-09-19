"""Durable per-book risk controls, restart and external cash-flow semantics."""
from contextlib import closing
import json
import sqlite3
from types import SimpleNamespace

import pytest

from bot.engine import TradingEngine
from bot.journal import Journal
from bot.risk import RiskManager
from config import MarketSpec


DAY = "2026-09-18T12:00:00+00:00"
NEXT_DAY = "2026-09-19T00:00:00+00:00"


def manager(path, mode="paper"):
    return RiskManager(state_db_path=str(path), mode=mode)


def approval(risk, bar_epoch=100.0):
    decision = SimpleNamespace(action="LONG", confidence=0.9, price=100.0,
                               stop_distance=5.0, target_rr=None)
    return risk.approve(decision, MarketSpec("crypto", "TEST/USDT", "1h"),
                        10_000, 0, False, bar_epoch=bar_epoch)


def test_same_day_restart_retains_halt_despite_recovery(tmp_path):
    db = tmp_path / "risk.db"
    risk = manager(db)
    risk.note_equity(10_000, DAY)
    risk.note_equity(9_000, DAY)
    restored = manager(db)
    restored.note_equity(10_000, DAY)
    assert restored.halted
    assert restored.daily_start_equity == 10_000
    assert not approval(restored).approved


def test_utc_rollover_clears_daily_halt_but_preserves_drawdown_and_cooldown(tmp_path):
    db = tmp_path / "risk.db"
    risk = manager(db)
    risk.note_equity(10_000, DAY)
    risk.note_equity(7_500, DAY)
    risk.set_cooldown("TEST/USDT", 1000)
    restored = manager(db)
    restored.note_equity(7_500, NEXT_DAY)
    assert not restored.halted
    assert restored.daily_start_equity == 7_500
    assert restored.daily_day == "2026-09-19"
    assert restored.peak_equity == 10_000
    assert restored.dd_risk_scale == 0.25
    assert "cooldown" in approval(restored).reason
    assert approval(restored, 1001).approved
    again = manager(db)
    assert again.daily_day == "2026-09-19"
    assert again.dd_risk_scale == 0.25


def test_books_are_independent(tmp_path):
    db = tmp_path / "risk.db"
    paper, hft = manager(db), manager(db, "hft")
    paper.note_equity(10_000, DAY)
    paper.note_equity(7_000, DAY)
    hft.note_equity(2_000, DAY)
    hft.set_cooldown("OTHER/USDT", 900)
    assert manager(db).halted
    restored = manager(db, "hft")
    assert not restored.halted
    assert restored.peak_equity == 2_000
    assert restored.cooldowns == {"OTHER/USDT": 900}


@pytest.mark.parametrize("delta", [2000, -2000])
def test_cash_flow_preserves_absolute_loss_and_halt(tmp_path, delta):
    db = tmp_path / "risk.db"
    risk = manager(db)
    risk.note_equity(10_000, DAY)
    risk.note_equity(9_000, DAY)
    risk.adjust_cash_flow(delta)
    restored = manager(db)
    assert restored.daily_start_equity - (9_000 + delta) == 1_000
    assert restored.peak_equity == 10_000 + delta
    assert restored.halted


@pytest.mark.parametrize("payload", ["{broken", '{}',
    json.dumps(dict(version=1, daily_day="2026-09-18", daily_start_equity=float("nan"),
                    halted=False, peak_equity=10000, dd_risk_scale=1, cooldowns={}))])
def test_invalid_checkpoint_blocks_entries_and_is_preserved(tmp_path, payload):
    db = tmp_path / "risk.db"
    manager(db)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE risk_state SET state_json=? WHERE mode='paper'", (payload,))
    risk = manager(db)
    risk.note_equity(10_000, NEXT_DAY)
    assert not approval(risk).approved
    assert "checkpoint restore failed" in approval(risk).reason
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT state_json FROM risk_state").fetchone()[0] == payload


def test_unchanged_marks_do_not_write_checkpoint(tmp_path, monkeypatch):
    db = tmp_path / "risk.db"
    risk = manager(db)
    risk.note_equity(10_000, DAY)
    def no_connection(*args, **kwargs):
        raise AssertionError("unchanged controls must not reconnect or write")
    monkeypatch.setattr(sqlite3, "connect", no_connection)
    risk.note_equity(9_999, DAY)
    assert risk.persistence_error is None


def test_failed_write_blocks_entries_until_durable_retry(tmp_path, monkeypatch):
    risk = manager(tmp_path / "risk.db")
    risk.note_equity(10_000, DAY)
    real_connect = sqlite3.connect
    def broken_connection(*args, **kwargs):
        raise sqlite3.OperationalError("read-only disk")
    monkeypatch.setattr(sqlite3, "connect", broken_connection)
    risk.set_cooldown("OTHER/USDT", 900)
    assert not approval(risk).approved
    assert "checkpoint write failed" in approval(risk).reason
    monkeypatch.setattr(sqlite3, "connect", real_connect)
    risk.persist_state()
    assert risk.persistence_error is None
    assert approval(risk).approved


def test_external_transaction_rollback_is_not_cached_as_committed(tmp_path):
    db = tmp_path / "risk.db"
    risk = manager(db)
    risk.note_equity(10_000, DAY)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        risk.adjust_cash_flow(500, conn=conn)
        conn.rollback()
    assert manager(db).daily_start_equity == 10_000
    # The owner normally rolls back its in-memory snapshot too. Even if it
    # chooses to retry instead, our cache must not suppress that durable write.
    risk.persist_state()
    assert manager(db).daily_start_equity == 10_500


def test_engine_restores_risk_before_any_cycle(tmp_path, monkeypatch):
    journal = Journal(str(tmp_path / "engine.db"))
    risk = manager(journal.db_path)
    risk.note_equity(10_000, DAY)
    risk.note_equity(7_500, DAY)
    risk.set_cooldown("TEST/USDT", 1000)
    monkeypatch.setattr(TradingEngine, "_init_kronos", lambda self: None)
    engine = TradingEngine(journal=journal, quiet=True)
    assert engine.risk.halted
    assert engine.risk.dd_risk_scale == 0.25
    assert engine.risk.cooldowns["TEST/USDT"] == 1000


def test_backtest_risk_remains_memory_only(monkeypatch):
    def no_connection(*args, **kwargs):
        raise AssertionError("backtest must not read live state")
    monkeypatch.setattr(sqlite3, "connect", no_connection)
    risk = RiskManager()
    risk.note_equity(10_000, DAY)
    risk.set_cooldown("TEST/USDT", 1000)
    assert risk.persistence_error is None


@pytest.mark.parametrize("blocked_by", ["halted", "persistence_error", "paused"])
@pytest.mark.parametrize("method", ["_triangular_scan", "_settle_tri_pending"])
def test_triangular_pending_orders_cannot_bypass_entry_controls(blocked_by, method):
    engine = TradingEngine.__new__(TradingEngine)
    engine.risk = RiskManager()
    if blocked_by == "paused":
        engine._paused_now = lambda: True
    else:
        setattr(engine.risk, blocked_by, True if blocked_by == "halted" else "disk failed")
        engine._paused_now = lambda: False
    engine._pending_tri = {"side": "long", "notional": 100}
    summary = {"holds": 0, "errors": []}
    if method == "_triangular_scan":
        engine._triangular_scan({}, summary)
    else:
        engine._settle_tri_pending({}, DAY, summary)
    assert engine._pending_tri is None
    assert not summary["errors"]
