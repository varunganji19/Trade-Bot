"""Cross-process ownership of a book's account state.

The process lock and SQLite's writer lock make each write safe on its own.
Neither stops a standalone `main.py run` engine and the dashboard from both
believing they own a book: the engine holds cash in memory for a whole cycle,
so a deposit committed in between is erased by its next checkpoint, while the
transactions ledger still claims the deposit happened.
"""
from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi import HTTPException

from bot.journal import NO_OWNER, BookOwnedError, Journal


@pytest.fixture
def journal(tmp_path):
    return Journal(str(tmp_path / "owner.db"))


def test_second_engine_cannot_claim_a_live_book(journal):
    token = journal.claim_book("paper")
    other = Journal(journal.db_path)          # a second process's journal
    with pytest.raises(BookOwnedError, match="owned by pid"):
        other.claim_book("paper")
    # books are independent
    assert other.claim_book("hft")
    # released books are claimable again
    journal.release_book("paper", token)
    assert other.claim_book("paper")


def test_the_erased_deposit_is_refused_instead(journal):
    """The exact corruption: a CLI engine owns the book, the dashboard deposits.

    Before ownership existed the deposit committed, then the engine's next
    checkpoint — written from cash it had held since before the deposit —
    became the newest anchor and silently erased it.
    """
    journal.add_equity(10_000, 10_000, mode="paper")
    cli = Journal(journal.db_path)
    token = cli.claim_book("paper")

    with pytest.raises(BookOwnedError, match="stop that engine first"):
        journal.adjust_account(500, "deposit", mode="paper")
    with pytest.raises(BookOwnedError):
        journal.reset_account(10_000, str(journal.db_path) + ".bak", mode="paper")

    # nothing was written, so nothing can be erased
    assert journal.recover_cash(10_000) == 10_000
    assert journal.recent_transactions(mode="paper") == []

    # the owner itself may adjust, carrying its lease
    result = cli.adjust_account(500, "deposit", mode="paper", owner_token=token)
    assert result["cash"] == pytest.approx(10_500)
    cli.release_book("paper", token)
    # and once it is gone the dashboard may adjust again
    assert journal.adjust_account(100, "deposit", mode="paper")["cash"] == pytest.approx(10_600)


def test_a_stale_engine_cannot_checkpoint_over_the_new_owner(journal):
    journal.add_equity(10_000, 10_000, mode="paper")
    stale = Journal(journal.db_path)
    stale_token = stale.claim_book("paper")
    # the book is taken over (its previous owner died — see _lease_is_live)
    stale.release_book("paper", stale_token)
    new_token = Journal(journal.db_path).claim_book("paper")
    assert new_token != stale_token

    assert stale.heartbeat_book("paper", stale_token) is False
    for write in (
        lambda: stale.add_equity(9_000, 9_000, mode="paper", owner_token=stale_token),
        lambda: stale.adjust_account(50, "deposit", mode="paper", owner_token=stale_token),
    ):
        with pytest.raises(BookOwnedError, match="lease was lost"):
            write()
    assert journal.recover_cash(10_000) == 10_000


def test_close_with_a_lost_lease_writes_nothing(journal):
    token = journal.claim_book("paper")
    trade_id = journal.open_trade("TEST/USDT", "long", 1, 100, 95, None, "test", "x",
                                  mode="paper", entry_fee=0.1)
    journal.release_book("paper", token)
    Journal(journal.db_path).claim_book("paper")
    with pytest.raises(BookOwnedError):
        journal.close_trade(trade_id, 110, 9.9, 9.9, 0.2, "target",
                            equity=10_009.9, cash=10_009.9, owner_token=token)
    assert journal.open_trades()[0]["id"] == trade_id
    assert journal.equity_curve() == []


def test_a_dead_owner_does_not_block_the_book_forever(journal):
    """A crashed engine leaves its row behind; a dead pid must free the book."""
    with journal._conn() as conn:
        conn.execute("INSERT INTO book_owner (mode, token, pid, host, started_ts, heartbeat)"
                     " VALUES ('paper','dead',?,?,?,?)",
                     (999_999, __import__("bot.journal", fromlist=["_HOST"])._HOST,
                      "2026-01-01T00:00:00+00:00", time.time()))
    assert journal.book_owner("paper") is None
    assert journal.claim_book("paper")            # taken over, not blocked


def test_a_silent_lease_from_another_host_expires(journal):
    import bot.journal as journal_mod
    with journal._conn() as conn:
        conn.execute("INSERT INTO book_owner (mode, token, pid, host, started_ts, heartbeat)"
                     " VALUES ('paper','remote',1,'some-other-host',?,?)",
                     ("2026-01-01T00:00:00+00:00", time.time()))
    # a heartbeating remote owner is respected...
    assert journal.book_owner("paper")["host"] == "some-other-host"
    with pytest.raises(BookOwnedError):
        journal.claim_book("paper")
    # ...a silent one is not, once its lease has run out
    with journal._conn() as conn:
        conn.execute("UPDATE book_owner SET heartbeat=? WHERE mode='paper'",
                     (time.time() - journal_mod._LEASE_TTL_S - 1,))
    assert journal.book_owner("paper") is None
    assert journal.claim_book("paper")


def test_heartbeat_keeps_a_quiet_owner_alive(journal):
    import bot.journal as journal_mod
    token = journal.claim_book("paper")
    with journal._conn() as conn:
        conn.execute("UPDATE book_owner SET host='some-other-host', heartbeat=?",
                     (time.time() - journal_mod._LEASE_TTL_S - 1,))
    assert journal.book_owner("paper") is None
    assert journal.heartbeat_book("paper", token) is True
    assert journal.book_owner("paper")["token"] == token
    with pytest.raises(BookOwnedError):
        Journal(journal.db_path).claim_book("paper")


def test_engine_takes_and_releases_the_lease_around_its_run(journal, monkeypatch):
    from copy import deepcopy
    from bot.engine import TradingEngine
    from config import CONFIG
    cfg = deepcopy(CONFIG)
    cfg.llm.provider = "none"
    engine = TradingEngine(cfg=cfg, journal=journal, quiet=True)
    assert engine.book_token is None                 # construction claims nothing
    assert journal.book_owner("paper") is None
    with engine.own_book() as token:
        assert journal.book_owner("paper")["token"] == token
        with pytest.raises(BookOwnedError):
            Journal(journal.db_path).claim_book("paper")
    assert engine.book_token is None
    assert journal.book_owner("paper") is None


def test_run_forever_stops_when_its_lease_is_taken(journal, monkeypatch):
    from copy import deepcopy
    from bot.engine import TradingEngine
    from config import CONFIG
    cfg = deepcopy(CONFIG)
    cfg.llm.provider = "none"
    engine = TradingEngine(cfg=cfg, journal=journal, quiet=True)
    cycles = []

    def steal_then_count():
        cycles.append(1)
        if len(cycles) == 1:                          # another process takes over
            with journal._conn() as conn:
                conn.execute("UPDATE book_owner SET token='someone-else' WHERE mode='paper'")
        return {}

    monkeypatch.setattr(engine, "_run_cycle_locked", steal_then_count)
    engine.run_forever(interval=1)                    # returns instead of looping
    assert len(cycles) == 1
    assert "lease was taken over" in engine.last_error


def test_dashboard_refuses_account_writes_for_a_foreign_owner(journal, monkeypatch):
    import bot.dashboard as dashboard
    monkeypatch.setattr(dashboard, "journal", journal)
    monkeypatch.setattr(dashboard, "_engine", None)
    monkeypatch.setattr(dashboard, "_engine_thread", None)
    monkeypatch.setattr(dashboard, "_engine_starting", False)
    journal.add_equity(10_000, 10_000, mode="paper")
    Journal(journal.db_path).claim_book("paper")      # a CLI engine elsewhere

    for call in (lambda: dashboard._adjust_account(100, "deposit"),
                 lambda: dashboard.api_account_reset(dashboard.ResetIn(capital=10_000))):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 409
    assert journal.recover_cash(10_000) == 10_000


def test_a_database_predating_the_table_is_read_as_unowned(journal):
    """Ownership reads must not fail on a database written before the table."""
    with journal._conn() as conn:
        conn.execute("DROP TABLE book_owner")
    assert journal.book_owner("paper") is None
    assert journal.recover_cash(10_000) == 10_000
    # the table comes back with the schema, and the book is free to claim
    assert Journal(journal.db_path).claim_book("paper")


def test_market_switch_will_not_close_a_foreign_engines_positions(journal, monkeypatch):
    import bot.dashboard as dashboard
    monkeypatch.setattr(dashboard, "journal", journal)
    journal.open_trade("TEST/USDT", "long", 1, 100, 95, None, "test", "x",
                       mode="paper", entry_fee=0.1)
    Journal(journal.db_path).claim_book("paper")      # a CLI engine elsewhere
    with pytest.raises(HTTPException) as exc:
        dashboard._close_all_open_positions(None)
    assert exc.value.status_code == 409
    assert len(journal.open_trades()) == 1


def test_a_quiet_engine_on_this_host_keeps_its_book(journal):
    """A cycle interval may be an hour, and a laptop may sleep.

    The lease used to AND the pid check with heartbeat freshness, so a live
    engine on a long interval lost its own book: its checkpoint was rejected,
    the dashboard's guard let a deposit through, and another process could
    claim the book out from under it.
    """
    import bot.journal as journal_mod
    token = journal.claim_book("paper")
    with journal._conn() as conn:                 # last heartbeat: long ago
        conn.execute("UPDATE book_owner SET heartbeat=? WHERE mode='paper'",
                     (time.time() - journal_mod._LEASE_TTL_S * 10,))
    assert journal.book_owner("paper")["token"] == token
    journal.add_equity(10_000, 10_000, mode="paper", owner_token=token)
    with pytest.raises(BookOwnedError):
        Journal(journal.db_path).claim_book("paper")
    with pytest.raises(BookOwnedError):
        journal.adjust_account(100, "deposit", mode="paper")


def test_a_round_trip_needs_the_lease_like_any_other_anchor(journal):
    """record_round_trip writes an equity anchor, so it is guarded too."""
    token = journal.claim_book("paper")
    journal.release_book("paper", token)
    Journal(journal.db_path).claim_book("paper")
    with pytest.raises(BookOwnedError):
        journal.record_round_trip(
            symbol="TRI-ETH", side="long", qty=1.0, price=100.0, pnl=1.0,
            pnl_pct=1.0, fees=0.1, cash_delta=1.0, strategy="hft_triangular_arb",
            rationale="r", rationale_close="rc", exit_reason="triangular round trip",
            cash_after=10_001.0, equity_after=10_001.0,
            transaction=("arb", 1.0, "note"), mode="paper", owner_token=token)
    assert journal.equity_curve() == []
    assert journal.recent_trades() == []


def test_a_busy_retry_never_replays_the_risk_callback(journal, monkeypatch):
    """`on_adjust` mutates in-memory baselines before the commit.

    Retrying the whole call after the writer race was lost would apply the
    delta twice in memory while the rolled-back database recorded it once.
    """
    import bot.journal as journal_mod
    journal.add_equity(10_000, 10_000, mode="paper")
    calls = []

    def on_adjust(delta, conn):
        calls.append(delta)
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        journal.adjust_account(100, "deposit", mode="paper", on_adjust=on_adjust)
    assert calls == [100], "the callback must not be replayed behind the caller's back"
    assert journal.recover_cash(10_000) == 10_000
    assert journal.recent_transactions(mode="paper") == []

    # the callback-free form still retries a transient lock
    attempts = []
    real_insert = journal_mod.Journal._insert_equity

    def flaky(conn, equity, cash, mode, note, ts):
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        real_insert(conn, equity, cash, mode, note, ts)

    monkeypatch.setattr(journal_mod.Journal, "_insert_equity", staticmethod(flaky))
    assert journal.adjust_account(100, "deposit", mode="paper")["cash"] == pytest.approx(10_100)
    assert len(attempts) == 2
    assert len(journal.recent_transactions(mode="paper")) == 1


def test_the_offline_force_close_refuses_inside_its_own_transaction(journal):
    """The up-front check and the close must not leave a claimable window."""
    trade_id = journal.open_trade("TEST/USDT", "long", 1, 100, 95, None, "test", "x",
                                  mode="paper", entry_fee=0.1)
    Journal(journal.db_path).claim_book("paper")   # claimed AFTER the check would pass
    with pytest.raises(BookOwnedError):
        journal.close_trade(trade_id, 100, -0.2, -0.2, 0.2, "market switch",
                            mode="paper", owner_token=NO_OWNER)
    assert len(journal.open_trades()) == 1
