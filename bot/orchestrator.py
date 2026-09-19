"""
Decision orchestrator — the bot's "brain".

1. Classifies the regime (trending / ranging) from ADX + EMA structure.
2. Weight-blends strategy signals per regime (see RESEARCH.md §3):
     trending  -> turtle 0.55, scalper 0.30, meanrev 0.15
     ranging   -> meanrev 0.55, scalper 0.30, turtle 0.15
   REALITY CHECK: the blend only engages when strategies SHARE a timeframe.
   The three shipped watchlist strategies are disjoint (turtle 1h, meanrev
   4h/1d, scalper 5m/15m), so every spec in a shipped watchlist is still
   evaluated by exactly ONE strategy and the "blend" reduces to that
   strategy's own confidence. But the REGISTERED set now overlaps — the
   Milestone-C strategies (ts_momentum 1h/4h, fx_regime_meanrev 1h) vote
   alongside turtle/meanrev the day they enter a watchlist — so the regime
   weights and the conflict guard are live code waiting on configuration,
   not dead code.
3. Weighted confidence vote with a conflict guard (strong simultaneous LONG and
   SHORT conviction => HOLD) — dormant while shipped watchlists keep one
   strategy per timeframe, live the day Milestone-C strategies join one
   (Kronos, when promoted, votes but is excluded from the conflict guard).
The sentiment overlay AND the LLM tie-breaker were REMOVED from this path on 2026-09-19. A
pair of nondeterministic, network-dependent calls (RSS + an LLM) sat between
the vote and the risk manager: they could shrink confidence, veto to HOLD, or
fail — none of it measured, all of it in the fill path, and none of it
reproducible in a backtest (the backtester passed llm_client=None and
include_sentiment=False, so live and backtest were literally running
different decision code, which is the one thing this repo's design is
supposed to prevent). The LLM still explains the book through the chatbot,
where being wrong costs nothing.

Kronos (the foundation-model forecaster) was REMOVED from this path on
2026-09-19. It never earned its vote, a single 1m forecast measured 41s of
CPU against a 2s cycle, and running it in two books at once aborted the
process on Metal. It lives on as an OFFLINE research job — `main.py kronos`
writes the IC ledger, the Evidence tab reads it — and it can come back to
the vote the day its ledger says it deserves one. See bot/kronos_signal.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from bot.promotion import is_demoted, load_verdicts
from bot.strategies import CANDIDATE_STRATEGIES, get_strategies, Signal

REGIME_WEIGHTS = {
    "trending": {"turtle_trend": 0.55, "vwap_scalper": 0.30, "connors_meanrev": 0.15,
                 "ts_momentum": 0.55, "fx_regime_meanrev": 0.15,
                 "hft_micro_breakout": 0.45, "hft_exhaustion_fade": 0.30},
    "ranging": {"connors_meanrev": 0.55, "vwap_scalper": 0.30, "turtle_trend": 0.15,
                "fx_regime_meanrev": 0.55, "ts_momentum": 0.15,
                "hft_exhaustion_fade": 0.45, "hft_micro_breakout": 0.25},
}


@dataclass
class Decision:
    action: str = "HOLD"           # LONG | SHORT | HOLD
    confidence: float = 0.0
    stop_distance: float | None = None
    target_rr: float | None = None
    regime: str = "unknown"
    rationale: str = ""
    strategy_signals: dict = field(default_factory=dict)
    sentiment: dict = field(default_factory=dict)
    price: float = 0.0
    strategy_name: str = ""        # dominant strategy driving the decision (attribution)
    # maker entry (HFT book): when the winning strategy quoted a resting
    # limit, the order rests at this price instead of crossing the spread
    limit_price: float | None = None


def detect_regime(df, i: int) -> tuple[str, dict]:
    adx_ = float(df["adx"].iloc[i]) if df["adx"].iloc[i] is not None else float("nan")
    ema50 = float(df["ema50"].iloc[i]) if df["ema50"].iloc[i] is not None else float("nan")
    ema200 = float(df["ema200"].iloc[i]) if df["ema200"].iloc[i] is not None else float("nan")
    close = float(df["close"].iloc[i])
    if math.isnan(adx_) or math.isnan(ema50):
        return "unknown", {"adx": adx_}
    slope = close > ema50 > ema200 if not math.isnan(ema200) else close > ema50
    if adx_ >= 20:
        return ("trending", {"adx": round(adx_, 1),
                             "bias": "up" if slope else "down"})
    return "ranging", {"adx": round(adx_, 1), "bias": "neutral"}


class Orchestrator:
    def __init__(self, params=None, llm_client=None, sentiment_overlay=None, cfg=None,
                 book: str = "standard"):
        # llm_client is accepted and IGNORED for decisions (see the module
        # docstring): kept in the signature so existing callers/tests are not
        # broken by the removal, and so the chatbot's client can still be
        # handed around without a second wiring path.
        from config import CONFIG
        self.cfg = cfg or CONFIG
        # which book's strategies may vote here (BaseStrategy.book). Timeframe
        # alone stopped separating the books when the fast one moved to 5m.
        self.book = book
        self.strategies = get_strategies(params)
        # the promotion gate, read ONCE per orchestrator (a decision loop must
        # not stat a file per bar). A strategy the harness measured as a loser
        # does not vote; see bot/promotion.py for why it is three states.
        self._verdicts = load_verdicts()
        self.llm = llm_client
        self.sentiment = sentiment_overlay

    # ------------------------------------------------------------------ main
    def decide(self, df, i: int, spec, include_sentiment: bool = False) -> Decision:
        # include_sentiment is accepted and ignored (the overlay is gone) so
        # existing call sites keep working
        regime, regime_meta = detect_regime(df, i)
        weights = dict(REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS["ranging"]))

        # Only run strategies suited to this timeframe (scalper on 5m/15m, etc.)
        tf = spec.timeframe
        raw_signals: dict[str, Signal] = {}
        for name, strat in self.strategies.items():
            if tf not in strat.preferred_timeframes:
                continue
            if getattr(strat, "book", "standard") != self.book:
                continue    # fast-book strategies never vote on the standard
                            # book's 5m specs, and vice versa
            # candidates are registered for the Lab and the battery but do not
            # vote until the harness says they beat the incumbents. Skipping
            # the EVALUATION (not just the weight) is deliberate: `best` below
            # picks the stop/limit by confidence irrespective of weight, and
            # the conflict guard counts any strong directional signal — a
            # zero-weight strategy would still steer live decisions.
            if name in CANDIDATE_STRATEGIES:
                continue
            # measured losers do not vote (promotion gate). Unmeasured ones
            # do — the gate can only take a vote away on evidence.
            if is_demoted(name, self._verdicts):
                continue
            raw_signals[name] = strat.evaluate(df, i)

        price = float(df["close"].iloc[i])

        # weighted vote
        long_score = sum(weights.get(n, 0.0) * s.confidence for n, s in raw_signals.items() if s.action == "LONG")
        short_score = sum(weights.get(n, 0.0) * s.confidence for n, s in raw_signals.items() if s.action == "SHORT")
        # the denominator counts only DIRECTIONAL votes: a FLAT strategy is an
        # abstention, not a vote against — counting its weight diluted every
        # lone signal to silence on any timeframe where several strategies are
        # registered (the HFT book's 1m trio never traded; the standard book
        # would hit the same the day ts_momentum/fx_regime_meanrev join one)
        total_weight = sum(weights.get(n, 0.0) for n, s in raw_signals.items()
                           if s.action in ("LONG", "SHORT")) or 1.0

        action = "HOLD"
        confidence = 0.0
        strategy_only = dict(raw_signals)
        best = max(strategy_only.values(), key=lambda s: s.confidence, default=None)

        if long_score >= short_score and long_score / total_weight >= 0.25:
            action, confidence = "LONG", long_score / total_weight
        elif short_score > long_score and short_score / total_weight >= 0.25:
            action, confidence = "SHORT", short_score / total_weight

        # conflict guard: two strategies with strong opposite conviction
        strong_longs = [n for n, s in strategy_only.items() if s.action == "LONG" and s.confidence >= 0.6]
        strong_shorts = [n for n, s in strategy_only.items() if s.action == "SHORT" and s.confidence >= 0.6]
        conflict = bool(strong_longs and strong_shorts)
        if conflict:
            action, confidence = "HOLD", 0.0

        stop_distance = best.stop_distance if (best and best.action == action) else None
        target_rr = best.target_rr if (best and best.action == action) else None
        limit_price = best.limit_price if (best and best.action == action) else None

        rationale_parts = [f"Regime {regime} (ADX {regime_meta.get('adx', '?')}, bias {regime_meta.get('bias', '?')})."]
        rationale_parts.append(self._signals_summary(raw_signals))
        if action != "HOLD":
            rationale_parts.append(f"Weighted vote: {action} @ {confidence:.2f}.")
        elif conflict:
            rationale_parts.append("Conflict guard: strong opposing signals -> standing down.")

        # A directional vote with no strategy stop would be an unbracketed
        # trade the risk layer must refuse downstream. Fall back to a 2xATR
        # stop from the frame's own ATR; if ATR is missing/non-finite, refuse
        # with a clear reason instead of emitting a stop-less decision. (This
        # guarded the promoted-Kronos-only case; it stays as the general
        # invariant: every directional decision leaves here bracketed.)
        atr_fallback = False
        if action != "HOLD" and stop_distance is None:
            atr_fb = None
            try:
                atr_fb = float(df["atr"].iloc[i]) if "atr" in df.columns else None
            except Exception:
                atr_fb = None
            if atr_fb is not None and math.isfinite(atr_fb) and atr_fb > 0:
                stop_distance = 2.0 * atr_fb
                atr_fallback = True
                rationale_parts.append(
                    f"No strategy stop — ATR fallback stop "
                    f"2.0xATR ({stop_distance:.6g}).")
            else:
                rationale_parts.append(
                    "No strategy stop and ATR unavailable — refusing "
                    "(no stop could be bracketed) -> HOLD.")
                action, confidence = "HOLD", 0.0

        if action != "HOLD" and confidence < self.cfg.risk.min_confidence:
            rationale_parts.append(
                f"Confidence {confidence:.2f} below floor {self.cfg.risk.min_confidence:.2f} -> HOLD.")
            action, confidence = "HOLD", confidence

        out_signals = {n: {"action": s.action, "confidence": round(s.confidence, 3),
                           "rationale": s.rationale, "meta": s.meta}
                       for n, s in raw_signals.items()}
        return Decision(
            action=action,
            confidence=confidence,
            stop_distance=stop_distance,
            target_rr=target_rr,
            regime=regime,
            rationale=" ".join(rationale_parts),
            strategy_signals=out_signals,
            sentiment={},
            price=price,
            strategy_name=(best.strategy if (best and best.action == action and action != "HOLD")
                           else ("atr_fallback" if (atr_fallback and action != "HOLD") else "")),
            limit_price=limit_price if action != "HOLD" else None,
        )

    @staticmethod
    def _signals_summary(signals: dict) -> str:
        active = {n: s for n, s in signals.items() if s.action != "FLAT"}
        if not active:
            bits = [f"{n}: flat" for n, s in signals.items()]
            return "No strategy sees a setup (" + "; ".join(bits) + ")."
        bits = [f"{n} -> {s.action} ({s.confidence:.2f})" for n, s in active.items()]
        flat = [n for n, s in signals.items() if s.action == "FLAT"]
        out = "; ".join(bits)
        if flat:
            out += f". {', '.join(flat)} flat"
        return out + "."
