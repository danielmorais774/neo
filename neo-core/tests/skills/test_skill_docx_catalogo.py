"""A skill docx entra no catálogo e o pedido real do turno 1 da session-20cb02b6 a aciona.

Separado dos testes dos scripts porque não precisa de python-docx: roda na suíte de
todo dia.
"""

from __future__ import annotations

from pathlib import Path

def test_skill_docx_carrega_e_o_pedido_do_turno_1_aciona_ela():
    from types import SimpleNamespace

    from core.skills.library import SkillLibrary

    raiz = Path(__file__).resolve().parents[2] / "skills"
    lib = SkillLibrary(
        root=raiz,
        settings=SimpleNamespace(workbench_enabled=True, skills_library_enabled=True,
                                 skill_read_max_bytes=16_384, skills_catalog_max_entries=30),
        tool_registry=SimpleNamespace(exists=lambda nome: True, get=lambda nome: SimpleNamespace(name=nome)),
    )
    lib.load()
    assert not [e for e in lib.errors() if "docx" in str(e)]
    entrada = lib.get("docx")
    assert entrada is not None and entrada.kind == "scripted" and "workbench" in entrada.requires
    nota = lib.catalog_note("coloca o meu R.A e o meu nome no cabeçalho desse arquivo e me gera outro arquivo")
    assert nota and "docx" in nota


def test_formulario_do_google_nao_aciona_a_docx():
    """Sessão d9fae447: "Preenche esse formulário no meu navegador local" ganhou o
    aviso "⚠️ Esta mensagem casa com `docx` (casou: formulario)", que manda ler a
    skill e executá-la antes de responder. O pedido do MAPA (20cb02b6) nunca disse
    "formulário"; quem diz é o Google Forms."""
    from types import SimpleNamespace

    from core.skills.library import SkillLibrary

    raiz = Path(__file__).resolve().parents[2] / "skills"
    lib = SkillLibrary(
        root=raiz,
        settings=SimpleNamespace(workbench_enabled=True, skills_library_enabled=True,
                                 skill_read_max_bytes=16_384, skills_catalog_max_entries=30),
        tool_registry=SimpleNamespace(exists=lambda nome: True, get=lambda nome: SimpleNamespace(name=nome)),
    )
    lib.load()
    nota = lib.catalog_note(
        "Preenche esse formulário no meu navegador local e visual, quero ver acontecendo: "
        "https://docs.google.com/forms/d/e/x/viewform"
    ) or ""
    assert "casa com `docx`" not in nota
