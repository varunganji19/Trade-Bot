"""
Optional LLM client — powers the chatbot's free-form answers only; it never
takes part in a trading decision.

Auto-detects an OpenAI-compatible endpoint (OPENAI_API_KEY, optional
OPENAI_BASE_URL) or Anthropic (ANTHROPIC_API_KEY). With no key the bot runs
fully deterministic ("quant mode") — everything still works.
"""
from __future__ import annotations

import json
import os

import requests

from config import CONFIG, LLMConfig


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

    def chat_answer(self, question: str, context: dict) -> str:
        system = ("You are the assistant of an autonomous trading bot. Answer the user's question "
                  "about the bot's performance, trades, and strategies using ONLY the provided JSON "
                  "context from its journal. Be concise (<=120 words). If the context lacks the "
                  "answer, say so honestly.")
        user = json.dumps({"question": question, "journal_context": context}, indent=1, default=str)[:3500]
        return self._chat(system, user, max_tokens=250).strip()
