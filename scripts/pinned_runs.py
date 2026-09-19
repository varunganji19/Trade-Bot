#!/usr/bin/env python3
"""Run the 7 pinned Milestone-A backtest windows into a results directory.

Usage: python3 scripts/pinned_runs.py <before|mid|after>

The windows match the Round-8 cached frames (see MILESTONES.md) so continuity
is preserved; pinned --start/--end makes every rerun byte-identical.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RUNS = [
    # symbol, timeframe, start, end, strategy, out-name
    ("BTC/USDT", "1h", "2025-09-03", "2026-09-04", "turtle_trend", "BTCUSDT_1h_turtle"),
    ("ETH/USDT", "1h", "2026-01-04", "2026-09-04", "turtle_trend", "ETHUSDT_1h_turtle"),
    ("SOL/USDT", "1h", "2026-01-10", "2026-09-04", "turtle_trend", "SOLUSDT_1h_turtle"),
    ("BTC/USDT", "4h", "2026-03-09", "2026-09-06", "connors_meanrev", "BTCUSDT_4h_connors"),
    ("ETH/USDT", "4h", "2026-03-09", "2026-09-06", "connors_meanrev", "ETHUSDT_4h_connors"),
    ("BTC/USDT", "15m", "2026-03-09", "2026-09-06", "vwap_scalper", "BTCUSDT_15m_scalper"),
    ("ETH/USDT", "15m", "2026-01-05", "2026-09-05", "vwap_scalper", "ETHUSDT_15m_scalper"),
    # forex leg (same pinned style; Yahoo 1h history comfortably covers it)
    ("EURUSD=X", "1h", "2026-01-04", "2026-09-04", "fx_regime_meanrev", "EURUSD_1h_fxmr"),
    # india leg (NSE 1h; Yahoo intraday caps 1h history at 730d — this window
    # stays inside it, matching the battery's 1h depth)
    ("RELIANCE.NS", "1h", "2025-09-08", "2026-09-04", "ts_momentum", "RELIANCE_1h_tsmom"),
]


def main() -> int:
    stage = sys.argv[1] if len(sys.argv) > 1 else "before"
    # script-root outdir: runnable from ANY cwd (the old relative
    # "data/results/..." silently scattered outputs under $PWD)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    outdir = os.path.join(root, "data", "results", f"pinned_{stage}")
    os.makedirs(outdir, exist_ok=True)
    from bot.backtest import Backtester, results_to_json
    from bot.data import fetch_history
    from config import MarketSpec, infer_kind

    for symbol, tf, start, end, strategy, name in RUNS:
        spec = MarketSpec(infer_kind(symbol), symbol, tf)
        df = fetch_history(spec, start=start, end=end)
        res = Backtester().run(spec, df, strategy=strategy)
        out = os.path.join(outdir, f"{name}.json")
        results_to_json(res, out)
        s = res.stats()
        exits = {}
        for t in res.trades:
            exits[t["exit_reason"]] = exits.get(t["exit_reason"], 0) + 1
        print(f"{stage:5s} {symbol:9s} {tf:3s} {strategy:15s} "
              f"ret {s['return_pct']:+7.2f}%  dd {s['max_drawdown_pct']:6.2f}%  "
              f"trades {s['trades']:4d}  pf {s['profit_factor']}  exits {exits}")
    print(f"[pinned] stage '{stage}' written to {outdir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
