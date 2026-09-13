#!/usr/bin/env python3
"""
AI Trading Bot — command line interface.

Usage:
  python3 main.py backtest [--symbol BTC/USDT] [--timeframe 1h] [--days 365]
                           [--strategy turtle_trend|connors_meanrev|vwap_scalper|ensemble]
                           [--walk-forward] [--json out.json]
  python3 main.py run [--once]                 # paper-trade (live loop or one cycle)
  python3 main.py pause [note]                 # manual halt: blocks NEW entries only
  python3 main.py resume                       # clear the manual pause (new entries allowed)
  python3 main.py market [--mode forex|india]  # show or switch the active market universe
  python3 main.py dashboard [--port 8000]      # web dashboard + chatbot
  python3 main.py status                       # journal summary
  python3 main.py chat "question"              # chatbot from the terminal
  python3 main.py seed-demo                    # demo journal: real backtest replay, marked mode='demo'
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# make project importable when run from anywhere
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import CONFIG, MarketSpec, DEFAULT_WATCHLIST, infer_kind  # noqa: E402


def _spec_from_args(args) -> MarketSpec:
    """CLI symbols arrive unvalidated — infer kind the one shared way and
    uppercase ONLY forex pairs like the dashboard's validator does. India
    tickers ('RELIANCE.NS', '^NSEI') and crypto ('BTC/USDT') stay verbatim
    ('.NS' is already uppercase; mangling it would break the yfinance
    lookup).
    Note: this unifies the old call-site fallbacks. A malformed CLI symbol
    (no '/', '=' or '.NS'/'^') used to guess 'forex' here, now guesses
    'crypto'; no VALID symbol changes behavior — valid ones contain exactly
    one of the kind markers, so only the garbage-input case can shift."""
    sym = args.symbol if infer_kind(args.symbol) in ("crypto", "india") else args.symbol.upper()
    return MarketSpec(infer_kind(args.symbol), sym, args.timeframe)


def cmd_backtest(args):
    from bot.backtest import Backtester, results_to_json
    from bot.data import fetch_history
    import time as _t

    spec = None
    if args.symbol:
        spec = _spec_from_args(args)
    else:
        candidates = [s for s in DEFAULT_WATCHLIST if s.timeframe == args.timeframe]
        spec = candidates[0] if candidates else DEFAULT_WATCHLIST[0]

    window = f"{args.start} → {args.end}" if args.start else f"last {args.days}d"
    print(f"[backtest] {spec.symbol} {spec.timeframe} | {window} | "
          f"strategy: {args.strategy} | capital: ${CONFIG.paper_capital:,.0f}")
    t0 = _t.time()
    df = fetch_history(spec, days=args.days, start=args.start, end=args.end)
    print(f"[backtest] {len(df)} bars loaded ({df.index[0].date()} → {df.index[-1].date()}) "
          f"in {_t.time() - t0:.1f}s")

    bt = Backtester()
    if args.walk_forward:
        res = bt.run_walk_forward(spec, df, folds=args.folds, strategy=None if args.strategy == "ensemble" else args.strategy)
        agg = res["aggregate"]
        print(f"[backtest] walk-forward ({args.folds} folds, out-of-sample):")
        _print_stats(agg)
        for f in res["folds"]:
            print(f"   fold: {f['trades']} trades, ret {f['return_pct']}%, dd {f['max_drawdown_pct']}%, "
                  f"pf {f['profit_factor']}, sharpe {f['sharpe']}")
        if args.json:
            with open(args.json, "w") as fh:
                json.dump(res, fh, indent=1, default=str)
            print(f"[backtest] wrote {args.json}")
    else:
        res = bt.run(spec, df, strategy=None if args.strategy == "ensemble" else args.strategy)
        stats = res.stats()
        _print_stats(stats)
        wins = [t for t in res.trades if t["pnl"] > 0]
        losses = [t for t in res.trades if t["pnl"] <= 0]
        print(f"   wins {len(wins)} / losses {len(losses)} | fees paid ${stats['fees']:.2f}")

        if args.purged_cv:
            # distribution of out-of-sample paths (skfolio CombinatorialPurgedCV)
            from bot.validation import oos_trade_distribution, signal_ic_report, print_purged_cv
            try:
                dist = oos_trade_distribution(res.trades, df, n_folds=args.cv_folds,
                                              n_test_folds=2, purge_bars=args.purge_bars)
                print_purged_cv(dist, f"[backtest] purged-CV OOS distribution "
                                     f"({args.cv_folds} folds, 2 test folds, "
                                     f"{args.purge_bars}-bar purge):")
            except ValueError as e:
                print(f"[backtest] purged-CV skipped: {e}")
            if args.strategy != "ensemble":
                try:
                    from bot.strategies import get_strategy
                    ic = signal_ic_report(df, get_strategy(args.strategy, CONFIG.params),
                                          horizon=args.ic_horizon)
                    print(f"[backtest] signal IC ({args.strategy}, {args.ic_horizon}-bar horizon): "
                          f"pooled {ic['pooled_ic']} (t≈{ic['ic_t_stat_adj']}), "
                          f"path mean {ic['mean_path_ic']} ± {ic['std_path_ic']}, "
                          f"{ic['pct_paths_positive_ic']}% of paths positive")
                except ValueError as e:
                    print(f"[backtest] signal IC skipped: {e}")

        if args.json:
            results_to_json(res, args.json)
            print(f"[backtest] wrote {args.json}")


def _print_stats(s):
    print("  ┌──────────────────────────────────────────────")
    print(f"  │ return {s['return_pct']:+.2f}%  |  pnl ${s['total_pnl']:+,.2f}  |  trades {s['trades']}")
    print(f"  │ win rate {s['win_rate_pct']}%  |  profit factor {s['profit_factor']}  |  sharpe {s['sharpe']}")
    print(f"  │ max drawdown {s['max_drawdown_pct']}%  |  equity ${s['start_equity']:,.0f} → ${s['end_equity']:,.0f}")
    print("  └──────────────────────────────────────────────")


def cmd_validate(args):
    """The honest-statistics battery on one strategy/symbol:
    purged-CV path distribution -> PBO -> Deflated Sharpe -> Monte Carlo -> MinTRL.
    Run with more --days for stronger evidence (multi-year where the data allows)."""
    from bot.backtest import Backtester
    from bot.data import fetch_history
    from bot.validation import (oos_trade_distribution, print_purged_cv,
                                deflated_sharpe,
                                monte_carlo_paths, min_trl)
    from config import bars_per_year

    spec = _spec_from_args(args)

    window = (f"{args.start} → {args.end}" if args.start else f"last {args.days}d")
    print(f"[validate] {spec.symbol} {spec.timeframe} | {window} | "
          f"strategy: {args.strategy}")
    df = fetch_history(spec, days=args.days, start=args.start, end=args.end)
    print(f"[validate] {len(df)} bars ({df.index[0].date()} → {df.index[-1].date()})")

    bt = Backtester()
    res = bt.run(spec, df, strategy=None if args.strategy == "ensemble" else args.strategy)
    stats = res.stats()
    _print_stats(stats)

    report: dict = {"symbol": spec.symbol, "timeframe": spec.timeframe,
                    "strategy": args.strategy, "days": args.days,
                    "start": args.start, "end": args.end,
                    "bars": len(df), "backtest": stats}

    # 1) purged-CV out-of-sample path distribution
    try:
        dist = oos_trade_distribution(res.trades, df, n_folds=args.cv_folds,
                                      n_test_folds=2, purge_bars=args.purge_bars)
        print_purged_cv(dist, "[validate] purged-CV OOS path distribution:")
        report["purged_cv"] = dist
    except ValueError as e:
        print(f"[validate] purged-CV skipped: {e}")

    # 2) PBO over the strategy/config FAMILY (same data, same path layout):
    #    would picking the best-looking in-sample strategy hold out of sample?
    #    Paths stay INDEX-ALIGNED across strategies (an empty path = 0.0: the
    #    account earned nothing there) so every column is the same OOS period.
    if report.get("purged_cv", {}).get("paths"):
        from bot.validation import pbo_cscv
        base_paths = report["purged_cv"]["paths"]
        family = {args.strategy: [p["return_pct"] for p in base_paths]}
        for strat in ("turtle_trend", "connors_meanrev", "vwap_scalper"):
            if strat == args.strategy:
                continue
            try:
                fam_res = Backtester().run(spec, df, strategy=strat)
                if not fam_res.trades:
                    continue
                fam_dist = oos_trade_distribution(fam_res.trades, df,
                                                 n_folds=args.cv_folds,
                                                 n_test_folds=2,
                                                 purge_bars=args.purge_bars)
                if len(fam_dist["paths"]) == len(base_paths):
                    family[strat] = [p["return_pct"] for p in fam_dist["paths"]]
            except ValueError:
                continue
        if len(family) >= 2 and len(base_paths) >= 8:
            pbo = pbo_cscv(family)
            print(f"[validate] PBO over {len(family)}-strategy family, "
                  f"{pbo['n_paths']} aligned OOS paths: "
                  f"{pbo['pbo']} ({pbo['verdict']})")
            report["pbo"] = pbo
        else:
            print("[validate] PBO skipped: family needs >=2 strategies with "
                  ">=8 traded paths each")

    # 3) Deflated Sharpe: how many configs did we try while shipping this?
    #    BACKTESTS.md documents the tried configurations — keep this number
    #    honest as the config history grows. The primary run's equity returns
    #    feed the moment-aware SE (skew/kurtosis widen the SE on fat-tailed
    #    assets; the normal-only SE overstated confidence there).
    sharpes = [s for s in (args.trial_sharpes or [])]
    if sharpes:
        import pandas as pd
        eq = pd.Series([p["equity"] for p in res.equity_curve]) if res.equity_curve else None
        eq_rets = eq.pct_change().dropna().tolist() if eq is not None and len(eq) > 2 else None
        dsr = deflated_sharpe(sharpes, n_obs=len(df),
                              bars_per_year=bars_per_year(spec.timeframe, spec.kind),
                              returns=eq_rets)
        print(f"[validate] Deflated Sharpe over {len(sharpes)} documented trial Sharpes "
              f"(SE model: {dsr.get('se_model', 'normal')}): {dsr}")
        report["deflated_sharpe"] = dsr

    # 4) Monte Carlo: the order of trades was one draw — show the distribution
    mc = monte_carlo_paths(res.trades, starting_capital=CONFIG.paper_capital)
    if mc.get("n_sims"):
        print(f"[validate] Monte Carlo ({mc['n_sims']} resampled orders): "
              f"terminal equity p5 ${mc['terminal_p5']:,.0f} / p50 ${mc['terminal_p50']:,.0f} / "
              f"p95 ${mc['terminal_p95']:,.0f} | lose money {mc['p_lose_money']}% of sims | "
              f"DD beyond -10% in {mc['p_dd_beyond_10pct']}% of sims")
    report["monte_carlo"] = mc

    # 5) MinTRL: how much OOS track record the Sharpe needs to be believed
    if stats.get("sharpe") not in (None, 0):
        mtrl = min_trl(float(stats["sharpe"]), bars_per_year(spec.timeframe, spec.kind))
        print(f"[validate] MinTRL for sharpe {stats['sharpe']}: "
              f"{mtrl.get('min_years')} years ≈ {mtrl.get('min_bars')} "
              f"{spec.timeframe} bars of OOS record at 95% confidence")
        report["min_trl"] = mtrl

    out = args.json or os.path.join("data", "results",
                                    f"validation_{spec.symbol.replace('/', '')}_"
                                    f"{spec.timeframe}_{args.days}d.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump(report, fh, indent=1, default=str)
    print(f"[validate] wrote {out}")

    if args.report:
        from bot.report import render_validation_report
        from config import utc_now
        report["generated_at"] = utc_now()
        with open(args.report, "w") as fh:
            fh.write(render_validation_report(report))
        print(f"[validate] wrote {args.report}")


def cmd_run(args):
    from bot.engine import TradingEngine
    from config import apply_market_mode
    apply_market_mode()  # the persisted market mode seeds/normalizes watchlist.json
    engine = TradingEngine(mode="paper")
    if args.once:
        summary = engine.run_cycle()
        print(json.dumps(summary, indent=1, default=str))
    else:
        engine.run_forever(interval=args.interval or CONFIG.live_interval_seconds)


def cmd_pause(args):
    from bot.pause import set_paused
    if not set_paused(True, args.note):
        print("[pause] could not write the pause flag (disk error?) — trading is NOT paused")
        sys.exit(1)
    print("[pause] trading paused."
          + (f" Note: {args.note}" if args.note else ""))
    print("[pause] New entries are now blocked. Open positions (if any) are still "
          "managed — nothing is force-closed.")
    print("[pause] This stays until you run: python3 main.py resume")


def cmd_resume(args):
    from bot.pause import set_paused
    if not set_paused(False):
        print("[resume] could not write the pause flag (disk error?) — trading is still paused")
        sys.exit(1)
    print("[resume] trading resumed — new entries are allowed again (all other "
          "risk gates still apply).")


def cmd_market(args):
    """Show or switch the active market universe (forex default | india).
    Switching is REFUSED while open paper positions exist: the watchlist is
    rewritten by the switch, and an open position whose market dropped out
    of the universe would be orphaned (its feed gone, its books gone)."""
    from bot.journal import Journal
    from config import get_market_mode, set_market_mode, active_specs
    if not args.mode:
        mode = get_market_mode()
        print(f"[market] active mode: {mode}")
        for s in active_specs(mode):
            print(f"  {s.kind:6s} {s.symbol:14s} {s.timeframe}  {s.display}")
        return
    if args.mode not in ("forex", "india"):
        print(f"[market] unknown mode {args.mode!r} (expected --mode forex|india)")
        sys.exit(1)
    open_trades = Journal().open_trades()
    if open_trades:
        print(f"[market] REFUSING to switch: {len(open_trades)} open paper "
              f"position(s) exist:")
        for t in open_trades:
            print(f"  {t['side'].upper()} {t['symbol']} qty {t['qty']} @ {t['entry_price']}")
        print("[market] close them first (dashboard or close_manual) so nothing "
              "gets orphaned when its market's feed drops out of the watchlist.")
        sys.exit(1)
    if not set_market_mode(args.mode):
        print(f"[market] could not persist mode {args.mode!r} (disk error?) — "
              "the market is NOT switched")
        sys.exit(1)
    print(f"[market] switched to {args.mode} — active universe:")
    for s in active_specs(args.mode):
        print(f"  {s.kind:6s} {s.symbol:14s} {s.timeframe}  {s.display}")
    print("[market] data/watchlist.json rewritten to match; restart run/"
          "dashboard to load the new universe.")


def cmd_dashboard(args):
    import errno
    import socket
    import uvicorn
    from config import apply_market_mode
    from bot.dashboard import app
    apply_market_mode()  # the persisted market mode seeds/normalizes watchlist.json

    # bind check BEFORE uvicorn starts: a second dashboard on the same port
    # used to surface as a raw "[Errno 48] address already in use" traceback
    # that read like a crash — name the squatter and the two real outs instead.
    # SO_REUSEADDR matches uvicorn's own socket settings so TIME_WAIT remnants
    # from a just-killed server don't read as a false "in use".
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", args.port))
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        who = _port_owner(args.port)
        print(f"[dashboard] port {args.port} is already in use"
              + (f" by {who}" if who else "") + ".\n"
              f"[dashboard]   · an existing dashboard may already be live at "
              f"http://127.0.0.1:{args.port} — open it in your browser\n"
              f"[dashboard]   · or start this one on another port: "
              f"python3 main.py dashboard --port {args.port + 1}")
        # non-zero exit: `make demo` used to report success with nothing served
        # (the operator opens the squatter's page instead of the dashboard)
        sys.exit(1)
    finally:
        probe.close()

    print(f"[dashboard] http://127.0.0.1:{args.port}  (Ctrl-C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


def _port_owner(port: int) -> str | None:
    """Best-effort PID+command of whatever holds `port` (for the bind-failure
    message); returns None when nothing can be identified — never raises."""
    import subprocess
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=5).stdout
        line = out.strip().splitlines()[-1]  # header first, listener last
        pid, cmd = line.split()[1], line.split()[0]
        return f"PID {pid} ({cmd})"
    except Exception:
        return None


def cmd_status(args):
    from bot.journal import Journal
    from bot.pause import is_paused
    j = Journal()
    stats = j.stats()
    print(json.dumps(stats, indent=1))
    paused, pause_note = is_paused()
    if paused:
        print("trading: PAUSED (manual flag) — blocks new entries only; open positions "
              "are still managed, nothing is force-closed"
              + (f" · note: {pause_note}" if pause_note else ""))
    else:
        print("trading: active (no manual pause)")
    open_trades = j.open_trades()
    if open_trades:
        print(f"open positions: {len(open_trades)}")
        for t in open_trades:
            print(f"  {t['side'].upper()} {t['symbol']} qty {t['qty']} @ {t['entry_price']} ({t['strategy']})")


def cmd_chat(args):
    from bot.chatbot import ChatBot
    print(ChatBot().answer(args.question))


def cmd_seed_demo(args):
    from bot.seed_demo import seed
    n = seed()
    print(f"[seed-demo] journal seeded: {n['trades']} trades, {n['decisions']} decisions, "
          f"{n['equity']} equity points. Run `python3 main.py dashboard`.")


def cmd_kronos(args):
    """Offline Kronos evaluation: walk history bar-by-bar, resolve IC, report
    whether the model has EARNED an orchestrator vote on this data."""
    from bot.data import fetch_history
    from bot.kronos_signal import KronosSignalEngine

    spec = _spec_from_args(args)
    eng = KronosSignalEngine()
    if not eng.predictor.available:
        print("[kronos] model unavailable — vendor it first:")
        print("  git clone https://github.com/shiyu-coder/Kronos models/kronos")
        print("  pip install torch transformers")
        sys.exit(1)

    print(f"[kronos] {spec.symbol} {spec.timeframe} | {args.days}d | "
          f"model {eng.cfg.model_name} | horizon {args.horizon} bars")
    df = fetch_history(spec, days=args.days)
    n = len(df)
    step = max(1, args.step)
    n_eval = 0
    for i in range(240, n - args.horizon, step):
        sig = eng.evaluate(df.iloc[: i + 1], horizon=args.horizon)
        if sig is not None:
            eng.log_and_maybe_resolve(df, sig,
                                      market=f"{spec.symbol}|{spec.timeframe}")
            n_eval += 1
            if n_eval % 10 == 0:
                ic = eng.tracker.ic()
                print(f"  ... {n_eval} forecasts | IC so far: "
                      f"{ic if ic is None else round(ic, 4)}")
    ic = eng.tracker.ic()
    total = eng.tracker.n()
    print(f"\n[kronos] resolved forecasts: {total}")
    print(f"[kronos] rolling IC: {ic if ic is None else round(ic, 4)} "
          f"(hurdle {eng.cfg.ic_hurdle}, min obs {eng.cfg.min_observations})")
    if eng.promoted():
        print("[kronos] VERDICT: promoted — Kronos EARNED an orchestrator vote")
    else:
        print("[kronos] VERDICT: NOT promoted — remains a tracked non-voter")


def cmd_shadow(args):
    """Shadow Account: what the bot did vs what its own rules would have done."""
    from bot.journal import Journal
    from bot.shadow import behavior_profile, rule_adherence, shadow_compare
    from bot.data import fetch_history
    from config import CONFIG

    j = Journal()
    trades = j.recent_trades(limit=2000, mode=None if args.include_demo else "paper")
    demo = j.trade_mode_counts().get("demo", 0)
    if demo and not args.include_demo:
        print(f"[shadow] excluding {demo} mode='demo' (seed-demo backtest-replay) rows — "
              f"pass --include-demo to audit them too")
    closed = [t for t in trades if t["status"] == "CLOSED"]
    if not closed:
        print("[shadow] journal has no closed trades — run the bot (or seed-demo) first.")
        return

    profile = behavior_profile(trades)
    print(f"\n[shadow] behavior profile over {profile['n_trades']} closed trades")
    print(f"  win rate {profile['win_rate_pct']}% | avg hold {profile['avg_hold_hours']}h | "
          f"avg R {profile['avg_r']} (best {profile['max_r']}, worst {profile['min_r']})")
    print(f"  disposition gap {profile['disposition_gap_hours']}h "
          f"(negative = cut winners early) | blew through stop: {profile['n_blew_through_stop']}")

    # group trades by (symbol, timeframe) — the row's own timeframe when the
    # journal records one (post-2026-09-04), else the first watchlist spec
    by_spec: dict = {}
    for t in closed:
        tf = t.get("timeframe")
        if not tf:
            for spec in CONFIG.watchlist:
                if spec.symbol == t["symbol"]:
                    tf = spec.timeframe
                    break
        if not tf:
            continue
        by_spec.setdefault((t["symbol"], tf), []).append(t)

    report: dict = {"profile": profile, "symbols": {}}
    for (symbol, timeframe), sym_trades in sorted(by_spec.items()):
        spec = next((s for s in CONFIG.watchlist
                     if s.symbol == symbol and s.timeframe == timeframe), None)
        if spec is None:  # symbol/timeframe no longer traded: pick kind by format
            spec = MarketSpec(infer_kind(symbol), symbol, timeframe)
        print(f"\n[shadow] {symbol} ({timeframe}): {len(sym_trades)} closed trades")
        try:
            days = max(90, _parse_last_days(sym_trades))
            fetch_spec = MarketSpec(spec.kind, symbol, timeframe)
            df = fetch_history(fetch_spec, days=days)
        except Exception as exc:
            print(f"  data unavailable ({type(exc).__name__}: {exc}) — replay skipped")
            continue
        rep = rule_adherence(sym_trades, df, CONFIG.params)
        print(f"  rule adherence: {rep.adherence_pct}% on-rule "
              f"({rep.n_on_rule} on-rule, {rep.n_late} late, "
              f"{rep.n_rule_break} rule breaks, {rep.n_unknown} unknown)")
        for tr in rep.trades:
            if tr["verdict"] != "on-rule":
                print(f"    #{tr['id']} {tr['verdict']}: {tr.get('note', '')[:100]}")
        comp = shadow_compare(spec, sym_trades, df)
        for sh in comp.get("shadows") or []:
            if sh.get("error"):
                print(f"  shadow ({sh['strategy']}): {sh['error']}")
                continue
            print(f"  actual journal PnL ${comp['actual']['pnl']:+.2f} over "
                  f"{comp['actual']['trades']} trades | shadow ({sh['strategy']}, "
                  f"pure rules, same window): ${sh['pnl']:+.2f} over {sh['trades']} trades "
                  f"| gap {comp['actual']['pnl'] - sh['pnl']:+.2f}")
        report["symbols"][f"{symbol} {timeframe}"] = {
            "adherence_pct": rep.adherence_pct, "on_rule": rep.n_on_rule,
            "late": rep.n_late, "rule_breaks": rep.n_rule_break,
            "unknown": rep.n_unknown, "deviations": [
                {"id": tr["id"], "verdict": tr["verdict"], "note": tr.get("note", "")}
                for tr in rep.trades if tr["verdict"] != "on-rule"],
            "comparison": comp,
        }

    out = args.json or "data/results/shadow_report.json"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as f:
        json.dump(report, f, indent=1, default=str)
    print(f"\n[shadow] report written to {out}")


def _parse_last_days(trades: list) -> int:
    """Days from the oldest journal entry to now (for history fetch depth)."""
    import pandas as pd
    stamps = [pd.Timestamp(t.get("opened_ts") or t.get("entry_ts"))
              for t in trades if t.get("opened_ts") or t.get("entry_ts")]
    if not stamps:
        return 120
    first = min(stamps)
    if first.tzinfo is None:
        first = first.tz_localize("UTC")
    return max(90, int((pd.Timestamp.now(tz="UTC") - first).total_seconds() // 86400) + 7)


def main():
    p = argparse.ArgumentParser(prog="ai-trading-bot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    bt = sub.add_parser("backtest", help="backtest a strategy on real historical data")
    bt.add_argument("--symbol", default=None, help="e.g. BTC/USDT, ETH/USDT, EURUSD=X (default: watchlist pick)")
    bt.add_argument("--timeframe", default="1h", choices=["5m", "15m", "1h", "4h", "1d"])
    bt.add_argument("--days", type=int, default=365,
                    help="rolling window ending today (use --start/--end to pin)")
    bt.add_argument("--start", default=None,
                    help="pinned window start YYYY-MM-DD (byte-identical reruns; overrides --days)")
    bt.add_argument("--end", default=None,
                    help="pinned window end YYYY-MM-DD (with --start)")
    bt.add_argument("--strategy", default="ensemble",
                    choices=["ensemble", "turtle_trend", "connors_meanrev", "vwap_scalper",
                             "fx_regime_meanrev", "ts_momentum"])
    bt.add_argument("--walk-forward", action="store_true")
    bt.add_argument("--folds", type=int, default=4)
    bt.add_argument("--purged-cv", action="store_true",
                    help="distribution of out-of-sample paths via skfolio CombinatorialPurgedCV")
    bt.add_argument("--cv-folds", type=int, default=8, help="fold count for --purged-cv")
    bt.add_argument("--purge-bars", type=int, default=24,
                    help="bars purged at path boundaries for --purged-cv")
    bt.add_argument("--ic-horizon", type=int, default=24,
                    help="forward-return horizon (bars) for signal IC under --purged-cv")
    bt.add_argument("--json", default=None, help="write results JSON here")
    bt.set_defaults(fn=cmd_backtest)

    run = sub.add_parser("run", help="run the paper-trading engine")
    run.add_argument("--once", action="store_true", help="one cycle then exit")
    run.add_argument("--interval", type=int, default=None, help="seconds between cycles")
    run.set_defaults(fn=cmd_run)

    pz = sub.add_parser("pause", help="manual halt: block NEW entries only "
                                      "(open positions stay managed, nothing is force-closed)")
    pz.add_argument("note", nargs="?", default="",
                    help="optional reason recorded with the flag (quote multi-word notes)")
    pz.set_defaults(fn=cmd_pause)

    rp = sub.add_parser("resume", help="clear the manual pause (new entries allowed again)")
    rp.set_defaults(fn=cmd_resume)

    mk = sub.add_parser("market",
                        help="show or switch the active market universe "
                             "(--mode forex|india; switching refused while "
                             "paper positions are open)")
    mk.add_argument("--mode", default=None, choices=["forex", "india"],
                    help="switch the persisted market mode and rewrite "
                         "watchlist.json to that mode's universe "
                         "(omit to just show the active mode)")
    mk.set_defaults(fn=cmd_market)

    va = sub.add_parser("validate",
                        help="honest-statistics battery: purged-CV, PBO, Deflated Sharpe, "
                             "Monte Carlo, MinTRL for one strategy/symbol")
    va.add_argument("--symbol", default="BTC/USDT",
                    help="e.g. BTC/USDT, ETH/USDT, EURUSD=X")
    va.add_argument("--timeframe", default="1h", choices=["5m", "15m", "1h", "4h", "1d"])
    va.add_argument("--days", type=int, default=730,
                    help="history depth; more days = stronger statistics (multi-year where data allows)")
    va.add_argument("--start", default=None,
                    help="pinned window start YYYY-MM-DD (byte-identical reruns; overrides --days)")
    va.add_argument("--end", default=None, help="pinned window end YYYY-MM-DD (with --start)")
    va.add_argument("--strategy", default="turtle_trend",
                    choices=["ensemble", "turtle_trend", "connors_meanrev", "vwap_scalper",
                             "fx_regime_meanrev", "ts_momentum"])
    va.add_argument("--cv-folds", type=int, default=8)
    va.add_argument("--purge-bars", type=int, default=24)
    va.add_argument("--trial-sharpes", type=float, nargs="*", default=None,
                    help="the Sharpes of the documented config trials (BACKTESTS.md) "
                         "for the Deflated Sharpe correction")
    va.add_argument("--report", default=None,
                    help="also render the report as Markdown here (e.g. REPORT.md)")
    va.add_argument("--json", default=None)
    va.set_defaults(fn=cmd_validate)

    dash = sub.add_parser("dashboard", help="start the web dashboard")
    dash.add_argument("--port", type=int, default=8000)
    dash.set_defaults(fn=cmd_dashboard)

    st = sub.add_parser("status", help="journal summary")
    st.set_defaults(fn=cmd_status)

    ch = sub.add_parser("chat", help="ask the journal-aware chatbot")
    ch.add_argument("question")
    ch.set_defaults(fn=cmd_chat)

    sd = sub.add_parser("seed-demo", help="seed the journal with a demo history "
                                          "(real backtest replay, mode='demo')")
    sd.set_defaults(fn=cmd_seed_demo)

    kr = sub.add_parser("kronos", help="offline Kronos IC evaluation on history")
    kr.add_argument("--symbol", default="BTC/USDT")
    kr.add_argument("--timeframe", default="1h", choices=["5m", "15m", "1h", "4h", "1d"])
    kr.add_argument("--days", type=int, default=60)
    kr.add_argument("--horizon", type=int, default=24, help="forecast horizon in bars")
    kr.add_argument("--step", type=int, default=8,
                    help="evaluate every Nth bar (compute control)")
    kr.set_defaults(fn=cmd_kronos)

    sh = sub.add_parser("shadow", help="Shadow Account: journal vs its own rules")
    sh.add_argument("--json", default=None, help="report path (default data/results/shadow_report.json)")
    sh.add_argument("--include-demo", action="store_true",
                    help="audit mode='demo' (seed-demo backtest-replay) rows too; "
                         "default audits the bot's own paper record only")
    sh.set_defaults(fn=cmd_shadow)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
