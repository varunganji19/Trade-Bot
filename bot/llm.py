"""
Optional LLM client — used for headline scoring and final decision commentary.

Auto-detects an OpenAI-compatible endpoint (OPENAI_API_KEY, optional
OPENAI_BASE_URL) or Anthropic (ANTHROPIC_API_KEY). With no key the bot runs
fully deterministic ("quant mode") — everything still works.
"""
from __future__ import annotations

import json
import os

import requests

from config import CONFIG


class LLMClient:
    def __init__(self, cfg: "LLMConfig | None" = None):
        self.cfg = cfg or CONFIG.llm
        self.provider = self.cfg.provider
        self.model = self.cfg.model

    @property
    def enabled(self) -> bool:
        return self.provider in ("openai", "anthropic")

    # ------------------------------------------------------------------ core
    def _chat(self, system: str, user: str, max_tokens: int = 500) -> str:
        if self.provider == "openai":
            base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            resp = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                         "Content-Type": "application/json"},
                json={"model": self.model, "temperature": self.cfg.temperature,
                      "max_tokens": max_tokens,
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": user}]},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

        if self.provider == "anthropic":
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                         "anthropic-version": "2023-06-01",
                         "Content-Type": "application/json"},
                json={"model": self.model, "max_tokens": max_tokens,
                      "system": system,
                      "messages": [{"role": "user", "content": user}]},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]

        raise RuntimeError("LLM not configured")

    @staticmethod
    def _extract_json(text: str) -> dict:
        text = text.strip()
        if "```" in text:
            text = text.split("```")[1].removeprefix("json").strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        return json.loads(text)

    # -------------------------------------------------------------- features
    def score_headlines(self, headlines: list, asset_hint: str = "") -> dict:
        lines = "\n".join(f"- [{h['source']}] {h['title']}" for h in headlines[:12])
        system = ("You are a financial news analyst. Score market sentiment. "
                  "Respond ONLY with JSON: "
                  '{"score": <float -1..1>, "summary": "<one sentence>"}. '
                  "-1 = very bearish, +1 = very bullish.")
        user = f"Asset context: {asset_hint or 'crypto & forex markets'}\nHeadlines:\n{lines}"
        return self._extract_json(self._chat(system, user, max_tokens=200))

    def decide(self, decision_context: dict) -> dict:
        """
        LLM acts as tie-breaker/veto over the quant decision. Returns
        {'action': 'LONG'|'SHORT'|'HOLD', 'confidence': 0..1, 'rationale': str}.
        """
        system = (
            "You are the decision layer of an autonomous trading bot trading crypto and forex "
            "on 5m-1h timeframes. You receive a quant analysis: regime, strategy signals, and news "
            "sentiment. Respond ONLY with JSON: "
            '{"action": "LONG"|"SHORT"|"HOLD", "confidence": <0..1>, "rationale": "<max 3 sentences>"}. '
            "Be conservative: prefer HOLD when signals conflict or data is thin. "
            "Never invent positions without supporting signals."
        )
        user = json.dumps(decision_context, indent=1, default=str)[:3500]
        return self._extract_json(self._chat(system, user, max_tokens=300))

    def chat_answer(self, question: str, context: dict) -> str:
        system = ("You are the assistant of an autonomous trading bot. Answer the user's question "
                  "about the bot's performance, trades, and strategies using ONLY the provided JSON "
                  "context from its journal. Be concise (<=120 words). If the context lacks the "
                  "answer, say so honestly.")
        user = json.dumps({"question": question, "journal_context": context}, indent=1, default=str)[:3500]
        return self._chat(system, user, max_tokens=250).strip()
