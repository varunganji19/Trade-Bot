#!/usr/bin/env python3
"""
AI Trading Bot — command line interface.

Usage:
  python3 main.py backtest [--symbol BTC/USDT] [--timeframe 1h] [--days 365]
                           [--strategy ensemble|turtle_trend|connors_meanrev|vwap_scalper|fx_regime_meanrev|ts_momentum]
                           [--walk-forward] [--json out.json]
  python3 main.py validate --symbol BTC/USDT [--strategy turtle_trend]
                           [--trial-sharpes 0.8 1.1 ...] [--report REPORT.md]
  python3 main.py run [--once]                 # paper-trade (live loop or one cycle)
  python3 main.py hft-backtest [--symbol BTC/USDT] [--strategy hft_micro_breakout|hft_exhaustion_fade|hft_ofi_momentum]
  python3 main.py hft-run [--once]             # fast paper book (separate 5m account)
  python3 main.py hft-status                   # HFT book journal summary
  python3 main.py hft-battery [--days 3] [--tier perp|spot|both]
  python3 main.py kronos [--symbol BTC/USDT]   # offline Kronos IC evaluation (tracked non-voter verdict)
  python3 main.py shadow [--include-demo]      # journal-vs-own-rules Shadow Account report
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


def _strip_inline_comment(s: str) -> str:
    """Cut a ` #` comment outside quotes (a `#` inside '...'/"..." stays)."""
    in_single = in_double = False
    for i, ch in enumerate(s):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double \
                and i > 0 and s[i - 1] in (" ", "\t"):
            return s[:i].rstrip()
    return s


def _load_env_file(path: str | None = None) -> None:
    """Load KEY=VALUE pairs from .env into os.environ BEFORE config reads
    them (existing process env wins). Defaults to the SCRIPT dir's .env
    (not the CWD's) so `python3 path/to/main.py` works from anywhere. The
    documented workflow ships .env.example -> .env — but nothing ever loaded
    it, so a DASHBOARD_TOKEN placed there silently left dashboard auth OFF
    while the operator believed it was on. Deliberately dependency-free;
    no shell expansion, no multiline values. A leading `export ` is stripped
    and ` #` comments outside quotes are ignored."""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                key, _, value = line.partition("=")
                key = key.strip()
                value = _strip_inline_comment(value.strip())
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass  # no .env is the normal case


_load_env_file()

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
    kind = infer_kind(args.symbol)
    sym = args.symbol if kind in ("crypto", "india") else args.symbol.upper()
    return MarketSpec(kind, sym, args.timeframe)


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


# ------------------------------------------------------------------ HFT book
def cmd_hft_backtest(args):
    """Backtest a fast-book strategy on 5m data with the book's own fee tier."""
    from bot.backtest import Backtester
    from bot.data import fetch_history
    import time as _t

    from bot.hft import build_hft_config, HFT_WATCHLIST
    cfg = build_hft_config(fee_tier=args.fee_tier)
    if args.symbol:
        sym = args.symbol.upper()
        kind = infer_kind(sym)
        spec = MarketSpec(kind, sym, args.timeframe)
    else:
        spec = HFT_WATCHLIST[0]
    c = cfg.costs
    rt_bps = (c.fee(spec.kind) + c.slippage(spec.kind)) * 2 * 1e4
    window = f"{args.start} → {args.end}" if args.start else f"last {args.days}d"
    print(f"[hft-backtest] {spec.symbol} {spec.timeframe} | {window} | strategy: {args.strategy} "
          f"| tier: {args.fee_tier or os.environ.get('HFT_FEE_TIER', 'perp')} "
          f"| capital: ${cfg.paper_capital:,.0f} | taker RT ~{rt_bps:.1f}bp")
    t0 = _t.time()
    df = fetch_history(spec, days=args.days, start=args.start, end=args.end)
    print(f"[hft-backtest] {len(df)} bars ({df.index[0]} → {df.index[-1]}) in {_t.time() - t0:.1f}s")
    bt = Backtester(cfg, book="fast")
    res = bt.run(spec, df, strategy=args.strategy, warmup_bars=args.warmup_bars)
    stats = res.stats()
    _print_stats(stats)
    print(f"   fees paid ${stats['fees']:.2f}")
    hist = {}
    for t in res.trades:
        hist[t["exit_reason"]] = hist.get(t["exit_reason"], 0) + 1
    print("   exits: " + (", ".join(f"{k} x{v}" for k, v in sorted(hist.items())) or "none"))
    if args.json:
        from bot.backtest import results_to_json
        results_to_json(res, args.json)
        print(f"[hft-backtest] wrote {args.json}")


def _run_owned(engine, once: bool, interval: int, label: str):
    """Run a live engine holding its book's lease, with a readable refusal.

    A second engine on one book would fork the account, so the lease is not
    advisory — but the operator's fix is simply to stop the other one, which
    deserves a sentence rather than a traceback.
    """
    from bot.journal import BookOwnedError
    try:
        if once:
            # a single cycle owns the book for its duration too — it writes
            # the same checkpoints a continuous loop does
            with engine.own_book():
                return engine.run_cycle()
        engine.run_forever(interval=interval)   # claims the lease itself
    except BookOwnedError as exc:
        print(f"[{label}] refusing to start — {exc}")
        sys.exit(1)
    return None


def cmd_hft_run(args):
    """Run the HFT paper engine (the separate high-frequency book)."""
    from bot.hft import build_hft_engine
    from config import apply_market_mode
    apply_market_mode()  # keeps the standard book's watchlist normalized; HFT book ignores it
    engine = build_hft_engine()
    interval = args.interval or CONFIG.hft.live_interval_seconds
    summary = _run_owned(engine, args.once, interval, "hft")
    if args.once:
        print(json.dumps(summary, indent=1, default=str))


def cmd_hft_status(args):
    """The HFT book's journal summary: separate account, separate history."""
    from bot.journal import Journal
    j = Journal()
    s = j.stats(mode="hft")
    print("[hft] high-frequency paper book (mode='hft' journal rows):")
    print(f"  return {s['return_pct']:+.2f}%  |  pnl ${s['total_pnl']:+,.2f}  |  "
          f"closed trades {s['closed_trades']}  |  win rate {s['win_rate']}%  |  "
          f"pf {s['profit_factor']}  |  max dd {s['max_drawdown_pct']}%")
    print(f"  equity ${s['current_equity']:,.2f} (start ${s['start_equity']:,.2f})")
    if s.get("by_strategy"):
        for name, v in s["by_strategy"].items():
            print(f"   · {name}: {v['trades']} trades, {v['wins']} wins, "
                  f"pnl ${v['pnl']:+,.2f}")
    open_rows = j.open_trades(mode="hft")
    print(f"  open positions: {len(open_rows)}")
    for r in open_rows:
        print(f"   - {r['side']} {r['symbol']} {r.get('timeframe') or '1m'} "
              f"qty {r['qty']} @ {r['entry_price']} via {r['strategy']}")
    eq = j.equity_curve(limit=1, mode="hft")
    if eq:
        print(f"  last equity point: {eq[-1]['equity']} (cash {eq[-1].get('cash')}) @ {eq[-1]['ts']}")


def cmd_hft_battery(args):
    """The HFT harness: every strategy x symbol x fee tier, plus the
    the measured fee-sensitivity table (HFT.md)."""
    from bot.hft.harness import run_battery
    tiers = ("perp", "spot") if args.tier == "both" else (args.tier,)
    run_battery(days=args.days, tiers=tiers, quiet=False)


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
    sharpes = list(args.trial_sharpes or [])
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
    interval = args.interval or CONFIG.live_interval_seconds
    summary = _run_owned(engine, args.once, interval, "engine")
    if args.once:
        print(json.dumps(summary, indent=1, default=str))


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
    live = _live_engine_state()
    if live:
        print(f"[market] REFUSING to switch: the {live} engine reports "
              f"desired='running' (engine_state.json) — stop the engine "
              f"(dashboard Stop, or kill the run loop) before switching, so no "
              f"live cycle trades the old universe mid-switch.")
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


def _live_engine_state() -> str | None:
    """'paper'/'hft' when that book's engine_state file says desired='running',
    else None. The market switch is journal-blind without this: a live engine
    mid-cycle would keep trading the OLD universe after the watchlist is
    rewritten. Never raises (missing/unreadable file = not running)."""
    from config import CONFIG
    base = os.path.dirname(os.path.abspath(CONFIG.db_path))
    for book, fname in (("paper", "engine_state.json"), ("hft", "hft_engine_state.json")):
        try:
            with open(os.path.join(base, fname)) as fh:
                if json.load(fh).get("desired") == "running":
                    return book
        except (OSError, ValueError):
            continue
    return None


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
    if args.horizon < 1 or args.horizon > eng.cfg.max_context:
        # the predictor generates at most max_context steps; a longer horizon
        # raises inside every single forecast instead of once, here
        print(f"[kronos] --horizon must be 1..{eng.cfg.max_context} "
              f"(the predictor's max_context), got {args.horizon}")
        sys.exit(2)
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

    hb = sub.add_parser("hft-backtest",
                        help="backtest a fast-book strategy on 5m data "
                             "(the fast book's fee tier + capital)")
    hb.add_argument("--symbol", default=None, help="e.g. BTC/USDT (the fast book is USD-only)")
    hb.add_argument("--timeframe", default="5m", choices=["5m", "15m"])
    hb.add_argument("--days", type=int, default=14, help="history depth (5m bars: 14d ≈ 4030 bars)")
    hb.add_argument("--start", default=None, help="pinned window start YYYY-MM-DD (overrides --days)")
    hb.add_argument("--end", default=None, help="pinned window end YYYY-MM-DD (with --start)")
    hb.add_argument("--strategy", default="hft_micro_breakout",
                    choices=["hft_micro_breakout", "hft_exhaustion_fade",
                             "hft_market_maker", "hft_ofi_momentum"])
    hb.add_argument("--warmup-bars", type=int, default=400,
                    help="bars before trading starts (clears the ema200 column)")
    hb.add_argument("--fee-tier", default=None, choices=["perp", "spot"],
                    help="cost model (default perp: maker 2bp/taker 5bp; spot = base tier)")
    hb.add_argument("--json", default=None, help="write results JSON here")
    hb.set_defaults(fn=cmd_hft_backtest)

    hr = sub.add_parser("hft-run", help="run the fast-book paper engine (separate 5m book)")
    hr.add_argument("--once", action="store_true", help="one cycle then exit")
    hr.add_argument("--interval", type=int, default=None, help="seconds between cycles")
    hr.set_defaults(fn=cmd_hft_run)

    hs = sub.add_parser("hft-status", help="HFT book journal summary (all high-frequency trades)")
    hs.set_defaults(fn=cmd_hft_status)

    hbat = sub.add_parser("hft-battery",
                          help="HFT harness: every strategy x symbol x fee tier "
                               "-> data/results/hft_battery.json")
    hbat.add_argument("--days", type=int, default=3)
    hbat.add_argument("--tier", default="both", choices=["perp", "spot", "both"])
    hbat.set_defaults(fn=cmd_hft_battery)

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
