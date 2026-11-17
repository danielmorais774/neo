"""F4 — o SkillManifest/SkillLibrary como fonte única (docs/skills-library-plan.md).

Cobre: handoff parser dinâmico (manifests novos viram alvos de [[HANDOFF]]),
_build_handoff_instruction gerada do registry, factory do SkillExecutor via
pipeline_factory, e a nota do router vinda da SkillLibrary.
"""

import sys
import types
from types import SimpleNamespace
from uuid import uuid4

import pytest

import shared.config
from core.pipeline.handoff_parser import extract_handoffs
from core.skills.library import SkillLibrary
from core.skills.manifest import SkillManifest
from core.skills.skill_executor import SkillExecutor
from shared.types import IntentType

# Importa os 3 pacotes embutidos para popular o registry (como o api/main faz).
import core.skills.home_assistant  # noqa: F401
import core.skills.music  # noqa: F401
import core.skills.whatsapp_messaging  # noqa: F401


@pytest.fixture
def fake_manifest():
    """Registra um manifest temporário e remove no teardown."""
    manifest = SkillManifest(
        name="fake_skill",
        description="Skill de teste.",
        intent_type=IntentType.MUSIC,
        handoff_line="skill de teste (não use).",
        pipeline_factory="_fake_pipe_mod:DummyPipeline",
    )
    manifest.register()
    yield manifest
    SkillManifest._registry.pop("fake_skill", None)


# ── handoff parser dinâmico ──────────────────────────────────────────────────

def test_handoff_accepts_builtin_skills():
    text = "ok!\n[[HANDOFF:home_assistant]]ligar o ar do quarto[[/HANDOFF]]"
    clean, handoffs = extract_handoffs(text)
    assert clean == "ok!"
    assert handoffs[0].target_intent == IntentType.HOME_ASSISTANT


def test_handoff_accepts_manifest_registered_skill(fake_manifest):
    _, handoffs = extract_handoffs("[[HANDOFF:fake_skill]]fazer algo[[/HANDOFF]]")
    assert len(handoffs) == 1
    assert handoffs[0].target_skill == "fake_skill"
    assert handoffs[0].target_intent == IntentType.MUSIC


def test_handoff_unknown_skill_still_dropped():
    clean, handoffs = extract_handoffs("[[HANDOFF:invencao]]x[[/HANDOFF]]")
    assert handoffs == [] and clean == ""


# ── instrução de handoff gerada ──────────────────────────────────────────────

def test_handoff_instruction_built_from_manifests():
    from modules.desktop.ai_service import _build_handoff_instruction

    text = _build_handoff_instruction()
    assert "- `home_assistant`: controlar a casa" in text
    assert "- `whatsapp_messaging`: enviar mensagem de WhatsApp" in text
    assert "- `music`: tocar música no Spotify/YouTube" in text
    assert text.startswith("## Acionar uma skill (handoff)")
    assert "Regras:" in text


def test_handoff_instruction_includes_new_manifest(fake_manifest):
    from modules.desktop.ai_service import _build_handoff_instruction

    assert "- `fake_skill`: skill de teste (não use)." in _build_handoff_instruction()


# ── factory do SkillExecutor via pipeline_factory ────────────────────────────

class _DummyPipeline:
    def __init__(self, agent_executor, db, broker):  # sem pks — deve ser filtrado
        self.kwargs = dict(agent_executor=agent_executor, db=db, broker=broker)


def _executor():
    ex = object.__new__(SkillExecutor)
    ex.agent_executor = "AGENT"
    ex.db = "DB"
    ex.broker = "BROKER"
    ex.pks = "PKS"
    return ex


def test_create_pipeline_via_manifest(fake_manifest):
    mod = types.ModuleType("_fake_pipe_mod")
    mod.DummyPipeline = _DummyPipeline
    sys.modules["_fake_pipe_mod"] = mod
    try:
        pipeline = _executor()._create_pipeline("fake_skill")
        assert isinstance(pipeline, _DummyPipeline)
        # pks não está na assinatura → não injetado
        assert pipeline.kwargs == {"agent_executor": "AGENT", "db": "DB", "broker": "BROKER"}
    finally:
        sys.modules.pop("_fake_pipe_mod", None)


def test_create_pipeline_whatsapp_alias_resolves():
    ex = _executor()
    # whatsapp_order → whatsapp_messaging (mesmo manifest/pipeline); só até o
    # import do módulo real (pesado), que deve existir.
    manifest = SkillManifest.get("whatsapp_messaging")
    assert manifest.pipeline_factory.endswith(":WhatsAppMessagingPipeline")
    pipeline = ex._create_pipeline("whatsapp_order")
    assert type(pipeline).__name__ == "WhatsAppMessagingPipeline"


def test_create_pipeline_unknown_raises():
    with pytest.raises(KeyError):
        _executor()._create_pipeline("nao-existe")


# ── nota do router ───────────────────────────────────────────────────────────

def _library_with_skill(tmp_path):
    d = tmp_path / "postgres-neo"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: postgres-neo\ndescription: Consulta o banco.\n"
        "keywords: [banco, sql, consulta, dados, extra-nao-listada]\n---\ncorpo",
        encoding="utf-8",
    )
    settings = SimpleNamespace(
        workbench_enabled=True, skills_library_enabled=True,
        skill_read_max_bytes=16_384, skills_catalog_max_entries=30,
        skills_router_note_enabled=True,
    )
    lib = SkillLibrary(root=tmp_path, settings=settings)
    lib.load()
    return lib


def test_router_note_lists_names_and_keywords(tmp_path):
    note = _library_with_skill(tmp_path).router_note()
    assert "DIRECT_RESPONSE" in note
    assert "postgres-neo (banco, sql, consulta, dados)" in note  # cap 4 keywords
    assert "extra-nao-listada" not in note


def test_router_brain_note_respects_flag(tmp_path, monkeypatch):
    from core.router.intent_router import IntentRouter

    router = object.__new__(IntentRouter)
    router.skill_library = _library_with_skill(tmp_path)

    monkeypatch.setattr(
        shared.config, "get_settings",
        lambda: SimpleNamespace(skills_router_note_enabled=True),
    )
    assert "postgres-neo" in router._skills_brain_note()

    monkeypatch.setattr(
        shared.config, "get_settings",
        lambda: SimpleNamespace(skills_router_note_enabled=False),
    )
    assert router._skills_brain_note() is None


def test_router_brain_note_none_without_library():
    from core.router.intent_router import IntentRouter

    router = object.__new__(IntentRouter)
    router.skill_library = None
    assert router._skills_brain_note() is None


# ── grupo: a nota do router não pode diluir as skills que o compõem ──────────
#
# Sessão b54c4f36 (2026-09-12): o grupo `cadastros` (clientes + playlists +
# produtos, 25 keywords) chegava ao classificador como
# `cadastros (anexa na playlist, camiseta, caneca, catalogo)` — resultado de um
# `sorted(<união>)[:4]`, ou seja ordem ALFABÉTICA. Três das quatro palavras eram
# de caneca/camiseta e nenhuma de música, então "como tá a minha fila?" não se
# parecia com nenhum assunto de skill e o classificador mandava para
# `list_connected_devices`. A skill `playlists` — que tem um dataset `fila` —
# nunca era alcançada.

def _library_with_group(tmp_path):
    skills = {
        "clientes": "[cliente, clientes, comprador, contato]",
        "playlists": "[playlist, fila de reproducao, playlists, minha fila]",
        "produtos": "[produto, produtos, caneca, camiseta, catalogo]",
    }
    for nome, kws in skills.items():
        d = tmp_path / nome
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: {nome}\ndescription: Cadastro de {nome}.\n"
            f"keywords: {kws}\ngroup: cadastros\n---\ncorpo",
            encoding="utf-8",
        )
    settings = SimpleNamespace(
        workbench_enabled=True, skills_library_enabled=True,
        skill_read_max_bytes=16_384, skills_catalog_max_entries=30,
        skills_router_note_enabled=True,
    )
    lib = SkillLibrary(root=tmp_path, settings=settings)
    lib.load()
    return lib


def test_router_note_grupo_da_voz_a_todas_as_skills(tmp_path):
    """Toda skill do grupo contribui antes de qualquer uma contribuir 2x."""
    note = _library_with_group(tmp_path).router_note()
    assert "cliente" in note
    assert "playlist" in note
    assert "produto" in note


def test_router_note_grupo_nao_ordena_por_alfabeto(tmp_path):
    """O defeito original: 'caneca'/'camiseta' venciam 'playlist' por serem antes no alfabeto."""
    note = _library_with_group(tmp_path).router_note()
    assert "fila de reproducao" in note, "a palavra que o usuário usa tem que chegar ao classificador"
    assert "caneca" not in note and "camiseta" not in note, (
        "keyword do fim da lista do autor não pode roubar a vaga da primeira de outra skill"
    )


def test_router_note_grupo_cita_os_nomes_das_skills(tmp_path):
    """Sem isso a palavra 'playlists' não existia no prompt — o grupo a substituía."""
    note = _library_with_group(tmp_path).router_note()
    assert "playlists" in note and "clientes" in note and "produtos" in note


def test_router_note_grupo_respeita_o_teto(tmp_path):
    lib = _library_with_group(tmp_path)
    kws = lib._round_robin_keywords(list(lib.all()), lib._ROUTER_NOTE_MAX_GROUP_KEYWORDS)
    assert len(kws) == lib._ROUTER_NOTE_MAX_GROUP_KEYWORDS
    assert len(kws) == len(set(kws)), "sem repetir keyword"


def test_round_robin_alterna_entre_as_skills(tmp_path):
    """Primeira de cada uma, depois a segunda de cada uma — ordem de autoria."""
    lib = _library_with_group(tmp_path)
    ordem = [e.name for e in lib.all()]
    kws = lib._round_robin_keywords(list(lib.all()), 3)
    esperado = [lib.get(n).keywords[0] for n in ordem][:3]
    assert kws == esperado
