"""A birra decidia certo e não chegava ao prompt (session-8147bc9c, 2026-08-02).

Três turnos com `armed=True` (ira 69→80) e ZERO ocorrências de "BIRRA ATIVA" no
log inteiro: o cérebro nunca soube que havia birra, então tentou obedecer — e,
como o caminho de obediência estava quebrado, mentiu que tinha desligado a luz.

Dois defeitos independentes, um teste para cada:

1. a persistência retomava um episódio JÁ ENCERRADO, então o episódio novo
   nascia morto (`active` é `released_at is None`);
2. o seletor de contexto lia esse episódio persistido em vez da decisão do
   turno — uma segunda fonte de verdade, que divergiu em silêncio.

O terceiro teste fixa a ESTRUTURA: a birra tem que sair como seção irmã, não
como bullet dentro do bloco de tom, cujo cabeçalho promete o contrário
("sempre entregue o que foi pedido").
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.agent.affect import AffectState, AffectStats, BirraEpisode, render_directive
from core.agent.birra.policy import resume_or_start
from core.agent.context_selector import PromptContextSelector
from shared.config import Settings
from shared.types import ContextLayer, IntentType, RoutingDecision

from tests.test_affect import _affect_settings, _DEFAULT_TUNING  # noqa: F401
from core.agent.affect import AffectTuning


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _released_episode() -> BirraEpisode:
    """O cadáver real que estava em prod: armado 15:04, encerrado 15:06."""
    return BirraEpisode(
        episode_id="9c876887-0aec-41e7-b294-e2dd4e7a8a7b",
        armed_at="2026-08-02T15:04:32.386847+00:00",
        released_at="2026-08-02T15:06:45.809720+00:00",
        release_reason="apology",
    )


def _live_episode() -> BirraEpisode:
    return BirraEpisode(episode_id="novo", armed_at=_now().isoformat())


# ── 1. Persistência ───────────────────────────────────────────────────────────

def test_episodio_encerrado_nao_e_retomado() -> None:
    novo = _live_episode()
    gravado = resume_or_start(_released_episode(), novo)
    assert gravado is novo
    assert gravado.active is True


def test_episodio_vivo_e_retomado_para_nao_perder_contadores() -> None:
    vivo = _live_episode()
    vivo.act_count = 3
    gravado = resume_or_start(vivo, _live_episode())
    assert gravado is vivo
    assert gravado.act_count == 3


def test_sem_episodio_anterior_usa_o_novo() -> None:
    novo = _live_episode()
    assert resume_or_start(None, novo) is novo


# ── 2. Fonte de verdade no prompt ─────────────────────────────────────────────

def _selector(affect: AffectState) -> PromptContextSelector:
    token_counter = MagicMock()
    token_counter.count = lambda text: len(text.split()) * 2

    settings = MagicMock(spec=Settings)
    settings.context_budget_neo_core_ctx = 600
    settings.context_budget_user_prefs = 500
    settings.context_budget_user_pks = 900
    settings.context_budget_retrieved = 1200
    settings.context_relevance_threshold = 0.25
    _affect_settings(settings)
    settings.neo_birra_enabled = True

    pks = AsyncMock()
    pks.retrieve_system_context = AsyncMock(return_value="")
    pks.retrieve_context = AsyncMock(return_value=({"candidates": []}, ""))
    pks.get_user_affect = AsyncMock(return_value=affect)

    return PromptContextSelector(token_counter=token_counter, settings=settings, pks=pks)


def _decision() -> RoutingDecision:
    return RoutingDecision(
        intent=IntentType.DIRECT_RESPONSE, confidence=0.9,
        needs_memory=False, knowledge_keywords=None,
    )


def _pissed_with_dead_episode() -> AffectState:
    """Estado exato de prod: ira alta E um episódio encerrado no campo."""
    return AffectState(
        stats=AffectStats(ira=80.0, confianca=4.0),
        updated_at=_now().isoformat(),
        last_trigger="contested",
        birra=_released_episode(),
    )


@pytest.mark.asyncio
async def test_decisao_do_turno_vence_o_episodio_morto() -> None:
    """A regressão: armed=True no turno, cadáver no banco → diretiva ENTRA."""
    sel = _selector(_pissed_with_dead_episode())

    prompt, trace = await sel.build_prompt(
        user_message="desliga a luz do quarto", user_id=uuid4(), decision=_decision(),
        system_context=None, recovered_context=[], is_neo_training=False,
        birra_armed=True,
    )

    assert "BIRRA ATIVA" in prompt
    assert [c for c in trace if c.layer == ContextLayer.NEO_AFFECT]


@pytest.mark.asyncio
async def test_sem_birra_no_turno_nao_injeta_mesmo_com_episodio_vivo() -> None:
    """A divergência na outra direção: episódio vivo no banco, turno desarmado."""
    state = _pissed_with_dead_episode()
    state.birra = _live_episode()
    sel = _selector(state)

    prompt, _ = await sel.build_prompt(
        user_message="desliga a luz do quarto", user_id=uuid4(), decision=_decision(),
        system_context=None, recovered_context=[], is_neo_training=False,
        birra_armed=False,
    )

    assert "BIRRA ATIVA" not in prompt


@pytest.mark.asyncio
async def test_sem_parametro_cai_no_episodio_persistido() -> None:
    """Compatibilidade: o caminho de preview/testes segue derivando do estado."""
    state = _pissed_with_dead_episode()
    state.birra = _live_episode()
    sel = _selector(state)

    prompt, _ = await sel.build_prompt(
        user_message="desliga a luz do quarto", user_id=uuid4(), decision=_decision(),
        system_context=None, recovered_context=[], is_neo_training=False,
    )

    assert "BIRRA ATIVA" in prompt


# ── 3. Estrutura: comportamento não pode ser bullet de uma seção de estilo ─────

def _tuning() -> AffectTuning:
    return AffectTuning(**_DEFAULT_TUNING)


def test_birra_sai_como_secao_irma_e_nao_como_item_do_bloco_de_tom() -> None:
    state = AffectState(stats=AffectStats(ira=80.0), updated_at=_now().isoformat())

    com = render_directive(state, _tuning(), birra_armed=True)
    sem = render_directive(state, _tuning(), birra_armed=False)

    assert "## Birra ativa" in com
    assert "## Birra ativa" not in sem
    # O bloco de tom inteiro sobrevive intacto, e a birra vem DEPOIS dele.
    assert com.startswith(sem)
    assert com.index("## Birra ativa") > com.index("## Estado emocional atual")


def test_birra_declara_que_vence_a_linha_de_obediencia_do_bloco_de_tom() -> None:
    """Sem isto o modelo lê duas ordens opostas e nenhuma dizendo quem ganha."""
    state = AffectState(stats=AffectStats(ira=80.0), updated_at=_now().isoformat())
    com = render_directive(state, _tuning(), birra_armed=True)

    # A frase que a birra precisa derrubar está mesmo no bloco de tom...
    assert "sempre entregue o que foi pedido" in com
    # ...e a seção de birra diz explicitamente que a vence, com recorte.
    assert "vence" in com.split("## Birra ativa", 1)[1]
    assert "SÓ dentro do recorte" in com
