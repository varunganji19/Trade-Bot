"""Research commands: backtests (standard and fast book), the honest-statistics
battery (`validate`), the validator for other people's backtests
(`validate-trades`), pre-registered experiments, the fast-book harness, the
offline Kronos evaluation and the Shadow Account."""
from __future__ import annotations

import json
import os
import sys

from config import CONFIG, DEFAULT_WATCHLIST, MarketSpec, infer_kind


def _spec_from_args(args) -> MarketSpec:
    """CLI symbols arrive unvalidated — infer kind the one shared way and
    uppercase ONLY forex pairs like the dashboard's validator does; crypto
    ('BTC/USDT') stays verbatim. A malformed symbol (no '/' or '=') is
    guessed to be crypto."""
    kind = infer_kind(args.symbol)
    sym = args.symbol if kind == "crypto" else args.symbol.upper()
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


def cmd_experiment(args):
    """Pre-registered experiments (bot/experiments.py): run declarations,
    list them, or rebuild the registry."""
    from bot import experiments as ex
    if args.action == "list":
        for path in ex.declarations():
            d = ex.load_declaration(path)
            done = os.path.exists(ex.results_path(d))
            print(f"  {d.id:28s} {d.kind:5s} {d.book:8s} "
                  f"{'ran' if done else 'not run':7s} {d.hypothesis[:60]}")
        return
    if args.action == "registry":
        path = ex.rebuild_registry()
        print(f"[experiment] registry rebuilt -> {path} ({len(ex.load_registry(path))} records)")
        return
    if not args.files:
        print("[experiment] name one or more declarations, e.g. experiments/standard_gate.toml")
        sys.exit(2)
    for path in args.files:
        try:
            ex.run_declaration(path, workers=args.workers)
        except ex.DeclarationError as exc:
            print(f"[experiment] {exc}")
            sys.exit(1)


def cmd_hft_battery(args):
    """The fast-book harness: every strategy x symbol x fee tier, plus
    the measured fee-sensitivity table (docs/archive/HFT.md)."""
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
    #    The experiment registry (bot/experiments.py) records every trial, so
    #    the count is read, not typed; --trial-sharpes overrides it. The
    #    primary run's equity returns feed the moment-aware SE (skew/kurtosis
    #    widen the SE on fat-tailed assets).
    sharpes = list(args.trial_sharpes or [])
    if not sharpes and args.strategy != "ensemble":
        from bot.experiments import trials_for
        trials = trials_for(args.strategy, spec.timeframe)
        sharpes = trials["sharpes"]
        print(f"[validate] registry: {trials['n_trials']} recorded trials of "
              f"{args.strategy} {spec.timeframe}, {len(sharpes)} with an OOS Sharpe")
        report["trials"] = trials
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

    from config import db_dir
    out = args.json or os.path.join(db_dir(), "results",
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
    trades = j.recent_trades(limit=2000,
                             mode=("paper", "demo") if args.include_demo else "paper")
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

    from config import db_dir
    # beside the journal, where the Evidence tab reads it (a cwd-relative
    # data/results/ ignored BOT_DB_PATH and wrote into the real data dir)
    out = args.json or os.path.join(db_dir(), "results", "shadow_report.json")
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


def cmd_validate_trades(args):
    """Validate someone else's backtest from its trade list (bot/validator.py):
    a freqtrade export, a trade CSV or this repo's backtest --json."""
    import time as _t
    from bot import validator as v
    t0 = _t.time()
    try:
        source, by_strategy = v.load_trades(args.file)
        daily = None
        if args.prices:
            daily = v.load_daily(args.prices)
        elif args.regime_market:
            daily = v.regime_daily(by_strategy, args.regime_market)
        report = v.validate(args.file, trials=args.trials, daily=daily,
                            strategy=args.strategy)
    except (v.ValidatorError, OSError, ValueError) as exc:
        print(f"[validate-trades] cannot validate {args.file}: {exc}")
        sys.exit(1)
    md = v.render_markdown(report)
    print(md)
    if args.report:
        with open(args.report, "w") as fh:
            fh.write(md)
        print(f"[validate-trades] wrote {args.report}")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=1, default=str)
        print(f"[validate-trades] wrote {args.json}")
    n = len(report["strategies"])
    print(f"[validate-trades] {source}, {n} strateg{'y' if n == 1 else 'ies'} in "
          f"{_t.time() - t0:.1f}s")


def cmd_record_book(args):
    """Record Binance public order-book snapshots and trades (bot/recorder.py)."""
    import time as _t
    from bot import recorder as r
    if args.summary:
        s = r.summary()
        if not s:
            print(f"[record-book] nothing recorded under {r.default_root()}")
        for sym, v in s.items():
            print(f"  {sym:10s} {v['hours']:5d} hours  {v['bytes'] / 1e6:8.1f} MB  "
                  f"{v['first_day']} → {v['last_day']}")
        return
    rec = r.Recorder()
    end = _t.time() + args.minutes * 60 if args.minutes else None
    print(f"[record-book] recording {', '.join(args.symbols)} to {rec.root} "
          f"(depth every {args.speed}{f', for {args.minutes} min' if args.minutes else ''}; "
          "Ctrl-C stops). Data terms: docs/COMPLIANCE.md Q8.")
    rec.run(args.symbols, args.speed, stop=(lambda: _t.time() >= end) if end else (lambda: False))
    print(f"[record-book] stopped: {rec.counts}")
