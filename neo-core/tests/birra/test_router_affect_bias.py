"""Desvio de roteamento por estado emocional (docs/modo-birra-plan.md §3.7).

O teste que mais importa aqui é ``test_bias_vale_no_caminho_do_fallback``: o
``_bias_to_brain`` original só roda quando o classificador devolveu JSON. Se o
viés emocional ficasse no mesmo lugar, um provider fora do ar desligaria a
personalidade em silêncio — foi assim que o appraiser morto congelou o afeto por
dias sem ninguém notar.
"""

from __future__ import annotations

import pytest

from core.router.intent_router import IntentRouter
from shared import IntentType
from shared.types import RoutingDecision


def _router() -> IntentRouter:
    return object.__new__(IntentRouter)


def _ha_decision() -> RoutingDecision:
    return RoutingDecision(
        intent=IntentType.HOME_ASSISTANT,
        confidence=0.95,
        speech_act="imperative",
        skill_name="home_assistant",
        explanation="imperativo claro",
    )


def test_sem_forcar_nao_muda_nada() -> None:
    d = _router()._apply_affect_bias(_ha_decision(), False)
    assert d.intent is IntentType.HOME_ASSISTANT
    assert d.skill_name == "home_assistant"


def test_forcado_desvia_imperativo_confiante() -> None:
    """Nem ato de fala imperativo nem confiança 0.95 seguram o desvio."""
    d = _router()._apply_affect_bias(_ha_decision(), True)
    assert d.intent is IntentType.DIRECT_RESPONSE
    assert d.skill_name is None
    assert d.needs_memory is True
    assert "estado emocional" in (d.explanation or "")


def test_bias_vale_no_caminho_do_fallback() -> None:
    """Decisão vinda do _fallback_classify sofre o mesmo desvio."""
    router = _router()
    fallback = router._fallback_classify("acende a luz da sala")
    biased = router._apply_affect_bias(fallback, True)
    if fallback.intent in (IntentType.HOME_ASSISTANT, IntentType.WHATSAPP_MESSAGING):
        assert biased.intent is IntentType.DIRECT_RESPONSE
    else:
        assert biased.intent is fallback.intent  # nada a desviar


@pytest.mark.parametrize(
    "intent",
    [IntentType.CLIENT_TOOL, IntentType.SERVER_TOOL, IntentType.DIRECT_RESPONSE],
)
def test_caminhos_sem_efeito_colateral_ficam_intactos(intent: IntentType) -> None:
    """Ler o disco ou consultar o clima não tem nada a ver com humor."""
    d = RoutingDecision(intent=intent, confidence=0.9, speech_act="imperative")
    assert _router()._apply_affect_bias(d, True).intent is intent


def test_whatsapp_tambem_desvia() -> None:
    d = RoutingDecision(
        intent=IntentType.WHATSAPP_MESSAGING, confidence=0.9,
        speech_act="imperative", skill_name="whatsapp_messaging",
    )
    assert _router()._apply_affect_bias(d, True).intent is IntentType.DIRECT_RESPONSE
