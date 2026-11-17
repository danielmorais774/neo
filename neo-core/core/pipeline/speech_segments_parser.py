"""Parse ``[[VOICE:name]]...[[/VOICE]]`` blocks from a model reply into ordered
speech segments (voice imitation).

The cognitive core may wrap parts of its reply in a voice tag so the renderer
speaks that span with a different voice (e.g. imitating a character). This mirrors
the ``[[HANDOFF:skill]]`` convention in :mod:`core.pipeline.handoff_parser`: we
split the reply into ordered segments and strip every tag from the user-visible
text so nothing leaks.

The parser is pure/sync and does NOT resolve voice names — it returns the raw
requested name per segment. Resolution against the neo-voice catalog (name →
concrete engine/voice id) happens later, where the catalog is available (see
``AIService._resolve_speech_segments`` + ``VoiceCatalog.resolve_named``).

A reply with no well-formed voice tag is returned unchanged with an empty segment
list — callers treat that as the ordinary single-voice path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ParsedSegment:
    """One ordered chunk of a multi-voice reply.

    ``voice`` is the raw friendly voice name the model asked for (``None`` → speak
    with the reply's default/active voice).
    """

    text: str
    voice: str | None = None


# [[VOICE:mario]] ... [[/VOICE]] — name is anything up to the closing ]].
_VOICE_RE = re.compile(
    r"\[\[\s*VOICE\s*:\s*([^\]]+?)\s*\]\](.*?)\[\[\s*/\s*VOICE\s*\]\]",
    re.DOTALL | re.IGNORECASE,
)

# Defensive cleanup for malformed/partial voice tags the model might emit
# (an opening tag without a close, a stray close, etc.).
_STRAY_TAG_RE = re.compile(r"\[\[\s*/?\s*VOICE[^\]]*\]\]", re.IGNORECASE)


def _norm_ws(s: str) -> str:
    """Collapse runs of whitespace to single spaces and strip the ends."""
    return re.sub(r"\s+", " ", s or "").strip()


def extract_speech_segments(text: str) -> tuple[str, list[ParsedSegment]]:
    """Split ``text`` into ``(clean_user_text, segments)``.

    ``segments`` is empty when the reply has no well-formed voice tag (ordinary
    single-voice reply). Otherwise it partitions the whole reply in order: spans
    outside a tag get ``voice=None`` (default voice); spans inside a tag carry the
    requested voice name. ``clean_user_text`` is the concatenation of the segment
    texts — the full reply for display/back-compat, with every tag removed.
    """
    if not text or "[[" not in text:
        return text, []

    segments: list[ParsedSegment] = []
    last = 0
    has_voice = False
    for m in _VOICE_RE.finditer(text):
        gap = _norm_ws(_STRAY_TAG_RE.sub("", text[last:m.start()]))
        if gap:
            segments.append(ParsedSegment(text=gap, voice=None))
        name = _norm_ws(m.group(1))
        body = _norm_ws(_STRAY_TAG_RE.sub("", m.group(2) or ""))
        if body:
            segments.append(ParsedSegment(text=body, voice=name or None))
            if name:
                has_voice = True
        last = m.end()

    tail = _norm_ws(_STRAY_TAG_RE.sub("", text[last:]))
    if tail:
        segments.append(ParsedSegment(text=tail, voice=None))

    if not has_voice:
        # No usable voice tag. Only touch the text if a stray VOICE fragment was
        # present; otherwise return it byte-for-byte (never alter normal replies).
        stripped = _STRAY_TAG_RE.sub("", text)
        if stripped == text:
            return text, []
        return re.sub(r"\n{3,}", "\n\n", stripped).strip(), []

    clean = " ".join(s.text for s in segments).strip()
    return clean, segments
