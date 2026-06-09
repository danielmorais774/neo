"""Parse ``[[HANDOFF:skill]]...[[/HANDOFF]]`` control blocks from a model reply.

The cognitive core (DIRECT_RESPONSE) may end its reply with one or more handoff
blocks asking the orchestrator to run another pipeline next. This mirrors the
existing ``[[NEED_CLARIFICATION]]`` sentinel pattern: we extract the directive and
strip it from the user-visible text so the block never leaks to the user.

Unknown skills are dropped (and still stripped from the text) — we never trust the
model to invent a routable target.
"""

from __future__ import annotations

import logging
import re

from core.pipeline.types import Handoff
from shared.types import IntentType

logger = logging.getLogger(__name__)

# Map the skill name written inside the block to its routable intent.
_SKILL_TO_INTENT: dict[str, IntentType] = {
    "home_assistant": IntentType.HOME_ASSISTANT,
    "whatsapp_messaging": IntentType.WHATSAPP_MESSAGING,
}

_HANDOFF_RE = re.compile(
    r"\[\[\s*HANDOFF\s*:\s*([a-zA-Z_]+)\s*\]\](.*?)\[\[\s*/\s*HANDOFF\s*\]\]",
    re.DOTALL | re.IGNORECASE,
)

# Defensive cleanup for malformed/partial tags the model might emit.
_STRAY_TAG_RE = re.compile(r"\[\[\s*/?\s*HANDOFF[^\]]*\]\]", re.IGNORECASE)


def extract_handoffs(text: str) -> tuple[str, list[Handoff]]:
    """Split ``text`` into (clean_user_text, handoffs).

    ``clean_user_text`` has every handoff block removed and whitespace tidied.
    ``handoffs`` contains one ``Handoff`` per well-formed block with a known skill.
    """
    if not text or "[[" not in text:
        return text, []

    handoffs: list[Handoff] = []
    for match in _HANDOFF_RE.finditer(text):
        skill = match.group(1).strip().lower()
        input_text = (match.group(2) or "").strip()
        intent = _SKILL_TO_INTENT.get(skill)
        if intent is None:
            logger.warning("[HANDOFF] unknown skill in block, dropping: %r", skill)
            continue
        if not input_text:
            logger.warning("[HANDOFF] empty input for skill %r, dropping", skill)
            continue
        handoffs.append(
            Handoff(
                target_intent=intent,
                input_text=input_text,
                target_skill=skill,
                reason="cognitive_core_handoff",
            )
        )

    clean = _HANDOFF_RE.sub("", text)
    clean = _STRAY_TAG_RE.sub("", clean)
    # Collapse the blank lines left behind by the removed block(s).
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, handoffs
