"""
Shadow Account — compare what the bot ACTUALLY did against what its own
rules would have done on the same bars (the Vibe-Trading "Shadow Account"
pattern, applied to a journal we already own).

Two questions it answers, with numbers:

1. DISCIPLINE — did the live/paper bot follow its own strategy rules?
   For every journaled trade we replay the owning strategy's check_exit on
   the bar where the trade actually closed. A trade that exited while the
   strategy saw no exit is a RULE BREAK (discretionary/LLM override, latency,
   or bug); a trade held past its strategy exit is LATE (costs money in
   churned stops); a trade that exited exactly on signal is ON-RULE.

2. PROFILE — behavior diagnostics on the journal itself: holding time,
   win rate, R-multiple distribution, disposition effect (cutting winners
   earlier than losers), and stop discipline (trades that blew through
   their initial stop distance).

The shadow comparison per symbol: re-run the pure strategy backtest over the
journal's date window on the same data and put the two equity paths side by
side. The gap between them is the measured cost (or value) of the bot's
orchestration layer — risk vetoes, sentiment, LLM tie-breaks, cooldowns —
which until now was a matter of opinion.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from bot.indicators import add_all_indicators
from bot.strategies import get_strategy


# ------------------------------------------------------------------ helpers
def _parse_ts(ts) -> pd.Timestamp | None:
    if not ts:
        return None
    try:
        dt = pd.Timestamp(ts)
        return dt.tz_localize("UTC") if dt.tzinfo is None else dt.tz_convert("UTC")
    except Exception:
        return None


def _entry_exit_bars(trade: dict, df: pd.DataFrame) -> tuple[int | None, int | None]:
    """Positional (entry_i, exit_i) of a trade inside the frame. Accepts both
    key conventions: journal rows (opened_ts/closed_ts) and backtest trade
    dicts (entry_ts/exit_ts)."""
    entry = _parse_ts(trade.get("opened_ts") or trade.get("entry_ts"))
    exit_ = _parse_ts(trade.get("closed_ts") or trade.get("exit_ts"))
    if entry is None or not len(df):
        return None, None

    # 1-bar tolerance (median frame step × 1.5): a 7-day snap silently
    # pinned week-old bars as exact fills — anything farther is UNKNOWN.
    try:
        _step = float((df.index[1:] - df.index[:-1]).total_seconds().median())
    except Exception:
        _step = 86400.0
    _tol = max(_step * 1.5, 1.0)

    def pos_of(ts):
        if ts is None:
            return None
        p = df.index.searchsorted(ts, side="right") - 1
        if p < 0 or p >= len(df):
            return None
        if abs((df.index[p] - ts).total_seconds()) > _tol:
            return None
        return p

    return pos_of(entry), pos_of(exit_)


# ------------------------------------------------------------------ discipline
@dataclass
class AdherenceReport:
    n_trades: int = 0
    n_on_rule: int = 0
    n_rule_break: int = 0
    n_late: int = 0
    n_unknown: int = 0
    trades: list = field(default_factory=list)

    @property
    def adherence_pct(self) -> float:
        decided = self.n_on_rule + self.n_rule_break + self.n_late
        return round(self.n_on_rule / decided * 100.0, 1) if decided else 0.0


def rule_adherence(trades: list[dict], df: pd.DataFrame, params=None,
                   warmup: int = 220) -> AdherenceReport:
    """Replay each closed trade against its owning strategy's exit rules.

    ON-RULE   — at the bar the trade actually exited, the strategy also fired
                the same-direction exit (or the exit reason matches a hard
                stop/target/time stop the broker itself enforces).
    LATE      — strategy fired an exit strictly EARLIER than the trade's close
                bar; the trade lingered after its own rules said leave.
    RULE BREAK— trade exited with the strategy still saying stay, and the
                reason isn't a hard stop/target (i.e. discretionary).
    UNKNOWN   — strategy not found for the trade's attribution, or the trade's
                bars fall outside the frame (missing data).
    """
    rep = AdherenceReport()
    ind = add_all_indicators(df, params)
    n = len(ind)

    for t in trades:
        if t.get("status") != "CLOSED":
            continue
        entry_i, exit_i = _entry_exit_bars(t, df)
        if entry_i is None or exit_i is None or exit_i < warmup or exit_i >= n:
            rep.n_unknown += 1
            continue
        rep.n_trades += 1
        reason = (t.get("exit_reason") or "").lower()
        strategy_name = t.get("strategy") or ""

        # broker-enforced exits are on-rule by construction (OCO brackets).
        # STARTSWITH, not substring: a discretionary note like "manual close
        # (stop loss was far)" must not masquerade as a hard bracket.
        if reason.startswith(("stop loss", "take profit", "end of backtest")):
            rep.n_on_rule += 1
            rep.trades.append({**t, "verdict": "on-rule (hard bracket)"})
            continue

        strat = None
        try:
            strat = get_strategy(strategy_name, params)
        except Exception:
            strat = None
        if strat is None:
            rep.n_unknown += 1
            continue

        # when did the strategy FIRST want out at/after entry? (time stops need
        # bars_held set per replayed bar, so refresh it on the stand-in)
        first_signal_i = None
        for i in range(entry_i + 1, min(exit_i + 5, n)):
            pos_view = _pos_view(t)
            pos_view.bars_held = i - entry_i
            reason_i, _ = strat.check_exit(ind, i, pos_view)
            if reason_i:
                first_signal_i = i
                break
        if first_signal_i is None:
            rep.n_rule_break += 1
            rep.trades.append({**t, "verdict": "rule break",
                               "note": f"exited '{t.get('exit_reason')}' with strategy silent"})
        elif first_signal_i == exit_i:
            rep.n_on_rule += 1
            rep.trades.append({**t, "verdict": "on-rule"})
        elif first_signal_i < exit_i:
            rep.n_late += 1
            rep.trades.append({**t, "verdict": "late",
                               "note": f"strategy signaled exit {exit_i - first_signal_i} bars earlier; trade lingered"})
        else:
            rep.n_rule_break += 1
            rep.trades.append({**t, "verdict": "rule break",
                               "note": f"exited {first_signal_i - exit_i} bars BEFORE its strategy's exit signal"})
    return rep


def _pos_view(trade: dict):
    """A minimal position stand-in exposing what strategy exits read."""
    class _P:
        pass
    p = _P()
    p.side = trade.get("side") or "long"
    p.entry_price = float(trade.get("entry_price") or 0.0)
    stop = trade.get("stop_price")
    p.stop = float(stop) if stop is not None else None
    # initial-risk ground truth: the latched initial stop when the journal has
    # it; the final (trailed) stop only as a legacy approximation
    initial = trade.get("initial_stop_price")
    risk_stop = initial if initial is not None else stop
    p.risk_per_unit = abs(p.entry_price - float(risk_stop)) if risk_stop is not None else 0.0
    p.bars_held = 0  # strategies use time stops; recomputed per call site below
    return p


# ------------------------------------------------------------------ profile
def behavior_profile(trades: list[dict]) -> dict:
    """Behavior diagnostics over closed journal trades (symbol-agnostic)."""
    closed = [t for t in trades if t.get("status") == "CLOSED" and t.get("pnl") is not None]
    if not closed:
        return {"n_trades": 0}

    wins = [t for t in closed if t["pnl"] > 0]
    r_multiples = []
    for t in closed:
        entry = t.get("entry_price")
        # INITIAL stop, not the trailed one: stop_price is overwritten by every
        # trail, so dividing by it turned BE-trailed losers into ±20R explosions
        # and excluded stop==entry rows entirely (a biased R sample). The
        # latched initial_stop_price is the ground truth; legacy rows without
        # it keep the old approximation (documented in journal._migrate).
        stop = t.get("initial_stop_price")
        if stop is None:
            stop = t.get("stop_price")
        if entry and stop and abs(entry - stop) > 0:
            risk_amount = abs(entry - stop) * (t.get("qty") or 1.0)
            if risk_amount > 0:
                # pnl is already direction-signed; R = pnl / initial risk
                r_multiples.append((t["pnl"] or 0.0) / risk_amount)

    # holding time in hours (when timestamps parse)
    held_h = []
    for t in closed:
        a, b = _parse_ts(t.get("opened_ts")), _parse_ts(t.get("closed_ts"))
        if a is not None and b is not None and b >= a:
            held_h.append((b - a).total_seconds() / 3600.0)
    avg_hold_h = sum(held_h) / len(held_h) if held_h else None

    # disposition effect: do winners get cut earlier than losers?
    win_held, loss_held = [], []
    for t in closed:
        a, b = _parse_ts(t.get("opened_ts")), _parse_ts(t.get("closed_ts"))
        if a is None or b is None or b < a:
            continue
        (win_held if t["pnl"] > 0 else loss_held).append((b - a).total_seconds() / 3600.0)
    disp = None
    if win_held and loss_held:
        disp = round(sum(win_held) / len(win_held) - sum(loss_held) / len(loss_held), 2)

    # stop discipline: losses bigger than the initial stop implies
    stop_blown = sum(1 for r in r_multiples if r < -1.05)
    return {
        "n_trades": len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100.0, 1),
        "avg_hold_hours": round(avg_hold_h, 2) if avg_hold_h is not None else None,
        "avg_r": round(sum(r_multiples) / len(r_multiples), 3) if r_multiples else None,
        "max_r": round(max(r_multiples), 2) if r_multiples else None,
        "min_r": round(min(r_multiples), 2) if r_multiples else None,
        "disposition_gap_hours": disp,  # negative => cut winners early (classic)
        "n_blew_through_stop": stop_blown,
    }


# ------------------------------------------------------------------ shadow
def shadow_compare(spec, trades: list[dict], df: pd.DataFrame) -> dict:
    """Actual journal PnL vs the pure-strategy backtest over the same window.

    The comparison window is bounded by the journal's first entry and last
    close so both paths see the same market. Returns per-trade actual totals
    plus the strategy-only shadow totals and the gap."""
    from bot.backtest import Backtester

    if not trades:
        return {"n_trades": 0}
    first = min((_parse_ts(t.get("opened_ts")) for t in trades if t.get("opened_ts")),
                default=None)
    last = max((_parse_ts(t.get("closed_ts")) for t in trades if t.get("closed_ts")),
               default=None)
    if first is None or last is None:
        return {"n_trades": 0, "error": "unparseable timestamps"}

    window = df[(df.index >= first) & (df.index <= last)]
    if len(window) < 260:
        return {"n_trades": 0, "error": f"window too small ({len(window)} bars)"}

    actual_pnl = sum(t.get("pnl") or 0.0 for t in trades if t.get("status") == "CLOSED")
    actual_fees = sum(t.get("fees") or 0.0 for t in trades if t.get("status") == "CLOSED")
    # one shadow backtest PER strategy that owns trades here: a mixed journal
    # used to be compared against whichever strategy happened to be seen first
    # (set iteration order) — every other owner was scored against the wrong
    # rules. Deterministic order, and each shadow only counts its own window.
    # Unsupported names (hft_*, ensemble, typos) are REPORTED explicitly —
    # never silently dropped (the old 3-name allowlist hid them).
    from bot.strategies import STRATEGY_CLASSES
    owned = sorted({t.get("strategy") for t in trades if t.get("strategy")})
    shadows = []
    for strategy_name in owned:
        if strategy_name == "ensemble":
            shadows.append({"strategy": strategy_name,
                            "error": "unsupported: ensemble is a blend, no single-strategy shadow"})
            continue
        if strategy_name not in STRATEGY_CLASSES:
            shadows.append({"strategy": strategy_name,
                            "error": f"unsupported strategy {strategy_name!r} — no registered rules to shadow"})
            continue
        try:
            bt = Backtester()
            res = bt.run(spec, window, strategy=strategy_name)
            s = res.stats()
            shadows.append({"strategy": strategy_name, "trades": s["trades"],
                            "pnl": s["total_pnl"], "fees": s["fees"],
                            "return_pct": s["return_pct"], "max_dd_pct": s["max_drawdown_pct"]})
        except Exception as exc:
            shadows.append({"strategy": strategy_name,
                            "error": f"{type(exc).__name__}: {exc}"})

    return {
        "n_trades": len(trades),
        "window_bars": len(window),
        "window_start": str(first), "window_end": str(last),
        "actual": {"trades": len(trades), "pnl": round(actual_pnl, 2),
                   "fees": round(actual_fees, 4)},
        "shadows": shadows,
    }
