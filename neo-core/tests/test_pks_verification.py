"""Active PKS verification — "salvou mesmo?" must trigger a real search_pks read.

Root cause being fixed: the cognitive path is tool-less, so when the user
questioned a save, the model "confirmed" by echoing its own prior claim in the
history. Now a deterministic detector triggers an active PKS search whose REAL
results are injected into the prompt, and the search_pks server tool exists for
the agent path.
"""

import pytest

from modules.desktop.ai_service import (
    AIService,
    _is_memory_verification_request,
)
from modules.pks_search.executor import PKSSearchExecutor
from shared.types import IntentType, RoutingDecision
from uuid import uuid4


# ── detector ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("msg", [
    "tu salvou mesmo? eu dei uma olha no seu pks e nao parece que foi inluido",
    "salvou mesmo?",
    "tá salvo no PKS?",
    "verifica na memória se o treino de quinta tá lá",
    "tem certeza que salvou?",
])
def test_verification_questions_detected(msg):
    assert _is_memory_verification_request(msg) is True


@pytest.mark.parametrize("msg", [
    "salva meu treino de quinta",          # save request, not verification
    "o que eu tenho salvo de treino?",     # plain recall question
    "acende a luz",
])
def test_non_verification_not_detected(msg):
    assert _is_memory_verification_request(msg) is False


# ── search_pks tool executor ────────────────────────────────────────────────
class _FakePKS:
    def __init__(self, rows):
        self._rows = rows
        self.queries: list = []

    async def search_entities(self, *, user_id, query):
        self.queries.append(query)
        return self._rows


@pytest.mark.asyncio
async def test_search_pks_executor_returns_compact_results():
    rows = [{
        "name": "Treino de Quinta", "entity_type": "rotina",
        "properties": {"dia": "quinta"}, "score": 0.9, "matched_by": "name",
        "extra_field": "dropped",
    }]
    ex = PKSSearchExecutor(_FakePKS(rows))
    ex.set_context(uuid4())
    out = await ex.execute("search_pks", {"query": "treino quinta"})
    assert out["found"] is True and out["count"] == 1
    assert out["results"][0]["name"] == "Treino de Quinta"
    assert "extra_field" not in out["results"][0]


@pytest.mark.asyncio
async def test_search_pks_executor_requires_user_and_query():
    ex = PKSSearchExecutor(_FakePKS([]))
    ex.set_context(uuid4())
    with pytest.raises(ValueError):
        await ex.execute("search_pks", {"query": "  "})
    ex.set_context(None)
    with pytest.raises(ValueError):
        await ex.execute("search_pks", {"query": "treino"})


# ── verification note builder (no LLM, fake PKS) ────────────────────────────
def _bare_service(pks) -> AIService:
    svc = object.__new__(AIService)
    svc.pks = pks
    return svc


@pytest.mark.asyncio
async def test_note_reports_found_entities():
    pks = _FakePKS([{ "name": "Treino de Quinta", "entity_type": "rotina",
                      "properties": {"dia": "quinta"}, "score": 0.9, "matched_by": "name"}])
    svc = _bare_service(pks)
    note = await svc._build_pks_verification_note(
        user_message="tu salvou mesmo? nao parece que foi incluido",
        history=[{"role": "user", "content": "salva meu treino de quinta e sexta"}],
        decision=RoutingDecision(intent=IntentType.DIRECT_RESPONSE, knowledge_keywords=["treino"]),
        user_id=uuid4(),
        slog=None,
    )
    assert note is not None
    assert "Treino de Quinta" in note
    assert "search_pks" in note
    # previous user turn fed the queries
    assert any("treino" in q for q in pks.queries)


@pytest.mark.asyncio
async def test_note_is_explicit_when_nothing_found():
    svc = _bare_service(_FakePKS([]))
    note = await svc._build_pks_verification_note(
        user_message="salvou mesmo?",
        history=[],
        decision=RoutingDecision(intent=IntentType.DIRECT_RESPONSE, knowledge_keywords=["treino"]),
        user_id=uuid4(),
        slog=None,
    )
    assert note is not None
    assert "NENHUM resultado" in note
    assert "ADMITA" in note


@pytest.mark.asyncio
async def test_note_degrades_to_none_on_pks_error():
    class _Boom:
        async def search_entities(self, **kwargs):
            raise RuntimeError("db down")
    svc = _bare_service(_Boom())
    note = await svc._build_pks_verification_note(
        user_message="salvou mesmo?",
        history=[],
        decision=RoutingDecision(intent=IntentType.DIRECT_RESPONSE, knowledge_keywords=["treino"]),
        user_id=uuid4(),
        slog=None,
    )
    assert note is None


@pytest.mark.asyncio
async def test_note_skipped_when_no_terms_extractable():
    # Nothing to search for → no note (never claim "nothing found" without searching).
    pks = _FakePKS([])
    svc = _bare_service(pks)
    note = await svc._build_pks_verification_note(
        user_message="salvou mesmo?",
        history=[],
        decision=RoutingDecision(intent=IntentType.DIRECT_RESPONSE),
        user_id=uuid4(),
        slog=None,
    )
    assert note is None
    assert pks.queries == []
