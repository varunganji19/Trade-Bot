"""The ledger consistency check (roadmap M4): a book's cash must reconcile
with its own history, and a gap must reach the dashboard on the next poll."""
from fastapi.testclient import TestClient

import config as config_mod


def _journal(tmp_path, monkeypatch):
    import bot.dashboard as dash
    path = str(tmp_path / "t.db")
    monkeypatch.setattr(config_mod.CONFIG, "db_path", path)
    j = dash.Journal(path)
    monkeypatch.setattr(dash, "journal", j)
    j.reset_account(10_000.0, str(tmp_path / "backup.db"))
    return dash, j


def test_a_consistent_book_reconciles(tmp_path, monkeypatch):
    _, j = _journal(tmp_path, monkeypatch)
    assert j.ledger_check()["ok"]
    tid = j.open_trade("BTC/USDT", "LONG", 0.1, 50_000.0, 49_000.0, 52_000.0, "turtle_trend",
                       "test", entry_fee=2.0)
    j.close_trade(tid, 50_500.0, 48.0, 0.96, 4.0, "target", entry_fee=2.0,
                  realized_cash_delta=50.0)
    j.adjust_account(500.0, "deposit")
    j.open_trade("ETH/USDT", "LONG", 1.0, 3_000.0, 2_900.0, 3_300.0, "turtle_trend",
                 "test", entry_fee=1.5)
    check = j.ledger_check()
    assert check["ok"], check
    assert check["expected_cash"] == 10_000 + 500 + 48 - 1.5
    assert check["start_source"] == "reset"


def test_a_mismatched_journal_raises_the_banner(tmp_path, monkeypatch):
    """Inject the shape of the legacy mismatch: an equity anchor whose cash
    no trade history explains."""
    dash, j = _journal(tmp_path, monkeypatch)
    tid = j.open_trade("BTC/USDT", "LONG", 0.1, 50_000.0, 49_000.0, 52_000.0, "turtle_trend",
                       "test", entry_fee=2.0)
    j.close_trade(tid, 49_000.0, -102.0, -2.04, 4.0, "stop", entry_fee=2.0,
                  realized_cash_delta=-100.0)
    j.add_equity(10_688.64, 10_688.64)            # cash out of thin air
    check = j.ledger_check()
    assert not check["ok"]
    assert check["gap"] == round(10_688.64 - (10_000 - 102.0), 2)

    payload = TestClient(dash.app, base_url="http://127.0.0.1").get("/api/stats").json()
    assert payload["ledger"]["standard"]["ok"] is False
    assert payload["ledger"]["standard"]["gap"] == check["gap"]
    assert payload["ledger"]["fast"]["ok"] is True       # the other book is untouched

    html = open(dash.static_path("index.html")).read()
    js = open(dash.static_path("app.js")).read()
    assert 'id="ledgerBanner"' in html and "renderLedger(s.ledger)" in js


def test_an_untouched_journal_is_not_flagged(tmp_path, monkeypatch):
    """No equity anchor yet (fresh clone): nothing to reconcile, no banner."""
    import bot.dashboard as dash
    monkeypatch.setattr(config_mod.CONFIG, "db_path", str(tmp_path / "t.db"))
    assert dash.Journal(str(tmp_path / "t.db")).ledger_check("paper")["ok"]
