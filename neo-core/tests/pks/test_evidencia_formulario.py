"""A evidência que o respondedor de formulário recebe: por pergunta, e redigida.

Ver ``core/browser_local/respondedor.py`` e a sessão e752a399 (2026-11-01). A
busca é uma por pergunta, com o rótulo literal; o dono vem à parte, porque é
ele quem responde "seu email" e "a academia que você frequenta" — e vem com os
campos redigidos, porque o respondedor é um LLM e não pode ver CPF nem e-mail.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.pks.ranking import RankConfig, rank_pks
from core.pks.schemas import EntityRecord, FieldRecord, RelationRecord
from core.pks.service import PKSService

WS = uuid4()


def _campo(entity_id, key, value, sensitivity="private"):
    return FieldRecord(
        id=uuid4(), entity_id=entity_id, key=key, value_type="text", value=value,
        sensitivity=sensitivity,
    )


class _Repo:
    def __init__(self, dono, campos, relacoes):
        self.dono, self.campos, self.relacoes = dono, campos, relacoes

    async def get_primary_user_entity(self, session, workspace_id):
        return self.dono

    async def get_fields_for_entities(self, session, ids):
        return {self.dono.id: self.campos} if self.dono else {}

    async def get_relations(self, session, entity_id):
        return self.relacoes


def _servico(repo, rank=None):
    svc = PKSService.__new__(PKSService)

    async def ensure_workspace(user_id):
        return WS

    @asynccontextmanager
    async def get_session():
        yield None

    svc.bootstrap = SimpleNamespace(ensure_workspace=ensure_workspace)
    svc.db = SimpleNamespace(get_session=get_session)
    svc.repository = repo
    svc.retriever = SimpleNamespace(rank=rank)
    return svc


def _dono():
    dono = EntityRecord(id=uuid4(), workspace_id=WS, name="Daniel Morais", type="person")
    campos = [
        _campo(dono.id, "email", "daniel@example.com"),
        _campo(dono.id, "cpf", "52998224725", "sensitive"),
        _campo(dono.id, "full_name", "Daniel Morais"),
        _campo(dono.id, "senha_wifi", "hunter2"),  # gravado "private", mas é segredo
        _campo(dono.id, "neo_affect", '{"joy": 0.4}'),
    ]
    academia = uuid4()
    relacoes = [
        RelationRecord(id=uuid4(), workspace_id=WS, from_entity_id=dono.id, relation_type="belongs_to",
                       to_entity_id=academia, to_name="Academia Exemplo", to_type="place"),
        RelationRecord(id=uuid4(), workspace_id=WS, from_entity_id=dono.id, relation_type="owns",
                       to_entity_id=uuid4(), to_name="Cofre", to_type="document", sensitivity="sensitive"),
        RelationRecord(id=uuid4(), workspace_id=WS, from_entity_id=uuid4(), relation_type="related_to",
                       to_entity_id=dono.id, from_name="Objetivo do treino de Daniel", from_type="goal"),
    ]
    return dono, campos, relacoes


@pytest.mark.asyncio
async def test_bloco_do_dono_redige_e_mostra_as_relacoes():
    dono, campos, relacoes = _dono()
    texto = await _servico(_Repo(dono, campos, relacoes))._bloco_do_dono(uuid4())

    assert texto.startswith("### Daniel Morais [person]")
    assert "- email: «pks:email»" in texto
    assert "- cpf: «pks:cpf»" in texto
    assert "daniel@example.com" not in texto and "52998224725" not in texto
    assert "- full_name: Daniel Morais" in texto  # nome não é redigido (exceção do PKS)
    assert "hunter2" not in texto and "senha_wifi" not in texto
    assert "neo_affect" not in texto
    assert "- belongs_to -> Academia Exemplo [place]" in texto
    assert "- related_to <- Objetivo do treino de Daniel [goal]" in texto
    assert "Cofre" not in texto  # relação sensível fica de fora


@pytest.mark.asyncio
async def test_sem_dono_o_bloco_e_vazio():
    assert await _servico(_Repo(None, [], []))._bloco_do_dono(uuid4()) == ""


@pytest.mark.asyncio
async def test_evidencia_e_uma_busca_por_pergunta_com_o_rotulo_literal():
    dono, campos, relacoes = _dono()
    perguntas_vistas: list[tuple[str, int, float, bool]] = []
    saude = EntityRecord(id=uuid4(), workspace_id=WS, name="Lesão no ombro", type="health_state")

    def _grupo(nome, tipo="workout", entidade=None):
        e = entidade or EntityRecord(id=uuid4(), workspace_id=WS, name=nome, type=tipo)
        return SimpleNamespace(head=SimpleNamespace(entity=e), text=f"### {e.name} [{e.type}]")

    async def rank(*, user_id, user_message, budget_tokens, relative_cutoff, touch):
        perguntas_vistas.append((user_message, budget_tokens, relative_cutoff, touch))
        grupos = [_grupo("Lesão no ombro", entidade=saude)]
        if "segunda" in user_message:
            grupos.append(_grupo("Treino de segunda-feira"))
        return WS, SimpleNamespace(groups=grupos), [], {}, []

    svc = _servico(_Repo(dono, campos, relacoes), rank)
    ev = await svc.evidencia_de_formulario(
        user_id=uuid4(), perguntas=["Treino de segunda-feira com repetições", "seu email"]
    )

    assert [p for p, *_ in perguntas_vistas] == ["Treino de segunda-feira com repetições", "seu email"]
    # Não toca no PKS (é leitura de formulário, não uso) e com orçamento de pergunta.
    assert all(touch is False for *_, touch in perguntas_vistas)
    assert [t for _, t in ev["perguntas"][0]] == ["### Treino de segunda-feira [workout]"]
    assert ev["perguntas"][1] == []
    # Saúde vem sempre no bloco do turno: aqui ela vai com o dono, UMA vez.
    assert ev["dono"].count("### Lesão no ombro [health_state]") == 1
    assert ev["dono"].startswith("### Daniel Morais [person]")


@pytest.mark.asyncio
async def test_pergunta_cuja_busca_falha_fica_sem_evidencia_e_as_outras_seguem():
    dono, campos, relacoes = _dono()

    async def rank(*, user_message, **_):
        if "quebra" in user_message:
            raise RuntimeError("banco caiu")
        return WS, SimpleNamespace(groups=[]), [], {}, []

    svc = _servico(_Repo(dono, campos, relacoes), rank)
    ev = await svc.evidencia_de_formulario(user_id=uuid4(), perguntas=["quebra", "ok"])
    assert ev["perguntas"] == [[], []]


# ── o bloco do ranking v2: mesma redação do bloco antigo, e com os aliases ───


def test_bloco_v2_redige_campo_sensivel_pela_chave_e_mostra_os_aliases():
    agora = datetime(2026, 11, 1, tzinfo=timezone.utc)
    pessoa = EntityRecord(id=uuid4(), workspace_id=WS, name="Marcos Personal", type="person", updated_at=agora)
    objetivo = EntityRecord(
        id=uuid4(), workspace_id=WS, name="Objetivo do treino de Daniel", type="goal",
        aliases=["hipertrofia e força", "força", "Objetivo do treino de Daniel"], updated_at=agora,
    )
    campos = {
        pessoa.id: [
            _campo(pessoa.id, "telefone", "61999990000"),  # gravado "private": a chave manda
            _campo(pessoa.id, "senha", "abc123"),
            _campo(pessoa.id, "especialidade", "musculação"),
        ],
        objetivo.id: [],
    }
    out = rank_pks(
        entities=[pessoa, objetivo], fields_by_entity=campos, relations=[],
        message="marcos personal telefone e o objetivo do treino", config=RankConfig(), now=agora,
    )
    texto = out.text
    assert "- telefone: «pks:telefone»" in texto
    assert "61999990000" not in texto
    assert "abc123" not in texto and "senha" not in texto
    assert "- especialidade: musculação" in texto
    assert "- também chamado: hipertrofia e força; força" in texto
