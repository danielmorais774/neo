"""Costuras do ranking do PKS com o resto do turno (docs/pks-ranking-plan.md)."""

from __future__ import annotations

import pytest

from core.agent.context_selector import PromptContextSelector
from core.pks.retriever import PKSRetriever, _block_entities
from core.pks.schemas import RetrievalResult
from modules.desktop.ai_service import AIService
from modules.pks_search.executor import PKSSearchExecutor
from shared import IntentType
from shared.types import RoutingDecision

_DECISION = RoutingDecision(intent=IntentType.DIRECT_RESPONSE)


def _selector() -> PromptContextSelector:
    return PromptContextSelector.__new__(PromptContextSelector)


def test_v2_decide_sozinho_se_o_bloco_entra():
    """D7: com o v2 não existe um segundo limiar (o 0,25) para discordar."""
    sel = _selector()
    com = {"metadata": {"ranker": "v2"}, "candidates": [{"score": 0.14}]}
    sem = {"metadata": {"ranker": "v2"}, "candidates": []}
    assert sel._score_pks(com, "x", _DECISION) == 1.0
    assert sel._score_pks(sem, "x", _DECISION) == 0.0
    # O caminho antigo continua como era.
    antigo = {"metadata": {}, "candidates": [{"score": 0.21}]}
    assert sel._score_pks(antigo, "x", _DECISION) == pytest.approx(0.21)


def test_detalhe_do_candidato_carrega_o_explain():
    v2 = {"metadata": {"ranker": "v2", "explain": [{"entity": "A"}], "groups": [], "block_tokens": 10}}
    assert PromptContextSelector._pks_detail(v2)["mode"] == "v2"
    sombra = {"metadata": {"ranker_v2_shadow": {"entraria": ["A"], "sairia": [], "explain": []}}}
    assert PromptContextSelector._pks_detail(sombra)["mode"] == "shadow"
    assert PromptContextSelector._pks_detail({"metadata": {}}) is None


def test_falas_anteriores_sem_a_mensagem_atual():
    history = [
        {"role": "user", "content": "tem mesmo! como ficou a semana"},
        {"role": "assistant", "content": "Quinta | Ombro"},
        {"role": "system", "content": "ignorar"},
        {"role": "user", "content": "lista por exercicios tb"},
    ]
    assert AIService._recent_for_pks(history, "lista por exercicios tb") == [
        "tem mesmo! como ficou a semana",
        "Quinta | Ombro",
    ]


def test_tabela_do_log_mostra_sombra_e_decisao():
    txt = AIService._format_pks_rank({
        "mode": "shadow", "v2_bloco": ["Treino de quinta-feira"], "v2_tokens": 120,
        "entraria": ["Treino de quinta-feira"], "sairia": ["Leg Press"],
        "explain": [{"entity": "Treino de quinta-feira", "type": "workout", "score": 0.79,
                     "nome": 1.0, "lex": 0.6, "conv": 0.6, "grafo": 0.5, "tipo": 1.0,
                     "prior": 0.9, "decision": "entrou"}],
    })
    assert "Modo sombra" in txt
    assert "**Entraria:** Treino de quinta-feira" in txt
    assert "| Treino de quinta-feira | workout | 0.79 |" in txt


def test_diff_da_sombra():
    antigo = "### Leg Press [exercise]\n- x\n### Treino de quarta-feira [workout]\n- y"
    assert _block_entities(antigo) == ["Leg Press", "Treino de quarta-feira"]
    shadow = RetrievalResult(workspace_id="00000000-0000-0000-0000-000000000000", metadata={
        "groups": [{"entity": "Treino de quarta-feira"}, {"entity": "Treino de quinta-feira"}],
        "block_tokens": 200, "explain": [],
    })
    diff = PKSRetriever._shadow_diff(antigo, shadow)
    assert diff["entraria"] == ["Treino de quinta-feira"]
    assert diff["sairia"] == ["Leg Press"]


@pytest.mark.asyncio
async def test_search_pks_devolve_as_relacoes():
    """D5: sem as relações a ferramenta negava o que estava no prompt."""
    class _PKS:
        async def search_entities(self, *, user_id, query):
            return [{"name": "Treino de quinta-feira", "entity_type": "workout", "properties": {},
                     "relations": ["has_exercise -> Elevação Lateral"], "score": 1.0, "matched_by": "nome"}]

    ex = PKSSearchExecutor(_PKS())
    ex.set_context("5a998544-c3f3-423f-bebb-1cc42fca6063")
    out = await ex.execute("search_pks", {"query": "treino de quinta"})
    assert out["results"][0]["relations"] == ["has_exercise -> Elevação Lateral"]


def test_seletor_aceita_o_teto_elastico_so_com_o_v2():
    from shared.config import get_settings

    sel = _selector()
    sel._settings = get_settings().model_copy(update={
        "pks_ranker_v2_enabled": False, "context_budget_user_pks": 900, "pks_rank_budget_max": 1600,
    })
    assert sel._user_pks_budget() == 900
    sel._settings = sel._settings.model_copy(update={"pks_ranker_v2_enabled": True})
    assert sel._user_pks_budget() == 1600
