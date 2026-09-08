"""
Journal-aware chatbot for the dashboard.

Two tiers:
  1. LLM mode (if a key is configured): the LLM answers from a compact journal
     summary we build — it cannot invent trades, only explain what's in the DB.
  2. Deterministic mode (no key): intent matching over the journal with honest
     template answers, including plain-English strategy explainers.

Both modes answer from the journal — the bot's actual trading record.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from config import CONFIG

from bot.journal import Journal
from bot.llm import LLMClient

IST = ZoneInfo("Asia/Kolkata")


def _fmt_ts(ts: str) -> str:
    """Journal rows are ISO-UTC; the user reads IST — convert for display so
    chat answers never quote raw UTC times. Keep in sync with fmtTs in
    bot/dashboard.py DASHBOARD_HTML (same IST display contract)."""
    try:
        return datetime.fromisoformat(ts).astimezone(IST).strftime("%d %b %H:%M")
    except (ValueError, TypeError):
        return (ts or "")[:16]


STRATEGY_DOCS = {
    "turtle_trend": ("Turtle Trend — Donchian channel breakout, the rules taught to Richard Dennis's "
                     "Turtles: buy a 20-bar high breakout (or short the 20-bar low) only when ADX "
                     "confirms a trending regime, exit on the opposite 10-bar channel, stop 2xATR. "
                     "Low win rate, big winners — it's the strategy that made the Turtles famous."),
    "connors_meanrev": ("Connors RSI-2 — Larry Connors' mean-reversion pullback: buy when RSI(2) drops "
                        "below 5 (we tightened his published 10 threshold; see BACKTESTS.md) while "
                        "price is above the 200-EMA (uptrend filter), exit on the "
                        "snapback above RSI(2) 65 or EMA(5). Historically ~75% win rate on indices, "
                        "small winners / occasional larger losers (we add a 3xATR stop)."),
    "vwap_scalper": ("VWAP Scalper — intraday momentum: enters when price reclaims the rolling VWAP "
                     "(or breaks the prior 12-bar range) with EMA9>EMA21 momentum and above-average "
                     "volume; exits on VWAP cross-back, breakeven trail, or time stop. Based on the "
                     "Opening-Range-Breakout evidence (Zarattini & Aziz 2023) plus our VWAP prototype."),
    "orchestrator": ("The Orchestrator blends all three strategies per market regime (trending vs "
                     "ranging via ADX): trending favors the Turtle breakout, ranging favors Connors "
                     "mean reversion; conflicting strong signals stand down, news sentiment can veto "
                     "but never initiate."),
    "ensemble": "Ensemble — backtest mode where the orchestrator blends all three strategies.",
}


def _fmt_money(v: float) -> str:
    return f"${v:,.2f}"


def _symbol_from_question(q: str, known: list[str] | None = None) -> str | None:
    """Pull the market the user named out of a natural-language question, in
    either house format: 'BTC', 'btc/usdt', 'BTC/USDT' or 'GBPUSD', 'gbpusd=x'.

    Tiers, in order:
      1. pair form (BTC/USDT) — unambiguous on its face;
      2. a token that matches a market the bot actually trades (watchlist or
         journaled) — question English ('long', 'should') can never masquerade
         as a market this way;
      3. only when the question carries a trade verb (buy/sell/open/...), a
         bare token that is neither known nor a function word: an
         explicitly-asked but NEVER-TRADED market (DOGE, AAPL). Tier 2 alone
         would silently answer the newest entry of a different market.
    Returns None when no market is named (caller falls back to newest entry)."""
    import re
    up = q.upper()
    m = re.search(r"\b([A-Z]{2,10})/([A-Z]{2,10})\b", up)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    # tier 2: first token that matches a known market
    for m in re.finditer(r"\b([A-Z]{2,10})(?:=X)?\b", up):
        if any(_same_symbol(sym, m.group(1)) for sym in (known or [])):
            return m.group(1)
    # tier 3: explicit trade verb + a non-function-word token = an untraded market
    if re.search(r"\b(BUY|BOUGHT|SELL|SOLD|SHORT|ENTER|OPEN|TRADE)\w*\b", up):
        _FN = {"WHY", "DID", "YOU", "THE", "BOT", "WHAT", "WHEN", "HOW", "AND",
               "NOT", "FOR", "ARE", "WAS", "IS", "DO", "DOES", "DIDN", "DON",
               "A", "ON", "OF", "MY", "I", "ME", "IT", "OPEN", "LONG", "SHORT",
               "HOLD", "HOLDING", "BUY", "BOUGHT", "SELL", "SOLD", "ENTER",
               "TRADE", "MAKE", "MADE", "EARN", "BEST", "WHICH", "WHO", "TELL",
               "EXPLAIN", "PLEASE", "RECENT", "RECENTLY", "TODAY", "SYSTEM",
               "MARKET", "SHOULD", "COULD", "WOULD", "POSITION"}
        for m in re.finditer(r"\b([A-Z]{2,6})(?:=X)?\b", up):
            if m.group(1) not in _FN:
                return m.group(1)
    return None


def _same_symbol(journal_symbol: str, wanted: str | None) -> bool:
    """Does this journal row's market match what the user asked about?
    Wanted is the user's (possibly partial) spelling: 'BTC' matches
    'BTC/USDT'; 'BTC/USDT' matches exactly; 'GBPUSD' matches 'GBPUSD=X'."""
    if wanted is None:
        return True   # no preference: any market
    j = journal_symbol.upper()
    w = wanted.upper()
    if j == w or j.replace("=X", "") == w.replace("=X", ""):
        return True
    base = j.split("/")[0].replace("=X", "")
    return w == base or w.split("/")[0] == base


class ChatBot:
    def __init__(self, journal: Journal | None = None, llm: LLMClient | None = None):
        self.journal = journal or Journal()
        self.llm = llm or LLMClient()

    # ------------------------------------------------------------------ API
    def answer(self, question: str) -> str:
        # log the user's message exactly once, BEFORE any branch: the LLM path
        # used to log it inside _llm_answer, so a mid-path failure + fallback
        # double-logged the question
        self.journal.log_chat("user", question)
        try:
            if self.llm.enabled:
                return self._llm_answer(question)
            return self._deterministic_answer(question)
        except Exception as exc:
            # label honestly — this catches journal/DB failures too, not just
            # LLM ones. The journal itself may be the thing that's down, so the
            # fallback must tolerate the deterministic path failing again.
            try:
                tail = self._deterministic_answer(question)
            except Exception:
                tail = "journal unavailable — no stats to show"
            return f"(lookup failed — {type(exc).__name__}, showing what I could read) {tail}"

    # ------------------------------------------------------------------ LLM
    def _llm_answer(self, question: str) -> str:
        context = self._journal_context()
        reply = self.llm.chat_answer(question, context)
        self.journal.log_chat("assistant", reply)
        return reply

    def _journal_context(self) -> dict:
        stats = self.journal.stats(mode="paper")
        trades = self.journal.recent_trades(limit=15, mode="paper")
        decisions = self.journal.recent_decisions(limit=5)
        # IST-convert timestamps here too — the LLM quotes what it's given
        return {
            "stats": stats,
            "recent_trades": [{**{k: t[k] for k in ("symbol", "side", "qty",
                                                    "entry_price", "exit_price", "pnl",
                                                    "pnl_pct", "strategy", "status",
                                                    "exit_reason")},
                               "opened_ts": _fmt_ts(t["opened_ts"]),
                               "closed_ts": _fmt_ts(t["closed_ts"])
                               if t["closed_ts"] else None}
                              for t in trades],
            "recent_decisions": [{"ts": _fmt_ts(d["ts"]),
                                  **{k: d[k] for k in ("symbol", "action",
                                                      "confidence", "regime",
                                                      "rationale")}}
                                 for d in decisions],
            "strategy_docs": STRATEGY_DOCS,
        }

    # --------------------------------------------------------- deterministic
    def _demo_note(self) -> str:
        """Honest suffix when seeded demo (backtest-replay) rows exist: the
        answers describe the bot's OWN paper record, not the seeder's."""
        n = self.journal.trade_mode_counts().get("demo", 0)
        return f" (excludes {n} seeded mode='demo' replay trades)" if n else ""

    def _deterministic_answer(self, q: str) -> str:
        ql = q.lower()
        # NOTE: the user row is logged once in answer() BEFORE this method —
        # logging it again here double-writes the deterministic path
        stats = self.journal.stats(mode="paper")

        # strategy explainers
        for key, doc in STRATEGY_DOCS.items():
            if key.replace("_", " ").replace("connors meanrev", "connors") in ql or (
                    key.split("_")[0] in ql and key != "orchestrator"):
                self.journal.log_chat("assistant", doc)
                return doc

        if any(w in ql for w in ("earn", "profit", "p&l", "pnl", "performance", "made", "how much")):
            open_trades = stats["open_trades"]
            net_dep = self.journal.deposits_net(mode="paper")
            dep_note = ""
            if abs(net_dep) >= 0.01:
                # equity walk includes deposits, trade P&L doesn't — without
                # this line the two numbers in one sentence contradicted
                # ("total P&L −$194.71 (return 6.89%…)")
                dep_note = (f" Net deposits {_fmt_money(net_dep)} are included in the "
                            f"equity/return but not in trade P&L.")
            reply = (f"Closed trades: {stats['closed_trades']} with {stats['win_rate']}% win rate, "
                     f"total P&L {_fmt_money(stats['total_pnl'])} (return {stats['return_pct']}% from "
                     f"{_fmt_money(stats['start_equity'])} to {_fmt_money(stats['current_equity'])}). "
                     f"Max drawdown {stats['max_drawdown_pct']}%. "
                     f"Profit factor {stats['profit_factor'] if stats['profit_factor'] is not None else 'n/a (no losses yet)'}. "
                     f"{open_trades} position(s) still open.{dep_note}{self._demo_note()}")
            self.journal.log_chat("assistant", reply)
            return reply

        if "loss" in ql or "lost" in ql or "drawdown" in ql:
            reply = (f"Max drawdown so far: {stats['max_drawdown_pct']}%. "
                     f"Average loss per losing trade: {_fmt_money(stats['avg_loss'])}. "
                     f"The risk manager caps daily loss at 3% (kill switch) and risks 1% per trade.")
            self.journal.log_chat("assistant", reply)
            return reply

        # parens matter: `A and B or C` would misroute "which strategy" questions
        if ("strategy" in ql and "best" in ql) or "which strategy" in ql:
            by = stats.get("by_strategy", {})
            if not by:
                reply = "No closed trades yet, so no attribution to show. Run the bot or a backtest first."
            else:
                lines = [f"{name}: {d['trades']} trades, {d['wins']} wins, P&L {_fmt_money(d['pnl'])}"
                         for name, d in by.items()]
                reply = "Strategy attribution (closed trades):\n" + "\n".join(lines) + self._demo_note()
            self.journal.log_chat("assistant", reply)
            return reply

        if "trade" in ql or "history" in ql or "recent" in ql:
            trades = self.journal.recent_trades(limit=5, mode="paper")
            if not trades:
                reply = "No trades in the journal yet."
            else:
                lines = []
                for t in trades:
                    pnl = f"{_fmt_money(t['pnl'])}" if t["pnl"] is not None else "open"
                    lines.append(f"{_fmt_ts(t['opened_ts'])} {t['side'].upper()} {t['symbol']} "
                                  f"@{t['entry_price']:.6g} via {t['strategy']} -> {pnl}")
                reply = "Last 5 trades (times in IST):\n" + "\n".join(lines)
            self.journal.log_chat("assistant", reply)
            return reply

        if "why" in ql and ("buy" in ql or "sell" in ql or "open" in ql):
            # answer about the market the user ASKED about, not whichever
            # non-HOLD decision was newest: "why did you buy BTC?" used to
            # return a GBPUSD decision (question symbol ignored entirely)
            # the markets the bot trades (watchlist + anything journaled) are
            # the ONLY valid bare-token matches — question words can never
            # masquerade as a market this way
            known = {s.symbol for s in CONFIG.watchlist}
            known |= {t["symbol"] for t in self.journal.recent_trades(limit=200)}
            known |= {d["symbol"] for d in self.journal.recent_decisions(limit=200)}
            wanted = _symbol_from_question(q, sorted(known))
            decisions = self.journal.recent_decisions(limit=30, mode="paper")
            entries = [d for d in decisions
                       if d["action"] != "HOLD" and _same_symbol(d["symbol"], wanted)]
            demo_answer = False
            if not entries and wanted is None:
                # no symbol named: newest entry decision of the paper feed
                entries = [d for d in decisions if d["action"] != "HOLD"]
            if not entries:
                # fresh seed-demo journal: the same lookup over the replay rows,
                # honestly labeled
                demo = self.journal.recent_decisions(limit=30, mode="demo")
                entries = [d for d in demo
                           if d["action"] != "HOLD" and _same_symbol(d["symbol"], wanted)]
                demo_answer = bool(entries)
            if entries:
                d = entries[0]
                reply = (f"On {_fmt_ts(d['ts'])} the bot decided {d['action']} {d['symbol']} at "
                         f"~{d['price']:.6g} (confidence {d['confidence']:.2f}, regime {d['regime']}). "
                         f"Reasoning: {d['rationale'][:400]}"
                         + (" [seeded demo row — backtest replay, not a live decision]" if demo_answer else ""))
            else:
                reply = ("No entry decisions in the recent journal — the bot has been holding."
                         if wanted is None else
                         f"No recent entry decision on {wanted} — the bot may have only HOLDs "
                         f"there, or it hasn't traded that market.")
            self.journal.log_chat("assistant", reply)
            return reply

        if "risk" in ql or "safe" in ql:
            reply = ("Risk rules: 1% equity risked per trade, positions sized from ATR-based stops, "
                     "max 25% notional per position, max 4 concurrent positions, 3% daily-loss kill "
                     "switch, cooldown after stop-outs, and every entry needs >= 0.55 confidence.")
            self.journal.log_chat("assistant", reply)
            return reply

        reply = ("I can answer about: earnings (P&L), trade history, strategy attribution, why a "
                 "specific trade was taken, risk rules, or explain each strategy (turtle, connors, "
                 "scalper). Ask me one of those!")
        self.journal.log_chat("assistant", reply)
        return reply
