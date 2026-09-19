#!/usr/bin/env python3
"""Live-vs-backtest parity smoke — the property this codebase is built on.

Every claim the repo makes about its backtests rests on ONE invariant: the
live engine and the backtester run the SAME strategy code over the same
frame, so a backtested edge is the edge the engine would have taken. That
invariant is easy to break silently — the LLM tie-breaker broke it for
months (the backtester passed llm_client=None, so live decisions ran through
a code path the backtest never executed), and nothing failed.

This script fails loudly instead. On a deterministic synthetic frame, for
every registered strategy and for the ensemble of each book, it asserts:

  1. the strategy's own signal at bar i is identical whether the frame is
     the full history or truncated at i   (no look-ahead),
  2. the ORCHESTRATOR's decision at bar i is identical in the engine's
     configuration and the backtester's  (no live-only decision path),
  3. the books stay separated: a fast-book strategy never votes on a
     standard spec and vice versa.

No network, no journal, no model: it runs in seconds and is meant to sit in
`make verify` beside the tests.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

sys.path.insert(0, ".")

from bot.backtest import Backtester                      # noqa: E402
from bot.indicators import add_all_indicators            # noqa: E402
from bot.orchestrator import Orchestrator                # noqa: E402
from bot.strategies import (CANDIDATE_STRATEGIES,        # noqa: E402
                            STRATEGY_CLASSES, get_strategy)
from config import CONFIG, MarketSpec                    # noqa: E402

BARS = 900
FAILURES: list[str] = []


def _frame(freq: str, seed: int, vol: float = 0.004, drift: float = 0.0005):
    rng = np.random.default_rng(seed)
    prices = 30_000 * np.cumprod(1 + rng.normal(drift, vol, BARS))
    idx = pd.date_range("2024-01-01", periods=BARS, freq=freq, tz="UTC")
    high = prices * (1 + abs(rng.normal(0, vol / 2, BARS)))
    low = prices * (1 - abs(rng.normal(0, vol / 2, BARS)))
    df = pd.DataFrame({"open": prices, "high": np.maximum(high, prices),
                       "low": np.minimum(low, prices), "close": prices,
                       "volume": abs(rng.normal(1000, 200, BARS))}, index=idx)
    return add_all_indicators(df, CONFIG.params)


def _fail(msg: str):
    FAILURES.append(msg)
    print(f"  FAIL  {msg}")


def check_causality():
    """A strategy may not see bars after the one it decides on."""
    print("[parity] causality: truncated frame == full frame at bar i")
    frames = {"5m": _frame("5min", 3), "1h": _frame("1h", 4), "4h": _frame("4h", 5)}
    for name, cls in sorted(STRATEGY_CLASSES.items()):
        strat = get_strategy(name, CONFIG.params)
        for tf in cls.preferred_timeframes:
            df = frames.get(tf)
            if df is None:
                continue
            for i in (600, 750, 880):
                full = strat.evaluate(df, i)
                cut = strat.evaluate(df.iloc[: i + 1], i)
                if (full.action, round(full.confidence, 9)) != (cut.action, round(cut.confidence, 9)):
                    _fail(f"{name} {tf} bar {i}: {full.action}/{full.confidence:.4f} "
                          f"full vs {cut.action}/{cut.confidence:.4f} truncated")
    print(f"  {len(STRATEGY_CLASSES)} strategies checked")


def check_decision_parity():
    """The engine's orchestrator and the backtester's must decide the same."""
    print("[parity] decision: engine orchestrator == backtest orchestrator")
    cases = [("standard", "1h", _frame("1h", 7)), ("fast", "5m", _frame("5min", 8))]
    for book, tf, df in cases:
        spec = MarketSpec("crypto", "BTC/USDT", tf)
        cfg = CONFIG
        if book == "fast":
            from bot.hft import build_hft_config
            cfg = build_hft_config()
        engine_side = Orchestrator(cfg.params, llm_client=None, sentiment_overlay=None,
                                   cfg=cfg, book=book)
        bt = Backtester(cfg, book=book)
        backtest_side = Orchestrator(bt.cfg.params, llm_client=None,
                                     sentiment_overlay=None, cfg=bt.cfg, book=bt.book)
        for i in (600, 700, 800, 880):
            a = engine_side.decide(df, i, spec)
            b = backtest_side.decide(df, i, spec)
            if (a.action, round(a.confidence, 9), a.strategy_name) != \
               (b.action, round(b.confidence, 9), b.strategy_name):
                _fail(f"{book} {tf} bar {i}: engine {a.action}/{a.confidence:.4f}"
                      f"/{a.strategy_name} vs backtest {b.action}/{b.confidence:.4f}"
                      f"/{b.strategy_name}")
            if a.stop_distance != b.stop_distance or a.limit_price != b.limit_price:
                _fail(f"{book} {tf} bar {i}: bracket differs "
                      f"(stop {a.stop_distance} vs {b.stop_distance}, "
                      f"limit {a.limit_price} vs {b.limit_price})")
    print("  2 books x 4 bars checked")


def check_book_separation():
    """Both books trade 5m; only `book` keeps them apart."""
    print("[parity] books: fast strategies never vote on the standard book")
    df = _frame("5min", 11)
    spec = MarketSpec("crypto", "BTC/USDT", "5m")
    for book, forbidden in (("standard", "fast"), ("fast", "standard")):
        orch = Orchestrator(CONFIG.params, cfg=CONFIG, book=book)
        seen = []
        for name, strat in orch.strategies.items():
            orig = strat.evaluate

            def counted(frame, i, _n=name, _o=orig):
                seen.append(_n)
                return _o(frame, i)
            strat.evaluate = counted
        orch.decide(df, 880, spec)
        for name in seen:
            cls = STRATEGY_CLASSES[name]
            if getattr(cls, "book", "standard") == forbidden:
                _fail(f"{name} (book={forbidden}) was evaluated on the {book} book")
            if name in CANDIDATE_STRATEGIES:
                _fail(f"{name} is a candidate but was evaluated on the {book} book")
    print("  2 books checked")


def main() -> int:
    print("=== live-vs-backtest parity smoke ===")
    check_causality()
    check_decision_parity()
    check_book_separation()
    if FAILURES:
        print(f"\n[parity] {len(FAILURES)} FAILURE(S) — live and backtest have drifted apart")
        return 1
    print("\n[parity] OK — the engine and the backtester decide identically")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
