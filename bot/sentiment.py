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
        # word-boundary anchored: "risk" must not fire inside "brisk",
        # "sol" must not fire inside "console" (substring false positives).
        matches = re.findall(r"(?<!\w)(?:" + pattern + r")(?!\w)", text)
        if matches:
            score += value * len(matches)
            hits += len(matches)
    if hits == 0:
        return 0.0
    return max(-1.0, min(1.0, score / hits))


# Asset keyword map: which headlines are ABOUT a given asset. A crypto-crash
# headline must not veto an EUR/USD trade and vice versa (the global lexicon
# average did exactly that). Keys are matched against the lowercase headline
# text; `_MACRO_KEYS` apply to every asset (macro headlines move all books).
_ASSET_KEYWORDS = {
    "btc": ["bitcoin", "btc", "satoshi", "crypto", "cryptocurrency", "stablecoin",
            "altcoin", "memecoin", "exchange", "binance", "coinbase", "etf approval"],
    "eth": ["ethereum", "ether", "eth", "crypto", "cryptocurrency", "stablecoin",
            "altcoin", "defi", "binance", "coinbase", "etf approval"],
    "sol": ["solana", "sol ", "crypto", "cryptocurrency", "altcoin", "memecoin",
            "binance", "coinbase"],
    "eur/usd": ["euro", "eur", "ecb", "eurozone", "euro area", "germany", "france",
                "lagarde", "bund"],
    "gbp/usd": ["pound", "sterling", "gbp", "boe", "bank of england", "uk ",
                "britain", "british"],
}
# generic crypto/forex display names ("Bitcoin", "Ethereum", "Solana", "EUR/USD")
_HINT_ALIASES = {
    "bitcoin": "btc", "btc": "btc",
    "ethereum": "eth", "eth": "eth",
    "solana": "sol", "sol": "sol",
    "eur/usd": "eur/usd", "eurusd": "eur/usd", "eur-USD": "eur/usd",
    "gbp/usd": "gbp/usd", "gbpusd": "gbp/usd",
}
# macro headlines move every book: rate decisions, inflation, recession, war
_MACRO_KEYS = ["fed", "fomc", "powell", "rate cut", "rate hike", "inflation", "cpi",
               "recession", "dollar index", "dxy", "treasury", "risk-off", "risk off",
               "liquidity", "global markets", "stocks", "equities"]


def _wb_hit(text: str, key: str) -> bool:
    """Word-boundary substring: 'sol' must not match 'console', 'uk' must not
    match 'fluke', 'risk' must not match 'brisk'."""
    return re.search(r"(?<!\w)" + re.escape(key.strip()) + r"(?!\w)", text) is not None


def _headline_relevant(title: str, summary: str, asset_hint: str) -> bool:
    """Is this headline about the hinted asset (or macro-wide)? With no hint,
    or a hint we can't map, everything is relevant (the old behavior)."""
    hint = (asset_hint or "").strip().lower()
    key = _HINT_ALIASES.get(hint)
    if key is None:
        # unmapped hint (e.g. a custom watchlist entry): fall back to token
        # matching on the hint's own words so custom assets still filter
        words = [w for w in hint.replace("/", " ").split() if len(w) >= 3]
        if not words:
            return True
        text = (title + " " + (summary or "")).lower()
        return any(_wb_hit(text, w) for w in words) or any(_wb_hit(text, k) for k in _MACRO_KEYS)
    text = (title + " " + (summary or "")).lower()
    if any(_wb_hit(text, k) for k in _MACRO_KEYS):
        return True
    return any(_wb_hit(text, k) for k in _ASSET_KEYWORDS.get(key, []))


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
            # score only headlines relevant to THIS asset: a crypto-specific
            # crash headline must not veto an EUR/USD entry (and vice versa).
            # Zero relevant -> neutral 0.0 (scoring the whole batch let an
            # unrelated crash veto the wrong book).
            relevant = [h for h in headlines
                        if _headline_relevant(h["title"], h.get("summary", ""), asset_hint)]
            if not relevant:
                score, summary, scored, scores, nonzero = 0.0, (
                    f"lexicon scan: 0/{len(headlines)} headlines relevant to "
                    f"{asset_hint or 'the book'} — neutral"), [], [], []
            else:
                scored = relevant
                scores = [lexicon_score(h["title"] + " " + h.get("summary", "")) for h in scored]
                nonzero = [s for s in scores if s != 0.0]
                score = sum(nonzero) / len(nonzero) if nonzero else 0.0
                worst = min(zip(scores, scored), key=lambda t: t[0], default=None)
                best = max(zip(scores, scored), key=lambda t: t[0], default=None)
                summary = (f"lexicon scan of {len(scored)}/{len(headlines)} headlines relevant to "
                           f"{asset_hint or 'the book'}; "
                           f"most bullish: '{best[1]['title'][:80]}' ({best[0]:+.1f}); "
                           f"most bearish: '{worst[1]['title'][:80]}' ({worst[0]:+.1f})" if nonzero else
                           f"lexicon scan of {len(scored)} relevant headlines found no strong "
                           f"sentiment words")

        result = {
            "score": round(score, 3),
            "confidence": round(min(1.0, abs(score) * 1.5), 3),
            "method": method,
            "headlines": [{"title": h["title"], "source": h["source"]} for h in headlines[:8]],
            "summary": summary,
            "n_scored": len(scored) if method == "lexicon" else len(headlines[:12]),
            "n_relevant": len(scored) if method == "lexicon" else len(headlines[:12]),
        }
        self.last_result = result
        return result

    def apply(self, action: str, confidence: float, sentiment: dict) -> tuple[str, float, str]:
        """Return possibly-modified (action, confidence, note)."""
        score = sentiment.get("score", 0.0)
        # a veto needs corroboration: one extreme headline (n_scored<3) only
        # shrinks conviction — a single wire will never block an entry alone.
        n = int(sentiment.get("n_scored", sentiment.get("n_relevant", 0)) or 0)
        if action == "LONG" and score <= -0.6:
            if n and n < 3:
                return action, min(0.95, confidence * 0.85), (
                    f"sentiment contrary ({score:+.2f}) on {n} headline(s) — "
                    f"conviction trimmed, no veto (needs >=3)")
            return "HOLD", confidence, f"sentiment veto: strongly negative news ({score:+.2f})"
        if action == "SHORT" and score >= 0.6:
            if n and n < 3:
                return action, min(0.95, confidence * 0.85), (
                    f"sentiment contrary ({score:+.2f}) on {n} headline(s) — "
                    f"conviction trimmed, no veto (needs >=3)")
            return "HOLD", confidence, f"sentiment veto: strongly positive news ({score:+.2f})"
        # mild contrary news shrinks conviction, aligned news adds a little
        direction = 1 if action == "LONG" else -1 if action == "SHORT" else 0
        if direction:
            aligned = direction * score
            new_conf = confidence * (0.85 if aligned < -0.3 else (1.05 if aligned > 0.3 else 1.0))
            note = f"sentiment {'aligned' if aligned > 0.3 else 'contrary' if aligned < -0.3 else 'neutral'} ({score:+.2f})"
            return action, min(0.95, new_conf), note
        return action, confidence, ""
