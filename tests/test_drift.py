"""The drift monitor (bot/drift.py): a promoted strategy whose live results
stay below its expected range loses its vote, and keeps it lost."""
from __future__ import annotations

import datetime as dt

import pytest

from bot import drift
from bot import promotion as promo
from bot.journal import Journal
from config import CONFIG

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 1, 5, 12, 0, tzinfo=UTC)          # a Monday
NAME = "turtle_trend"                                     # a standard-book voter


def _verdicts(status=promo.PROMOTED):
    return {NAME: {"status": status, "why": "measured", "trades": 150, "pf": 1.3,
                   "pf_lo": 1.1, "pf_hi": 1.6, "rule": promo.RULE_VERSION,
                   "evidence": "walk_forward_oos"}}


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(CONFIG, "db_path", str(tmp_path / "trading.db"))
    promo.save_verdicts(_verdicts(), "spot", book="standard")
    return Journal()


def _trades(j, start: dt.datetime, pnls, strategy=NAME, mode="paper"):
    for k, pnl in enumerate(pnls):
        ts = (start + dt.timedelta(hours=k)).isoformat()
        tid = j.open_trade("BTC/USDT", "long", 1.0, 100.0, 95.0, 110.0, strategy, "x",
                           mode=mode, opened_ts=ts)
        j.close_trade(tid, 100.0 + pnl, pnl, pnl, 0.0, "exit", mode=mode, closed_ts=ts)


def _check(j, now):
    return drift.check("standard", j.db_path, now=now)


def test_week_ends_are_the_mondays_after_the_start():
    ends = drift.week_ends(T0, T0 + dt.timedelta(days=15))
    assert [e.date().isoformat() for e in ends] == ["2026-01-12", "2026-01-19"]


def test_three_weeks_below_the_range_demotes_and_the_gate_obeys(book):
    assert _check(book, T0) == []                                  # watch starts
    assert drift.load_state("standard")["watching"][NAME]["since"].startswith("2026-01-05")
    _trades(book, T0 + dt.timedelta(days=1), [1.0, -2.0] * 13)    # PF 0.5 on 26 trades
    # two weeks below is not yet drift
    assert _check(book, dt.datetime(2026, 1, 25, tzinfo=UTC)) == []
    assert promo.may_vote(NAME, promo.load_verdicts(book="standard"))

    assert _check(book, dt.datetime(2026, 1, 26, 1, 0, tzinfo=UTC)) == [NAME]
    v = promo.load_verdicts(book="standard")[NAME]
    assert v["status"] == promo.DEMOTED and v["drift"] is True
    assert "below its expected 1.10–1.60 for 3 weeks" in v["why"]
    assert not promo.may_vote(NAME, promo.load_verdicts(book="standard"))
    silent = {s["name"]: s["why"] for s in promo.voting_strategies("standard")["silent"]}
    assert silent[NAME].startswith("demoted — drift:")
    # the evidence file itself is untouched...
    assert promo.load_verdicts(book="standard", drift=False)[NAME]["status"] == promo.PROMOTED
    # ...and regenerating it does not hand the vote back
    promo.save_verdicts(_verdicts(), "spot", book="standard")
    assert promo.load_verdicts(book="standard")[NAME]["status"] == promo.DEMOTED


def test_a_week_back_inside_the_range_resets_the_count(book):
    _check(book, T0)
    _trades(book, T0 + dt.timedelta(days=1), [1.0, -2.0] * 13)          # weeks 1-2 below
    _trades(book, dt.datetime(2026, 1, 20, tzinfo=UTC), [3.0, -2.0] * 25)  # week 3: PF 1.5
    assert _check(book, dt.datetime(2026, 1, 26, 1, 0, tzinfo=UTC)) == []
    weeks = drift.report("standard", book.db_path,
                         now=dt.datetime(2026, 1, 26, 1, 0, tzinfo=UTC))[NAME]["weeks"]
    assert [w["state"] for w in weeks] == [drift.BELOW, drift.BELOW, drift.INSIDE]


def test_too_few_trades_never_demote(book):
    _check(book, T0)
    _trades(book, T0 + dt.timedelta(days=1), [-1.0] * (drift.DRIFT_MIN_TRADES - 1))
    assert _check(book, T0 + dt.timedelta(weeks=6)) == []


def test_only_this_strategys_live_trades_in_this_book_count(book):
    _check(book, T0)
    _trades(book, T0 + dt.timedelta(days=1), [-1.0] * 30, strategy="connors_meanrev")
    _trades(book, T0 + dt.timedelta(days=1), [-1.0] * 30, mode="hft")
    _trades(book, T0 - dt.timedelta(days=3), [-1.0] * 30)          # before the watch
    assert _check(book, T0 + dt.timedelta(weeks=6)) == []


def test_unpromoted_strategies_are_not_watched(book):
    promo.save_verdicts(_verdicts(promo.PROBATION), "spot", book="standard")
    _check(book, T0)
    assert drift.load_state("standard")["watching"] == {}


def test_clear_gives_the_vote_back_and_restarts_the_watch(book):
    _check(book, T0)
    _trades(book, T0 + dt.timedelta(days=1), [1.0, -2.0] * 13)
    _check(book, dt.datetime(2026, 1, 26, 1, 0, tzinfo=UTC))
    assert drift.clear("standard", NAME)
    assert not drift.clear("standard", NAME)
    assert promo.may_vote(NAME, promo.load_verdicts(book="standard"))
    later = dt.datetime(2026, 2, 1, tzinfo=UTC)
    assert _check(book, later) == []
    assert drift.load_state("standard")["watching"][NAME]["since"].startswith("2026-02-01")


def test_the_banner_shows_a_demotion_for_a_week(book):
    _check(book, T0)
    _trades(book, T0 + dt.timedelta(days=1), [1.0, -2.0] * 13)
    at = dt.datetime(2026, 1, 26, 1, 0, tzinfo=UTC)
    _check(book, at)
    alerts = drift.recent_alerts(now=at + dt.timedelta(days=2))
    assert [(a["book"], a["name"]) for a in alerts] == [("standard", NAME)]
    assert drift.recent_alerts(now=at + dt.timedelta(days=drift.ALERT_DAYS + 1)) == []


def test_a_failing_check_is_reported_and_never_stops_the_cycle(monkeypatch):
    from bot.engine import TradingEngine

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(drift, "check", boom)
    stub = type("E", (), {"orchestrator": type("O", (), {"book": "standard"})(),
                          "journal": type("J", (), {"db_path": "x"})(), "quiet": True})()
    summary = {"errors": []}
    TradingEngine._check_drift(stub, summary)
    assert summary["errors"] == ["drift check: OSError: disk full"]
