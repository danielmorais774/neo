"""``search_pks`` tem que devolver o CONTEÚDO da entidade, não só o nome.

Regressão de prod (session-5102534a, "uma musica que eu gosto"): a busca achou
"Preferência musical de Daniel" e devolveu ``entity_type=None, properties=None``
nas três tentativas do cérebro — o dict vinha do ``EntityRecord`` (``type`` /
``metadata``) e os fatos moram na ``pks_entity_field``, que ninguém carregava.
O teste do executor não pegou isso porque o fake dele já entregava a forma certa.
"""

from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest

from core.pks.schemas import EntityRecord, FieldRecord
from core.pks.service import PKSService

_WORKSPACE = uuid4()


def _entity(name: str, type_: str) -> EntityRecord:
    return EntityRecord(id=uuid4(), workspace_id=_WORKSPACE, name=name, type=type_)


def _field(entity_id: UUID, key: str, value, value_type: str = "text") -> FieldRecord:
    return FieldRecord(id=uuid4(), entity_id=entity_id, key=key, value_type=value_type, value=value)


class _FakeRepo:
    def __init__(self, rows, fields, relations=None):
        self._rows = rows
        self._fields = fields
        self._relations = relations or {}
        self.batch_calls = 0

    async def search_entities(self, session, workspace_id, query):
        return self._rows

    async def get_fields_for_entities(self, session, entity_ids):
        self.batch_calls += 1
        return {eid: self._fields.get(eid, []) for eid in entity_ids}

    async def get_relations(self, session, entity_id):
        return self._relations.get(entity_id, [])


class _FakeDB:
    @asynccontextmanager
    async def get_session(self):
        yield object()


class _FakeBootstrap:
    async def ensure_workspace(self, user_id):
        return _WORKSPACE


def _service(rows, fields, relations=None) -> tuple[PKSService, _FakeRepo]:
    svc = object.__new__(PKSService)
    repo = _FakeRepo(rows, fields, relations)
    svc.repository = repo
    svc.db = _FakeDB()
    svc.bootstrap = _FakeBootstrap()
    return svc, repo


@pytest.mark.asyncio
async def test_search_returns_entity_type_and_field_values():
    ent = _entity("Preferência musical de Daniel", "preference")
    rows = [(ent, 0.56, "trigram")]
    fields = {ent.id: [
        _field(ent.id, "bands", "['Papa Roach', 'Slipknot']"),
        _field(ent.id, "disliked_genres", "['funk', 'sertanejo']"),
    ]}
    svc, repo = _service(rows, fields)

    out = await svc.search_entities(user_id=uuid4(), query="musica favorita")

    assert len(out) == 1
    # As duas chaves que os consumidores leem — e que vinham None em prod.
    assert out[0]["entity_type"] == "preference"
    assert out[0]["properties"] == {
        "bands": "['Papa Roach', 'Slipknot']",
        "disliked_genres": "['funk', 'sertanejo']",
    }
    assert out[0]["score"] == 0.56 and out[0]["matched_by"] == "trigram"
    # Um lote só, não um SELECT por entidade.
    assert repo.batch_calls == 1


@pytest.mark.asyncio
async def test_entity_without_fields_returns_empty_properties():
    ent = _entity("Daniel", "person")
    svc, _ = _service([(ent, 1.0, "name")], {})
    out = await svc.search_entities(user_id=uuid4(), query="daniel")
    assert out[0]["properties"] == {}
    assert out[0]["entity_type"] == "person"


@pytest.mark.asyncio
async def test_ledger_entity_never_leaves_the_search():
    """Devolver valor reabre o canal do saldo fantasma — ver core/pks/ledger_guard."""
    wallet = _entity("Carteira", "financial")
    ok = _entity("Preferência musical de Daniel", "preference")
    rows = [(wallet, 0.9, "name"), (ok, 0.5, "trigram")]
    fields = {
        wallet.id: [_field(wallet.id, "balance", 980, value_type="number")],
        ok.id: [_field(ok.id, "bands", "['Slipknot']")],
    }
    svc, _ = _service(rows, fields)

    out = await svc.search_entities(user_id=uuid4(), query="gosto musical")

    assert [r["name"] for r in out] == ["Preferência musical de Daniel"]


@pytest.mark.asyncio
async def test_no_rows_skips_the_field_query():
    svc, repo = _service([], {})
    assert await svc.search_entities(user_id=uuid4(), query="nada") == []
    assert repo.batch_calls == 0


@pytest.mark.asyncio
async def test_search_devolve_as_relacoes_da_entidade():
    """session-098211a8: o treino de quinta estava no prompt com os 6
    exercícios, e a ferramenta devolvia só nome/fields — "o treino existe, os
    exercícios não". Agora as ligações vêm junto, nos dois sentidos."""
    from core.pks.schemas import RelationRecord

    treino = _entity("Treino de quinta-feira", "workout")
    plano_id, ex_id = uuid4(), uuid4()
    rels = {treino.id: [
        RelationRecord(id=uuid4(), workspace_id=_WORKSPACE, from_entity_id=treino.id,
                       relation_type="has_exercise", to_entity_id=ex_id, to_name="Elevação Lateral"),
        RelationRecord(id=uuid4(), workspace_id=_WORKSPACE, from_entity_id=plano_id,
                       relation_type="has_member", to_entity_id=treino.id, from_name="Plano de treino"),
        RelationRecord(id=uuid4(), workspace_id=_WORKSPACE, from_entity_id=treino.id,
                       relation_type="has_exercise", to_entity_id=uuid4(), to_name="Segredo",
                       sensitivity="secret"),
    ]}
    svc, _ = _service([(treino, 1.0, "name")], {}, rels)

    out = await svc.search_entities(user_id=uuid4(), query="treino de quinta")

    assert out[0]["relations"] == [
        "has_exercise -> Elevação Lateral",
        "has_member <- Plano de treino",
    ]
