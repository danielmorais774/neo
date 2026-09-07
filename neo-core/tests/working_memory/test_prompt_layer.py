"""A camada WORKING_MEM no system prompt + as guardas de regressão.

Dois compromissos são verificados aqui, e o segundo é o que protege o resto do
sistema: com a flag desligada o prompt e o toolset têm que ficar EXATAMENTE como
eram, e ligada o caderno não pode canibalizar o `pks_remember` (§13-R1).
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.agent.context_selector import PromptContextSelector
from shared.config import Settings
from shared.types import ContextLayer, IntentType, RoutingDecision

SESSION = uuid4()
USER = uuid4()


def _note(slug, summary, updated_at, session_id=None):
    return SimpleNamespace(
        slug=slug, summary=summary, title=slug, updated_at=updated_at,
        session_id=session_id,
    )


def _selector(*, enabled: bool, notes=None, budget: int = 700):
    token_counter = MagicMock()
    token_counter.count = lambda text: len(text.split()) * 2

    settings = MagicMock(spec=Settings)
    settings.context_budget_neo_core_ctx = 600
    settings.context_budget_user_prefs = 500
    settings.context_budget_user_pks = 900
    settings.context_budget_retrieved = 1200
    settings.context_budget_working_mem = budget
    settings.context_relevance_threshold = 0.25
    settings.neo_affect_enabled = False
    settings.working_memory_enabled = enabled

    wm = MagicMock()
    wm.index = AsyncMock(return_value=list(notes or []))

    sel = PromptContextSelector(
        token_counter=token_counter, settings=settings, working_memory=wm,
    )
    return sel, wm


def _decision():
    return RoutingDecision(
        intent=IntentType.DIRECT_RESPONSE, confidence=0.9,
        needs_memory=False, knowledge_keywords=None,
    )


async def _build(sel, **kw):
    params = dict(
        user_message="e os estornos?",
        user_id=USER,
        decision=_decision(),
        system_context=None,
        recovered_context=[],
        session_id=SESSION,
    )
    params.update(kw)
    return await sel.build_prompt(**params)


# ── ligado ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_indice_entra_no_prompt_com_a_flag_ligada():
    notes = [_note("extrato-marco", "412 lançamentos apurados", datetime(2026, 7, 31, 16, 41), SESSION)]
    sel, wm = _selector(enabled=True, notes=notes)

    prompt, trace = await _build(sel)

    wm.index.assert_awaited_once()
    assert wm.index.await_args.kwargs["session_id"] == SESSION
    assert "[[extrato-marco]]" in prompt
    assert "412 lançamentos apurados" in prompt
    layer = next(c for c in trace if c.layer == ContextLayer.WORKING_MEM)
    assert layer.included is True
    # Nota da sessão atual = tarefa em curso, relevância máxima.
    assert layer.final_score == 1.0


@pytest.mark.asyncio
async def test_nota_de_outra_sessao_entra_com_relevancia_menor():
    notes = [_note("antiga", "de ontem", datetime(2026, 7, 30, 10, 0), uuid4())]
    sel, _ = _selector(enabled=True, notes=notes)

    _, trace = await _build(sel)

    layer = next(c for c in trace if c.layer == ContextLayer.WORKING_MEM)
    assert layer.final_score == 0.7 and layer.included is True


@pytest.mark.asyncio
async def test_regra_13_so_existe_com_a_flag_ligada():
    sel_on, _ = _selector(enabled=True)
    sel_off, _ = _selector(enabled=False)
    assert "CADERNO DE TRABALHO" in sel_on._build_core_prompt()
    assert "CADERNO DE TRABALHO" not in sel_off._build_core_prompt()


@pytest.mark.asyncio
async def test_indice_grande_e_podado_por_nota_nunca_sumindo_inteiro():
    # Se o bloco inteiro estourasse o budget, _select_candidates o descartaria e
    # o modelo perderia até a EXISTÊNCIA das notas — pior que mostrar menos.
    notes = [
        _note(f"nota-{i}", "resumo bem comprido " * 12, datetime(2026, 7, 31, 10, i), SESSION)
        for i in range(8)
    ]
    sel, _ = _selector(enabled=True, notes=notes, budget=400)

    prompt, trace = await _build(sel)

    layer = next(c for c in trace if c.layer == ContextLayer.WORKING_MEM)
    assert layer.included is True
    assert layer.est_tokens <= 400
    assert "ficaram de fora por espaço" in prompt
    assert "[[nota-0]]" in prompt   # a de maior prioridade sobreviveu
    assert "[[nota-7]]" not in prompt  # a de menor foi podada


# ── desligado ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flag_desligada_nao_consulta_nem_injeta_nada():
    notes = [_note("n", "s", datetime(2026, 7, 31, 12, 0), SESSION)]
    sel, wm = _selector(enabled=False, notes=notes)

    prompt, trace = await _build(sel)

    wm.index.assert_not_awaited()
    assert not [c for c in trace if c.layer == ContextLayer.WORKING_MEM]
    assert "Memória de Trabalho" not in prompt


@pytest.mark.asyncio
async def test_sem_usuario_nao_ha_caderno():
    sel, wm = _selector(enabled=True, notes=[_note("n", "s", datetime.utcnow(), SESSION)])
    await _build(sel, user_id=None)
    wm.index.assert_not_awaited()


@pytest.mark.asyncio
async def test_falha_do_caderno_nao_derruba_o_prompt():
    sel, wm = _selector(enabled=True)
    wm.index = AsyncMock(side_effect=RuntimeError("banco fora"))

    prompt, trace = await _build(sel)

    assert prompt  # o turno segue
    assert not [c for c in trace if c.layer == ContextLayer.WORKING_MEM]


# ── R1: a fronteira com o pks_remember ──────────────────────────────────────

def test_tools_wm_nao_se_confundem_com_a_memoria_permanente():
    from modules.pks_write.tools import PKS_WRITE_TOOLS
    from modules.working_memory.tools import WORKING_MEMORY_TOOLS

    wm_write = next(t for t in WORKING_MEMORY_TOOLS if t.name == "wm_write")
    # O nome não pode ser 'anotar'/'note': "anota isso" é gatilho do pks_remember.
    assert {t.name for t in WORKING_MEMORY_TOOLS} == {"wm_write", "wm_read"}
    # E a description manda o fato permanente para o lugar certo, por nome.
    assert "pks_remember" in wm_write.description
    assert "permanente" in wm_write.description.lower()
    # O caminho inverso continua intacto.
    pks_remember = next(t for t in PKS_WRITE_TOOLS if t.name == "pks_remember")
    assert "memória permanente" in pks_remember.description


def test_toolset_cognitivo_respeita_a_flag(monkeypatch):
    from core.cognitive import read_tools
    from modules.working_memory.tools import WORKING_MEMORY_TOOL_NAMES

    def _settings(enabled: bool):
        s = MagicMock(spec=Settings)
        s.skills_library_enabled = False
        s.web_search_enabled = False
        s.neo_gateway_enabled = False
        s.neo_files_enabled = False
        s.pks_write_enabled = True
        s.neo_file_actuation_enabled = False
        s.neo_brain_shell_enabled = False
        s.working_memory_enabled = enabled
        return s

    monkeypatch.setattr("shared.config.get_settings", lambda: _settings(True))
    on = {t.name for t in read_tools.cognitive_tools_for(USER)}
    assert WORKING_MEMORY_TOOL_NAMES <= on

    monkeypatch.setattr("shared.config.get_settings", lambda: _settings(False))
    off = {t.name for t in read_tools.cognitive_tools_for(USER)}
    assert not (WORKING_MEMORY_TOOL_NAMES & off)


def test_wm_e_user_scoped_some_sem_usuario(monkeypatch):
    from core.cognitive import read_tools
    from modules.working_memory.tools import WORKING_MEMORY_TOOL_NAMES

    s = MagicMock(spec=Settings)
    s.skills_library_enabled = False
    s.web_search_enabled = False
    s.neo_gateway_enabled = False
    s.neo_files_enabled = False
    s.pks_write_enabled = True
    s.neo_file_actuation_enabled = False
    s.neo_brain_shell_enabled = False
    s.working_memory_enabled = True
    monkeypatch.setattr("shared.config.get_settings", lambda: s)

    anon = {t.name for t in read_tools.cognitive_tools_for(None)}
    assert not (WORKING_MEMORY_TOOL_NAMES & anon)
