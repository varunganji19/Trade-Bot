"""Cash checkpoints use commit order; financial effects and account history commit together."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from bot.journal import Journal


@pytest.fixture
def journal(tmp_path):
    return Journal(str(tmp_path / "cash.db"))


def entry(journal, *, mode="paper", fee=0.10005, pending=False):
    trade_id = journal.open_trade("TEST/USDT", "long", 1, 100, None, None,
                                  "test", "entry", mode=mode, pending_fill=pending)
    if not pending:
        journal.record_fill(trade_id, 100.05, stop=95, entry_fee=fee,
                            decision_bar_ts=1_700_000_000.0)
    return trade_id


def test_open_entry_after_anchor_survives_repeated_restarts(journal):
    journal.add_equity(10_000, 10_000)
    trade_id = entry(journal)
    for _ in range(3):
        restarted = Journal(journal.db_path)
        assert restarted.recover_cash(10_000) == pytest.approx(9_999.89995)
        assert restarted.open_trades()[0]["decision_bar_ts"] == 1_700_000_000.0
    # A repeated identical fill remains one charge; omitted fee never clears it.
    journal.record_fill(trade_id, 100.05, entry_fee=0.10005)
    journal.record_fill(trade_id, 100.05, stop=96)
    assert journal.recover_cash(10_000) == pytest.approx(9_999.89995)
    journal.add_equity(9_999.89995, 9_999.89995)
    assert Journal(journal.db_path).recover_cash(10_000) == pytest.approx(9_999.89995)


def test_same_second_close_replayed_exactly_once(journal, monkeypatch):
    monkeypatch.setattr("bot.journal._now", lambda: "2026-01-01T00:00:00+00:00")
    trade_id = entry(journal, fee=0.1)
    journal.add_equity(9999.9, 9999.9)
    journal.close_trade(trade_id, 110, 9.79, 9.79, 0.21, "target",
                        entry_fee=0.1, realized_cash_delta=9.89)
    assert journal.recover_cash(10_000) == pytest.approx(10009.79)
    journal.close_trade(trade_id, 110, 9.79, 9.79, 0.21, "target",
                        entry_fee=0.1, realized_cash_delta=9.89)
    for _ in range(3):
        assert Journal(journal.db_path).recover_cash(10_000) == pytest.approx(10009.79)
    journal.add_equity(10009.79, 10009.79)
    assert journal.recover_cash(10_000) == pytest.approx(10009.79)


def test_cash_recovery_without_anchor_and_modes(journal):
    entry(journal, mode="paper", fee=0.12)
    entry(journal, mode="hft", fee=0.07)
    journal.adjust_account(100, "deposit", mode="paper", initial_cash=10_000)
    assert journal.recover_cash(10_000, "paper") == pytest.approx(10099.88)
    assert journal.recover_cash(10_000, "hft") == pytest.approx(9999.93)
    journal.add_equity(9999.93, 9999.93, mode="hft")
    assert journal.recover_cash(10_000, "paper") == pytest.approx(10099.88)


def test_checkpoint_order_ignores_clock_rollback(journal):
    journal.add_equity(10000, 10000, ts="2026-03-01T00:00:00+00:00")
    entry(journal, fee=0.1)
    journal.add_equity(9999.9, 9999.9, ts="2026-02-01T00:00:00+00:00")
    assert journal.recover_cash(10000) == pytest.approx(9999.9)


def test_pending_intent_never_restores_as_position(journal):
    trade_id = entry(journal, pending=True)
    assert Journal(journal.db_path).open_trades() == []
    assert journal.recover_cash(10000) == 10000
    journal.record_fill(trade_id, 100, entry_fee=0.1, decision_bar_ts=123)
    assert Journal(journal.db_path).open_trades()[0]["id"] == trade_id
    assert journal.recover_cash(10000) == pytest.approx(9999.9)


def test_fill_event_failure_rolls_back_activation(journal):
    trade_id = entry(journal, pending=True)
    with journal._conn() as conn:
        conn.execute("CREATE TRIGGER reject_cash BEFORE INSERT ON cash_events "
                     "BEGIN SELECT RAISE(ABORT, 'cash failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        journal.record_fill(trade_id, 100, entry_fee=0.1)
    assert journal.open_trades() == []
    assert journal.recent_trades()[0]["status"] == "PENDING"
    assert journal.recover_cash(10000) == 10000
    journal.abort_trade(trade_id)
    assert journal.recent_trades()[0]["status"] == "ABORTED"


def test_close_checkpoint_failure_rolls_back_everything(journal):
    trade_id = entry(journal, fee=0.1)
    with journal._conn() as conn:
        conn.execute("CREATE TRIGGER reject_checkpoint BEFORE INSERT ON equity "
                     "BEGIN SELECT RAISE(ABORT, 'checkpoint failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        journal.close_trade(trade_id, 110, 9.79, 9.79, 0.21, "target",
                            realized_cash_delta=9.89, equity=10009.79, cash=10009.79)
    assert journal.open_trades()[0]["id"] == trade_id
    assert journal.recover_cash(10000) == pytest.approx(9999.9)


def test_account_transaction_failure_rolls_back_event_and_checkpoint(journal):
    journal.add_equity(10000, 10000)
    with journal._conn() as conn:
        conn.execute("CREATE TRIGGER reject_ledger BEFORE INSERT ON transactions "
                     "BEGIN SELECT RAISE(ABORT, 'ledger failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        journal.adjust_account(100, "deposit")
    assert journal.recover_cash(10000) == 10000
    assert len(journal.equity_curve()) == 1
    assert journal.recent_transactions() == []


def test_concurrent_account_adjustments_across_journal_instances(journal):
    journal.add_equity(10000, 10000)
    journals = [Journal(journal.db_path) for _ in range(4)]
    def deposit(index):
        return journals[index % 4].adjust_account(0.12345, "deposit")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(deposit, range(40)))
    assert journal.recover_cash(10000) == pytest.approx(10004.938)
    assert len(journal.recent_transactions()) == 40
    with pytest.raises(ValueError, match="insufficient cash"):
        journal.adjust_account(20000, "withdrawal")
    assert journal.recover_cash(10000) == pytest.approx(10004.938)


def test_legacy_anchor_recovers_untracked_legs_then_transitions(journal):
    # Model an old database: no event IDs and no ordered fill records.
    with journal._conn() as conn:
        conn.execute("INSERT INTO equity(ts,equity,cash,mode) VALUES (?,?,?,?)",
                     ("2026-01-01T00:00:00+00:00", 10000, 10000, "paper"))
        conn.execute("INSERT INTO trades(symbol,side,qty,entry_price,strategy,status,opened_ts,entry_fee) "
                     "VALUES ('OLD','long',1,100,'test','OPEN','2026-01-01T00:00:01+00:00',0.1)")
    restarted = Journal(journal.db_path)
    assert "same-second" in restarted.last_error
    assert restarted.recover_cash(10000) == pytest.approx(9999.9)
    restarted.adjust_account(100, "deposit")
    entry(restarted, fee=0.2)
    assert Journal(journal.db_path).recover_cash(10000) == pytest.approx(10099.7)


def test_legacy_same_second_ambiguity_is_not_invented(journal):
    with journal._conn() as conn:
        conn.execute("INSERT INTO equity(ts,equity,cash,mode) VALUES (?,?,?,?)",
                     ("2026-01-01T00:00:00+00:00", 9999.9, 9999.9, "paper"))
        conn.execute("INSERT INTO trades(symbol,side,qty,entry_price,strategy,status,opened_ts,entry_fee) "
                     "VALUES ('OLD','long',1,100,'test','OPEN','2026-01-01T00:00:00+00:00',0.1)")
    assert Journal(journal.db_path).recover_cash(10000) == pytest.approx(9999.9)


def test_offline_cashflow_keeps_daily_loss_and_halt(journal):
    journal.add_equity(9500, 9500)
    state = {"version": 1, "daily_start_equity": 10000, "peak_equity": 10500,
             "halted": True, "daily_day": "2026-01-01", "cooldowns": {"BTC": 123}}
    with journal._conn() as conn:
        conn.execute("CREATE TABLE risk_state(mode TEXT PRIMARY KEY,state_json TEXT NOT NULL)")
        conn.execute("INSERT INTO risk_state VALUES (?,?)", ("paper", json.dumps(state)))
    journal.adjust_account(100, "deposit")
    with journal._conn() as conn:
        result = json.loads(conn.execute("SELECT state_json FROM risk_state").fetchone()[0])
    assert result["daily_start_equity"] == 10100
    assert result["peak_equity"] == 10600
    assert result["halted"] is True
    assert result["daily_day"] == state["daily_day"]
    assert result["cooldowns"] == state["cooldowns"]


def test_risk_callback_failure_rolls_back_cash(journal):
    journal.add_equity(10000, 10000)
    def fail(delta, conn):
        raise RuntimeError("risk checkpoint rejected")
    with pytest.raises(RuntimeError):
        journal.adjust_account(100, "deposit", on_adjust=fail)
    assert journal.recover_cash(10000) == 10000
    assert journal.recent_transactions() == []


def test_engine_open_fill_restarts_with_exact_cash_and_decision_clock(journal, monkeypatch):
    from copy import deepcopy
    from types import SimpleNamespace
    from bot.engine import TradingEngine
    from config import CONFIG, MarketSpec
    cfg = deepcopy(CONFIG)
    cfg.paper_capital = 10000
    spec = MarketSpec("crypto", "TEST/USDT", "1h")
    cfg.watchlist = [spec]
    monkeypatch.setattr(TradingEngine, "_paused_now", lambda self: False)
    journal.add_equity(10000, 10000)
    engine = TradingEngine(cfg=cfg, quiet=True, journal=journal)
    decision = SimpleNamespace(action="LONG", confidence=0.9, price=100,
                               stop_distance=5, target_rr=None, strategy_name="test",
                               rationale="cash recovery")
    engine._fill_entry(spec, decision, 1, 100, False, 1_700_000_000, {"opened": []})
    actual = engine.broker.cash
    assert actual < 10000
    restarted = TradingEngine(cfg=cfg, quiet=True, journal=Journal(journal.db_path))
    assert restarted.broker.cash == actual
    assert restarted.broker.positions[("TEST/USDT", "1h")].entry_bar_ts == 1_700_000_000


def test_dashboard_live_failure_preserves_broker_and_risk(journal, monkeypatch):
    from types import SimpleNamespace
    import threading
    import bot.dashboard as dashboard
    from bot.risk import RiskManager
    risk = RiskManager(state_db_path=journal.db_path)
    risk.note_equity(10000)
    risk.note_equity(9500)
    broker = SimpleNamespace(cash=9500, equity=lambda marks: 9500)
    engine = SimpleNamespace(broker=broker, risk=risk, cycle_lock=threading.RLock(),
                             book_token=None)
    monkeypatch.setattr(dashboard, "journal", journal)
    monkeypatch.setattr(dashboard, "_engine", engine)
    monkeypatch.setattr(dashboard, "_mark_map", lambda eng: {})
    journal.add_equity(9500, 9500)
    with journal._conn() as conn:
        conn.execute("CREATE TRIGGER reject_risk BEFORE UPDATE ON risk_state "
                     "BEGIN SELECT RAISE(ABORT, 'risk failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        dashboard._adjust_account(100, "deposit")
    assert broker.cash == 9500
    assert risk.daily_start_equity == 10000
    assert risk.peak_equity == 10000
    assert risk.halted
    assert journal.recover_cash(10000) == 9500
    assert journal.recent_transactions() == []


def test_dashboard_live_adjustment_keeps_loss_and_persists_risk(journal, monkeypatch):
    from types import SimpleNamespace
    import threading
    import bot.dashboard as dashboard
    from bot.risk import RiskManager
    risk = RiskManager(state_db_path=journal.db_path)
    risk.note_equity(10000)
    risk.note_equity(9500)
    broker = SimpleNamespace(cash=9500, equity=lambda marks: 9500)
    engine = SimpleNamespace(broker=broker, risk=risk, cycle_lock=threading.RLock(),
                             book_token=None)
    monkeypatch.setattr(dashboard, "journal", journal)
    monkeypatch.setattr(dashboard, "_engine", engine)
    monkeypatch.setattr(dashboard, "_mark_map", lambda eng: {})
    journal.add_equity(9500, 9500)
    result = dashboard._adjust_account(100, "deposit")
    assert result["cash"] == broker.cash == 9600
    assert risk.daily_start_equity == 10100
    assert risk.halted
    restored = RiskManager(state_db_path=journal.db_path)
    assert restored.daily_start_equity == 10100
    assert restored.halted


def test_dashboard_concurrent_engine_off_adjustments(journal, monkeypatch):
    import bot.dashboard as dashboard
    monkeypatch.setattr(dashboard, "journal", journal)
    monkeypatch.setattr(dashboard, "_engine", None)
    monkeypatch.setattr(dashboard, "_engine_starting", False)
    monkeypatch.setattr(dashboard, "_engine_thread", None)
    journal.add_equity(10000, 10000)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: dashboard._adjust_account(10, "deposit"), range(12)))
    assert journal.recover_cash(10000) == 10120
    assert len(journal.recent_transactions()) == 12


@pytest.mark.parametrize("boundary,cash,positions", [
    ("pending", 10000, 0), ("filled", 9999.9, 1),
    ("closed", 10009.79, 0), ("checkpoint", 10009.79, 0),
])
def test_process_termination_at_persistence_boundaries(tmp_path, boundary, cash, positions):
    import subprocess
    import sys
    script = '''
import os, sys
from bot.journal import Journal
j = Journal(sys.argv[1])
j.add_equity(10000,10000)
t = j.open_trade("TEST","long",1,100,None,None,"test","crash",pending_fill=True)
if sys.argv[2] == "pending": os._exit(0)
j.record_fill(t,100,stop=95,entry_fee=0.1,decision_bar_ts=123)
if sys.argv[2] == "filled": os._exit(0)
j.close_trade(t,110,9.79,9.79,0.21,"target",entry_fee=0.1,realized_cash_delta=9.89)
if sys.argv[2] == "closed": os._exit(0)
j.add_equity(10009.79,10009.79)
os._exit(0)
'''
    path = str(tmp_path / "terminated.db")
    subprocess.run([sys.executable, "-c", script, path, boundary], check=True)
    for _ in range(2):
        journal = Journal(path)
        assert journal.recover_cash(10000) == pytest.approx(cash)
        assert len(journal.open_trades()) == positions
