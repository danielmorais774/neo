"""Escrita de skills pelo painel — SkillLibrary.write_* e api/routes/skills.py.

A regra que estes testes protegem: o painel NUNCA grava uma skill que o loader
recusaria no boot (validação na entrada), e nunca escreve fora da pasta da
skill (allowlist de subpasta + extensão + prefix check).
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import skills as skills_route
from core.skills.library import SkillLibrary


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
    """ToolRegistry fake — só o que a library/rota usam."""

    def __init__(self, names=("sql_query_ro", "neo_bash")):
        self._names = set(names)

    def exists(self, name):
        return name in self._names

    def get(self, name):
        return SimpleNamespace(name=name)

    def list_names(self):
        return list(self._names)


@pytest.fixture
def lib(tmp_path):
    library = SkillLibrary(root=tmp_path, settings=_settings(), tool_registry=_Registry())
    library.load()
    return library


@pytest.fixture
def client(lib):
    app = FastAPI()
    app.include_router(skills_route.router, prefix="/api/v1")
    skills_route.set_dependencies(lib, _Registry())
    with TestClient(app) as c:
        yield c
    skills_route.set_dependencies(None, None)


# ── write_skill: caminho feliz ───────────────────────────────────────────────

def test_write_skill_creates_and_loads(lib, tmp_path):
    entry = lib.write_skill(
        "minha-skill",
        description="Faz algo útil.",
        body="# Título\n\npasso 1",
        keywords=["util", "teste"],
    )
    assert entry.name == "minha-skill"
    assert (tmp_path / "minha-skill" / "SKILL.md").is_file()
    # Carregada de verdade: já está no catálogo do cérebro.
    assert "minha-skill" in lib.catalog_note("qualquer coisa")
    assert lib.read("minha-skill")["content"].startswith("# Título")


def test_write_skill_roundtrips_through_loader(lib):
    lib.write_skill(
        "roundtrip",
        description="Descrição da skill.",
        body="corpo",
        kind="tool_backed",
        tools=["sql_query_ro"],
        keywords=["a", "b"],
        version=3,
    )
    src = lib.read_source("roundtrip")
    assert src["parse_error"] is None
    assert src["meta"]["keywords"] == ["a", "b"]
    assert src["meta"]["tools"] == ["sql_query_ro"]
    assert src["meta"]["version"] == 3
    assert src["body"] == "corpo"
    assert lib.get("roundtrip").kind == "tool_backed"


def test_write_skill_scripted_derives_workbench_require(lib):
    entry = lib.write_skill("com-script", description="X.", body="corpo", kind="scripted")
    assert entry.requires == ["workbench"]
    assert lib.read_source("com-script")["meta"]["requires"] == ["workbench"]


def test_write_skill_preserves_unknown_frontmatter_keys(lib, tmp_path):
    d = tmp_path / "manual"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: manual\ndescription: X.\nautor: daniel\n---\n\ncorpo", encoding="utf-8"
    )
    lib.load()
    lib.write_skill("manual", description="Nova descrição.", body="novo corpo")
    meta = lib.read_source("manual")["meta"]
    assert meta["autor"] == "daniel"  # chave escrita à mão sobrevive à edição
    assert meta["description"] == "Nova descrição."


def test_write_skill_overwrite_updates_catalog(lib):
    lib.write_skill("edita", description="Antiga.", body="corpo")
    lib.write_skill("edita", description="Nova.", body="corpo")
    assert lib.get("edita").description == "Nova."


# ── write_skill: validação (o painel não cria skill que o boot recusaria) ────

@pytest.mark.parametrize("bad", ["Maiúscula", "com espaço", "-comeca-com-hifen", "", "a" * 41, "../fuga"])
def test_write_skill_rejects_bad_name(lib, bad):
    with pytest.raises(ValueError):
        lib.write_skill(bad, description="X.", body="corpo")


def test_write_skill_rejects_empty_description(lib):
    with pytest.raises(ValueError, match="descrição"):
        lib.write_skill("sem-desc", description="   ", body="corpo")


def test_write_skill_rejects_long_description(lib):
    with pytest.raises(ValueError, match="200"):
        lib.write_skill("longa", description="x" * 201, body="corpo")


def test_write_skill_rejects_empty_body(lib):
    with pytest.raises(ValueError, match="corpo"):
        lib.write_skill("sem-corpo", description="X.", body="  \n ")


def test_write_skill_rejects_invalid_kind(lib):
    with pytest.raises(ValueError, match="kind"):
        lib.write_skill("banana", description="X.", body="corpo", kind="pipeline")


def test_write_skill_rejects_unregistered_tool(lib):
    with pytest.raises(ValueError, match="não registradas"):
        lib.write_skill(
            "fantasma", description="X.", body="corpo", kind="tool_backed", tools=["nao_existe"]
        )


def test_write_skill_rejects_tool_backed_without_tools(lib):
    with pytest.raises(ValueError, match="ao menos uma tool"):
        lib.write_skill("vazia", description="X.", body="corpo", kind="tool_backed")


def test_write_skill_rejects_tools_on_instructional(lib):
    with pytest.raises(ValueError, match="tool_backed"):
        lib.write_skill("confusa", description="X.", body="corpo", tools=["sql_query_ro"])


def test_failed_write_leaves_no_partial_skill(lib, tmp_path):
    with pytest.raises(ValueError):
        lib.write_skill("nasce-morta", description="", body="corpo")
    assert not (tmp_path / "nasce-morta").exists()


# ── enabled / delete ─────────────────────────────────────────────────────────

def test_set_enabled_toggles_catalog(lib):
    lib.write_skill("liga-desliga", description="X.", body="corpo")
    lib.set_enabled("liga-desliga", False)
    assert lib.all() == []
    assert lib.get("liga-desliga").enabled is False
    lib.set_enabled("liga-desliga", True)
    assert [e.name for e in lib.all()] == ["liga-desliga"]


def test_set_enabled_preserves_body(lib):
    lib.write_skill("corpo-vivo", description="X.", body="# Passos\n\n1. faça")
    lib.set_enabled("corpo-vivo", False)
    assert "1. faça" in lib.read_source("corpo-vivo")["body"]


def test_set_enabled_refuses_broken_frontmatter(lib, tmp_path):
    d = tmp_path / "quebrada"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: [unclosed\n---\n\ncorpo", encoding="utf-8")
    lib.load()
    with pytest.raises(ValueError, match="quebrado"):
        lib.set_enabled("quebrada", False)
    # e não destruiu o arquivo — o operador ainda consegue consertar
    assert "unclosed" in (d / "SKILL.md").read_text(encoding="utf-8")


def test_delete_skill_removes_dir(lib, tmp_path):
    lib.write_skill("efemera", description="X.", body="corpo")
    lib.delete_skill("efemera")
    assert not (tmp_path / "efemera").exists()
    assert lib.get("efemera") is None


def test_delete_skill_refuses_non_skill_dir(lib, tmp_path):
    (tmp_path / "nao-skill").mkdir()
    with pytest.raises(ValueError, match="não existe"):
        lib.delete_skill("nao-skill")
    assert (tmp_path / "nao-skill").exists()


# ── read_source de skill quebrada (senão só se conserta por SSH) ─────────────

def test_read_source_of_broken_yaml_still_returns_body(lib, tmp_path):
    d = tmp_path / "quebrada"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: [unclosed\n---\n\ncorpo salvo", encoding="utf-8")
    lib.load()
    src = lib.read_source("quebrada")
    assert src["parse_error"] and src["loaded"] is False
    assert src["body"] == "corpo salvo"


def test_load_records_errors(lib, tmp_path):
    (tmp_path / "sem-md").mkdir()
    d = tmp_path / "sem-desc"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: sem-desc\n---\n\ncorpo", encoding="utf-8")
    lib.load()
    problemas = {e["skill"]: e["error"] for e in lib.errors()}
    assert "sem-md" in problemas and "sem-desc" in problemas
    assert "description" in problemas["sem-desc"]


# ── arquivos (references/ e scripts/) ────────────────────────────────────────

def test_write_and_read_file(lib):
    lib.write_skill("com-ref", description="X.", body="corpo")
    lib.write_file("com-ref", "references/schema.md", "# tabelas")
    assert lib.read_file("com-ref", "references/schema.md") == "# tabelas"
    files = lib.list_files("com-ref")
    assert files[0]["path"] == "references/schema.md"
    assert files[0]["sandbox_path"] is None


def test_script_file_reports_sandbox_path(lib):
    lib.write_skill("com-script", description="X.", body="corpo", kind="scripted")
    lib.write_file("com-script", "scripts/plot.py", "print('oi')")
    script = lib.list_files("com-script")[0]
    # o caminho que o modelo usa no neo_bash é o do mount, não o do host
    assert script["sandbox_path"] == "/skills/com-script/scripts/plot.py"


def test_delete_file(lib):
    lib.write_skill("com-ref", description="X.", body="corpo")
    lib.write_file("com-ref", "references/x.md", "a")
    lib.delete_file("com-ref", "references/x.md")
    assert lib.list_files("com-ref") == []


@pytest.mark.parametrize(
    "evil",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "references/../../fuga.md",
        "references/sub/nested.md",
        "outra-pasta/x.md",
        "SKILL.md",
        "references/binario.exe",
        "scripts/sem-extensao",
        "~/x.md",
        "scripts\\win.py",
    ],
)
def test_write_file_blocks_bad_paths(lib, evil):
    lib.write_skill("segura", description="X.", body="corpo")
    with pytest.raises(ValueError):
        lib.write_file("segura", evil, "conteudo")


def test_write_file_requires_existing_skill(lib):
    with pytest.raises(ValueError, match="não existe"):
        lib.write_file("fantasma", "references/x.md", "a")


# ── rotas HTTP ───────────────────────────────────────────────────────────────

def test_api_list_shape(client, lib):
    lib.write_skill("alfa", description="Faz alfa.", body="corpo")
    data = client.get("/api/v1/skills").json()
    assert [s["name"] for s in data["skills"]] == ["alfa"]
    assert data["skills"][0]["editable"] is True
    assert isinstance(data["pipelines"], list)
    assert data["errors"] == []


def test_api_lists_disabled_skill_with_reason(client, lib):
    lib.write_skill("off", description="X.", body="corpo", enabled=False)
    skill = client.get("/api/v1/skills").json()["skills"][0]
    assert skill["available"] is False
    assert skill["unavailable_reason"] == "desligada"


def test_api_create_then_conflict(client):
    payload = {"name": "nova", "description": "Faz algo.", "body": "corpo"}
    assert client.post("/api/v1/skills", json=payload).status_code == 201
    assert client.post("/api/v1/skills", json=payload).status_code == 409


def test_api_create_invalid_is_400_with_reason(client):
    r = client.post("/api/v1/skills", json={"name": "x y", "description": "d", "body": "c"})
    assert r.status_code == 400
    assert "Nome inválido" in r.json()["detail"]


def test_api_update_unknown_is_404(client):
    r = client.put("/api/v1/skills/fantasma", json={"description": "d", "body": "c"})
    assert r.status_code == 404


def test_api_toggle_enabled(client, lib):
    lib.write_skill("liga", description="X.", body="corpo")
    r = client.patch("/api/v1/skills/liga/enabled", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert lib.all() == []


def test_api_delete(client, lib):
    lib.write_skill("some", description="X.", body="corpo")
    assert client.delete("/api/v1/skills/some").status_code == 200
    assert lib.get("some") is None


def test_api_tools_route_not_shadowed_by_name(client):
    """GET /skills/tools é a lista de tools, não a skill chamada 'tools'."""
    data = client.get("/api/v1/skills/tools").json()
    assert "sql_query_ro" in data["tools"]


def test_api_catalog_preview(client, lib):
    lib.write_skill("alfa", description="Faz alfa.", body="corpo")
    data = client.get("/api/v1/skills/catalog").json()
    assert "alfa" in data["catalog_note"]
    assert data["estimated_tokens"] > 0


def test_api_file_write_read_delete(client, lib):
    lib.write_skill("arquivos", description="X.", body="corpo")
    url = "/api/v1/skills/arquivos/files/references/schema.md"
    assert client.put(url, json={"content": "# tabelas"}).status_code == 200
    assert client.get(url).json()["content"] == "# tabelas"
    assert client.delete(url).status_code == 200
    assert client.get(url).status_code == 404


@pytest.mark.parametrize("evil", ["outra-pasta/x.md", "references/binario.exe", "SKILL.md"])
def test_api_file_write_outside_allowlist_is_400(client, lib, evil):
    """Traversal com '..' o cliente HTTP já normaliza; o que chega ao servidor é isto."""
    lib.write_skill("segura", description="X.", body="corpo")
    r = client.put(f"/api/v1/skills/segura/files/{evil}", json={"content": "x"})
    assert r.status_code == 400


def test_api_503_when_library_disabled():
    app = FastAPI()
    app.include_router(skills_route.router, prefix="/api/v1")
    skills_route.set_dependencies(None, None)
    with TestClient(app) as c:
        r = c.get("/api/v1/skills")
    assert r.status_code == 503
    assert "desligada" in r.json()["detail"]
