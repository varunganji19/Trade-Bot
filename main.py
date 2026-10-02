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
  python3 main.py hft-status                   # fast book (experimental) journal summary
  python3 main.py hft-battery [--days 3] [--tier perp|spot|both]
  python3 main.py experiment run|list|registry [experiments/<id>.toml ...]
  python3 main.py kronos [--symbol BTC/USDT]   # offline Kronos IC evaluation (tracked non-voter verdict)
  python3 main.py shadow [--include-demo]      # journal-vs-own-rules Shadow Account report
  python3 main.py pause [note]                 # manual halt: blocks NEW entries only
  python3 main.py resume                       # clear the manual pause (new entries allowed)
  python3 main.py dashboard [--port 8000]      # web dashboard + chatbot
  python3 main.py status                       # paper-book summary
  python3 main.py track-record append|verify|render [--start YYYY-MM-DD]
  python3 main.py drift [report|clear NAME] [--book standard|fast]  # live-vs-expected monitor
  python3 main.py config                       # effective settings + where each came from
  python3 main.py chat "question"              # chatbot from the terminal
"""
from __future__ import annotations

import argparse
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

# the commands import config, so they load after the .env above
from bot.cli.operate import (cmd_chat, cmd_config, cmd_dashboard, cmd_drift,  # noqa: E402
                             cmd_hft_run, cmd_hft_status, cmd_pause, cmd_resume,
                             cmd_run, cmd_status, cmd_track_record)
from bot.cli.research import (cmd_backtest, cmd_experiment,  # noqa: E402
                              cmd_hft_backtest, cmd_hft_battery, cmd_kronos,
                              cmd_shadow, cmd_validate)


def build_parser() -> argparse.ArgumentParser:
    """The CLI parser. Split out of main() so the subcommand list has ONE
    source, which CI's smoke job derives instead of keeping a copy that
    drifts (see scripts/list_subcommands.py)."""
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

    hs = sub.add_parser("hft-status", help="fast book (experimental) journal summary (all fast-book trades)")
    hs.set_defaults(fn=cmd_hft_status)

    hbat = sub.add_parser("hft-battery",
                          help="fast-book harness: every strategy x symbol x fee tier "
                               "-> data/results/hft_battery.json (descriptive; "
                               "verdicts come from `make evidence`)")
    hbat.add_argument("--days", type=int, default=3)
    hbat.add_argument("--tier", default="both", choices=["perp", "spot", "both"])
    hbat.set_defaults(fn=cmd_hft_battery)

    exp = sub.add_parser("experiment", help="pre-registered experiments: run a committed "
                                            "declaration, list them, rebuild the registry")
    exp.add_argument("action", choices=["run", "list", "registry"])
    exp.add_argument("files", nargs="*", help="declarations under experiments/ (for run)")
    exp.add_argument("--workers", type=int, default=None,
                     help="parallel backtest processes (default: CPUs - 2, max 8)")
    exp.set_defaults(fn=cmd_experiment)

    pz = sub.add_parser("pause", help="manual halt: block NEW entries only "
                                      "(open positions stay managed, nothing is force-closed)")
    pz.add_argument("note", nargs="?", default="",
                    help="optional reason recorded with the flag (quote multi-word notes)")
    pz.set_defaults(fn=cmd_pause)

    rp = sub.add_parser("resume", help="clear the manual pause (new entries allowed again)")
    rp.set_defaults(fn=cmd_resume)

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
                    help="override the trial Sharpes for the Deflated Sharpe correction "
                         "(default: read from the experiment registry)")
    va.add_argument("--report", default=None,
                    help="also render the report as Markdown here (e.g. REPORT.md)")
    va.add_argument("--json", default=None)
    va.set_defaults(fn=cmd_validate)

    dash = sub.add_parser("dashboard", help="start the web dashboard")
    dash.add_argument("--port", type=int, default=8000)
    dash.set_defaults(fn=cmd_dashboard)

    st = sub.add_parser("status", help="paper-book summary (demo and fast-book rows left out)")
    st.set_defaults(fn=cmd_status)

    tr = sub.add_parser("track-record",
                        help="tamper-evident forward record of the paper book: seal "
                             "finished days, verify them against the journal, render "
                             "docs/TRACK_RECORD.md")
    tr.add_argument("action", choices=["append", "verify", "render"])
    tr.add_argument("--start", default=None,
                    help="first day of the record, YYYY-MM-DD (only on the first append)")
    tr.add_argument("--chain", default=None, help="chain file (default track_record/paper.jsonl)")
    tr.add_argument("--doc", default=None, help="document to render (default docs/TRACK_RECORD.md)")
    tr.set_defaults(fn=cmd_track_record)

    dr = sub.add_parser("drift", help="drift monitor: promoted strategies' live results "
                                      "against their expected range; clear a demotion")
    dr.add_argument("action", nargs="?", default="report", choices=["report", "clear"])
    dr.add_argument("name", nargs="?", default=None, help="strategy to clear")
    dr.add_argument("--book", default=None, choices=["standard", "fast"])
    dr.set_defaults(fn=cmd_drift)

    ch = sub.add_parser("chat", help="ask the journal-aware chatbot")
    ch.add_argument("question")
    ch.set_defaults(fn=cmd_chat)

    sub.add_parser("config",
                   help="print the effective configuration and where each "
                        "value came from (env vs default)").set_defaults(fn=cmd_config)

    kr = sub.add_parser("kronos", help="offline Kronos IC evaluation on history")
    kr.add_argument("--symbol", default="BTC/USDT")
    kr.add_argument("--timeframe", default="1h", choices=["5m", "15m", "1h", "4h", "1d"])
    kr.add_argument("--days", type=int, default=60)
    kr.add_argument("--horizon", type=int, default=24, help="forecast horizon in bars")
    kr.add_argument("--step", type=int, default=8,
                    help="evaluate every Nth bar (compute control)")
    kr.set_defaults(fn=cmd_kronos)

    sh = sub.add_parser("shadow", help="Shadow Account: journal vs its own rules")
    sh.add_argument("--json", default=None, help="report path (default: results/shadow_report.json beside the journal)")
    sh.add_argument("--include-demo", action="store_true",
                    help="audit mode='demo' (seed-demo backtest-replay) rows too; "
                         "default audits the bot's own paper record only")
    sh.set_defaults(fn=cmd_shadow)

    return p


def main():
    args = build_parser().parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
