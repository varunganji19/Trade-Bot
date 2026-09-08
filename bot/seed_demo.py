"""
Seed a realistic demo journal so the dashboard has content before the engine
has traded for days. Runs REAL backtests on real fetched data for the default
watchlist (no synthetic numbers) and writes them into the journal as mode='demo'
history — same schema as live rows, but LABELED: the dashboard badges demo rows
and the chatbot's paper-record answers exclude them, so seeded backtest replays
are never presented as trades the bot actually took.

This is also the smoke test for the whole pipeline end-to-end.
"""
from __future__ import annotations

from datetime import datetime, timezone

from bot.backtest import Backtester
from bot.data import fetch_history
from bot.journal import Journal
from config import CONFIG


def seed(cfg=None, days: int = 240, fold_equity_points: int = 400) -> dict:
    """Idempotent: clears prior mode='demo' rows first, so running the demo
    script twice (rehearsal + demo day) replaces the replay instead of stacking
    it (the old behavior doubled the trade count and flattened the headline
    return to 0.0% as duplicate timestamps interleaved)."""
    cfg = cfg or CONFIG
    journal = Journal()
    with journal._conn() as conn:
        for table in ("trades", "equity", "decisions"):
            conn.execute(f"DELETE FROM {table} WHERE mode='demo'")
    bt = Backtester(cfg)

    counts = {"trades": 0, "decisions": 0, "equity": 0}
    ts = datetime.now(timezone.utc)
    all_trades: list[dict] = []   # every spec's CLOSED trades, for one shared walk

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

        all_trades.extend(res.trades)
        for t in res.trades:
            trade_id = journal.open_trade(
                symbol=t["symbol"], side=t["side"], qty=t["qty"],
                entry_price=t["entry_price"], stop=t["stop"], target=t["target"],
                strategy=t["strategy"], rationale=t["rationale"], mode="demo",
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
                decision=_SimpleDecision(t_sample), mode="demo")

    # One PORTFOLIO-threaded equity walk: all specs' closed trades applied to
    # ONE shared account in exit order. The old per-spec walk restarted every
    # spec at paper_capital, so the headline showed ~1,400 restart-seam jumps —
    # "return %" uncorrelated with the summed trade P&L it sat next to, and a
    # seam-artifact max drawdown. One account = equity, return and drawdown
    # exactly coherent with the trades below them. Cash = equity (every seeded
    # trade is closed and cash is the restart anchor — it must stay honest).
    all_trades.sort(key=lambda t: str(t.get("exit_ts") or t.get("entry_ts") or ts))
    equity = cfg.paper_capital
    journal.add_equity(equity, equity, mode="demo",
                       ts=str(all_trades[0].get("entry_ts", ts)) if all_trades else str(ts))
    step = max(1, len(all_trades) // fold_equity_points)
    for i, t in enumerate(all_trades):
        equity += float(t.get("pnl") or 0.0)
        if i % step == 0 or i == len(all_trades) - 1:
            journal.add_equity(round(equity, 2), round(equity, 2), mode="demo",
                               ts=str(t.get("exit_ts") or ts))

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
