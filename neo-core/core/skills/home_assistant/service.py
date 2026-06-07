"""Home Assistant helpers — pure functions for entity resolution and formatting.

Kept free of I/O so they're trivially testable. The adapter fetches raw HA
state dicts; these helpers filter, fuzzy-match by alias ("luz da sala" →
``light.sala``) and render short human summaries.
"""

from difflib import SequenceMatcher
from typing import Any

# Reuse the accent-stripping convention from the intent router.
_ACCENT_MAP = {
    "á": "a", "à": "a", "â": "a", "ã": "a",
    "é": "e", "ê": "e", "í": "i",
    "ó": "o", "ô": "o", "õ": "o",
    "ú": "u", "ç": "c",
}


def _normalize(text: str) -> str:
    """Lowercase and strip PT accents for robust matching."""
    out = (text or "").lower().strip()
    for src, dst in _ACCENT_MAP.items():
        out = out.replace(src, dst)
    return out


def _friendly_name(entity: dict[str, Any]) -> str:
    return str((entity.get("attributes") or {}).get("friendly_name") or "")


def entity_domain(entity_id: str) -> str:
    """Return the domain part of an entity_id (``light.sala`` → ``light``)."""
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def filter_entities(
    states: list[dict[str, Any]],
    domain: str | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Filter raw HA states by domain and/or a free-text query.

    Domain matches the entity_id prefix. Query matches against both the
    entity_id and the friendly_name (accent-insensitive substring).
    """
    result = states
    if domain:
        dom = domain.strip().lower()
        result = [e for e in result if entity_domain(e.get("entity_id", "")) == dom]
    if query:
        q = _normalize(query)
        result = [
            e
            for e in result
            if q in _normalize(e.get("entity_id", "")) or q in _normalize(_friendly_name(e))
        ]
    return result


def resolve_entity(
    name: str,
    states: list[dict[str, Any]],
    domain: str | None = None,
    min_score: float = 0.45,
) -> dict[str, Any] | None:
    """Fuzzy-resolve a human name/alias to the best-matching HA entity.

    Tries, in order: exact entity_id, exact friendly_name, then fuzzy ratio
    over both. Returns the best entity above ``min_score`` or ``None``.
    """
    if not name or not states:
        return None

    candidates = filter_entities(states, domain=domain)
    target = _normalize(name)

    # Exact entity_id match.
    for e in candidates:
        if _normalize(e.get("entity_id", "")) == target:
            return e
    # Exact friendly_name match.
    for e in candidates:
        if _normalize(_friendly_name(e)) == target:
            return e

    # Fuzzy: best ratio across entity_id and friendly_name, with a bonus for
    # substring containment (handles "luz da sala" vs "Luz Sala").
    best: dict[str, Any] | None = None
    best_score = 0.0
    for e in candidates:
        eid = _normalize(e.get("entity_id", ""))
        fname = _normalize(_friendly_name(e))
        score = max(
            SequenceMatcher(None, target, eid).ratio(),
            SequenceMatcher(None, target, fname).ratio(),
        )
        if target and (target in fname or target in eid):
            score = max(score, 0.9)
        if score > best_score:
            best_score = score
            best = e

    return best if best_score >= min_score else None


def summarize_state(entity: dict[str, Any]) -> str:
    """Render a short, human-friendly one-line summary of an entity state."""
    if not entity:
        return "Entidade não encontrada."
    eid = entity.get("entity_id", "?")
    name = _friendly_name(entity) or eid
    state = entity.get("state", "?")
    attrs = entity.get("attributes") or {}

    extras: list[str] = []
    if "brightness" in attrs and attrs["brightness"] is not None:
        pct = round(int(attrs["brightness"]) / 255 * 100)
        extras.append(f"brilho {pct}%")
    if "temperature" in attrs and attrs["temperature"] is not None:
        extras.append(f"{attrs['temperature']}°")
    if "current_temperature" in attrs and attrs["current_temperature"] is not None:
        extras.append(f"atual {attrs['current_temperature']}°")
    if (unit := attrs.get("unit_of_measurement")) and not extras:
        extras.append(f"{state} {unit}")

    suffix = f" ({', '.join(extras)})" if extras else ""
    return f"{name}: {state}{suffix}"


def compact_entity(entity: dict[str, Any]) -> dict[str, Any]:
    """Trim a raw HA state dict down to the fields the LLM needs."""
    attrs = entity.get("attributes") or {}
    return {
        "entity_id": entity.get("entity_id"),
        "friendly_name": attrs.get("friendly_name"),
        "state": entity.get("state"),
        "domain": entity_domain(entity.get("entity_id", "")),
    }
