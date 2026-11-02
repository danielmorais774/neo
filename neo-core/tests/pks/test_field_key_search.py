"""Achar o fato pela CHAVE do campo, não só pelo nome da entidade.

Regressão de prod (sessão 369d812c, 2026-10-22): "ta lembrado do meu cep?" com
`Daniel Morais.cep` gravado no PKS. O Neo respondeu três vezes que não havia CEP
nenhum, e não estava mentindo — a `search_pks` dele devolvia `Tríceps`.

Três defeitos somados, um teste para cada:
  1. nada no índice ligava a palavra "cep" à entidade: o `search_text` é nome +
     apelidos + metadata, e o ranking v2 indexava só o VALOR do campo;
  2. `search_text LIKE '%cep%'` casava "trí-cep-s" e as três vagas do resolver
     iam para exercício;
  3. sem o classificador (403 do provedor), o fallback de keywords exigia 4
     letras e descartava justamente "cep".
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from core.pks.context_builder import ContextBuilder
from core.pks.ontology import field_key_terms, field_keys_for_term
from core.pks.ranking import RankConfig, rank_pks
from core.pks.repository import _field_keys_for_query
from core.pks.retriever import PKSRetriever
from core.pks.schemas import (
    EntityRecord,
    FieldRecord,
    Neighborhood,
    RetrievalResult,
)

WS = uuid4()
NOW = datetime(2026, 10, 22, 1, 40, tzinfo=timezone.utc)


# ── 1. o mapa de chaves ──────────────────────────────────────────────────────

def test_chave_leva_as_palavras_com_que_ele_pergunta():
    termos = field_key_terms("cep")
    assert "cep" in termos
    assert "endereco" in termos  # "qual meu endereço" também tem que achar


def test_chave_generica_nao_identifica_ninguem():
    # 11 entidades têm `description`: procurar por ela traria todas.
    assert field_key_terms("description") == ()
    assert field_keys_for_term("descricao") == ()


def test_palavra_desconhecida_vale_por_si_mesma():
    assert field_keys_for_term("headquarters") == ("headquarters",)


def test_termo_do_usuario_acha_a_chave_canonica():
    assert "cep" in field_keys_for_term("endereco")


# ── 2. a consulta que vai ao SQL ─────────────────────────────────────────────

def test_consulta_com_stopword_ainda_encontra_a_chave():
    # "meu cep" é UMA query só; sem quebrar em termos, dava zero resultado.
    # O mapa vale nos dois sentidos, então "cep" também procura `endereco`.
    assert _field_keys_for_query("meu cep") == ["meu", "cep", "endereco"]


def test_memoria_nao_e_sinonimo_de_ram():
    # "lembra do meu cep?" fazia o classificador mandar a keyword "memoria", e
    # ela puxava todo computador que tem o campo `ram_gb`.
    assert "ram_gb" not in _field_keys_for_query("memoria")
    assert "memory" not in _field_keys_for_query("memoria")


def test_artigo_portugues_nao_vira_sistema_operacional():
    assert _field_keys_for_query("quais os treinos") == ["quais", "treinos"]


def test_credencial_nao_vira_chave_de_busca():
    # "senha" não pode nem LOCALIZAR a entidade que guarda uma — o lugar
    # da credencial é o cofre (core/pks/sensitivity.py).
    assert _field_keys_for_query("senha do wifi") == ["wifi"]
    assert _field_keys_for_query("qual o token") == ["qual"]


# ── 3. o ranking v2 ──────────────────────────────────────────────────────────

def _workspace():
    """O essencial do caso real: quem TEM o cep, e o impostor de substring."""
    dono = EntityRecord(
        id=uuid4(), workspace_id=WS, name="Daniel Morais", type="person",
        aliases=["Daniel", "dono", "eu"], updated_at=NOW,
    )
    triceps = EntityRecord(
        id=uuid4(), workspace_id=WS, name="Tríceps", type="muscle_group", updated_at=NOW,
    )
    fields = {
        dono.id: [
            FieldRecord(id=uuid4(), entity_id=dono.id, key="cep",
                        value_type="text", value="72630-123", sensitivity="private"),
            FieldRecord(id=uuid4(), entity_id=dono.id, key="senha_do_banco",
                        value_type="text", value="hunter2", sensitivity="private"),
        ],
        triceps.id: [],
    }
    return [dono, triceps], fields


def _nomes(outcome) -> list[str]:
    return [g.head.entity.name for g in outcome.groups]


def _sinais(outcome) -> dict[str, dict]:
    return {linha["entity"]: linha for linha in outcome.explain(10)}


def _rank(message: str):
    entities, fields = _workspace()
    return rank_pks(
        entities=entities, fields_by_entity=fields,
        message=message, config=RankConfig(), now=NOW,
    )


def test_a_palavra_cep_aponta_para_quem_tem_o_cep():
    # O sinal, não o limiar: num workspace de duas entidades o BM25 quase não
    # tem IDF para dar. O que importa é a ORDEM — antes, "cep" dava lex=0 para
    # o dono e o único casamento possível era a substring de "trí-cep-s".
    sinais = _sinais(_rank("ta lembrado do meu cep?"))
    assert sinais["Daniel Morais"]["lex"] > 0
    assert sinais["Tríceps"]["lex"] == 0
    assert sinais["Daniel Morais"]["score"] > sinais["Tríceps"]["score"]


def test_triceps_nao_e_mais_resposta_para_cep():
    assert "Tríceps" not in _nomes(_rank("ta lembrado do meu cep?"))


def test_chave_de_credencial_nao_e_indexada():
    sinais = _sinais(_rank("qual a senha do banco?"))
    assert sinais["Daniel Morais"]["lex"] == 0
    assert "Daniel Morais" not in _nomes(_rank("qual a senha do banco?"))


# ── 4. o fallback de keywords (sem classificador) ────────────────────────────

def test_palavra_de_tres_letras_sobrevive_ao_fallback():
    # Com o classificador fora do ar, é este caminho que decide o que buscar.
    termos = PKSRetriever._keywords(None, "ta lembrado do meu cep?", None)
    assert "cep" in termos
    assert "meu" not in termos


def test_fallback_nao_vira_ruido_de_tres_letras():
    termos = PKSRetriever._keywords(None, "voce pode ver se tem algo com isso?", None)
    assert "ver" not in termos
    assert "tem" not in termos


# ── 5. achar não basta: o bloco precisa CABER ────────────────────────────────
# O bloco é UM candidato no seletor e é tudo-ou-nada. De nada adianta recuperar
# a entidade certa se um campo gordo passa do orçamento e derruba tudo junto —
# foi o que quase aconteceu: com `neo_affect` dentro, o bloco do dono media
# 1128 tokens contra um teto de 900.

def _bloco_do_dono(fields: list[FieldRecord], entity_id, workspace_id) -> str:
    resultado = RetrievalResult(
        workspace_id=workspace_id,
        keywords=["cep"],
        neighborhoods=[Neighborhood(
            entity=EntityRecord(id=entity_id, workspace_id=workspace_id,
                                name="Daniel Morais", type="person"),
            fields=fields,
        )],
    )
    return ContextBuilder().format(resultado, user_message="ta lembrado do meu cep?")


def test_escrituracao_interna_fica_fora_do_bloco():
    eid, wid = uuid4(), uuid4()
    texto = _bloco_do_dono([
        FieldRecord(id=uuid4(), entity_id=eid, key="cep", value_type="text", value="00000-000"),
        FieldRecord(id=uuid4(), entity_id=eid, key="neo_affect", value_type="json",
                    value={"stats": {"ira": 37.8}, "recent": ["x" * 2000]}),
    ], eid, wid)
    assert "cep" in texto
    assert "neo_affect" not in texto
    assert "ira" not in texto


def test_campo_gordo_nao_estoura_o_bloco_sozinho():
    eid, wid = uuid4(), uuid4()
    texto = _bloco_do_dono([
        FieldRecord(id=uuid4(), entity_id=eid, key="dossie", value_type="text", value="x" * 5000),
    ], eid, wid)
    assert len(texto) < 500
    assert texto.rstrip().endswith("…")
