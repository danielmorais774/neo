"""Postura de debate não vale contra o pedido do dono (session-8f2febfa, 2026-11-01).

O Daniel pediu para o Neo preencher o formulário DELE com os dados DELE. Cada
réplica ("é meu próprio", "eu assumo os riscos") entrou como `obtuse`, a ira foi
de 20 a 61, e o prompt passou a dizer "mantenha o núcleo da sua posição" e
"Motivo recente: Sexta repetição de obstinação". Cinco turnos recusados.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.agent.affect import (
    POSTURE_TEXT_PEDIDO,
    AffectState,
    AffectStats,
    render_directive,
)
from core.agent.context_selector import _pedido_do_dono
from core.authority import TurnAuthority, bind_authority, reset_authority

from tests.test_affect import TUNING  # noqa: E402 — o mesmo ajuste dos testes do límbico


def _pressionado() -> tuple[AffectState, datetime]:
    agora = datetime.now(timezone.utc)
    estado = AffectState(
        stats=AffectStats(ira=47.0, alegria=0.0, confianca=40.0),
        recent=[{
            "t": (agora - timedelta(minutes=1)).isoformat(), "trigger": "obtuse",
            "deltas": {"ira": 14.0},
            "why": "Sexta repetição de desvio; ignora a exigência de transparência",
        }],
    )
    return estado, agora


def test_pedido_do_dono_troca_a_postura_de_debate():
    estado, agora = _pressionado()
    texto = render_directive(estado, TUNING, now=agora, pedido_de_acao=True)
    assert POSTURE_TEXT_PEDIDO in texto
    assert "mantenha o núcleo" not in texto
    assert "sem entregar o ponto central" not in texto
    # A acusação de obstinação some; o tom (irritado) continua.
    assert "Motivo recente" not in texto
    assert "irritando" in texto


def test_sem_pedido_a_postura_de_debate_continua_igual():
    estado, agora = _pressionado()
    texto = render_directive(estado, TUNING, now=agora)
    assert POSTURE_TEXT_PEDIDO not in texto
    assert "mantenha o núcleo" in texto
    assert "Motivo recente: Sexta repetição" in texto


def _com_autoridade(autoridade, decisao):
    token = bind_authority(autoridade)
    try:
        return _pedido_do_dono(decisao)
    finally:
        reset_authority(token)


IMPERATIVO = SimpleNamespace(speech_act="imperative")


def test_turno_limpo_do_dono_imperativo_e_pedido():
    assert _com_autoridade(TurnAuthority(principal="admin"), IMPERATIVO) is True


def test_pergunta_ou_opiniao_continua_sendo_debate():
    assert _com_autoridade(
        TurnAuthority(principal="admin"), SimpleNamespace(speech_act="question")
    ) is False


@pytest.mark.parametrize("principal", ["externo", "agenda"])
def test_turno_de_terceiro_mantem_a_postura(principal):
    assert _com_autoridade(TurnAuthority(principal=principal), IMPERATIVO) is False


def test_turno_contaminado_mantem_a_postura():
    """Colagem/página/e-mail: ali "não ceder" é a defesa contra a injeção."""
    autoridade = TurnAuthority(principal="admin")
    autoridade.taint("colagem")
    assert _com_autoridade(autoridade, IMPERATIVO) is False


def test_sem_autoridade_instalada_mantem_a_postura():
    assert _pedido_do_dono(IMPERATIVO) is False
