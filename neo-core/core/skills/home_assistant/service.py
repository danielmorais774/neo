"""Home Assistant helpers — pure functions for entity resolution and formatting.

Kept free of I/O so they're trivially testable. The adapter fetches raw HA
state dicts; these helpers filter, fuzzy-match by alias ("luz da sala" →
``light.sala``) and render short human summaries.
"""

import re
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


# ── Live-snapshot injection into the cognitive core ──────────────────────────
# Domains worth surfacing/controlling from a chat turn. Everything else (sensors
# binários internos, device_tracker, sun, etc.) é ruído pro cérebro.
_SNAPSHOT_DOMAINS: tuple[str, ...] = (
    "light", "switch", "climate", "cover", "fan", "media_player",
    "lock", "scene", "script", "sensor",
)
# Broad whole-house triggers — let "como tá a casa?" / "apaga tudo" inject the
# snapshot even without naming a specific device.
_HOME_BROAD_TERMS: tuple[str, ...] = (
    "casa", "luzes", "tudo", "dispositivos", "aparelhos", "comodos",
)


def build_device_index(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact roster (entity_id/friendly_name/domain) used by the relevance gate.

    Cheap and stable: the device *roster* changes rarely, so this is cached with a
    long TTL and matched in-memory on every message (no HA call on the hot path).
    """
    return [compact_entity(e) for e in states]


def match_known_device(
    text: str,
    index: list[dict[str, Any]],
    min_score: float = 0.6,
) -> list[dict[str, Any]]:
    """Return index entries whose name/alias the message plausibly refers to.

    Mirrors :func:`resolve_entity`'s scoring (fuzzy ratio over entity_id and
    friendly_name + substring bonus), but scans the full message instead of an
    isolated name, so a free-form turn ("liga o arcondicionado do quarto")
    surfaces the matching entity. A small set of broad house terms ("casa",
    "luzes", "tudo") also match, so whole-house questions inject the snapshot.

    Empty list ⇒ the message does not refer to any HA device → caller skips the
    HA fetch entirely (e.g. "liga o meu computador").
    """
    norm = _normalize(text)
    if not norm or not index:
        return []

    # Broad house questions → surface everything relevant (no specific device).
    if any(re.search(rf"\b{re.escape(t)}\b", norm) for t in _HOME_BROAD_TERMS):
        return [e for e in index if e.get("domain") in _SNAPSHOT_DOMAINS]

    matched: list[dict[str, Any]] = []
    for entry in index:
        eid = _normalize(str(entry.get("entity_id") or ""))
        fname = _normalize(str(entry.get("friendly_name") or ""))
        # Drop the domain prefix so "ar condicionado" matches "climate.ar_condicionado".
        eid_tail = eid.split(".", 1)[1] if "." in eid else eid
        score = 0.0
        for name in (fname, eid_tail):
            if not name:
                continue
            if name in norm or norm in name:
                score = 1.0
                break
            score = max(score, _best_token_ratio(norm, name))
        if score >= min_score:
            matched.append(entry)
    return matched


def _best_token_ratio(haystack: str, name: str) -> float:
    """Best fuzzy ratio between ``name`` and any same-length-ish window of words.

    Compares ``name`` against the whole text and against each contiguous run of
    words up to the name's word count, so a short alias isn't drowned out by a
    long sentence.
    """
    best = SequenceMatcher(None, haystack, name).ratio()
    words = haystack.split()
    span = max(1, len(name.split()))
    for size in range(span, span + 2):
        for i in range(0, max(0, len(words) - size + 1)):
            window = " ".join(words[i : i + size])
            best = max(best, SequenceMatcher(None, window, name).ratio())
    return best


def render_snapshot(
    states: list[dict[str, Any]],
    query: str | None = None,
    max_entities: int = 40,
) -> str:
    """Render a compact, human-readable live snapshot of HA entities.

    When ``query`` resolves to specific rooms/devices the snapshot is narrowed to
    those (via :func:`filter_entities`); otherwise it is grouped by domain and
    capped at ``max_entities``. Each line uses :func:`summarize_state`.
    """
    relevant = [e for e in states if entity_domain(e.get("entity_id", "")) in _SNAPSHOT_DOMAINS]

    narrowed: list[dict[str, Any]] = []
    if query:
        norm = _normalize(query)
        narrowed = [
            e
            for e in relevant
            if norm and (norm in _normalize(_friendly_name(e)) or norm in _normalize(e.get("entity_id", "")))
        ]
    if narrowed:
        lines = [f"- {summarize_state(e)}" for e in narrowed[:max_entities]]
        return "\n".join(lines)

    # Grouped-by-domain fallback for broad questions.
    lines = []
    shown = 0
    for dom in _SNAPSHOT_DOMAINS:
        group = [e for e in relevant if entity_domain(e.get("entity_id", "")) == dom]
        if not group:
            continue
        lines.append(f"{dom}:")
        for e in group:
            if shown >= max_entities:
                lines.append("  …(lista truncada)")
                return "\n".join(lines)
            lines.append(f"  - {summarize_state(e)}")
            shown += 1
    return "\n".join(lines)


# Verification spec per (domain, service): how to confirm an action by re-reading
# the entity. ``kind`` ∈ {"state_eq", "state_ne", "attr_eq"}; for "attr_eq" the
# attribute name follows the colon ("attr_eq:temperature").
_VERIFY_SPECS: dict[tuple[str, str], tuple[str, Any]] = {
    ("light", "turn_on"): ("state_eq", "on"),
    ("light", "turn_off"): ("state_eq", "off"),
    ("switch", "turn_on"): ("state_eq", "on"),
    ("switch", "turn_off"): ("state_eq", "off"),
    ("fan", "turn_on"): ("state_eq", "on"),
    ("fan", "turn_off"): ("state_eq", "off"),
    ("input_boolean", "turn_on"): ("state_eq", "on"),
    ("input_boolean", "turn_off"): ("state_eq", "off"),
    ("cover", "open_cover"): ("state_eq", "open"),
    ("cover", "close_cover"): ("state_eq", "closed"),
    # Climate (ar-condicionado / termostato): state is the hvac_mode.
    ("climate", "turn_off"): ("state_eq", "off"),
    ("climate", "turn_on"): ("state_ne", "off"),  # any active mode = success
}


def verify_spec(
    domain: str,
    service: str,
    data: dict[str, Any] | None = None,
) -> tuple[str, Any] | None:
    """Return how to verify a (domain, service) action, or None when not verifiable.

    Some climate services carry the target in ``data`` (set_temperature →
    attribute ``temperature``; set_hvac_mode → resulting state), so they are
    resolved dynamically rather than from the static table.
    """
    data = data or {}
    if domain == "climate" and service == "set_temperature" and data.get("temperature") is not None:
        return ("attr_eq:temperature", data.get("temperature"))
    if domain == "climate" and service == "set_hvac_mode" and data.get("hvac_mode"):
        return ("state_eq", str(data.get("hvac_mode")))
    return _VERIFY_SPECS.get((domain, service))


def state_satisfies_spec(entity: dict[str, Any], spec: tuple[str, Any]) -> bool:
    """True when ``entity`` (a fresh HA state read) satisfies a ``verify_spec``."""
    kind, target = spec
    if kind == "state_eq":
        return entity.get("state") == target
    if kind == "state_ne":
        return entity.get("state") != target
    if kind.startswith("attr_eq:"):
        attr = kind.split(":", 1)[1]
        actual = (entity.get("attributes") or {}).get(attr)
        try:
            return float(actual) == float(target)
        except (TypeError, ValueError):
            return str(actual) == str(target)
    return False
