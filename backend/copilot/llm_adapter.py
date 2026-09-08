"""Pluggable reasoning/explanation backend for the Copilot.

Two backends:
  - RuleBasedFallbackAdapter: template-based, zero dependencies, zero
    cost, always available. This is the DEFAULT (COPILOT_LLM_BACKEND=none)
    so the Copilot works out of the box with no model download at all.
  - LocalOpenAICompatibleAdapter: talks to a local OpenAI-chat-compatible
    server (Ollama `ollama serve`, llama.cpp's `server`, vLLM, etc.) over
    HTTP on localhost — genuinely zero API cost, runs on the operator's
    own machine, no Anthropic/OpenAI key required. If that server isn't
    reachable, it falls back to the rule-based adapter rather than
    failing the whole request.

Neither adapter is ever given write access to anything — `explain()`
takes already-computed structured data and returns a string. It cannot
call tools itself in this implementation (see conversational.py, which
resolves ALL tool calls deterministically before the adapter ever runs),
which is a deliberate, simpler alternative to full LLM function-calling:
it makes "the LLM can't skip the tool and hallucinate a number" true by
construction, not just by prompting.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict

from backend.copilot.config import CopilotSettings, load_copilot_settings


class LLMAdapter(ABC):
    @abstractmethod
    def explain(self, question: str, context: Dict[str, Any]) -> str:
        """Turn already-resolved tool output (`context`) into a natural-
        language answer to `question`. Must never introduce a number,
        price, or state that isn't present in `context`."""
        raise NotImplementedError


class RuleBasedFallbackAdapter(LLMAdapter):
    """No model, no network call, no cost. Templates the structured
    context directly. Deliberately terse and literal rather than fluent —
    correctness over eloquence."""

    def explain(self, question: str, context: Dict[str, Any]) -> str:
        lines = []
        for key, value in context.items():
            if isinstance(value, dict) and value.get("available") is False:
                lines.append(f"- {key}: unavailable ({value.get('reason', 'unknown reason')})")
            elif isinstance(value, (dict, list)):
                lines.append(f"- {key}: {json.dumps(value, default=str)}")
            else:
                lines.append(f"- {key}: {value}")
        body = "\n".join(lines) if lines else "No data was resolved for this question."
        return f"Here's what I could verify:\n{body}"


class LocalOpenAICompatibleAdapter(LLMAdapter):
    """Talks to a local OpenAI-chat-compatible HTTP server. Falls back to
    the rule-based adapter on any connection/timeout/parse error — a
    local model being offline must never break the Copilot."""

    def __init__(self, settings: CopilotSettings) -> None:
        self.settings = settings
        self._fallback = RuleBasedFallbackAdapter()

    def explain(self, question: str, context: Dict[str, Any]) -> str:
        system_prompt = (
            "You are a trading bot's explanation assistant. You are given "
            "ALREADY-COMPUTED structured data below. Explain it in plain "
            "language, in at most 6 sentences. Do NOT invent any price, "
            "percentage, or status that is not present in the data. If the "
            "data says something is unavailable, say so plainly instead of "
            "guessing."
        )
        user_prompt = f"Question: {question}\n\nData:\n{json.dumps(context, indent=2, default=str)}"

        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 400,
        }
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.settings.llm_timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError) as e:
            fallback_text = self._fallback.explain(question, context)
            return f"[local LLM unavailable ({e}) — showing raw verified data instead]\n{fallback_text}"


def get_llm_adapter(settings: CopilotSettings = None) -> LLMAdapter:
    settings = settings or load_copilot_settings()
    if settings.llm_backend == "local_openai_compatible":
        return LocalOpenAICompatibleAdapter(settings)
    return RuleBasedFallbackAdapter()
