"""AI coaching — single module replaces coach/, memory/, knowledge/, analytics/."""

from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import NamedTuple

import anthropic
import openai
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from models import UserState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conversation history (replaces knowledge/history.py — in-memory ring buffer)
# ---------------------------------------------------------------------------

class Turn(NamedTuple):
    role: str  # "user" | "coach"
    text: str


class History:
    """Per-user conversation history kept in memory (last N turns)."""

    def __init__(self, max_turns: int = 20) -> None:
        self._data: dict[int, deque[Turn]] = defaultdict(lambda: deque(maxlen=max_turns))

    def add(self, uid: int, role: str, text: str) -> None:
        self._data[uid].append(Turn(role, text))

    def recent(self, uid: int, n: int = 10) -> list[Turn]:
        return list(self._data[uid])[-n:]

    def format(self, uid: int, n: int = 10) -> str:
        return "\n".join(f"{t.role}: {t.text}" for t in self.recent(uid, n))


# ---------------------------------------------------------------------------
# System prompt builder (replaces prompts.py, prompt_rules.py, category_prompts.py, coaching_style.py)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are the Unhooked coach — a direct, warm, no-bullshit addiction recovery coach on Telegram.

User profile:
- Quitting: {habit}
- Day: {streak_days}
- Their WHY: {why}
- Recent triggers: {triggers}
- Current mood: {mood}
- Recent journal: {recent}

Rules:
1. Match the user's language (German/English) automatically.
2. Be a live coach: ask before pushing plans. One step at a time.
3. Keep responses short — max 3-4 sentences for Telegram readability.
4. Never shame, never guilt. Identity > willpower.
5. When they report a win, celebrate briefly (1 sentence, not a paragraph).
6. When they're struggling, acknowledge it then redirect to action.
7. For mundane questions, answer normally like a friend would.
8. You're not a therapist. Suggest professional help for serious mental health concerns.
9. Never invent facts about the user. Only reference data from the profile above.
10. If recent conversation is provided, stay contextually aware — don't repeat yourself.

What the best responses look like:
- Crisis: "Was ist passiert?" (one line, open question)
- Craving: "Stark dass du's meldest. Was hilft dir gerade am meisten?"
- Win: "💪" or one sentence. Not a paragraph.
"""

_FALLBACK_REPLY = "Take one concrete action in the next 5 minutes."


def build_system_prompt(user: UserState) -> str:
    recent = "; ".join(e.get("text", "") for e in user.journal[-3:]) or "none"
    mood = "unknown"
    if user.mood_log:
        m = user.mood_log[-1]
        mood = str(m.get("morning") or m.get("score") or "unknown")
    return SYSTEM_PROMPT.format(
        habit=user.habit or "unknown",
        streak_days=user.streak_days,
        why=user.why or "not set",
        triggers=", ".join(user.triggers) or "none",
        mood=mood,
        recent=recent,
    )


# ---------------------------------------------------------------------------
# AI client (replaces ai_client.py + router.py — no OpenClaw dependency)
# ---------------------------------------------------------------------------

@dataclass
class AIClient:
    provider: str = "openai"  # "openai" or "anthropic"
    model: str = "gpt-4o"
    openai_key: str | None = None
    anthropic_key: str | None = None
    _oa_client: AsyncOpenAI | None = field(default=None, init=False, repr=False)
    _ant_client: AsyncAnthropic | None = field(default=None, init=False, repr=False)

    def configured(self) -> bool:
        if self.provider == "anthropic":
            return bool(self.anthropic_key)
        return bool(self.openai_key)

    async def reply(self, user: UserState, message: str, history_text: str = "") -> str:
        if not self.configured():
            return "Kurzer Aussetzer — versuch's gleich nochmal. Du schaffst das. 💪"
        system = build_system_prompt(user)
        if history_text:
            system += f"\n\nRecent conversation:\n{history_text}"
        try:
            if self.provider == "anthropic":
                return await self._anthropic(system, message)
            return await self._openai(system, message)
        except (openai.APIError, anthropic.APIError) as exc:
            logger.warning("AI call failed: %s", exc)
            return "Pause. Atme 4-4-8 — dann schreib mir, was als Nächstes kommt."

    async def _openai(self, system: str, message: str) -> str:
        if self._oa_client is None:
            self._oa_client = AsyncOpenAI(api_key=self.openai_key)
        r = await self._oa_client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": message}],
            temperature=0.5,
            max_tokens=300,
        )
        if not r.choices:
            return _FALLBACK_REPLY
        return r.choices[0].message.content or _FALLBACK_REPLY

    async def _anthropic(self, system: str, message: str) -> str:
        if self._ant_client is None:
            self._ant_client = AsyncAnthropic(api_key=self.anthropic_key)
        r = await self._ant_client.messages.create(
            model=self.model,
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": message}],
        )
        parts = [b.text for b in r.content if hasattr(b, "text")]
        return " ".join(parts).strip() or _FALLBACK_REPLY


# ---------------------------------------------------------------------------
# Intent detection (replaces handlers/intents.py — simple keyword match)
# ---------------------------------------------------------------------------

# Word-boundary matching avoids false positives like "glückstrückfall" or
# "geraucht" inside an unrelated compound. Multi-word phrases use \s+ so any
# whitespace between tokens matches.
_RELAPSE_RE = re.compile(
    r"\b(?:"
    r"rückfall|relapse|geraucht|gekifft"
    r"|hab(?:e)?\s+wieder"
    r"|i\s+smoked|i\s+relapsed"
    r"|geschafft\s+nicht"
    r")\b",
    re.IGNORECASE,
)

# If any of these appear in the 3 tokens immediately before the match,
# treat it as negated ("keinen rückfall", "heute nicht geraucht", ...) and skip.
_NEGATIONS = {
    # German
    "kein", "keine", "keinen", "keiner", "keinem", "keines",
    "nichts", "nie", "niemals", "nicht", "niemand",
    # English (with and without apostrophes — apostrophes get stripped
    # from tokens via `strip(".,!?;:")` so we match both forms anyway,
    # but listing both is explicit and safe)
    "no", "not", "never",
    "dont", "don't", "doesnt", "doesn't",
    "didnt", "didn't", "havent", "haven't", "hasnt", "hasn't",
    "wont", "won't",
}

_JOURNAL_PREFIX = ["journal:", "tagebuch:", "log:"]


def detect_intent(text: str) -> tuple[str, str] | None:
    lower = text.lower().strip()
    for prefix in _JOURNAL_PREFIX:
        if lower.startswith(prefix):
            return ("journal", text[len(prefix):].strip())
    match = _RELAPSE_RE.search(lower)
    if match:
        # Peek at the 3 tokens before the relapse keyword for a negation.
        preceding = lower[: match.start()].split()[-3:]
        if any(tok.strip(".,!?;:") in _NEGATIONS for tok in preceding):
            return None
        return ("relapse", text)
    return None
