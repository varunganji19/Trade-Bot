"""
News sentiment overlay.

Per RESEARCH.md §2.4, sentiment NEVER initiates a trade. It can:
  - veto an entry when strongly contrary news exists (LLM mode), or
  - shrink entry confidence (lexicon mode).

Two scoring backends:
  1. LLM (if an API key is configured) — scores the whole headline batch.
  2. Built-in finance lexicon — deterministic fallback so the bot works offline.
"""
from __future__ import annotations

import re

from bot.data import fetch_news

# Small hand-built lexicon: word stem -> score in [-1, 1].
LEXICON = {
    r"surge[sd]?|soar[s]?|rall(y|ies)|record high|all-time high|breakout|bullish|upgrade[sd]?|"
    r"inflow[s]?|approv(e|es|al)|adoption|partnership|launch(es|ed)?|beats? (expectations|estimates)|"
    r"rate cut|dovish|stimulus|etf approval|institutional": 0.6,
    r"crash(es|ed)?|plunge[sd]?|plummet[s]?|slash(es|ed)?|collapse[sd]?|bearish|downgrade[sd]?|"
    r"outflow[s]?|hack(ed)?|exploit(ed)?|breach|lawsuit|sued|sec (sues|charges)|bans?|banned|"
    r"liquidation[s]?|whale (sell|dump)|rate hike|hawkish|recession|default|delist(s|ed|ing)?": -0.6,
    r"volatile|volatility|uncertain(ty)?|fears?|concerns?|warns?|warning|risk|selloff|slide|"
    r"slump(s|ed)?|drop(s|ped)?|fall(s)?|decline[s]?|weak(er)?|slowdown|caution": -0.25,
    r"stabiliz(e|es)|recover(y|ies|ing)?|rebound(s|ed)?|bounce|support|consolidat(e|es|ion)": 0.2,
}


def lexicon_score(text: str) -> float:
    text = text.lower()
    score = 0.0
    hits = 0
    for pattern, value in LEXICON.items():
        matches = re.findall(pattern, text)
        if matches:
            score += value * len(matches)
            hits += len(matches)
    if hits == 0:
        return 0.0
    return max(-1.0, min(1.0, score / hits))


class SentimentOverlay:
    def __init__(self, llm_client=None):
        self.llm = llm_client
        self.last_result: dict | None = None

    def assess(self, asset_hint: str = "") -> dict:
        """
        Returns {'score': -1..1, 'confidence': 0..1, 'method': 'llm'|'lexicon',
                 'headlines': [...], 'summary': str}
        """
        headlines = fetch_news()
        if not headlines:
            result = {"score": 0.0, "confidence": 0.0, "method": "none",
                      "headlines": [], "summary": "no news available"}
            self.last_result = result
            return result

        method = "lexicon"
        score = 0.0
        summary = ""

        if self.llm is not None and self.llm.enabled:
            try:
                llm_res = self.llm.score_headlines(headlines, asset_hint)
                score = float(max(-1.0, min(1.0, llm_res.get("score", 0.0))))
                summary = str(llm_res.get("summary", ""))[:300]
                method = "llm"
            except Exception:
                method = "lexicon"

        if method == "lexicon":
            scores = [lexicon_score(h["title"] + " " + h.get("summary", "")) for h in headlines]
            nonzero = [s for s in scores if s != 0.0]
            score = sum(nonzero) / len(nonzero) if nonzero else 0.0
            worst = min(zip(scores, headlines), key=lambda t: t[0], default=None)
            best = max(zip(scores, headlines), key=lambda t: t[0], default=None)
            summary = (f"lexicon scan of {len(headlines)} headlines; "
                       f"most bullish: '{best[1]['title'][:80]}' ({best[0]:+.1f}); "
                       f"most bearish: '{worst[1]['title'][:80]}' ({worst[0]:+.1f})" if nonzero else
                       f"lexicon scan of {len(headlines)} headlines found no strong sentiment words")

        result = {
            "score": round(score, 3),
            "confidence": round(min(1.0, abs(score) * 1.5), 3),
            "method": method,
            "headlines": [{"title": h["title"], "source": h["source"]} for h in headlines[:8]],
            "summary": summary,
        }
        self.last_result = result
        return result

    def apply(self, action: str, confidence: float, sentiment: dict) -> tuple[str, float, str]:
        """Return possibly-modified (action, confidence, note)."""
        score = sentiment.get("score", 0.0)
        if action == "LONG" and score <= -0.6:
            return "HOLD", confidence, f"sentiment veto: strongly negative news ({score:+.2f})"
        if action == "SHORT" and score >= 0.6:
            return "HOLD", confidence, f"sentiment veto: strongly positive news ({score:+.2f})"
        # mild contrary news shrinks conviction, aligned news adds a little
        direction = 1 if action == "LONG" else -1 if action == "SHORT" else 0
        if direction:
            aligned = direction * score
            new_conf = confidence * (0.85 if aligned < -0.3 else (1.05 if aligned > 0.3 else 1.0))
            note = f"sentiment {'aligned' if aligned > 0.3 else 'contrary' if aligned < -0.3 else 'neutral'} ({score:+.2f})"
            return action, min(0.95, new_conf), note
        return action, confidence, ""
