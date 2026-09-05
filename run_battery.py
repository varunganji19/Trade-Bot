#!/usr/bin/env python3
"""Run the full backtest battery across symbols/strategies and print a table.

Writes each result JSON into data/results/ and prints a summary matrix.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.backtest import Backtester
from bot.data import fetch_history
from config import MarketSpec

BATTERY = [
    # crypto 1h (turtle + ensemble)
    MarketSpec("crypto", "BTC/USDT", "1h", "Bitcoin"),
    MarketSpec("crypto", "ETH/USDT", "1h", "Ethereum"),
    MarketSpec("crypto", "SOL/USDT", "1h", "Solana"),
    # crypto 15m (scalper + ensemble)
    MarketSpec("crypto", "BTC/USDT", "15m", "Bitcoin scalp"),
    MarketSpec("crypto", "ETH/USDT", "15m", "Ethereum scalp"),
    # crypto 4h (connors + ensemble)
    MarketSpec("crypto", "BTC/USDT", "4h", "Bitcoin meanrev"),
    MarketSpec("crypto", "ETH/USDT", "4h", "Ethereum meanrev"),
    # forex 1h (turtle + ensemble)
    MarketSpec("forex", "EURUSD=X", "1h", "EUR/USD"),
    MarketSpec("forex", "GBPUSD=X", "1h", "GBP/USD"),
    MarketSpec("forex", "USDJPY=X", "1h", "USD/JPY"),
]

DAYS = {"1h": 365, "5m": 30, "15m": 60, "4h": 730, "1d": 1825}
STRATEGIES = ["turtle_trend", "connors_meanrev", "vwap_scalper", "ensemble"]
# run each strategy only on its own timeframe (as shipped in the watchlist)
SKIP = {("1h", "vwap_scalper"), ("1h", "connors_meanrev"),
        ("15m", "turtle_trend"), ("15m", "connors_meanrev"),
        ("4h", "turtle_trend"), ("4h", "vwap_scalper")}


def main():
    bt = Backtester()
    rows = []
    t_start = time.time()
    for spec in BATTERY:
        days = DAYS[spec.timeframe]
        try:
            df = fetch_history(spec, days=days)
        except Exception as exc:
            print(f"!! {spec.symbol} {spec.timeframe}: data error {exc}")
            continue
        cache = {}
        for strat in STRATEGIES:
            if (spec.timeframe, strat) in SKIP:
                continue
            key = f"{spec.symbol.replace('/', '').replace('=X','')}_{spec.timeframe}_{strat}"
            try:
                t0 = time.time()
                res = bt.run(spec, df, strategy=None if strat == "ensemble" else strat)
                s = res.stats()
                s["runtime_s"] = round(time.time() - t0, 1)
                s["bars"] = len(df)
                rows.append(s)
                cache[key] = s
                with open(f"data/results/{key}.json", "w") as f:
                    json.dump({"stats": s, "trades": res.trades,
                               "equity_curve": res.equity_curve}, f, indent=1, default=str)
                print(f"  {spec.symbol:12s} {spec.timeframe} {strat:16s} "
                      f"ret {s['return_pct']:+7.2f}%  dd {s['max_drawdown_pct']:6.2f}%  "
                      f"trades {s['trades']:3d}  wr {s['win_rate_pct']:5.1f}%  "
                      f"pf {str(s['profit_factor']):5s}  ({s['runtime_s']}s)")
            except Exception as exc:
                print(f"!! {key}: {type(exc).__name__}: {exc}")

    print(f"\n{'='*100}\nSUMMARY ({len(rows)} runs, {time.time()-t_start:.0f}s total)\n{'='*100}")
    hdr = f"{'symbol':12s} {'tf':4s} {'strategy':16s} {'ret%':>8s} {'dd%':>7s} {'trades':>6s} {'wr%':>6s} {'pf':>6s} {'sharpe':>7s}"
    print(hdr); print("-" * len(hdr))
    for s in rows:
        print(f"{s['symbol']:12s} {s['timeframe']:4s} {s['strategy']:16s} "
              f"{s['return_pct']:+8.2f} {s['max_drawdown_pct']:7.2f} {s['trades']:6d} "
              f"{s['win_rate_pct']:6.1f} {str(s['profit_factor']):>6s} {str(s['sharpe']):>7s}")


if __name__ == "__main__":
    main()
