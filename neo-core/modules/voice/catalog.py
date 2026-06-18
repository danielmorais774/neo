"""Voice catalog + name resolver, shared by the voice tools and the config layer.

The neo-voice service (``GET /voices?backend=``) is the source of truth for which
voices exist (built-in speakers + cloned voices). :class:`VoiceCatalog` aggregates
that catalog across engines (short in-memory cache) and fuzzy-matches a requested
name (e.g. "jarvis") to a concrete entry. Both ``modules/voice`` (voice_list/
voice_get) and ``modules/config`` (the ``tts.voice`` setting) build on this so the
fetch/resolve logic lives in one place.
"""

from __future__ import annotations

import difflib
import logging
import time
import unicodedata
from typing import Any

import httpx

from shared.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Backends the neo-voice proxy can route to (see neo_voice_server.TTS_BACKENDS).
BACKENDS: tuple[str, ...] = ("qwen", "coqui", "dia", "cosyvoice", "f5", "chatterbox")
# Prefix neo-voice uses to label a cloned voice's friendly name in `desc`.
_CLONE_PREFIX = "🎤 clone:"
_CATALOG_TTL_SEC = 60.0


def normalize(s: str | None) -> str:
    """Lowercase + strip accents/whitespace for tolerant matching."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.strip().lower()


class VoiceCatalog:
    """Fetches + caches the neo-voice catalog and resolves names against it."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._catalog: list[dict[str, Any]] | None = None
        self._catalog_at: float = 0.0

    def seed(self, entries: list[dict[str, Any]]) -> None:
        """Inject a catalog directly (tests) so :meth:`fetch` skips HTTP."""
        self._catalog = list(entries)
        self._catalog_at = time.monotonic()

    async def fetch(self) -> list[dict[str, Any]]:
        """Aggregate ``GET /voices`` across backends into a flat list of entries.

        Each entry: ``{engine, voice, label, desc, native, is_clone, language}``.
        ``voice`` is the speaker id sent to the engine; ``label`` is the friendly
        name to match against (clone's friendly name, else the speaker name).
        """
        now = time.monotonic()
        if self._catalog is not None and (now - self._catalog_at) < _CATALOG_TTL_SEC:
            return self._catalog

        base = self.settings.neo_voice_url.rstrip("/")
        entries: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=self.settings.neo_voice_timeout) as client:
            for engine in BACKENDS:
                try:
                    r = await client.get(f"{base}/voices", params={"backend": engine})
                    r.raise_for_status()
                    data = r.json()
                except Exception as e:
                    logger.info("voice catalog: engine '%s' indisponível: %s", engine, e)
                    continue
                language = data.get("language")
                for v in data.get("voices", []):
                    name = v.get("name", "")
                    desc = v.get("desc", "") or ""
                    native = v.get("native", "")
                    is_clone = native == "clone" or desc.startswith(_CLONE_PREFIX)
                    label = desc[len(_CLONE_PREFIX):].strip() if is_clone and desc.startswith(_CLONE_PREFIX) else name
                    entries.append({
                        "engine": engine,
                        "voice": name,
                        "label": label,
                        "desc": desc,
                        "native": native,
                        "is_clone": is_clone,
                        "language": language,
                    })
        self._catalog = entries
        self._catalog_at = now
        return entries

    def resolve(self, query: str, entries: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Best-effort match of ``query`` against entry label/voice. Exact → substring → fuzzy.

        On ties (same name in multiple engines, e.g. a clone present in qwen and
        coqui) the default engine wins, so "usa a voz do X" lands on the engine the
        renderer defaults to.
        """
        q = normalize(query)
        if not q or not entries:
            return None
        default_engine = normalize(getattr(self.settings, "neo_voice_default_engine", ""))
        ordered = sorted(entries, key=lambda e: 0 if normalize(e["engine"]) == default_engine else 1)
        # candidate strings per entry: friendly label and the raw speaker id
        cands: list[tuple[str, dict[str, Any]]] = []
        for e in ordered:
            for c in {normalize(e["label"]), normalize(e["voice"])}:
                if c:
                    cands.append((c, e))

        # 1) exact
        for c, e in cands:
            if c == q:
                return e
        # 2) substring (query inside a candidate, or vice-versa) — prefer the closest length
        subs = [(c, e) for c, e in cands if q in c or c in q]
        if subs:
            subs.sort(key=lambda ce: abs(len(ce[0]) - len(q)))
            return subs[0][1]
        # 3) fuzzy
        keys = [c for c, _ in cands]
        match = difflib.get_close_matches(q, keys, n=1, cutoff=0.6)
        if match:
            for c, e in cands:
                if c == match[0]:
                    return e
        return None

    @staticmethod
    def compact(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Trim catalog entries to the fields a caller/UI needs."""
        return [
            {"name": e["label"], "voice": e["voice"], "engine": e["engine"],
             "is_clone": e["is_clone"], "desc": e["desc"]}
            for e in entries
        ]
