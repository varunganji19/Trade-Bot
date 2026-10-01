"""HFT harness — the evaluation rig for the high-frequency book.

Harness engineering for the 1m book, mirroring what docs/archive/BACKTESTS.md does for
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
from bot.strategies import HFT_STRATEGY_NAMES
from config import MarketSpec

BATTERY_SPECS = [
    MarketSpec("crypto", "BTC/USDT", "5m", "Bitcoin fast"),
    MarketSpec("crypto", "ETH/USDT", "5m", "Ethereum fast"),
    MarketSpec("crypto", "ETH/BTC", "5m", "ETH/BTC cross"),
    MarketSpec("forex", "EURUSD=X", "5m", "EUR/USD fast"),
]
DAYS_DEFAULT = 14         # 5m bars: 14d = ~4030 bars per spec
WARMUP_BARS = 400         # clears the ema200 column the shared indicator builder computes


def _round_trip_cost_bps(cfg, kind: str = "crypto") -> float:
    """Modeled round trip for a TAKER entry + TAKER exit (bp), for display —
    priced in the SPEC's own kind (forex legs pay the spread model, not the
    crypto taker fee, which would overstate EUR/USD round trips ~2.7x)."""
    c = cfg.costs
    return round((c.fee(kind) + c.slippage(kind)
                  + c.fee(kind) + c.slippage(kind)) * 1e4, 1)


def run_battery(days: int = DAYS_DEFAULT, tiers: tuple[str, ...] = ("perp", "spot"),
                out_path: str | None = None, quiet: bool = False) -> dict:
    """Descriptive fee-sensitivity battery. It does not write promotion
    verdicts: those come from `make evidence` (bot/experiments.py), so a
    quick 14-day run here can never replace the gate's evidence."""
    from config import db_dir
    out_path = out_path or os.path.join(db_dir(), "results", "hft_battery.json")
    started = time.time()
    results = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "days": days, "warmup_bars": WARMUP_BARS, "cells": [],
               "oos_method": "4-fold walk-forward", "oos_cells": []}
    frames: dict[str, object] = {}
    evidence_errors: list[str] = []

    for tier in tiers:
        cfg = build_hft_config(fee_tier=tier)
        bt = Backtester(cfg, book="fast")
        for spec in BATTERY_SPECS:
            if spec.symbol not in frames:
                from bot.data import fetch_history
                from bot.funding import attach_funding
                try:
                    frames[spec.symbol] = attach_funding(fetch_history(spec, days=days),
                                                         spec.symbol)
                except Exception as exc:
                    if not quiet:
                        print(f"!! {spec.symbol} {spec.timeframe}: data error {exc}")
                    evidence_errors.append(
                        f"{spec.symbol} {spec.timeframe}: data error {type(exc).__name__}: {exc}")
                    frames[spec.symbol] = None
            df = frames[spec.symbol]
            if df is None or len(df) < WARMUP_BARS + 10:
                if df is not None:
                    evidence_errors.append(
                        f"{spec.symbol} {spec.timeframe}: only {len(df)} bars; "
                        f"need {WARMUP_BARS + 10}")
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
                    wf = bt.run_walk_forward(
                        spec, df, folds=4, strategy=strat, progress=False,
                        warmup_bars=WARMUP_BARS,
                    )
                    for fold, fold_stats in enumerate(wf["folds"], start=1):
                        fold_stats.update({"tier": tier, "symbol": spec.symbol,
                                           "timeframe": spec.timeframe,
                                           "strategy": strat, "fold": fold})
                        results["oos_cells"].append(fold_stats)
                    if not quiet:
                        print(f"  [{tier:4s}] {spec.symbol:10s} {spec.timeframe} {strat:20s} "
                              f"ret {s['return_pct']:+7.2f}%  trades {s['trades']:4d}  "
                              f"wr {s['win_rate_pct']:5.1f}%  pf {str(s['profit_factor']):6s}  "
                              f"({s['runtime_s']}s)")
                except Exception as exc:
                    if not quiet:
                        print(f"!! [{tier}] {spec.symbol} {strat}: {type(exc).__name__}: {exc}")
                    evidence_errors.append(
                        f"[{tier}] {spec.symbol} {strat}: {type(exc).__name__}: {exc}")

    results["errors"] = evidence_errors
    results["runtime_s"] = round(time.time() - started, 1)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=1, default=str)
    if not quiet:
        print(f"\n[hft-battery] {len(results['cells'])} cells -> {out_path} "
              f"({results['runtime_s']}s); descriptive only — verdicts come from "
              f"`make evidence`")
    return results
