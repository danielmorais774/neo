"""Tests for the generic, registry-driven config layer (modules/config).

Covers the registry matcher, config_set for the two TTS settings (voice via fuzzy
catalog match, engine via enum with speaker reset), config_get/config_list, error
paths, and the Pattern-A invariant: config_set writes the same neo-voice/tts/active
row that modules.voice.get_active_voice reads.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from modules.config import ConfigExecutor, build_default_registry
from modules.voice import VoiceCatalog, get_active_voice
from modules.voice.executor import PREF_CATEGORY, PREF_KEY, PREF_SERVICE


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _FakePref:
    def __init__(self, key: str, value: str):
        self.key = key
        self.value = value


class _FakeDB:
    def __init__(self, prefs=None):
        self._prefs = list(prefs or [])
        self.upserts: list[dict] = []

    async def get_preferences(self, user_id=None, service=None, category=None):
        return list(self._prefs)

    async def upsert_preference(self, **kwargs):
        self.upserts.append(kwargs)
        self._prefs = [p for p in self._prefs if p.key != kwargs["key"]]
        self._prefs.append(_FakePref(kwargs["key"], kwargs["value"]))
        return self._prefs[-1]


CATALOG = [
    {"engine": "qwen", "voice": "Ryan", "label": "Ryan", "desc": "Masculino dinamico",
     "native": "Ingles", "is_clone": False, "language": "Portuguese"},
    {"engine": "qwen", "voice": "Vivian", "label": "Vivian", "desc": "Feminina jovem",
     "native": "Chines", "is_clone": False, "language": "Portuguese"},
    {"engine": "coqui", "voice": "qclone_abcd1234", "label": "jarvis",
     "desc": "🎤 clone: jarvis", "native": "clone", "is_clone": True, "language": "pt"},
]


def _registry():
    cat = VoiceCatalog()
    cat.seed(CATALOG)            # skip HTTP
    return build_default_registry(catalog=cat)


def _executor(db=None):
    ex = ConfigExecutor(db=db or _FakeDB(), registry=_registry())
    return ex


# ── registry matching ─────────────────────────────────────────────────────────

def test_registry_match_by_key_alias_fuzzy():
    reg = _registry()
    assert reg.match("tts.voice").key == "tts.voice"        # exact key
    assert reg.match("voz").key == "tts.voice"              # alias
    assert reg.match("motor").key == "tts.engine"           # alias
    assert reg.match("xpto") is None


# ── config_set: tts.voice ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_config_set_voice_persists_into_active_row():
    db = _FakeDB()
    ex = _executor(db=db)
    uid = uuid4()
    ex.set_context(uid)

    out = await ex.execute("config_set", {"setting": "tts.voice", "value": "jarvis"})
    assert out["ok"] is True and out["setting"] == "tts.voice" and out["value"] == "jarvis"

    assert len(db.upserts) == 1
    up = db.upserts[0]
    assert up["service"] == PREF_SERVICE and up["category"] == PREF_CATEGORY and up["key"] == PREF_KEY
    assert up["user_id"] == uid
    assert json.loads(up["value"]) == {
        "engine": "coqui", "voice": "qclone_abcd1234", "language": "pt", "label": "jarvis",
    }


@pytest.mark.asyncio
async def test_config_set_voice_no_match_lists_options():
    ex = _executor()
    out = await ex.execute("config_set", {"setting": "tts.voice", "value": "xptozzz"})
    assert out["ok"] is False
    assert set(out["available"]) >= {"Ryan", "Vivian", "jarvis"}


# ── config_set: tts.engine (resets speaker) ──────────────────────────────────

@pytest.mark.asyncio
async def test_config_set_engine_resets_voice():
    # Start from an active coqui/jarvis selection; switching engine clears the voice.
    selection = {"engine": "coqui", "voice": "qclone_abcd1234", "language": "pt", "label": "jarvis"}
    db = _FakeDB(prefs=[_FakePref(PREF_KEY, json.dumps(selection))])
    ex = _executor(db=db)

    out = await ex.execute("config_set", {"setting": "tts.engine", "value": "qwen"})
    assert out["ok"] is True and out["value"] == "qwen"
    assert json.loads(db.upserts[-1]["value"]) == {
        "engine": "qwen", "voice": None, "language": None, "label": None,
    }


@pytest.mark.asyncio
async def test_config_set_engine_enum_substring():
    ex = _executor()
    out = await ex.execute("config_set", {"setting": "tts.engine", "value": "coq"})  # substring
    assert out["ok"] is True and out["value"] == "coqui"


@pytest.mark.asyncio
async def test_config_set_unknown_setting():
    ex = _executor()
    out = await ex.execute("config_set", {"setting": "nope", "value": "x"})
    assert out["ok"] is False
    assert set(out["available"]) == {"tts.voice", "tts.engine"}


# ── config_get / config_list ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_config_get_engine_default_when_absent():
    ex = _executor()
    out = await ex.execute("config_get", {"setting": "tts.engine"})
    assert out["ok"] is True and out["current"]  # falls back to settings default engine
    assert set(out["options"]) == {"qwen", "coqui", "dia"}


@pytest.mark.asyncio
async def test_config_list_enumerates_settings():
    ex = _executor()
    out = await ex.execute("config_list", {})
    by_key = {s["setting"]: s for s in out["settings"]}
    assert {"tts.voice", "tts.engine"} <= set(by_key)
    assert set(by_key["tts.voice"]["options"]) >= {"Ryan", "Vivian", "jarvis"}
    assert by_key["tts.engine"]["current"]  # default engine


# ── Pattern A invariant ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_config_set_voice_then_get_active_voice():
    """config_set writes the row get_active_voice reads (Pattern A unchanged)."""
    db = _FakeDB()
    ex = _executor(db=db)
    await ex.execute("config_set", {"setting": "tts.voice", "value": "Ryan"})

    active = await get_active_voice(db, user_id=None)
    assert active["voice"] == "Ryan" and active["engine"] == "qwen" and active["label"] == "Ryan"
