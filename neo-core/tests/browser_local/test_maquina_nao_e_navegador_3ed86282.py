"""Reteste do vídeo do YouTube (session-3ed86282, 17/11, turnos 3 e 4).

Turno 3: "baixa o vídeo do YouTube … e me manda aqui no chat" virou turno de
MÚSICA ("baixa" + "youtube"), e o turno de música tira `run_command` da mesa — o
aviso de origem bloqueada saiu na versão "sem máquina".

Turno 4: "faz para mim pela minha maquina" — o classificador escolheu
`run_command yt-dlp` no Mac, `onde=maquina`, e o portão do navegador trocou por
`browser_local_task` só porque a frase nomeia a máquina.
"""

from __future__ import annotations

import pytest

from core.browser_local.gate import local_browser_requested, machine_only_request
from core.router.intent_router import IntentRouter
from shared import IntentType, RoutingDecision


@pytest.mark.parametrize(
    "message",
    [
        'baixa o vídeo do YouTube "taca-lê pau nesse carrinho Marco véio" e me manda aqui no chat',
        "baixa pra mim esse video do youtube",
        "quero baixar a música do spotify em mp3",
        "baixe o audio desse video do youtube",
    ],
)
def test_baixar_video_ou_musica_nao_e_turno_de_musica(message):
    assert not IntentRouter._is_music_request(message)


@pytest.mark.parametrize(
    "message",
    [
        "baixa o volume do youtube",
        "abaixa o som do video no youtube",
        "baixa um pouco o volume da musica",
        "toca duality do slipknot",
    ],
)
def test_controle_de_midia_continua_sendo_musica(message):
    assert IntentRouter._is_music_request(message)


@pytest.mark.parametrize(
    "message",
    ["faz para mim pela minha maquina", "quantas chaves ssh tenho no meu pc", "aumenta o volume desse mac"],
)
def test_so_a_maquina(message):
    assert local_browser_requested(message)
    assert machine_only_request(message)


@pytest.mark.parametrize(
    "message",
    [
        "era para ser no meu navegador local",
        "abre no chrome do meu mac",
        "usa a extensão no meu macbook",
        "pesquisa o preço da geladeira",
    ],
)
def test_com_navegador_ou_sem_maquina_nao_e_so_maquina(message):
    assert not machine_only_request(message)


def _router() -> IntentRouter:
    return object.__new__(IntentRouter)


def _decision(intent: IntentType, **over) -> RoutingDecision:
    base = dict(intent=intent, confidence=0.8, explanation="teste", speech_act="imperative")
    base.update(over)
    return RoutingDecision(**base)


@pytest.fixture(autouse=True)
def _local_ligado(monkeypatch):
    monkeypatch.setattr(
        "shared.config.get_settings",
        lambda: type("S", (), {"browser_local_enabled": True})(),
    )


def test_run_command_no_mac_fica_quando_so_a_maquina_foi_citada():
    """A decisão real do turno 4."""
    d = _router()._bias_local_browser(
        _decision(
            IntentType.CLIENT_TOOL,
            tool_name="run_command",
            raw_command='cd /Users/daniel/Downloads && yt-dlp "https://www.youtube.com/watch?v=nRuiacgrY0w"',
            onde="maquina",
        ),
        "faz para mim pela minha maquina",
    )
    assert d.intent == IntentType.CLIENT_TOOL
    assert d.tool_name == "run_command"
    assert d.raw_command
    assert not d.navegador_do_usuario


def test_cognitivo_com_onde_maquina_nao_ganha_aviso_de_navegador():
    d = _router()._bias_local_browser(
        _decision(IntentType.COGNITIVE, onde="maquina"), "faz para mim pela minha maquina"
    )
    assert not d.navegador_do_usuario


def test_cognitivo_com_onde_navegador_continua_com_aviso():
    d = _router()._bias_local_browser(
        _decision(IntentType.COGNITIVE, onde="navegador"), "toca um rock no meu macbook"
    )
    assert d.navegador_do_usuario


def test_trilho_do_servidor_continua_promovido_pela_maquina():
    """O motivo original da âncora: headless da VPS não é a máquina dele."""
    d = _router()._bias_local_browser(
        _decision(IntentType.SERVER_TOOL, tool_name="browser_task"), "toca um rock no meu macbook"
    )
    assert d.intent == IntentType.COGNITIVE


def test_pedido_de_navegador_explicito_continua_vencendo_o_run_command():
    d = _router()._bias_local_browser(
        _decision(IntentType.CLIENT_TOOL, tool_name="run_command", onde="maquina"),
        "abre isso no meu navegador local",
    )
    assert d.intent == IntentType.COGNITIVE
