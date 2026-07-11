"""SkillLibrary — loader fail-safe, catálogo com budget e skill_read seguro.

Cobre o núcleo da F1 do plano (docs/skills-library-plan.md): parse de
frontmatter, validação que PULA (nunca levanta), progressive disclosure
(catalog_note) e leitura com anti path-traversal + cap de bytes.
"""

from types import SimpleNamespace

import pytest

from core.skills.library import SkillLibrary, split_frontmatter


def _settings(**overrides):
    base = dict(
        workbench_enabled=True,
        skills_library_enabled=True,
        skill_read_max_bytes=16_384,
        skills_catalog_max_entries=30,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Registry:
    """ToolRegistry fake: exists() por lista de nomes."""

    def __init__(self, names=()):
        self._names = set(names)

    def exists(self, name):
        return name in self._names

    def get(self, name):
        assert name in self._names
        return SimpleNamespace(name=name)


def _write_skill(root, name, frontmatter, body="# Corpo\n\nInstruções."):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return d


def _lib(root, settings=None, registry=None):
    lib = SkillLibrary(root=root, settings=settings or _settings(), tool_registry=registry)
    lib.load()
    return lib


# ── frontmatter ──────────────────────────────────────────────────────────────

def test_split_frontmatter_basic():
    meta, body = split_frontmatter("---\nname: x\ndescription: d\n---\n\ncorpo aqui")
    assert meta == {"name": "x", "description": "d"}
    assert body.strip() == "corpo aqui"


def test_split_frontmatter_body_with_hr():
    meta, body = split_frontmatter("---\nname: x\n---\ncorpo\n---\nresto")
    assert meta == {"name": "x"}
    assert "corpo" in body and "resto" in body


def test_split_frontmatter_absent():
    meta, body = split_frontmatter("# só corpo")
    assert meta is None
    assert body == "# só corpo"


# ── loader fail-safe ─────────────────────────────────────────────────────────

def test_load_valid_skill(tmp_path):
    _write_skill(tmp_path, "minha-skill", "name: minha-skill\ndescription: Faz algo útil.\nkeywords: [util]")
    lib = _lib(tmp_path)
    entry = lib.get("minha-skill")
    assert entry is not None
    assert entry.description == "Faz algo útil."
    assert entry.kind == "instructional"
    assert [e.name for e in lib.all()] == ["minha-skill"]


def test_load_skips_invalid_and_keeps_valid(tmp_path):
    _write_skill(tmp_path, "boa", "name: boa\ndescription: Ok.")
    _write_skill(tmp_path, "sem-descricao", "name: sem-descricao")
    _write_skill(tmp_path, "yaml-quebrado", "name: [unclosed\ndescription broken")
    (tmp_path / "sem-md").mkdir()
    _write_skill(tmp_path, "_TEMPLATE", "name: nome-da-skill\ndescription: Modelo.")
    _write_skill(tmp_path, "kind-invalido", "name: kind-invalido\ndescription: X.\nkind: banana")

    lib = _lib(tmp_path)
    assert [e.name for e in lib.all()] == ["boa"]


def test_load_missing_root_is_empty(tmp_path):
    lib = _lib(tmp_path / "nao-existe")
    assert lib.all() == []
    assert lib.catalog_note("oi") is None


def test_frontmatter_name_divergent_uses_folder(tmp_path):
    _write_skill(tmp_path, "pasta-manda", "name: outro-nome\ndescription: X.")
    lib = _lib(tmp_path)
    assert lib.get("pasta-manda") is not None
    assert lib.get("outro-nome") is None


def test_description_truncated_at_200(tmp_path):
    _write_skill(tmp_path, "longa", f"name: longa\ndescription: {'x' * 400}")
    lib = _lib(tmp_path)
    assert len(lib.get("longa").description) <= 200


def test_disabled_skill_out_of_catalog_and_read(tmp_path):
    _write_skill(tmp_path, "off", "name: off\ndescription: X.\nenabled: false")
    lib = _lib(tmp_path)
    assert lib.all() == []
    with pytest.raises(ValueError):
        lib.read("off")


def test_scripted_requires_workbench(tmp_path):
    _write_skill(tmp_path, "com-script", "name: com-script\ndescription: X.\nkind: scripted")
    reg = _Registry({"neo_bash"})
    lib = _lib(tmp_path, registry=reg)
    assert lib.get("com-script").requires == ["workbench"]
    assert [e.name for e in lib.all()] == ["com-script"]

    # workbench off → fora do catálogo (require insatisfeito)
    lib_off = _lib(tmp_path, settings=_settings(workbench_enabled=False), registry=reg)
    assert lib_off.all() == []


# ── hot reload ───────────────────────────────────────────────────────────────

def test_maybe_reload_picks_up_new_skill(tmp_path):
    _write_skill(tmp_path, "primeira", "name: primeira\ndescription: X.")
    lib = _lib(tmp_path)
    assert len(lib.all()) == 1
    _write_skill(tmp_path, "segunda", "name: segunda\ndescription: Y.")
    lib.maybe_reload()
    assert {e.name for e in lib.all()} == {"primeira", "segunda"}


# ── catálogo ─────────────────────────────────────────────────────────────────

def test_catalog_note_lists_skills(tmp_path):
    _write_skill(tmp_path, "alfa", "name: alfa\ndescription: Faz alfa.")
    _write_skill(tmp_path, "beta", "name: beta\ndescription: Faz beta.")
    note = _lib(tmp_path).catalog_note("qualquer coisa")
    assert "- alfa — Faz alfa." in note
    assert "- beta — Faz beta." in note
    assert "skill_read" in note
    assert "PARCIAL" not in note


def test_catalog_note_prefilters_over_budget(tmp_path):
    for i in range(4):
        _write_skill(tmp_path, f"skill-{i}", f"name: skill-{i}\ndescription: D{i}.\nkeywords: [kw{i}]")
    lib = _lib(tmp_path, settings=_settings(skills_catalog_max_entries=2))
    note = lib.catalog_note("preciso de kw3 agora")
    assert "PARCIAL" in note and "skill_list" in note
    assert "skill-3" in note  # keyword match vence
    assert note.count("\n- ") + note.count("- skill") >= 2  # 2 entradas listadas


# ── read ─────────────────────────────────────────────────────────────────────

def test_read_body_strips_frontmatter(tmp_path):
    _write_skill(tmp_path, "corpo", "name: corpo\ndescription: X.", body="# Título\n\npasso 1")
    data = _lib(tmp_path).read("corpo")
    assert data["content"].startswith("# Título")
    assert "description" not in data["content"]


def test_read_unknown_skill_lists_available(tmp_path):
    _write_skill(tmp_path, "unica", "name: unica\ndescription: X.")
    with pytest.raises(ValueError, match="unica"):
        _lib(tmp_path).read("inexistente")


def test_read_reference_file(tmp_path):
    d = _write_skill(tmp_path, "com-ref", "name: com-ref\ndescription: X.")
    (d / "references").mkdir()
    (d / "references" / "schema.md").write_text("tabela X", encoding="utf-8")
    lib = _lib(tmp_path)
    body = lib.read("com-ref")
    assert body["references"] == ["references/schema.md"]
    ref = lib.read("com-ref", file="references/schema.md")
    assert ref["content"] == "tabela X"


@pytest.mark.parametrize("evil", ["../outra/SKILL.md", "/etc/passwd", "~/x", "a\\b"])
def test_read_blocks_traversal(tmp_path, evil):
    _write_skill(tmp_path, "segura", "name: segura\ndescription: X.")
    with pytest.raises(ValueError):
        _lib(tmp_path).read("segura", file=evil)


def test_read_caps_bytes(tmp_path):
    _write_skill(tmp_path, "grande", "name: grande\ndescription: X.", body="x" * 50_000)
    lib = _lib(tmp_path, settings=_settings(skill_read_max_bytes=2_048))
    data = lib.read("grande")
    assert "[truncado" in data["content"]
    assert len(data["content"].encode()) < 3_000


def test_read_scripts_listed_with_sandbox_paths(tmp_path):
    d = _write_skill(tmp_path, "roda-script", "name: roda-script\ndescription: X.\nkind: scripted")
    (d / "scripts").mkdir()
    (d / "scripts" / "go.py").write_text("print('oi')", encoding="utf-8")
    lib = _lib(tmp_path, registry=_Registry({"neo_bash"}))
    data = lib.read("roda-script")
    assert data["scripts"] == ["/skills/roda-script/scripts/go.py"]


# ── tool_backed / tools_for ──────────────────────────────────────────────────

def test_tools_for_only_registered(tmp_path):
    _write_skill(
        tmp_path, "com-tools",
        "name: com-tools\ndescription: X.\nkind: tool_backed\ntools: [sql_query_ro, fantasma]",
    )
    lib = _lib(tmp_path, registry=_Registry({"sql_query_ro"}))
    assert [t.name for t in lib.tools_for("com-tools")] == ["sql_query_ro"]
    data = lib.read("com-tools")
    assert data["tools_unlocked"] == ["sql_query_ro"]
    assert data["tools_unavailable"] == ["fantasma"]


def test_tools_for_instructional_is_empty(tmp_path):
    _write_skill(tmp_path, "pura", "name: pura\ndescription: X.")
    assert _lib(tmp_path, registry=_Registry()).tools_for("pura") == []
