"""
Decision orchestrator — the bot's "brain".

1. Classifies the regime (trending / ranging) from ADX + EMA structure.
2. Weight-blends strategy signals per regime (see RESEARCH.md §3):
     trending  -> turtle 0.55, scalper 0.30, meanrev 0.15
     ranging   -> meanrev 0.55, scalper 0.30, turtle 0.15
3. Weighted confidence vote with a conflict guard (strong simultaneous LONG and
   SHORT conviction => HOLD).
4. Kronos (financial foundation model, bot/kronos_signal.py): its probabilistic
   forecast is ALWAYS journaled in strategy_signals (tracked non-voter), and it
   joins the vote with weight KRONOS_VOTE_WEIGHT only after its rolling IC
   earned voting rights (promoted() gate — the same evidence standard the bot
   applies to any other signal).
5. Sentiment overlay may veto/shrink (never initiates).
6. Optional LLM: acts as tie-breaker/veto with guardrails; in quant mode the
   deterministic vote is the decision.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from bot.strategies import get_strategies, Signal

REGIME_WEIGHTS = {
    "trending": {"turtle_trend": 0.55, "vwap_scalper": 0.30, "connors_meanrev": 0.15},
    "ranging": {"connors_meanrev": 0.55, "vwap_scalper": 0.30, "turtle_trend": 0.15},
}
KRONOS_VOTE_WEIGHT = 0.20   # only applied once Kronos has earned voting rights


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
                 kronos_engine=None):
        from config import CONFIG
        self.cfg = cfg or CONFIG
        self.strategies = get_strategies(params)
        self.llm = llm_client
        self.sentiment = sentiment_overlay
        # kronos_engine: bot.kronos_signal.KronosSignalEngine (lazy; may be None)
        self.kronos = kronos_engine

    # ------------------------------------------------------------------ main
    def decide(self, df, i: int, spec, include_sentiment: bool = True,
               kronos_signal=None, kronos_promoted: bool = False) -> Decision:
        regime, regime_meta = detect_regime(df, i)
        weights = dict(REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS["ranging"]))

        # Only run strategies suited to this timeframe (scalper on 5m/15m, etc.)
        tf = spec.timeframe
        raw_signals: dict[str, Signal] = {}
        for name, strat in self.strategies.items():
            if tf not in strat.preferred_timeframes:
                continue
            raw_signals[name] = strat.evaluate(df, i)

        # Kronos: tracked always, voting only with earned rights. It provides
        # direction/confidence but NEVER a stop distance — sizing stays with the
        # strategy signals, so `best` below only considers real strategies.
        kronos_note = ""
        kronos_meta: dict = {}
        if kronos_signal is not None:
            ks = kronos_signal
            kronos_meta = {"action": ks.direction, "confidence": round(ks.p_up, 3),
                           "p_up": ks.p_up,
                           "expected_return_pct": ks.expected_return_pct,
                           "dispersion_pct": ks.dispersion_pct,
                           "horizon_bars": ks.horizon_bars, "voting": kronos_promoted}
            kronos_note = (f"Kronos{' [VOTING]' if kronos_promoted else ' [tracked, no vote]'}: "
                           f"P(up) {ks.p_up:.0%} over {ks.horizon_bars} bars")
            if kronos_promoted and ks.direction in ("LONG", "SHORT"):
                weights["kronos"] = KRONOS_VOTE_WEIGHT
                conf = float(min(0.90, max(0.30, ks.p_up if ks.direction == "LONG" else 1.0 - ks.p_up)))
                raw_signals["kronos"] = Signal("kronos", ks.direction, conf,
                                               rationale=ks.rationale)

        price = float(df["close"].iloc[i])

        # weighted vote
        long_score = sum(weights.get(n, 0.0) * s.confidence for n, s in raw_signals.items() if s.action == "LONG")
        short_score = sum(weights.get(n, 0.0) * s.confidence for n, s in raw_signals.items() if s.action == "SHORT")
        total_weight = sum(weights.get(n, 0.0) for n in raw_signals) or 1.0

        action = "HOLD"
        confidence = 0.0
        strategy_only = {n: s for n, s in raw_signals.items() if n != "kronos"}
        best = max(strategy_only.values(), key=lambda s: s.confidence, default=None)

        if long_score >= short_score and long_score / total_weight >= 0.25:
            action, confidence = "LONG", long_score / total_weight
        elif short_score > long_score and short_score / total_weight >= 0.25:
            action, confidence = "SHORT", short_score / total_weight

        # conflict guard: two strategies with strong opposite conviction
        # (kronos excluded — it can't manufacture a conflict by itself)
        strong_longs = [n for n, s in strategy_only.items() if s.action == "LONG" and s.confidence >= 0.6]
        strong_shorts = [n for n, s in strategy_only.items() if s.action == "SHORT" and s.confidence >= 0.6]
        conflict = bool(strong_longs and strong_shorts)
        if conflict:
            action, confidence = "HOLD", 0.0

        stop_distance = best.stop_distance if (best and best.action == action) else None
        target_rr = best.target_rr if (best and best.action == action) else None

        rationale_parts = [f"Regime {regime} (ADX {regime_meta.get('adx', '?')}, bias {regime_meta.get('bias', '?')})."]
        rationale_parts.append(self._signals_summary(raw_signals))
        if kronos_note:
            rationale_parts.append(kronos_note + ".")
        if action != "HOLD":
            rationale_parts.append(f"Weighted vote: {action} @ {confidence:.2f}.")
        elif conflict:
            rationale_parts.append("Conflict guard: strong opposing signals -> standing down.")

        # sentiment overlay (live mode only)
        sentiment_note = ""
        if include_sentiment and self.sentiment is not None and action in ("LONG", "SHORT"):
            sent = self.sentiment.assess(asset_hint=spec.display)
            action2, confidence2, sentiment_note = self.sentiment.apply(action, confidence, sent)
            if action2 != action:
                rationale_parts.append(f"Sentiment veto: {sentiment_note}.")
            elif sentiment_note:
                rationale_parts.append(f"Sentiment: {sentiment_note}.")
            action, confidence = action2, confidence2

        # optional LLM tie-breaker / veto
        if self.llm is not None and self.llm.enabled and tf in ("5m", "15m", "1h"):
            try:
                llm_res = self.llm.decide({
                    "market": spec.display, "timeframe": tf, "price": price,
                    "regime": {"name": regime, **regime_meta},
                    "strategy_signals": {n: {"action": s.action, "confidence": round(s.confidence, 2),
                                             "rationale": s.rationale}
                                         for n, s in raw_signals.items()},
                    "quant_decision": {"action": action, "confidence": round(confidence, 2)},
                    "sentiment": self.sentiment.last_result if self.sentiment else None,
                })
                llm_action = str(llm_res.get("action", "HOLD")).upper()
                llm_conf = float(max(0.0, min(1.0, llm_res.get("confidence", 0.5))))
                if llm_action == action:
                    confidence = min(0.95, max(confidence, (confidence + llm_conf) / 2))
                    rationale_parts.append(f"LLM agrees ({llm_conf:.2f}).")
                elif llm_action == "HOLD" and confidence < 0.65 and llm_conf >= 0.6:
                    action, confidence = "HOLD", 0.0
                    rationale_parts.append(f"LLM vetoed to HOLD: {llm_res.get('rationale', '')[:140]}")
                else:
                    confidence *= 0.8
                    rationale_parts.append(f"LLM suggests {llm_action}; quant decision stands with reduced size.")
            except Exception as exc:  # LLM must never break trading
                rationale_parts.append(f"(LLM unavailable: {type(exc).__name__})")

        if action != "HOLD" and confidence < self.cfg.risk.min_confidence:
            rationale_parts.append(
                f"Confidence {confidence:.2f} below floor {self.cfg.risk.min_confidence:.2f} -> HOLD.")
            action, confidence = "HOLD", confidence

        out_signals = {n: {"action": s.action, "confidence": round(s.confidence, 3),
                           "rationale": s.rationale, "meta": s.meta}
                       for n, s in raw_signals.items()}
        if kronos_meta:
            out_signals["kronos"] = kronos_meta
        return Decision(
            action=action,
            confidence=confidence,
            stop_distance=stop_distance,
            target_rr=target_rr,
            regime=regime,
            rationale=" ".join(rationale_parts),
            strategy_signals=out_signals,
            sentiment=self.sentiment.last_result or {} if self.sentiment else {},
            price=price,
            strategy_name=(best.strategy if (best and best.action == action and action != "HOLD") else ""),
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
