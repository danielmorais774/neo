"""Preference Extractor — detects user preferences from conversation turns.

Uses a lightweight LLM call to extract preferences like formatting choices,
display options, language preferences, etc. from user messages and assistant
responses. Extracted preferences are persisted in the DB and injected into
future system prompts.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any
from uuid import UUID

from core.llm.router import LLMRouter
from core.pks.training_envelope import TrainingEnvelopeParser
from db.async_client import AsyncPostgresClient
from shared.types import Message, MessageRole

logger = logging.getLogger(__name__)

_ENVELOPE_PARSER = TrainingEnvelopeParser()

_EXTRACTION_PROMPT = """\
You are a preference extractor. Given a user message and the assistant's response,
determine if the user expressed any PERSISTENT preference that should be remembered
for future interactions.

Preferences include (but are not limited to):
- Display/formatting preferences (e.g., use emojis, specific colors, markdown style)
- Language preferences (formality level, language choice)
- Behavior preferences (verbosity, tone, response length)
- Tool/workflow preferences (preferred commands, editors, tools)
- Game preferences (symbols, rules, display format)

Rules:
- Only extract EXPLICIT preferences the user stated (not implicit ones)
- Ignore one-time requests (e.g., "translate this to English" is NOT a preference)
- A preference is something the user wants GOING FORWARD in all similar interactions
- Return an empty array if no preferences are found
- Each preference must have: category, key, value (human-readable description)

Respond ONLY with valid JSON:
{
  "preferences": [
    {
      "category": "display",
      "key": "tic_tac_toe_symbols",
      "value": "Use 🔴 for X and 🔵 for O in tic-tac-toe games"
    }
  ]
}

Categories: display, language, behavior, tools, games, formatting, other
"""


class PreferenceExtractor:
    """Extracts and persists user preferences from conversation turns."""

    def __init__(self, llm_router: LLMRouter, db: AsyncPostgresClient) -> None:
        self.llm = llm_router
        self.db = db

    async def extract_and_save(
        self,
        user_message: str,
        assistant_response: str,
        user_id: UUID | None = None,
        service: str = "neo-desktop",
    ) -> list[dict[str, str]]:
        """Analyze a conversation turn and extract/save any preferences.

        Returns list of extracted preferences (may be empty).
        This is fire-and-forget — errors are logged but never raised.
        """
        # Skip very short messages — unlikely to contain preferences
        if len(user_message.strip()) < 20:
            return []
        if _ENVELOPE_PARSER.parse(user_message).is_training or self._looks_like_agent_training(user_message):
            logger.info("[PREF] Skipping user preference extraction for agent training message")
            return []

        try:
            preferences = await self._extract(user_message, assistant_response)
            if not preferences:
                return []

            saved = []
            for pref in preferences:
                category = pref.get("category", "other")
                key = pref.get("key", "")
                value = pref.get("value", "")
                if not key or not value:
                    continue

                await self.db.upsert_preference(
                    category=category,
                    key=key,
                    value=value,
                    user_id=user_id,
                    service=service,
                    source_message=user_message[:500],
                )
                saved.append(pref)
                logger.info(f"[PREF] Saved preference: [{category}] {key} = {value}")

            return saved

        except Exception as e:
            logger.warning(f"[PREF] Extraction failed (non-blocking): {e}")
            return []

    async def _extract(
        self,
        user_message: str,
        assistant_response: str,
    ) -> list[dict[str, str]]:
        """Call LLM to extract preferences from a conversation turn."""
        messages = [
            Message(role=MessageRole.SYSTEM, content=_EXTRACTION_PROMPT),
            Message(
                role=MessageRole.USER,
                content=(
                    f"User message:\n{user_message}\n\n"
                    f"Assistant response:\n{assistant_response[:1000]}"
                ),
            ),
        ]

        provider = self.llm.get_provider()
        full_response = ""

        async for chunk in provider.stream_complete(messages=messages):
            from shared.types import StreamEventType
            if chunk.event == StreamEventType.TOKEN and chunk.content:
                full_response += chunk.content
            elif chunk.event == StreamEventType.ERROR:
                logger.warning(f"[PREF] LLM error during extraction: {chunk.content}")
                return []

        return self._parse_response(full_response)

    @staticmethod
    def _parse_response(response: str) -> list[dict[str, str]]:
        """Parse JSON response from LLM."""
        try:
            # Try to find JSON in the response
            text = response.strip()
            # Handle markdown code blocks
            if "```" in text:
                start = text.find("```")
                end = text.rfind("```")
                if start != end:
                    inner = text[start:end]
                    # Remove the opening ``` and optional language tag
                    first_newline = inner.find("\n")
                    if first_newline >= 0:
                        text = inner[first_newline + 1:]

            data = json.loads(text)
            prefs = data.get("preferences", [])
            if not isinstance(prefs, list):
                return []
            return [
                p for p in prefs
                if isinstance(p, dict) and p.get("key") and p.get("value")
            ]
        except (json.JSONDecodeError, AttributeError, KeyError):
            logger.debug(f"[PREF] Failed to parse extraction response: {response[:200]}")
            return []

    @staticmethod
    def _looks_like_agent_training(text: str) -> bool:
        normalized = unicodedata.normalize("NFD", (text or "").strip().lower())
        normalized = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
        normalized = re.sub(r"[^a-z0-9_ ]+", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        markers = (
            "neo modo treino",
            "modo treino",
            "treine o neo",
            "treinar o neo",
            "neo deve",
            "quando acontecer",
            "quando isso acontecer",
            "quando ocorrer",
            "isso foi errado",
            "correto seria",
            "nao aja assim",
            "a partir de agora voce deve",
            "a partir de agora vc deve",
            "a partir de agora o neo deve",
        )
        return any(marker in normalized for marker in markers)

    async def get_preferences_text(
        self,
        user_id: UUID | None = None,
        service: str = "neo-desktop",
    ) -> str | None:
        """Build a formatted text block of all user preferences for injection into system prompt."""
        prefs = await self.db.get_preferences(user_id=user_id, service=service)
        if not prefs:
            return None

        lines: list[str] = []
        current_cat = ""
        for p in prefs:
            if p.category != current_cat:
                current_cat = p.category
                lines.append(f"\n**{current_cat.title()}:**")
            lines.append(f"- {p.value}")

        return "\n".join(lines)
