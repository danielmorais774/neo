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

# Static fallback: the three built-in pipeline skills. The live map is built in
# _skill_intent_map() from the SkillManifest registry (fonte única — F4 do
# docs/skills-library-plan.md), so a new pipeline skill only declares its
# manifest; this dict guarantees handoffs keep working even if no manifest was
# imported (e.g. bare unit tests).
_SKILL_TO_INTENT_STATIC: dict[str, IntentType] = {
    "home_assistant": IntentType.HOME_ASSISTANT,
    "whatsapp_messaging": IntentType.WHATSAPP_MESSAGING,
    "music": IntentType.MUSIC,
}


def _skill_intent_map() -> dict[str, IntentType]:
    """Fallback estático + manifests pipeline registrados (handoff=True)."""
    mapping = dict(_SKILL_TO_INTENT_STATIC)
    try:
        from core.skills.manifest import SkillManifest

        for name, manifest in SkillManifest.all().items():
            if getattr(manifest, "handoff", True) and manifest.intent_type is not None:
                mapping[name] = manifest.intent_type
    except Exception as e:  # noqa: BLE001 — parsing de handoff nunca pode quebrar
        logger.warning("[HANDOFF] manifest registry unavailable, using static map: %s", e)
    return mapping

_HANDOFF_RE = re.compile(
    r"\[\[\s*HANDOFF\s*:\s*([a-zA-Z_]+)\s*\]\](.*?)\[\[\s*/\s*HANDOFF\s*\]\]",
    re.DOTALL | re.IGNORECASE,
)

# Defensive cleanup for malformed/partial tags the model might emit.
_STRAY_TAG_RE = re.compile(r"\[\[\s*/?\s*HANDOFF[^\]]*\]\]", re.IGNORECASE)

# Some models copy the prompt's placeholder literally, e.g. emitting
# ``<ordem concreta: AC/DC no YouTube>`` instead of just ``AC/DC no YouTube``.
# Strip a wrapping ``<...>`` and a leading ``ordem ...:`` label so the skill gets
# the real order, not the template. Model-agnostic safety net.
_ORDER_LABEL_RE = re.compile(r"^\s*ordem\b[^:>]{0,80}:\s*", re.IGNORECASE)


def _clean_order(raw: str) -> str:
    """Strip a leaked prompt placeholder (``<ordem concreta: ...>``) from an order."""
    s = (raw or "").strip()
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1].strip()
    s = _ORDER_LABEL_RE.sub("", s).strip()
    return s.strip("<>").strip()


def extract_handoffs(text: str) -> tuple[str, list[Handoff]]:
    """Split ``text`` into (clean_user_text, handoffs).

    ``clean_user_text`` has every handoff block removed and whitespace tidied.
    ``handoffs`` contains one ``Handoff`` per well-formed block with a known skill.
    """
    if not text or "[[" not in text:
        return text, []

    handoffs: list[Handoff] = []
    skill_to_intent = _skill_intent_map()
    for match in _HANDOFF_RE.finditer(text):
        skill = match.group(1).strip().lower()
        input_text = _clean_order(match.group(2) or "")
        intent = skill_to_intent.get(skill)
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
