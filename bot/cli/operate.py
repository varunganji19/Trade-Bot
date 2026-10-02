"""Operating commands: run the paper engines, pause and resume, the
dashboard, the journal summary, the chatbot and `config` (what the process
actually believes, and where each setting came from)."""
from __future__ import annotations

import json
import os
import sys

from config import CONFIG


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
    """Run the fast-book paper engine (the separate, experimental 5m book)."""
    from bot.hft import build_hft_engine
    from config import apply_saved_watchlist
    apply_saved_watchlist()   # data/watchlist.json -> CONFIG.watchlist
    engine = build_hft_engine()
    interval = args.interval or CONFIG.hft.live_interval_seconds
    summary = _run_owned(engine, args.once, interval, "hft")
    if args.once:
        print(json.dumps(summary, indent=1, default=str))


def cmd_hft_status(args):
    """The fast book's journal summary: separate account, separate history."""
    from bot.journal import Journal
    j = Journal()
    s = j.stats(mode="hft")
    print("[hft] fast paper book — experimental, no proven edge (mode='hft' journal rows):")
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


def cmd_run(args):
    from bot.engine import TradingEngine
    from config import apply_saved_watchlist
    apply_saved_watchlist()   # data/watchlist.json -> CONFIG.watchlist
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


def cmd_dashboard(args):
    import errno
    import socket
    import uvicorn
    from config import apply_saved_watchlist
    from bot.dashboard import app
    apply_saved_watchlist()   # data/watchlist.json -> CONFIG.watchlist

    # bind check BEFORE uvicorn starts: a second dashboard on the same port
    # would otherwise surface as a raw "[Errno 48] address already in use"
    # traceback that reads like a crash — name the squatter and the outs.
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
        # non-zero exit, so a wrapper never reports success with nothing
        # served (the operator would open the squatter's page instead)
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
    """The standard paper book's summary. The journal also holds seeded demo
    rows and the fast book; pooling them would pass both off as the paper
    bot's record, so they are left out and counted instead."""
    from bot.journal import Journal
    from bot.pause import is_paused
    j = Journal()
    print("[status] standard paper book (mode='paper' journal rows):")
    print(json.dumps(j.stats(mode="paper"), indent=1))
    counts = j.trade_mode_counts()
    print(f"excluded: {counts.get('demo', 0)} demo rows, "
          f"{counts.get('hft', 0)} fast-book rows "
          "(see `python3 main.py hft-status` for the fast book)")
    paused, pause_note = is_paused()
    if paused:
        print("trading: PAUSED (manual flag) — blocks new entries only; open positions "
              "are still managed, nothing is force-closed"
              + (f" · note: {pause_note}" if pause_note else ""))
    else:
        print("trading: active (no manual pause)")
    open_trades = j.open_trades(mode="paper")
    if open_trades:
        print(f"open positions: {len(open_trades)}")
        for t in open_trades:
            print(f"  {t['side'].upper()} {t['symbol']} qty {t['qty']} @ {t['entry_price']} ({t['strategy']})")


def cmd_chat(args):
    from bot.chatbot import ChatBot
    print(ChatBot().answer(args.question))


def cmd_config(args):
    """Print the EFFECTIVE configuration and where each value came from.

    A wrong env var fails silently in this system: the documented `.env`
    workflow never loaded the file for months, so DASHBOARD_TOKEN never
    reached the process and the dashboard ran with auth OFF while every
    document said it was on. Nothing surfaced it because nothing ever printed
    what the process actually believed. This does."""
    import config as cfg_mod
    from bot.hft import build_hft_config, hft_fee_tier

    c = cfg_mod.CONFIG
    print("=== effective configuration ===")
    print(f"journal        {os.path.abspath(c.db_path)}")
    print(f"cache          {cfg_mod.cache_dir()}")
    print(f"watchlist      {len(c.watchlist)} specs "
          f"({', '.join(sorted({s.timeframe for s in c.watchlist}))})")
    for spec in c.watchlist:
        print(f"    {spec.kind:6s} {spec.symbol:12s} {spec.timeframe}")
    hft = build_hft_config()
    print(f"fast book      {len(hft.watchlist)} specs @ "
          f"{ {s.timeframe for s in hft.watchlist} } · tier {hft_fee_tier()} · "
          f"{hft.live_interval_seconds}s cadence · capital ${hft.paper_capital:,.0f}")
    rt = (hft.costs.fee('crypto') + hft.costs.slippage('crypto')) * 2 * 1e4
    print(f"cost floors    taker round trip {rt:.1f}bp "
          f"(stop floor {hft.params.hft_cost_floor_bps * hft.params.hft_cost_buffer:.1f}bp)")
    print(f"risk           risk/trade {c.risk.risk_per_trade:.2%} · "
          f"gross {c.risk.max_gross_leverage:.2f}x · "
          f"cluster {c.risk.max_cluster_leverage:.2f}x · "
          f"max positions {c.risk.max_open_positions}")
    llm_on = c.llm.provider not in ("", "none")
    print(f"llm            {c.llm.provider if llm_on else 'quant mode'}"
          f"{' (chatbot only — it does not vote)' if llm_on else ''}")
    auth = os.environ.get("DASHBOARD_TOKEN")
    print(f"dashboard auth {'ON' if auth else 'OFF — anyone on this host can drive the bot'}")

    print("\n=== environment (value <- source) ===")
    if not cfg_mod.ENV_PROVENANCE:
        print("  (nothing read yet)")
    for name in sorted(cfg_mod.ENV_PROVENANCE):
        p_ = cfg_mod.ENV_PROVENANCE[name]
        mark = "  " if p_["source"] == "default" else "->"
        bad = "" if p_["ok"] else "   !! UNREADABLE, fell back to the default"
        print(f" {mark} {name:24s} {str(p_['value']):22s} <- {p_['source']}"
              f" (default {p_['default']}){bad}")

    from bot.promotion import gate_state, load_verdicts
    print("\n=== promotion gates ===")
    for book in ("standard", "fast"):
        gate = gate_state(book=book)
        verdicts = load_verdicts(book=book)
        print(f"  [{book}] file   {gate['path']}")
        if gate["state"] == "no_evidence":
            # the exact state that let a strategy measured at PF 0.39 vote
            # again on another data directory, without a line of output
            print("             state  UNMEASURED — every registered strategy votes")
            print("                    run `make evidence` to gather verdicts here")
        else:
            label = "STALE" if gate.get("stale") else "active"
            print(f"             state  {label} ({gate['why']}, "
                  f"generated {gate['generated_at']})")
            print(f"             basis  {gate.get('evidence', 'unknown')}")
        for name in sorted(verdicts):
            v = verdicts[name]
            print(f"             {v['status']:9s} {name:22s} {v.get('why', '')}")


def cmd_track_record(args):
    """Seal, verify or render the paper book's forward track record
    (docs/TRACK_RECORD.md). Reads the journal read-only."""
    import datetime as dt
    from bot import track_record as tr
    chain = args.chain or tr.DEFAULT_CHAIN
    if args.action == "verify":
        n = len(tr.read_chain(chain))
        problems = tr.verify(CONFIG.db_path, chain)
        for p_ in problems:
            print(f"[track-record] BROKEN {p_}")
        if problems:
            sys.exit(1)
        print(f"[track-record] {n} sealed days verify against {CONFIG.db_path}")
        return
    if args.action == "append":
        start = dt.date.fromisoformat(args.start) if args.start else None
        try:
            new = tr.append(CONFIG.db_path, chain, start=start)
        except tr.NotStartedYet as exc:
            print(f"[track-record] not yet: {exc}")
            return
        except tr.ChainError as exc:
            print(f"[track-record] refusing: {exc}")
            sys.exit(1)
        for line in new:
            n = line["trades"]
            print(f"[track-record] sealed {line['date']}: {n} trade{'' if n == 1 else 's'}, "
                  f"pnl {line['pnl']:+.2f} · {line['hash'][:16]}")
        if not new:
            print("[track-record] nothing to seal (the last finished day is already in)")
        else:
            print(f"[track-record] commit and push {chain} — the push is what fixes "
                  "these days in time")
    tr.render(chain, args.doc or tr.DEFAULT_DOC)
    print(f"[track-record] rendered {args.doc or tr.DEFAULT_DOC}")


def cmd_drift(args):
    """The drift monitor's view of each book (bot/drift.py): which promoted
    strategies are watched, how their live weeks compare with the expected
    range, and which were demoted. `clear NAME` gives one its vote back."""
    from bot import drift
    books = [args.book] if args.book else ["standard", "fast"]
    if args.action == "clear":
        if not args.name or len(books) != 1:
            print("[drift] clear needs a strategy name and --book standard|fast")
            sys.exit(1)
        if not drift.clear(books[0], args.name):
            print(f"[drift] {args.name} is not drift-demoted on the {books[0]} book")
            sys.exit(1)
        print(f"[drift] cleared {args.name} on the {books[0]} book: it votes again if the "
              "gate still promotes it, and its watch restarts from the next cycle")
        return
    print(f"rule: demote after {drift.DRIFT_WEEKS} consecutive weeks with live PF of the "
          f"last {drift.DRIFT_WINDOW} trades (min {drift.DRIFT_MIN_TRADES}) below the "
          "lower end of the promoted interval")
    for book in books:
        print(f"\n[{book}] state {drift.drift_path(book)}")
        rep = drift.report(book, CONFIG.db_path)
        if not rep:
            print("  watching nothing: no strategy is promoted on a measured interval")
        for name, a in sorted(rep.items()):
            lo, hi = a["expected"]
            print(f"  watching {name}: expected PF {lo:.2f}–{hi:.2f}, "
                  f"{a['live_trades']} live trades since {a['since'][:10]}")
            for w in a["weeks"][-drift.DRIFT_WEEKS:]:
                pf = "—" if w["pf"] is None else f"{w['pf']:.2f}"
                print(f"    week to {w['week_end']}: PF {pf} over {w['trades']} trades "
                      f"({w['state']})")
        for name, r in sorted(drift.demotions(book).items()):
            print(f"  DEMOTED {name} on {r['demoted_at'][:10]} — {r['why']}")


def cmd_testnet(args):
    """The Binance SPOT TESTNET book (bot/testnet.py): real orders, fake
    money. `run` trades it, `status` shows it, `reconcile` checks the
    exchange against the journal, `kill`/`unkill` are the manual switch."""
    import os
    from bot import testnet as tn
    if args.action == "kill":
        tn.set_kill("all" if args.all else "entries", args.reason or "manual")
        print(f"[testnet] kill switch ON ({'every order' if args.all else 'new entries'})")
        return
    if args.action == "unkill":
        tn.set_kill(None, "manual")
        print("[testnet] kill switch off")
        return
    try:
        if args.action == "run":
            engine = tn.build_testnet_engine()
            if not os.path.exists(tn.baseline_path()):
                base = tn.snapshot_baseline(engine.broker.exchange,
                                            [s.symbol for s in engine.cfg.watchlist])
                print(f"[testnet] baseline balances recorded: {base}")
            interval = args.interval or CONFIG.live_interval_seconds
            summary = _run_owned(engine, args.once, interval, "testnet")
            if args.once:
                print(json.dumps(summary, indent=1, default=str))
            return
        exchange = tn.make_exchange()
    except tn.NotTestnetError as exc:
        print(f"[testnet] {exc}")
        sys.exit(1)
    from bot.journal import Journal
    journal = Journal()
    problems = tn.reconcile(exchange, journal, trip=args.action == "reconcile")
    if args.action == "status":
        ks = tn.kill_state()
        print(f"[testnet] kill switch: {ks['level'] or 'off'}"
              + (f" — {ks.get('reason')} ({ks.get('at')})" if ks["level"] else ""))
        s = journal.stats(mode=tn.MODE)
        print(f"  closed trades {s['closed_trades']} · pnl {s['total_pnl']:+,.2f} · "
              f"open {s['open_trades']} · orders recorded {len(tn.recorded_orders())}")
        bal = exchange.fetch_balance()
        print("  balances: " + ", ".join(f"{a} {float((bal.get(a) or {}).get('total') or 0):g}"
                                         for a in ["USDT"] + sorted({s.symbol.split('/')[0]
                                                    for s in tn.testnet_watchlist()})))
    for p_ in problems:
        print(f"  MISMATCH {p_}")
    print(f"[testnet] reconciliation: {'OK' if not problems else f'{len(problems)} mismatch(es)'}"
          + (" — entry kill switch engaged" if problems and args.action == "reconcile" else ""))
    if problems:
        sys.exit(1)
