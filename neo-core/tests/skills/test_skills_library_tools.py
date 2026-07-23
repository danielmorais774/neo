"""Tools skill_read/skill_list (executor) + gating no read set do cognitivo."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

import shared.config
from core.cognitive import cognitive_tools_for
from core.skills.library import SkillLibrary
from modules.skills_library import SkillsLibraryExecutor


def _settings(**overrides):
    base = dict(
        workbench_enabled=True,
        skills_library_enabled=True,
        web_search_enabled=True,
        skill_read_max_bytes=16_384,
        skills_catalog_max_entries=30,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _library(tmp_path):
    d = tmp_path / "diagnostico-voz"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: diagnostico-voz\ndescription: Diagnóstico de voz.\n---\n\n# Passos\n1. voice_get",
        encoding="utf-8",
    )
    lib = SkillLibrary(root=tmp_path, settings=_settings())
    lib.load()
    return lib


@pytest.mark.asyncio
async def test_skill_read_returns_formatted_body(tmp_path):
    ex = SkillsLibraryExecutor(_library(tmp_path))
    out = await ex.execute("skill_read", {"name": "diagnostico-voz"})
    assert isinstance(out, str)
    assert out.startswith("# SKILL: diagnostico-voz (instructional)")
    assert "1. voice_get" in out


@pytest.mark.asyncio
async def test_skill_read_unknown_raises_with_catalog(tmp_path):
    ex = SkillsLibraryExecutor(_library(tmp_path))
    with pytest.raises(ValueError, match="diagnostico-voz"):
        await ex.execute("skill_read", {"name": "nao-existe"})


@pytest.mark.asyncio
async def test_skill_list(tmp_path):
    ex = SkillsLibraryExecutor(_library(tmp_path))
    out = await ex.execute("skill_list", {})
    assert "diagnostico-voz [instructional] — Diagnóstico de voz." in out


# ── read set do cognitivo ────────────────────────────────────────────────────

def _patch_settings(monkeypatch, **overrides):
    fake = _settings(**overrides)
    monkeypatch.setattr(shared.config, "get_settings", lambda: fake)


def test_cognitive_tools_include_skill_read_when_enabled(monkeypatch):
    _patch_settings(monkeypatch, skills_library_enabled=True)
    names = {t.name for t in cognitive_tools_for(uuid4())}
    assert {"skill_read", "skill_list"} <= names


def test_cognitive_tools_exclude_skill_read_when_disabled(monkeypatch):
    _patch_settings(monkeypatch, skills_library_enabled=False)
    names = {t.name for t in cognitive_tools_for(uuid4())}
    assert "skill_read" not in names and "skill_list" not in names
