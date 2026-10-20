"""Reconstruct the missing EURUSD window from agreeing saved snapshots.

This produces separate reconstructed evidence. It never manufactures the
missing pinned cache, changes old research, or claims original byte identity.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from bot.backtest import Backtester, METRICS_VERSION
from config import CONFIG, MarketSpec, bars_per_year
from scripts.rerun_corrected_research import (
    ROOT, configuration, digest, legacy_backtest, offline_guard, resolve_baseline,
)

START, END = "2026-01-04", "2026-09-04"
SOURCES = {
    "EURUSD_1h_2025-09-10_2026-09-10.parquet":
        "fa02815b2843a0e5ab9235e123a50d1fbd0637c820ca0fdcd94d38b2432cc01b",
    "forex_EURUSD_1h_365d_20260919.parquet":
        "9404b153d9a424b9411c107fc66d44145f5fed0b19fc636be799ad774cc58e0c",
    "forex_EURUSD_1h_730d_20261001.parquet":
        "bbc79feb273ac121a6036b0a154e3c96a655c8c9d39b4a995042b05b12602191",
}
COLS = ["open", "high", "low", "close", "volume"]


def reconstruct(paths, start=START, end=END):
    if len({Path(p).resolve() for p in paths}) < 2:
        raise ValueError("at least two distinct saved snapshots must agree")
    selected = []
    lo, hi = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    for path in paths:
        frame = pd.read_parquet(path)
        if (not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None
                or not frame.index.is_unique or not frame.index.is_monotonic_increasing):
            raise ValueError(f"invalid timestamp index: {path}")
        frame.index = frame.index.tz_convert("UTC")
        window = frame.loc[(frame.index >= lo) & (frame.index < hi), COLS].astype(float)
        if window.empty or not window.map(math.isfinite).all().all():
            raise ValueError(f"empty or non-finite reconstructed window: {path}")
        window.attrs = {}
        if selected and not window.equals(selected[0]):
            raise ValueError("saved snapshots disagree on timestamps or OHLCV; recovery refused")
        selected.append(window)
    result = selected[0]
    result.attrs = {"source": "yahoo", "caliber": "spot-fx", "kind": "forex",
                    "symbol": "EURUSD=X", "timeframe": "1h",
                    "reconstructed": True}
    return result


def audit(result, frame):
    """Reconstruct all equity points independently from exported fills and prices."""
    trades, points = result.trades, result.equity_curve
    for trade in trades:
        exit_rate = (CONFIG.costs.maker_fee_forex if CONFIG.costs.maker_pricing
                     and trade["exit_reason"] == "take profit" else CONFIG.costs.fee_forex)
        expected_fee = trade["qty"] * (trade["entry_price"]*CONFIG.costs.fee_forex
                                       + trade["exit_price"]*exit_rate)
        assert math.isclose(trade["fees"], expected_fee, rel_tol=1e-12)
    gross_net = math.fsum((t["exit_price"]-t["entry_price"]) * t["qty"]
                         * (1 if t["side"] == "long" else -1) - t["fees"] for t in trades)
    assert math.isclose(gross_net, math.fsum(t["pnl"] for t in trades), abs_tol=1e-8)
    assert math.isclose(result.end_equity-result.start_equity, gross_net, abs_tol=1e-8)
    worst = 0.0
    for point in points:
        at = pd.Timestamp(point["ts"])
        expected = result.start_equity
        for trade in trades:
            entered, exited = pd.Timestamp(trade["entry_ts"]), pd.Timestamp(trade["exit_ts"])
            if exited <= at:
                expected += trade["pnl"]
            elif entered <= at:
                expected -= trade["entry_price"] * trade["qty"] * CONFIG.costs.fee_forex
                expected += (float(frame.loc[at, "close"])-trade["entry_price"]) * trade["qty"] * (
                    1 if trade["side"] == "long" else -1)
        worst = max(worst, abs(expected-point["equity"]))
    assert worst < 1e-8, f"equity reconstruction differs by {worst}"
    equity = pd.Series([p["equity"] for p in points], dtype=float)
    returns = equity.pct_change().dropna()
    sharpe = (round(float(returns.mean()/returns.std()*math.sqrt(bars_per_year("1h", "forex"))), 2)
              if len(returns) >= 20 and returns.std() > 0 else None)
    stats = result.stats()
    assert stats["sharpe"] == sharpe
    assert stats["max_drawdown_pct"] == round(float((equity/equity.cummax()-1).min()*100), 2)
    assert stats["total_pnl"] == round(gross_net, 2)
    assert stats["fees"] == round(math.fsum(t["fees"] for t in trades), 2)
    assert stats["return_pct"] == round(gross_net/result.start_equity*100, 2)
    return {"passed": True, "reconstructed_equity_points": len(points),
            "max_equity_error": worst, "net_pnl_full_precision": gross_net,
            "sharpe": sharpe, "net_fees_drawdown_sharpe_reconciled": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, default=ROOT/"data/cache")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be new; earlier evidence is preserved")
    baseline = resolve_baseline("0c74d1b")
    sys.addaudithook(offline_guard)
    inputs = [args.cache_root/name for name in SOURCES]
    for path in inputs:
        if digest(path) != SOURCES[path.name]:
            raise ValueError(f"saved source hash changed: {path}")
    frame = reconstruct(inputs)
    assert len(frame) == 4107
    spec = MarketSpec("forex", "EURUSD=X", "1h")
    history = {p: digest(p) for p in (ROOT/"docs/research/verified_metrics_v3").glob("*") if p.is_file()}
    with tempfile.TemporaryDirectory(prefix="algo-eurusd-") as tmp, patch.object(
            CONFIG, "db_path", str(Path(tmp)/"isolated.db")):
        legacy = legacy_backtest(baseline).Backtester().run(spec, frame, strategy="fx_regime_meanrev")
        current = Backtester().run(spec, frame, strategy="fx_regime_meanrev")
        repeated = Backtester().run(spec, frame, strategy="fx_regime_meanrev")
        assert current.trades == repeated.trades and current.equity_curve == repeated.equity_curve
        checks = audit(current, frame)
    assert all(digest(p) == SOURCES[p.name] for p in inputs)
    assert all(digest(p) == expected for p, expected in history.items())
    source_paths = ("bot/backtest.py", "bot/broker.py", "bot/risk.py", "bot/params.py",
                    "config.py", "bot/indicators.py", "bot/strategies/base.py", "bot/strategies/__init__.py",
                    "bot/strategies/fx_regime_meanrev.py", "scripts/recover_eurusd_replay.py")
    provenance = {"reconstruction": True, "original_pinned_artifact_recovered": False,
                  "requested_window": [START, END], "actual_window": [str(frame.index[0]), str(frame.index[-1])],
                  "bars": len(frame), "source_snapshots": [{"path": str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p),
                                                           "sha256": SOURCES[p.name]} for p in inputs],
                  "ohlcv_sha256": hashlib.sha256(frame.to_json(date_format="iso", double_precision=15).encode()).hexdigest(),
                  "baseline_commit": baseline, "configuration": configuration(),
                  "source_sha256": {p: digest(ROOT/p) for p in source_paths},
                  "v1_same_config": legacy.stats(), "v3": current.stats(), "checks": checks,
                  "identical_repeat": True, "historical_v3_files_unchanged": True, "network": "disabled"}
    args.output.mkdir(parents=True)
    (args.output/"checks.json").write_text(json.dumps(provenance, indent=2)+"\n")
    detail = {"metrics_version": METRICS_VERSION, "stats": current.stats(),
              "trades": current.trades, "equity_curve": current.equity_curve, "reconstructed": True}
    (args.output/"EURUSD_1h_fxmr.reconstructed.json").write_text(json.dumps(detail, indent=1, default=str)+"\n")
    print(json.dumps({"stats": current.stats(), "checks": checks}, indent=2))


if __name__ == "__main__":
    main()
