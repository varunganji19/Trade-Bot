"""`python3 main.py status` reports the standard paper book and nothing else.

The journal holds three books in one database: the bot's own paper record
(mode='paper'), seeded backtest-replay rows (mode='demo') and the
experimental fast book (mode='hft'). Pooling them made the demo replay and
the fast book's results read as the paper bot's performance.
"""
from __future__ import annotations

import json

from bot.cli.operate import cmd_status
from bot.journal import Journal
from config import CONFIG


def _closed(j: Journal, mode: str, pnl: float, strategy: str):
    tid = j.open_trade("BTC/USDT", "long", 1.0, 100.0, 95.0, 110.0, strategy,
                       "test", mode=mode)
    j.close_trade(tid, 100.0 + pnl, pnl, pnl, 0.0, "target", mode=mode)


def test_status_reports_only_the_paper_book(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(CONFIG, "db_path", str(tmp_path / "trading.db"))
    j = Journal()
    j.add_equity(10_000.0, 10_000.0, mode="paper", ts="2026-01-01T00:00:00")
    j.add_equity(10_050.0, 10_050.0, mode="paper", ts="2026-01-02T00:00:00")
    _closed(j, "paper", 50.0, "paper_strategy")
    for _ in range(3):
        _closed(j, "demo", -400.0, "demo_strategy")
    j.add_equity(1_000.0, 1_000.0, mode="hft", ts="2026-01-03T00:00:00")
    _closed(j, "hft", 7.0, "fast_strategy")
    _closed(j, "hft", 7.0, "fast_strategy")
    j.open_trade("ETH/USDT", "long", 1.0, 10.0, 9.0, 12.0, "fast_strategy",
                 "test", mode="hft")

    cmd_status(type("A", (), {})())
    out = capsys.readouterr().out

    stats = json.loads(out[out.index("{"):out.rindex("}") + 1])
    assert stats["closed_trades"] == 1
    assert stats["total_pnl"] == 50.0
    assert stats["open_trades"] == 0
    assert set(stats["by_strategy"]) == {"paper_strategy"}
    assert stats["current_equity"] == 10_050.0
    # the rows left out are counted, so nobody wonders where they went
    assert "excluded: 3 demo rows, 3 fast-book rows" in out
    # the other book's open position is not listed as a paper position
    assert "ETH/USDT" not in out
    assert "trading: " in out
