"""HFT harness — the evaluation rig for the high-frequency book.

Harness engineering for the 1m book, mirroring what BACKTESTS.md does for
the standard book: every strategy x symbol x fee tier cell runs through the
SAME event-driven backtester, full costs, and the results land in
data/results/hft_battery.json for the dashboard/docs to cite.

The battery deliberately runs each strategy under BOTH fee tiers:
  perp  (taker 5bp / maker 2bp / slippage 3bp) — the HFT book's default
  spot  (taker 10bp / maker 10bp / slippage 5bp) — base-tier reality
so the fee sensitivity is a measured number per strategy, not a claim.

Usage:  python3 main.py hft-battery [--days 3] [--tier perp|spot|both]
"""
from __future__ import annotations

import json
import os
import time

from bot.backtest import Backtester
from bot.hft import build_hft_config
from bot.hft.triangular import tri_backtest
from bot.strategies import HFT_STRATEGY_NAMES
from config import TRIANGULAR_LEGS, MarketSpec

BATTERY_SPECS = [
    MarketSpec("crypto", "BTC/USDT", "1m", "Bitcoin HFT"),
    MarketSpec("crypto", "ETH/USDT", "1m", "Ethereum HFT"),
    MarketSpec("crypto", "ETH/BTC", "1m", "ETH/BTC cross"),
    MarketSpec("forex", "EURUSD=X", "1m", "EUR/USD HFT"),
]
DAYS_DEFAULT = 3          # 1m bars: 3d = ~4320 bars per spec
WARMUP_BARS = 400         # clears the ema200 column the shared indicator builder computes


def _round_trip_cost_bps(cfg, kind: str = "crypto") -> float:
    """Modeled round trip for a TAKER entry + TAKER exit (bp), for display —
    priced in the SPEC's own kind (forex legs pay the spread model, not the
    crypto taker fee; the old crypto-hardcoded display overstated EUR/USD
    round trips ~2.7x)."""
    c = cfg.costs
    return round((c.fee(kind) + c.slippage(kind)
                  + c.fee(kind) + c.slippage(kind)) * 1e4, 1)


def run_battery(days: int = DAYS_DEFAULT, tiers: tuple[str, ...] = ("perp", "spot"),
                out_path: str = "data/results/hft_battery.json", quiet: bool = False) -> dict:
    started = time.time()
    results = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "days": days, "warmup_bars": WARMUP_BARS, "cells": [],
               "triangular": None}
    frames: dict[str, object] = {}

    for tier in tiers:
        cfg = build_hft_config(fee_tier=tier)
        bt = Backtester(cfg)
        for spec in BATTERY_SPECS:
            if spec.symbol not in frames:
                from bot.data import fetch_history
                try:
                    frames[spec.symbol] = fetch_history(spec, days=days)
                except Exception as exc:
                    if not quiet:
                        print(f"!! {spec.symbol} {spec.timeframe}: data error {exc}")
                    frames[spec.symbol] = None
            df = frames[spec.symbol]
            if df is None or len(df) < WARMUP_BARS + 10:
                continue
            for strat in HFT_STRATEGY_NAMES:
                try:
                    t0 = time.time()
                    res = bt.run(spec, df, strategy=strat, warmup_bars=WARMUP_BARS)
                    s = res.stats()
                    s.update({
                        "tier": tier, "symbol": spec.symbol, "timeframe": spec.timeframe,
                        "strategy": strat, "runtime_s": round(time.time() - t0, 1),
                        "bars": len(df),
                        "taker_round_trip_bps": _round_trip_cost_bps(cfg, spec.kind),
                    })
                    results["cells"].append(s)
                    if not quiet:
                        print(f"  [{tier:4s}] {spec.symbol:10s} {spec.timeframe} {strat:20s} "
                              f"ret {s['return_pct']:+7.2f}%  trades {s['trades']:4d}  "
                              f"wr {s['win_rate_pct']:5.1f}%  pf {str(s['profit_factor']):6s}  "
                              f"({s['runtime_s']}s)")
                except Exception as exc:
                    if not quiet:
                        print(f"!! [{tier}] {spec.symbol} {strat}: {type(exc).__name__}: {exc}")

    # triangular arb monitor over the three crypto legs (any tier's costs —
    # report under the first tier, the edge is tier-independent, the
    # threshold is not)
    try:
        cfg = build_hft_config(fee_tier=tiers[0] if tiers else "perp")
        legs = {}
        for sym in TRIANGULAR_LEGS:
            if frames.get(sym) is not None:
                legs[sym] = frames[sym]
        if len(legs) == 3:
            tri = tri_backtest(legs, cfg.costs,
                               min_edge_bps=cfg.hft.tri_min_edge_bps)
            results["triangular"] = tri
            if not quiet:
                s = tri.get("summary", {})
                print(f"  [tri ] aligned {s.get('bars_aligned')} bars | "
                      f"gross opps {s.get('gross_opportunities')} | "
                      f"fired {s.get('fired')} | max edge {s.get('max_abs_edge_bps')}bp "
                      f"vs cost {s.get('cost_bps')}bp")
    except Exception as exc:
        results["triangular"] = {"error": f"{type(exc).__name__}: {exc}"}

    results["runtime_s"] = round(time.time() - started, 1)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=1, default=str)
    if not quiet:
        print(f"\n[hft-battery] {len(results['cells'])} cells -> {out_path} "
              f"({results['runtime_s']}s)")
    return results
