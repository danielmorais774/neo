"""Pure helpers for the music skill — service/query parsing and media-context reads.

Kept free of I/O so they're trivially testable. The pipeline does the HTTP and the
side effects; these functions only normalize text and inspect the desktop-provided
``media_context`` snapshot.
"""

from __future__ import annotations

import json
import re
from typing import Any

_ACCENT_MAP = {
    "á": "a", "à": "a", "â": "a", "ã": "a",
    "é": "e", "ê": "e", "í": "i",
    "ó": "o", "ô": "o", "õ": "o",
    "ú": "u", "ç": "c",
}


def _normalize(text: str) -> str:
    out = (text or "").lower().strip()
    for src, dst in _ACCENT_MAP.items():
        out = out.replace(src, dst)
    return " ".join(out.split())


# Media-control actions detected from the handoff order. The cognitive core emits a
# clean control word (pausar/retomar/próxima/...) for control, or a track for play.
# Ordered: unambiguous controls first; anything else falls through to "play". We avoid
# ambiguous tokens like "para"/"volta" here — the cognitive translates those to clean
# words (see _MUSIC_HANDOFF_INSTRUCTION).
_ACTION_RULES = [
    ("next", r"\b(proxima|proximo|pula|pular|avancar|skip|next)\b"),
    ("previous", r"\b(anterior|previous)\b"),
    ("volume_up", r"\b(aumenta|aumentar|sobe|subir)\b"),
    ("volume_down", r"\b(abaixa|abaixar|diminui|diminuir|baixa|baixar)\b"),
    ("mute", r"\b(mudo|muta|mutar|silencia|silenciar)\b"),
    ("pause", r"\b(pausa|pausar|pause|stop|interrompe)\b"),
    ("resume", r"(\b(retoma|retomar|continua|continuar|continue|despausa|despausar|resume)\b"
               r"|\b(da|dar|de)\s+(o\s+)?play\b|\bplay de novo\b|\bvolta(r)? a tocar\b)"),
]


def detect_action(text: str) -> str:
    """Classify the handoff order into a media action; default 'play' (a track)."""
    n = _normalize(text)
    for action, pat in _ACTION_RULES:
        if re.search(pat, n):
            return action
    return "play"


def detect_service(
    text: str,
    *,
    spotify_available: bool,
    youtube_available: bool,
    default_service: str = "youtube",
) -> str:
    """Return 'spotify' or 'youtube'.

    Explicit mention in the text ALWAYS wins. Otherwise pick ``default_service``
    (configurable via ``music_default_service``; defaults to YouTube since it needs
    no premium/login), falling back to whichever service is actually available.
    """
    n = _normalize(text)
    if "spotify" in n:
        return "spotify"
    if "youtube" in n or "you tube" in n or re.search(r"\byt\b", n):
        return "youtube"

    preferred = default_service if default_service in ("spotify", "youtube") else "youtube"
    fallback = "spotify" if preferred == "youtube" else "youtube"
    avail = {"spotify": spotify_available, "youtube": youtube_available}
    if avail.get(preferred):
        return preferred
    if avail.get(fallback):
        return fallback
    # Nothing configured — return the preference so the caller surfaces a hint.
    return preferred


# Phrases stripped from the handoff order to leave a clean search query. Applied
# case-insensitively with word boundaries; order matters (longer first).
_QUERY_STRIP = [
    r"\b(no|na|pelo|pela|la no|ai no|aqui no)\s+(spotify|youtube|yt|deezer)\b",
    r"\b(spotify|youtube|deezer)\b",
    r"^\s*(por favor|pf)\b",
    r"\b(toca|tocar|toque|coloca|colocar|bota|botar|poe|poem|reproduz|reproduzir|manda|mandar)\b",
    r"\b(quero|queria|gostaria de|pode|poderia)\s+(ouvir|escutar|tocar|colocar)\b",
    r"\b(ouvir|escutar)\b",
    r"\b(uma|um|a|o|aquela|aquele|aquela musica|aquele som)\s+(musica|som|playlist|cancao|faixa)\b",
    r"\b(pra mim|pra eu|pra gente|pra nos|ai|por favor|agora)\b",
]


def clean_query(text: str) -> str:
    """Strip play verbs / service mentions / fillers to leave a search query.

    Imperfect on purpose — Spotify/YouTube search tolerate messy queries, so we
    only remove the obvious noise and never return empty (falls back to the input).
    """
    out = " " + (text or "").strip() + " "
    for pattern in _QUERY_STRIP:
        out = re.sub(pattern, " ", out, flags=re.IGNORECASE)
    out = " ".join(out.split()).strip(" ,.-")
    return out or (text or "").strip()


def parse_media_context(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Read ``metadata['media_context']`` (a JSON string) into a dict; never raises."""
    if not metadata:
        return {}
    raw = metadata.get("media_context")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def find_music_tab(media_context: dict[str, Any], site: str) -> dict[str, Any] | None:
    """Return the first open browser tab whose ``site`` matches (e.g. 'youtube')."""
    tabs = media_context.get("browser_tabs") or []
    for tab in tabs:
        if isinstance(tab, dict) and tab.get("site") == site:
            return tab
    return None


def pick_active_device(devices: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the active Spotify Connect device, else the first available one."""
    if not devices:
        return None
    for dev in devices:
        if dev.get("is_active"):
            return dev
    return devices[0]
