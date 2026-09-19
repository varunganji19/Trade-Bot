"""Account isolation, numeric risk bounds and failed journal transactions."""
from __future__ import annotations

from copy import deepcopy
import sqlite3
from types import SimpleNamespace

import pytest

from bot.engine import TradingEngine
from bot.journal import Journal
from bot.risk import RiskManager
from config import CONFIG, MarketSpec


def decision(**overrides):
    values = dict(action="LONG", confidence=0.9, price=100.0,
                  stop_distance=5.0, target_rr=None,
                  strategy_name="turtle_trend", rationale="audit regression")
    return SimpleNamespace(**(values | overrides))


@pytest.mark.parametrize("kind,price,stop,equity", [
    ("india", 1600.0, 60.0, 10_000.0),
    ("forex", 1.1, 0.02, 101.0),
    ("crypto", 100.0, 5.0, 1234.56789),
])
def test_sizing_never_rounds_above_either_budget(kind, price, stop, equity):
    risk = RiskManager()
    qty = risk.size_position(equity, price, stop, kind)
    assert qty > 0
    assert qty * price <= equity * CONFIG.risk.max_position_pct
    assert qty * stop <= equity * CONFIG.risk.risk_per_trade


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("field", ["equity", "confidence", "target_rr", "open_gross_notional"])
def test_risk_rejects_nonfinite_inputs(field, value):
    dec = decision()
    kwargs = dict(equity=10_000.0, open_gross_notional=0.0)
    if field in kwargs:
        kwargs[field] = value
    else:
        setattr(dec, field, value)
    result = RiskManager().approve(dec, MarketSpec("crypto", "TEST/USDT", "1h"),
                                   open_positions=0, has_position_on_symbol=False, **kwargs)
    assert not result.approved
    assert result.qty == 0


@pytest.mark.parametrize("field", ["equity", "price", "stop_distance", "risk_fraction"])
def test_direct_sizing_rejects_nan(field):
    args = dict(equity=10_000.0, price=100.0, stop_distance=5.0, risk_fraction=0.01)
    args[field] = float("nan")
    assert RiskManager().size_position(**args) == 0


@pytest.fixture
def journal(tmp_path):
    return Journal(str(tmp_path / "audit.db"))


def seed_books(journal):
    for mode in ("paper", "hft", "demo"):
        journal.open_trade("TEST/USDT", "long", 1.0, 100.0, 95.0, None,
                           "turtle_trend", "audit", mode=mode)
        journal.add_equity(10_000.0, 10_000.0, mode=mode)
        journal.add_transaction("deposit", 100.0, mode=mode)
        dec = decision(regime="trend", strategy_signals={}, sentiment={})
        journal.add_decision("TEST/USDT", "1h", dec, mode=mode)
    journal.log_chat("user", "keep shared chat")


def test_reset_isolates_book_and_backs_up_uncheckpointed_wal(journal, tmp_path):
    # Keep a connection alive so WAL contents are not auto-checkpointed by
    # closing the last connection. The backup must include these records.
    with journal._conn() as keeper:
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        seed_books(journal)
        backup = str(tmp_path / "backup.db")
        journal.reset_account(5000.0, backup)
        with sqlite3.connect(backup) as conn:
            assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert journal.trade_mode_counts() == {"hft": 1, "demo": 1}
    with journal._conn() as conn:
        for table in ("trades", "decisions", "equity", "transactions"):
            for mode in ("hft", "demo"):
                assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE mode=?",
                                    (mode,)).fetchone()[0] == 1
        assert conn.execute("SELECT content FROM chat_log").fetchone()[0] == "keep shared chat"
    assert journal.last_equity_point(mode="paper")["cash"] == 5000.0
    assert [r["kind"] for r in journal.recent_transactions(mode="paper")] == ["reset"]


def test_reset_rolls_back_deletions_if_new_ledger_write_fails(journal, tmp_path):
    seed_books(journal)
    with journal._conn() as conn:
        conn.execute("CREATE TRIGGER fail_reset BEFORE INSERT ON transactions "
                     "WHEN NEW.kind='reset' BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        journal.reset_account(5000.0, str(tmp_path / "backup.db"))
    assert journal.trade_mode_counts() == {"paper": 1, "hft": 1, "demo": 1}
    assert journal.last_equity_point(mode="paper")["cash"] == 10_000.0


def test_reset_backup_failure_leaves_book_untouched(journal, tmp_path):
    seed_books(journal)
    with pytest.raises(sqlite3.OperationalError):
        journal.reset_account(5000.0, str(tmp_path / "missing" / "backup.db"))
    assert journal.trade_mode_counts() == {"paper": 1, "hft": 1, "demo": 1}


def test_repeated_reset_waits_for_detached_engine_thread(journal, monkeypatch):
    from bot import dashboard as dash
    from fastapi import HTTPException

    seed_books(journal)
    monkeypatch.setattr(dash, "journal", journal)
    monkeypatch.setattr(dash, "_engine", None)
    monkeypatch.setattr(dash, "_engine_starting", False)
    monkeypatch.setattr(dash, "_engine_thread", SimpleNamespace(is_alive=lambda: True))
    monkeypatch.setattr(dash, "_write_engine_state", lambda *a: True)
    for _ in range(2):
        assert dash.api_engine_stop(dash.EmptyIn())["status"] == "stopping"
        with pytest.raises(HTTPException) as exc:
            dash.api_account_reset(dash.ResetIn(capital=5000))
        assert exc.value.status_code == 409
    assert journal.trade_mode_counts()["paper"] == 1


def test_reset_refuses_inflight_engine_construction(journal, monkeypatch):
    from bot import dashboard as dash
    from fastapi import HTTPException

    monkeypatch.setattr(dash, "journal", journal)
    monkeypatch.setattr(dash, "_engine", None)
    monkeypatch.setattr(dash, "_engine_thread", None)
    monkeypatch.setattr(dash, "_engine_starting", True)
    monkeypatch.setattr(dash, "_write_engine_state", lambda *a: True)
    with pytest.raises(HTTPException) as exc:
        dash.api_account_reset(dash.ResetIn(capital=5000))
    assert exc.value.status_code == 409


def test_token_guard_runs_through_actual_asgi_middleware_stack():
    from bot.dashboard import _TokenGuard
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.add_middleware(_TokenGuard, token="audit-test-token")

    @app.get("/")
    def shell():
        return {"shell": True}

    @app.get("/api/account")
    def account():
        return {"cash": 100.0}

    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/api/account").status_code == 401
    assert client.get("/api/account", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.get("/api/account", headers={"Authorization": "Bearer audit-test-token"})
    assert response.status_code == 200 and response.json()["cash"] == 100.0


@pytest.fixture
def engine(journal, monkeypatch):
    monkeypatch.setattr(TradingEngine, "_init_kronos", lambda self: None)
    monkeypatch.setattr(TradingEngine, "_paused_now", lambda self: False)
    cfg = deepcopy(CONFIG)
    cfg.llm.provider = "none"
    return TradingEngine(cfg=cfg, journal=journal, quiet=True)


def test_entry_journal_failure_reverts_simulated_fill(engine, monkeypatch):
    def fail(*a, **kw):
        raise sqlite3.OperationalError("injected fill failure")

    before = (engine.broker.cash, engine.broker.fees_paid, engine.broker.realized_pnl)
    monkeypatch.setattr(engine.journal, "record_fill", fail)
    summary = {"opened": [], "closed": []}
    with pytest.raises(sqlite3.OperationalError, match="injected fill"):
        engine._fill_entry(MarketSpec("crypto", "TEST/USDT", "1h"), decision(),
                           1.0, 100.0, False, 1.0, summary)
    assert (engine.broker.cash, engine.broker.fees_paid, engine.broker.realized_pnl) == before
    assert engine.broker.positions == {}
    assert engine.journal.open_trades() == []
    assert engine.journal.recent_trades()[0]["status"] == "ABORTED"
    assert summary["opened"] == []


def test_exit_journal_failure_keeps_position_managed_and_retryable(engine, monkeypatch):
    spec = MarketSpec("crypto", "TEST/USDT", "1h")
    summary = {"opened": [], "closed": []}
    engine._fill_entry(spec, decision(), 1.0, 100.0, False, 1.0, summary)
    key = (spec.symbol, spec.timeframe)
    pos = engine.broker.positions[key]
    before = (engine.broker.cash, engine.broker.fees_paid, engine.broker.realized_pnl)
    # Fail the database UPDATE itself so this exercises real rollback, not
    # merely a mock replacing the entire persistence path.
    with engine.journal._conn() as conn:
        conn.execute("CREATE TRIGGER fail_close BEFORE UPDATE ON trades "
                     "WHEN NEW.status='CLOSED' BEGIN SELECT RAISE(ABORT, 'close failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="close failure"):
        engine._close(spec, 110.0, "manual", summary)
    assert engine.broker.positions[key] is pos
    assert (engine.broker.cash, engine.broker.fees_paid, engine.broker.realized_pnl) == before
    assert len(engine.journal.open_trades()) == 1
    assert summary["closed"] == []
    with engine.journal._conn() as conn:
        conn.execute("DROP TRIGGER fail_close")
    engine._close(spec, 110.0, "manual", summary)
    assert engine.broker.positions == {}
    assert engine.journal.open_trades() == []
    assert len(summary["closed"]) == 1
    assert engine.journal.last_equity_point(mode="paper")["cash"] == engine.broker.cash


def _tri_legs(o_usdt, o_ethbtc, o_btcusdt):
    import pandas as pd
    return {"ETH/USDT": pd.DataFrame({"open": [o_usdt]}),
            "ETH/BTC": pd.DataFrame({"open": [o_ethbtc]}),
            "BTC/USDT": pd.DataFrame({"open": [o_btcusdt]})}


@pytest.fixture
def hft_engine(journal, monkeypatch):
    monkeypatch.setattr(TradingEngine, "_init_kronos", lambda self: None)
    monkeypatch.setattr(TradingEngine, "_paused_now", lambda self: False)
    cfg = deepcopy(CONFIG)
    cfg.llm.provider = "none"
    return TradingEngine(cfg=cfg, mode="hft", journal=journal, quiet=True)


def test_arb_settlement_books_the_same_cash_as_the_broker(hft_engine):
    """The cash-settled arb must not credit half the round-trip cost twice.

    close_trade's fallback is `pnl + fees/2` when no entry fee was recorded,
    so an arb that moved broker cash by `pnl` used to leave a ledger event of
    `pnl + fees/2` — invisible until a restart replayed the event.
    """
    engine = hft_engine
    engine._pending_tri = {"side": "long", "notional": 1000.0, "d": 0.004,
                           "ts": "2026-01-01T00:00:00Z", "rationale": "arb"}
    cash_before = engine.broker.cash
    summary = {"closed": [], "errors": []}
    engine._settle_tri_pending(_tri_legs(2000.0, 0.05, 39_000.0), "2026-01-01T00:01:00Z", summary)
    moved = engine.broker.cash - cash_before
    assert summary["closed"] and moved != 0.0
    # The settled cash event IS the number a restart replays, so it must equal
    # the broker's own movement — not pnl plus half the round-trip cost.
    with engine.journal._conn() as conn:
        events = conn.execute("SELECT kind, amount FROM cash_events WHERE mode='hft'"
                              " ORDER BY id").fetchall()
    assert [e["kind"] for e in events] == ["close"]
    assert sum(e["amount"] for e in events) == pytest.approx(moved, abs=1e-9)
    # flat by construction: the round trip must leave nothing restorable
    assert engine.journal.open_trades(mode="hft") == []
    # Recovery agrees from the committed anchor and from an anchor that predates
    # the arb (a checkpoint the process never got to write).
    assert engine.journal.recover_cash(cash_before, mode="hft") == pytest.approx(engine.broker.cash, abs=1e-9)
    with engine.journal._conn() as conn:
        conn.execute("DELETE FROM equity WHERE mode='hft'")
    assert engine.journal.recover_cash(cash_before, mode="hft") == pytest.approx(engine.broker.cash, abs=1e-9)
    assert engine.journal.recent_transactions(mode="hft")[0]["kind"] == "arb"


def test_arb_ledger_failure_leaves_broker_and_journal_on_the_pre_arb_number(hft_engine):
    engine = hft_engine
    engine._pending_tri = {"side": "long", "notional": 1000.0, "d": 0.004,
                           "ts": "2026-01-01T00:00:00Z", "rationale": "arb"}
    cash_before = engine.broker.cash
    with engine.journal._conn() as conn:
        conn.execute("CREATE TRIGGER fail_tx BEFORE INSERT ON transactions "
                     "WHEN NEW.kind='arb' BEGIN SELECT RAISE(ABORT, 'ledger failure'); END")
    summary = {"closed": [], "errors": []}
    with pytest.raises(sqlite3.IntegrityError, match="ledger failure"):
        engine._settle_tri_pending(_tri_legs(2000.0, 0.05, 39_000.0), "2026-01-01T00:01:00Z", summary)
    assert engine.broker.cash == cash_before
    assert engine.journal.recover_cash(cash_before, mode="hft") == pytest.approx(cash_before, abs=1e-9)
    assert engine.journal.recent_transactions(mode="hft") == []
    assert summary["closed"] == []
    # nothing half-written: no ghost TRI-ETH row for a symbol no watchlist marks
    assert engine.journal.recent_trades(mode="hft") == []
    assert engine.journal.open_trades(mode="hft") == []



def test_arb_anchor_uses_the_cycles_marks_not_entry_prices(hft_engine):
    """The arb's equity anchor must not zero out open-position P&L.

    broker.equity({}) marks every position at its ENTRY price, so anchoring on
    it stamped a curve point with no unrealized P&L — a phantom drawdown that
    outlived the cycle tail's correction, since stats() walks every point.
    """
    engine = hft_engine
    spec = MarketSpec("crypto", "TEST/USDT", "1h")
    engine._fill_entry(spec, decision(), 1.0, 100.0, False, 1.0,
                       {"opened": [], "closed": [], "errors": []})
    engine._cycle_equity = engine.broker.equity({"TEST/USDT": 130.0})
    assert engine._cycle_equity > engine.broker.equity({})     # 30 unrealized
    engine._pending_tri = {"side": "long", "notional": 1000.0, "d": 0.004,
                           "ts": "2026-01-01T00:00:00Z", "rationale": "arb"}
    summary = {"closed": [], "errors": []}
    engine._settle_tri_pending(_tri_legs(2000.0, 0.05, 39_000.0), "2026-01-01T00:01:00Z", summary)
    settled = summary["closed"][0]["pnl"]
    anchor = engine.journal.last_equity_point(mode="hft")
    assert anchor["equity"] == pytest.approx(engine._cycle_equity + settled, abs=1e-3)
    assert anchor["equity"] > anchor["cash"]                   # the position is still marked

# --------------------------------------------------------------- replay parity
def _parity_frame():
    import numpy as np
    import pandas as pd
    from bot.indicators import add_all_indicators
    n = 40
    idx = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    close = np.full(n, 100.0)
    frame = pd.DataFrame({"open": close, "high": close + 0.5, "low": close - 0.5,
                          "close": close, "volume": np.full(n, 1000.0)}, index=idx)
    # one bar well below the stop, deep enough to breach on any scan
    frame.iloc[25, frame.columns.get_loc("low")] = 90.0
    return add_all_indicators(frame)


def _open_parity_position(engine, spec, frame, entry_i):
    """Open through the real fill path so the journal carries the decision bar."""
    summary = {"opened": [], "closed": [], "errors": []}
    engine._fill_entry(spec, decision(price=100.0, stop_distance=5.0), 1.0, 100.0,
                       False, float(frame.index[entry_i].timestamp()), summary)
    return summary


def _drive(engine, spec, frame, bars, summary):
    """Manage the position bar by bar, recording the bars_held clock it sees."""
    trace = []
    for i in bars:
        pos = engine.broker.positions.get((spec.symbol, spec.timeframe))
        if pos is None:
            break
        engine._manage_position(spec, pos, frame, i, summary,
                                bar_epoch=float(frame.index[i].timestamp()))
        still = engine.broker.positions.get((spec.symbol, spec.timeframe))
        trace.append((i, pos.bars_held, None if still is not None else
                      summary["closed"][-1]["reason"]))
    return trace


def _fresh_engine(journal, monkeypatch):
    monkeypatch.setattr(TradingEngine, "_init_kronos", lambda self: None)
    monkeypatch.setattr(TradingEngine, "_paused_now", lambda self: False)
    cfg = deepcopy(CONFIG)
    cfg.llm.provider = "none"
    return TradingEngine(cfg=cfg, journal=journal, quiet=True)


def test_restart_replays_the_same_exit_bar_and_time_stop_clock(tmp_path, monkeypatch):
    """Downtime must not move the exit bar or the bars-held clock.

    Recovery reads the DECISION bar from the journal, so a restarted engine
    counts the same bars as one that never stopped. Reading wall-clock
    opened_ts instead would drift both the time stop and the replay window.
    """
    spec = MarketSpec("crypto", "TEST/USDT", "1h")
    frame = _parity_frame()
    entry_i, bars = 10, range(11, 31)

    continuous_journal = Journal(str(tmp_path / "continuous.db"))
    engine = _fresh_engine(continuous_journal, monkeypatch)
    summary = _open_parity_position(engine, spec, frame, entry_i)
    continuous = _drive(engine, spec, frame, bars, summary)

    # same run, but the process dies after bar 20 and a new engine restores it
    restart_journal = Journal(str(tmp_path / "restart.db"))
    engine = _fresh_engine(restart_journal, monkeypatch)
    summary = _open_parity_position(engine, spec, frame, entry_i)
    first = _drive(engine, spec, frame, range(11, 21), summary)
    restarted = _fresh_engine(restart_journal, monkeypatch)
    assert (spec.symbol, spec.timeframe) in restarted.broker.positions, "position not restored"
    summary = {"opened": [], "closed": [], "errors": []}
    second = _drive(restarted, spec, frame, range(21, 31), summary)

    assert first + second == continuous
    # and the run really did exit on the breach bar, not merely agree on nothing
    assert continuous[-1][0] == 25 and continuous[-1][2] == "stop loss"
    # the restored clock is the decision bar's own, not the wall clock
    assert restarted.broker.positions == {}
    assert [row[1] for row in second] == [11, 12, 13, 14, 15]
