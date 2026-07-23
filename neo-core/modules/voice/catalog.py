"""Voice catalog + name resolver, shared by the voice tools and the config layer.

The neo-voice service (``GET /voices?backend=``) is the source of truth for which
voices exist (built-in speakers + cloned voices). :class:`VoiceCatalog` aggregates
that catalog across engines (short in-memory cache) and fuzzy-matches a requested
name (e.g. "jarvis") to a concrete entry. Both ``modules/voice`` (voice_list/
voice_get) and ``modules/config`` (the ``tts.voice`` setting) build on this so the
fetch/resolve logic lives in one place.
"""

from __future__ import annotations

import asyncio
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
# Per-request timeout for the catalog fetch. Shorter than the generic neo-voice
# timeout on purpose: a DOWN engine answers ``/voices`` with a slow 502, and we
# must not let one dead backend blow the voice_list tool budget (15s).
_CATALOG_FETCH_TIMEOUT_SEC = 6.0


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
        # Per-engine catalog cache: key = engine name (or "*" for the full
        # aggregate), value = (entries, monotonic_ts). Scoping by engine matters:
        # a single dead backend (slow 502) must not be paid for by a request that
        # only wants coqui. 60s TTL.
        self._cache: dict[str, tuple[list[dict[str, Any]], float]] = {}
        self._seeded: list[dict[str, Any]] | None = None

    def seed(self, entries: list[dict[str, Any]]) -> None:
        """Inject a catalog directly (tests) so :meth:`fetch` skips HTTP."""
        self._seeded = list(entries)

    async def fetch(self, engine: str | None = None) -> list[dict[str, Any]]:
        """Return catalog entries, optionally scoped to a single ``engine``.

        ``GET /voices?backend=X`` is answered per engine by the neo-voice proxy,
        and a DOWN engine returns a slow 502. Querying all backends serially made
        a coqui-only request pay for every dead engine and blow the tool timeout.
        So we query only the backend(s) we need, in parallel, with a short timeout,
        and cache per engine.

        Each entry: ``{engine, voice, label, desc, native, is_clone, language}``.
        ``voice`` is the speaker id sent to the engine; ``label`` is the friendly
        name to match against (clone's friendly name, else the speaker name).
        """
        eng = normalize(engine) if engine else None
        if self._seeded is not None:
            return [e for e in self._seeded if not eng or normalize(e["engine"]) == eng]

        key = eng or "*"
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and (now - cached[1]) < _CATALOG_TTL_SEC:
            return cached[0]

        backends = (eng,) if eng else BACKENDS
        base = self.settings.neo_voice_url.rstrip("/")
        timeout = min(self.settings.neo_voice_timeout, _CATALOG_FETCH_TIMEOUT_SEC)

        async def _fetch_one(client: httpx.AsyncClient, backend: str) -> list[dict[str, Any]]:
            try:
                r = await client.get(f"{base}/voices", params={"backend": backend})
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                logger.info("voice catalog: engine '%s' indisponível: %s", backend, e)
                return []
            language = data.get("language")
            out: list[dict[str, Any]] = []
            for v in data.get("voices", []):
                name = v.get("name", "")
                desc = v.get("desc", "") or ""
                native = v.get("native", "")
                is_clone = native == "clone" or desc.startswith(_CLONE_PREFIX)
                label = desc[len(_CLONE_PREFIX):].strip() if is_clone and desc.startswith(_CLONE_PREFIX) else name
                out.append({
                    "engine": backend,
                    "voice": name,
                    "label": label,
                    "desc": desc,
                    "native": native,
                    "is_clone": is_clone,
                    "language": language,
                })
            return out

        async with httpx.AsyncClient(timeout=timeout) as client:
            results = await asyncio.gather(*(_fetch_one(client, b) for b in backends))
        entries = [e for sub in results for e in sub]
        self._cache[key] = (entries, now)
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

    async def resolve_named(self, name: str, engine: str = "coqui") -> dict[str, Any] | None:
        """Resolve a friendly voice name to a voice-settings dict, scoped to ``engine``.

        Fetches the catalog, keeps only ``engine`` entries (voice imitation uses
        coqui for now), fuzzy-matches ``name`` and returns the shape the transport
        expects (``{engine, voice, language, label}``), or ``None`` if nothing
        matches so the caller can fall back to the default voice.
        """
        if not name:
            return None
        entries = await self.fetch(engine=engine)
        match = self.resolve(name, entries)
        if not match:
            return None
        return {
            "engine": match["engine"],
            "voice": match["voice"],
            "language": match.get("language"),
            "label": match.get("label") or name,
        }

    @staticmethod
    def compact(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Trim catalog entries to the fields a caller/UI needs."""
        return [
            {"name": e["label"], "voice": e["voice"], "engine": e["engine"],
             "is_clone": e["is_clone"], "desc": e["desc"]}
            for e in entries
        ]
