"""Declarative registry of configurable Neo settings.

A :class:`ConfigSpec` describes ONE setting: where it persists (a ``user_preference``
row), how to list its options, how to resolve a raw user value into a concrete one,
and how to merge that into the stored blob. Adding a new configurable setting is
``+1 ConfigSpec`` — no new tool/executor/router code. The generic ``config_*`` tools
(see :mod:`modules.config`) drive everything off this registry.

Several specs may share one preference row by writing different ``field`` keys of
the same JSON blob (e.g. ``tts.voice`` and ``tts.engine`` both live in
``neo-voice/tts/active``), which keeps existing readers like ``get_active_voice``
working unchanged.
"""

from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass, field as dc_field
from typing import Any, Awaitable, Callable, Literal, Optional


def _norm(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.strip().lower()


@dataclass
class ConfigSpec:
    """One configurable setting (a whitelisted, validated entry)."""

    key: str                       # stable id, e.g. "tts.voice", "tts.engine"
    label: str                     # human label, e.g. "Voz do TTS"
    description: str               # what it controls (shown to the model/UI)
    service: str                   # user_preference.service
    category: str                  # user_preference.category
    pref_key: str                  # user_preference.key (the row)
    aliases: list[str] = dc_field(default_factory=list)  # NL hints for matching
    field: Optional[str] = None    # JSON field within the row (None = whole value)
    scope: Literal["user", "global"] = "user"
    value_type: Literal["enum", "string", "int", "bool", "dynamic"] = "string"
    enum: Optional[list[str]] = None
    # async () -> list of options (strings or dicts) for dynamic value_type
    options_provider: Optional[Callable[[], Awaitable[list[Any]]]] = None
    # (raw, options) -> resolved value (or None when nothing matches)
    resolver: Optional[Callable[[str, Any], Any]] = None
    # (current_blob, resolved) -> new_blob to persist
    apply: Optional[Callable[[dict, Any], Any]] = None
    on_set: Optional[Callable[[], None]] = None      # post-persist hook
    default: Any = None                              # current value when unset
    default_factory: Optional[Callable[[], Any]] = None
    describe: Optional[Callable[[Any], str]] = None  # human display of a resolved value

    # -- options / resolve / apply / default -----------------------------------
    async def options(self) -> Optional[list[Any]]:
        """Allowed options: static ``enum`` or the dynamic ``options_provider``."""
        if self.options_provider is not None:
            return await self.options_provider()
        return list(self.enum) if self.enum is not None else None

    def resolve(self, raw: str, options: Optional[list[Any]]) -> Any:
        """Map a raw user value to a concrete one. Custom ``resolver`` wins; else
        exact/substring match against ``enum``/option strings (normalized)."""
        if self.resolver is not None:
            return self.resolver(raw, options)
        q = _norm(raw)
        if not q:
            return None
        choices = [str(o) for o in (options or [])]
        for c in choices:                       # exact
            if _norm(c) == q:
                return c
        for c in choices:                       # substring
            if q in _norm(c) or _norm(c) in q:
                return c
        match = difflib.get_close_matches(q, [_norm(c) for c in choices], n=1, cutoff=0.6)
        if match:
            for c in choices:
                if _norm(c) == match[0]:
                    return c
        return None

    def merge(self, current_blob: dict, resolved: Any) -> Any:
        """Produce the new value to persist. Custom ``apply`` wins; else set
        ``field`` on the blob (or replace the whole value when ``field`` is None)."""
        if self.apply is not None:
            return self.apply(current_blob, resolved)
        if self.field is not None:
            return {**(current_blob or {}), self.field: resolved}
        return resolved

    def current_default(self) -> Any:
        """Effective value when nothing is persisted yet."""
        if self.default_factory is not None:
            return self.default_factory()
        return self.default

    def display(self, resolved: Any) -> str:
        """Human-friendly rendering of a resolved value (for replies)."""
        if self.describe is not None:
            return self.describe(resolved)
        if isinstance(resolved, dict):
            return str(resolved.get("label") or resolved.get("voice") or resolved.get("name") or resolved)
        return str(resolved)


class ConfigRegistry:
    """Holds the known :class:`ConfigSpec`s and matches a query to one."""

    def __init__(self) -> None:
        self._specs: dict[str, ConfigSpec] = {}

    def register(self, spec: ConfigSpec) -> None:
        self._specs[spec.key] = spec

    def register_many(self, specs: list[ConfigSpec]) -> None:
        for s in specs:
            self.register(s)

    def get(self, key: str) -> Optional[ConfigSpec]:
        return self._specs.get(key)

    def all(self) -> list[ConfigSpec]:
        return list(self._specs.values())

    def match(self, query: str) -> Optional[ConfigSpec]:
        """Resolve a setting id OR natural-language name to a spec.

        Order: exact key → exact alias/label → substring on key/aliases/label →
        fuzzy. Lets the router pass ``"tts.voice"`` and a human pass ``"voz"``.
        """
        q = _norm(query)
        if not q:
            return None
        if query in self._specs:
            return self._specs[query]
        # build candidate strings → spec
        cands: list[tuple[str, ConfigSpec]] = []
        for spec in self._specs.values():
            for token in (spec.key, spec.label, *spec.aliases):
                if token:
                    cands.append((_norm(token), spec))
        for c, spec in cands:           # exact
            if c == q:
                return spec
        for c, spec in cands:           # substring
            if q in c or c in q:
                return spec
        match = difflib.get_close_matches(q, [c for c, _ in cands], n=1, cutoff=0.6)
        if match:
            for c, spec in cands:
                if c == match[0]:
                    return spec
        return None
