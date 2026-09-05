"""
Seed a realistic demo journal so the dashboard has content before the engine
has traded for days. Runs REAL backtests on real fetched data for the default
watchlist (no synthetic numbers) and writes them into the journal as 'paper'
history — the same schema the live engine writes.

This is also the smoke test for the whole pipeline end-to-end.
"""
from __future__ import annotations

from datetime import datetime, timezone

from bot.backtest import Backtester
from bot.data import fetch_history
from bot.journal import Journal
from config import CONFIG


def seed(cfg=None, days: int = 240, fold_equity_points: int = 400) -> dict:
    cfg = cfg or CONFIG
    journal = Journal()
    bt = Backtester(cfg)

    counts = {"trades": 0, "decisions": 0, "equity": 0}
    ts = datetime.now(timezone.utc)

    for spec in cfg.watchlist:
        try:
            df = fetch_history(spec, days=days)
        except Exception as exc:
            print(f"[seed] skipping {spec.symbol}: {exc}")
            continue
        if df is None or len(df) < 260:
            print(f"[seed] skipping {spec.symbol}: only {0 if df is None else len(df)} bars")
            continue

        res = bt.run(spec, df, strategy=None)  # ensemble
        stats = res.stats()
        print(f"[seed] {spec.symbol} {spec.timeframe}: {stats['trades']} trades, "
              f"pnl ${stats['total_pnl']:+,.2f}, win {stats['win_rate_pct']}%")

        # walk the REAL backtest equity curve into the journal (thinned).
        # Cash = equity: every seeded trade is CLOSED, so nothing is tied up in
        # positions — and cash is the restart anchor, so it must be honest
        # (an earlier 0.9 haircut here became broker cash on the next start).
        step = max(1, len(res.equity_curve) // fold_equity_points)
        running = cfg.paper_capital
        journal.add_equity(running, running, mode="paper", ts=str(res.equity_curve[0]["ts"]) if res.equity_curve else str(ts))
        for point in res.equity_curve[::step]:
            journal.add_equity(point["equity"], point["equity"], mode="paper",
                               ts=str(point["ts"]), note=f"seed {spec.symbol}")
        for t in res.trades:
            trade_id = journal.open_trade(
                symbol=t["symbol"], side=t["side"], qty=t["qty"],
                entry_price=t["entry_price"], stop=t["stop"], target=t["target"],
                strategy=t["strategy"], rationale=t["rationale"], mode="paper",
                opened_ts=t["entry_ts"], timeframe=spec.timeframe)
            journal.close_trade(
                trade_id=trade_id, exit_price=t["exit_price"],
                pnl=t["pnl"], pnl_pct=t["pnl_pct"], fees=t["fees"],
                exit_reason=t["exit_reason"], rationale_close="", closed_ts=t["exit_ts"])
            counts["trades"] += 1

        counts["decisions"] += 6
        for k in range(6):
            t_sample = res.trades[min(k, max(0, len(res.trades) - 1))] if res.trades else None
            journal.add_decision(
                symbol=spec.symbol, timeframe=spec.timeframe,
                decision=_SimpleDecision(t_sample), mode="paper")

    counts["equity"] = len(journal.equity_curve(limit=10**9))
    return counts


class _SimpleDecision:
    """Adapt a backtest trade into a journal-able decision object."""

    def __init__(self, trade):
        self.action = "HOLD"
        self.confidence = 0.0
        self.price = 0.0
        self.regime = "unknown"
        self.stop_distance = None
        self.target_rr = None
        self.strategy_signals = {}
        self.sentiment = {}
        self.rationale = "seeded decision"
        if trade is not None:
            self.action = "LONG" if trade["side"] == "long" else "SHORT"
            self.confidence = 0.7
            self.price = trade["entry_price"]
            self.regime = "trending"
            self.rationale = trade["rationale"] or "seeded from backtest"
            self.strategy_signals = {trade["strategy"]: {"action": self.action, "confidence": 0.7}}
