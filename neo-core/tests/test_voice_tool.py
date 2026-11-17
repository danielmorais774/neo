"""Tests for the READ-ONLY `voice_*` tools and the active-voice persistence.

Covers the VoiceCatalog fuzzy matcher (exact / friendly clone name / substring /
fuzzy / miss), voice_list filtering, voice_get, and get_active_voice (defaults vs.
persisted), plus the ToolSchema definitions. *Setting* the voice now lives in
``modules/config`` (see test_config_tool.py).
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from modules.voice import VOICE_TOOLS, VoiceCatalog, VoiceExecutor, get_active_voice
from modules.voice.executor import PREF_KEY


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _FakePref:
    def __init__(self, key: str, value: str):
        self.key = key
        self.value = value


class _FakeDB:
    """Captures upserts and serves get_preferences from an in-memory list."""

    def __init__(self, prefs=None):
        self._prefs = list(prefs or [])
        self.upserts: list[dict] = []

    async def get_preferences(self, user_id=None, service=None, category=None):
        return list(self._prefs)

    async def upsert_preference(self, **kwargs):
        self.upserts.append(kwargs)
        self._prefs = [p for p in self._prefs if p.key != kwargs["key"]]
        pref = _FakePref(kwargs["key"], kwargs["value"])
        self._prefs.append(pref)
        return pref


# Catalog as produced by VoiceCatalog.fetch (aggregated /voices).
CATALOG = [
    {"engine": "qwen", "voice": "Ryan", "label": "Ryan", "desc": "Masculino dinamico",
     "native": "Ingles", "is_clone": False, "language": "Portuguese"},
    {"engine": "qwen", "voice": "Vivian", "label": "Vivian", "desc": "Feminina jovem",
     "native": "Chines", "is_clone": False, "language": "Portuguese"},
    {"engine": "coqui", "voice": "qclone_abcd1234", "label": "jarvis",
     "desc": "🎤 clone: jarvis", "native": "clone", "is_clone": True, "language": "pt"},
]


def _catalog() -> VoiceCatalog:
    cat = VoiceCatalog()
    cat.seed(CATALOG)          # skip HTTP
    return cat


def _executor(db=None) -> VoiceExecutor:
    ex = VoiceExecutor(db=db or _FakeDB())
    ex.catalog.seed(CATALOG)
    return ex


# ── VoiceCatalog.resolve ──────────────────────────────────────────────────────

def test_resolve_exact_name():
    m = _catalog().resolve("Ryan", CATALOG)
    assert m is not None and m["voice"] == "Ryan" and m["engine"] == "qwen"


def test_resolve_clone_friendly_name():
    """'jarvis' matches the clone by its friendly label, not the qclone_ id."""
    m = _catalog().resolve("jarvis", CATALOG)
    assert m is not None and m["voice"] == "qclone_abcd1234" and m["label"] == "jarvis"


def test_resolve_substring():
    m = _catalog().resolve("viv", CATALOG)
    assert m is not None and m["voice"] == "Vivian"


def test_resolve_fuzzy_typo():
    m = _catalog().resolve("jarvas", CATALOG)  # typo → fuzzy
    assert m is not None and m["label"] == "jarvis"


def test_resolve_no_match():
    assert _catalog().resolve("xptozzz", CATALOG) is None


# ── voice_list ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_list_filters_by_engine():
    ex = _executor()
    out = await ex.execute("voice_list", {"engine": "coqui"})
    assert out["count"] == 1 and out["voices"][0]["name"] == "jarvis"


# ── voice_get / get_active_voice ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_active_voice_default_when_absent():
    db = _FakeDB()
    active = await get_active_voice(db, user_id=None)
    assert active["voice"] is None and active["engine"]  # engine defaults from settings


@pytest.mark.asyncio
async def test_get_active_voice_reads_persisted():
    selection = {"engine": "coqui", "voice": "qclone_abcd1234", "language": "pt", "label": "jarvis"}
    db = _FakeDB(prefs=[_FakePref(PREF_KEY, json.dumps(selection))])
    active = await get_active_voice(db, user_id=None)
    assert active == selection


@pytest.mark.asyncio
async def test_voice_get_reads_active():
    selection = {"engine": "qwen", "voice": "Ryan", "language": "Portuguese", "label": "Ryan"}
    db = _FakeDB(prefs=[_FakePref(PREF_KEY, json.dumps(selection))])
    ex = _executor(db=db)
    ex.set_context(uuid4())
    got = await ex.execute("voice_get", {})
    assert got["active"]["voice"] == "Ryan" and got["active"]["engine"] == "qwen"


# ── schemas ──────────────────────────────────────────────────────────────────

def test_voice_tool_schemas():
    names = {t.name for t in VOICE_TOOLS}
    assert names == {"voice_list", "voice_get"}
    for t in VOICE_TOOLS:
        assert t.execution == "server-side" and t.module == "voice"
