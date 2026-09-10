"""Conversation memory for the Copilot chat.

Deliberately simple and in-process: a dict of session_id -> ConversationState,
living in memory for the lifetime of the running backend process. This is
NOT a durable store — a restart clears it, and it doesn't work across
multiple backend processes/instances. That's an accepted limitation for
a local single-process trading bot (see docs/COPILOT.md); if this ever
needs to survive restarts or scale out, it should move to the existing
DatabaseManager rather than growing its own persistence layer.

What it tracks:
  - `turns`: recent (role, text) history, capped, used to give the LLM
    adapter real conversational context.
  - `last_symbol`: the last symbol a MARKET/TRADING question actually
    resolved to (NIFTY50, SENSEX, ...) — the deterministic backbone that
    makes "Which market are you analyzing?" answer correctly regardless
    of whether a local LLM is configured. This is not an LLM guess; it's
    exactly the symbol the last tool call actually used.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ConversationTurn:
    role: str  # "user" | "assistant"
    text: str


@dataclass
class ConversationState:
    turns: List[ConversationTurn] = field(default_factory=list)
    last_symbol: Optional[str] = None
    last_intent: Optional[str] = None

    MAX_TURNS = 20

    def add_turn(self, role: str, text: str) -> None:
        self.turns.append(ConversationTurn(role, text))
        if len(self.turns) > self.MAX_TURNS:
            self.turns = self.turns[-self.MAX_TURNS:]

    def recent_history(self, n: int = 8) -> List[ConversationTurn]:
        return self.turns[-n:]


_SESSIONS: Dict[str, ConversationState] = {}


def get_session(session_id: str) -> ConversationState:
    """Returns the existing state for `session_id`, creating a fresh one
    if this is the first message in that session. Callers that don't
    have/want a session (e.g. a one-off script) should just not pass a
    session_id — `conversational.chat()` works fine with `state=None`,
    it simply has no memory of prior turns in that case."""
    if session_id not in _SESSIONS:
        _SESSIONS[session_id] = ConversationState()
    return _SESSIONS[session_id]


def clear_session(session_id: str) -> None:
    _SESSIONS.pop(session_id, None)
