"""The forward track record (docs/TRACK_RECORD.md): a sealed day cannot be
changed, added to or removed without `verify` naming it."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

from bot import track_record as tr
from bot.journal import Journal

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 1, 5, 2, 0, tzinfo=UTC)     # Jan 4 ended 2 hours ago


def _closed(j, day: str, pnl: float, mode: str = "paper") -> int:
    tid = j.open_trade("BTC/USDT", "long", 1.0, 100.0, 95.0, 110.0, "s", "x",
                       mode=mode, opened_ts=f"{day}T09:00:00+00:00")
    j.close_trade(tid, 100.0 + pnl, pnl, pnl, 0.1, "target", mode=mode,
                  closed_ts=f"{day}T10:00:00+00:00")
    return tid


@pytest.fixture
def book(tmp_path):
    j = Journal(str(tmp_path / "trading.db"))
    j.add_equity(10_000.0, 10_000.0, ts="2026-01-01T00:00:00+00:00")
    _closed(j, "2026-01-01", 25.0)
    _closed(j, "2026-01-03", -10.0)                 # Jan 2 is a quiet day
    j.add_equity(10_015.0, 10_015.0, ts="2026-01-03T12:00:00+00:00")
    j.add_transaction("deposit", 500.0, ts="2026-01-03T13:00:00+00:00")
    j.add_transaction("withdrawal", 200.0, ts="2026-01-03T14:00:00+00:00")
    _closed(j, "2026-01-02", -999.0, mode="demo")   # other books never enter
    _closed(j, "2026-01-02", 999.0, mode="hft")
    return j, tmp_path / "paper.jsonl"


def _sql(j, q, *args):
    with sqlite3.connect(j.db_path) as conn:
        conn.execute(q, args)


def test_first_append_needs_a_start_and_seals_only_finished_days(book):
    j, chain = book
    with pytest.raises(tr.ChainError):
        tr.append(j.db_path, chain, now=NOW)
    new = tr.append(j.db_path, chain, start=dt.date(2026, 1, 1), now=NOW)
    assert [line["date"] for line in new] == ["2026-01-01", "2026-01-02",
                                              "2026-01-03", "2026-01-04"]
    assert [line["trades"] for line in new] == [1, 0, 1, 0]
    assert new[0]["pnl"] == 25.0 and new[2]["pnl"] == -10.0
    assert new[2]["end_equity"] == 10_015.0 and new[3]["end_equity"] is None
    assert new[2]["flows"] == 300.0                 # +500 deposit, -200 withdrawal
    assert new[0]["prev"] == tr.GENESIS
    assert all(b["prev"] == a["hash"] for a, b in zip(new, new[1:]))
    # Jan 5 has not ended; within the settle hour Jan 4 is not sealable either
    assert tr.last_sealable_day(dt.datetime(2026, 1, 5, 0, 30, tzinfo=UTC)) \
        == dt.date(2026, 1, 3)
    assert tr.verify(j.db_path, chain) == []


def test_later_appends_continue_the_chain_and_refuse_a_second_start(book):
    j, chain = book
    tr.append(j.db_path, chain, start=dt.date(2026, 1, 1), now=NOW)
    assert tr.append(j.db_path, chain, now=NOW) == []
    _closed(j, "2026-01-05", 5.0)
    later = tr.append(j.db_path, chain, now=NOW + dt.timedelta(days=1))
    assert [line["date"] for line in later] == ["2026-01-05"]
    with pytest.raises(tr.ChainError):
        tr.append(j.db_path, chain, start=dt.date(2026, 1, 1), now=NOW)
    assert tr.verify(j.db_path, chain) == []


@pytest.mark.parametrize("tamper, day", [
    (lambda j: _sql(j, "UPDATE trades SET pnl=250 WHERE closed_ts LIKE '2026-01-01%'"),
     "2026-01-01"),
    (lambda j: _closed(j, "2026-01-02", 40.0), "2026-01-02"),       # backfilled
    (lambda j: _sql(j, "DELETE FROM trades WHERE closed_ts LIKE '2026-01-03%'"
                       " AND mode='paper'"), "2026-01-03"),
    (lambda j: _sql(j, "UPDATE equity SET equity=20000 WHERE ts LIKE '2026-01-03%'"),
     "2026-01-03"),
])
def test_any_change_to_a_sealed_day_is_named(book, tamper, day):
    j, chain = book
    tr.append(j.db_path, chain, start=dt.date(2026, 1, 1), now=NOW)
    tamper(j)
    problems = tr.verify(j.db_path, chain)
    assert problems and all(p.startswith(day) for p in problems)
    # a broken chain is never extended: a new link would hide the break
    with pytest.raises(tr.ChainError):
        tr.append(j.db_path, chain, now=NOW + dt.timedelta(days=3))


def test_editing_the_chain_file_is_caught(book):
    j, chain = book
    tr.append(j.db_path, chain, start=dt.date(2026, 1, 1), now=NOW)
    lines = chain.read_text().splitlines()

    edited = json.loads(lines[1])
    edited["pnl"] = 500.0                        # a prettier summary
    chain.write_text("\n".join([lines[0], json.dumps(edited), *lines[2:]]) + "\n")
    assert any("edited after it was sealed" in p for p in tr.verify(j.db_path, chain))

    chain.write_text("\n".join([lines[0], *lines[2:]]) + "\n")   # a day dropped
    problems = tr.verify(j.db_path, chain)
    assert any("missing or out of order" in p for p in problems)
    assert any("does not link" in p for p in problems)


def test_sealing_and_verifying_never_write_to_the_journal(book):
    j, chain = book
    before = open(j.db_path, "rb").read()
    tr.append(j.db_path, chain, start=dt.date(2026, 1, 1), now=NOW)
    tr.verify(j.db_path, chain)
    assert open(j.db_path, "rb").read() == before


def test_render_fills_only_the_generated_block(book, tmp_path):
    j, chain = book
    doc = tmp_path / "TRACK_RECORD.md"
    doc.write_text(f"intro\n\n{tr.BEGIN}\nold\n{tr.END}\n\noutro\n")
    tr.render(chain, doc)
    assert "Not started" in doc.read_text()
    tr.append(j.db_path, chain, start=dt.date(2026, 1, 1), now=NOW)
    tr.render(chain, doc)
    text = doc.read_text()
    assert text.startswith("intro\n") and text.endswith("\noutro\n")
    assert "old" not in text and "Not started" not in text
    assert "**Sealed through:** 2026-01-04 (4 days)" in text
    assert "**Realized P&L:** +15.00" in text
    assert tr.read_chain(chain)[-1]["hash"] in text


def test_the_shipped_document_has_the_generated_block():
    text = tr.DEFAULT_DOC.read_text()
    assert tr.BEGIN in text and tr.END in text
