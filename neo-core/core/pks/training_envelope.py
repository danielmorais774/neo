"""Phase 1 — TrainingEnvelopeParser.

Detects whether an incoming message is an explicit *training* message for the Neo
agent (e.g. "Neo, modo treino") and separates the operational *envelope* (meta
commands like "salve no workspace do Neo") from the *real* training content.

Design rules (see docs/relatorio-roteamento-memoria-treinamento-neo.md):
- The envelope must NEVER become an active rule description.
- The raw text is preserved only in ``source_audit.raw_message``.
- Detection is conservative: only explicit training envelopes flip ``is_training``
  to True, so non-enveloped messages keep flowing through the legacy heuristics.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

NEO_AGENT = "neo-core"
NEO_WORKSPACE = "neo"
TRAINING_KIND_AGENT_BEHAVIOR = "agent_behavior"


def normalize_text(value: str | None) -> str:
    """Accent/case-insensitive normalization used for marker matching."""
    if not value:
        return ""
    text = unicodedata.normalize("NFD", value.strip().lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# Presence of any of these markers flips ``is_training`` to True.
_TRAINING_MARKERS: tuple[str, ...] = (
    "neo modo treino",
    "modo treino",
    "treinamento do neo",
    "treinamento direto",
    "esta mensagem e um treinamento",
    "isto e um treinamento",
    "nao e uma preferencia pessoal",
    "nao e preferencia pessoal",
    "salve no workspace do neo",
    "salvar no workspace do neo",
    "salve este aprendizado",
    "como voce deve se comportar como neo",
    "treine o neo",
    "treinar o neo",
)

# Lines whose normalized form matches any of these are pure operational envelope
# and are stripped from ``clean_content``.
_ENVELOPE_LINE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"\bneo\b.*\bmodo treino\b",
        r"^modo treino\b",
        r"\bmodo treino\b",
        r"nao e uma preferencia pessoal",
        r"nao e preferencia pessoal",
        r"esta mensagem e um treinamento",
        r"isto e um treinamento",
        r"\btreinamento direto\b",
        r"como voce deve se comportar como neo",
        r"salve.*workspace do neo",
        r"salvar.*workspace do neo",
        r"salve.*memoria do neo",
        r"salve este aprendizado",
    )
)

# Leading inline phrase removed even when content shares the same line.
_LEADING_ENVELOPE = re.compile(
    r"^\s*neo[,]?\s*modo\s+treino[\.\:\!\-]*\s*",
    flags=re.IGNORECASE,
)


@dataclass
class TrainingEnvelopeResult:
    is_training: bool
    target_agent: str | None = None
    target_workspace: str | None = None
    training_kind: str | None = None
    raw_message: str = ""
    clean_content: str = ""
    envelope_phrases: list[str] = field(default_factory=list)
    source_audit: dict[str, Any] = field(default_factory=dict)


class TrainingEnvelopeParser:
    """Splits a training message into envelope phrases and clean content."""

    def parse(self, message: str) -> TrainingEnvelopeResult:
        raw = message or ""
        normalized = normalize_text(raw)
        is_training = any(marker in normalized for marker in _TRAINING_MARKERS)

        if not is_training:
            return TrainingEnvelopeResult(
                is_training=False,
                raw_message=raw,
                clean_content=raw.strip(),
            )

        clean_content, envelope_phrases = self._strip_envelope(raw)
        source_audit = {
            "raw_message": raw,
            "clean_content": clean_content,
            "removed_envelope_phrases": envelope_phrases,
            "detected_at": datetime.now(timezone.utc).isoformat(),
        }
        return TrainingEnvelopeResult(
            is_training=True,
            target_agent=NEO_AGENT,
            target_workspace=NEO_WORKSPACE,
            training_kind=TRAINING_KIND_AGENT_BEHAVIOR,
            raw_message=raw,
            clean_content=clean_content,
            envelope_phrases=envelope_phrases,
            source_audit=source_audit,
        )

    def _strip_envelope(self, raw: str) -> tuple[str, list[str]]:
        kept_lines: list[str] = []
        removed: list[str] = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                kept_lines.append("")
                continue
            inline_clean = _LEADING_ENVELOPE.sub("", stripped)
            if inline_clean != stripped:
                removed.append("Neo, modo treino")
                if inline_clean:
                    kept_lines.append(inline_clean)
                continue
            normalized_line = normalize_text(stripped)
            if any(pattern.search(normalized_line) for pattern in _ENVELOPE_LINE_PATTERNS):
                removed.append(stripped)
                continue
            kept_lines.append(line)

        clean = "\n".join(kept_lines)
        # Strip a leading inline "Neo, modo treino." that shared a line with content.
        new_clean = _LEADING_ENVELOPE.sub("", clean)
        if new_clean != clean:
            removed.append("Neo, modo treino")
            clean = new_clean

        # Collapse runs of blank lines and trim.
        clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
        return clean, removed
