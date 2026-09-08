"""Redes sobre a SAÍDA do classificador — o que sobra quando a frase escapa.

Enumerar frase é perder: em três sessões seguidas (ace000b8, 7687b737,
0565907e) o classificador produziu a MESMA improvisação para mexer no som do
navegador do usuário —

    osascript -e 'tell application "Google Chrome" to set volume of tab 1 …'

— por três caminhos diferentes ("consegue mutar essa aba", "essa do podcast",
"usar a extensão de bridge"). O comando nunca funciona (o Chrome não expõe
volume por AppleScript, e a máquina usa Edge) e quem controla a aba é a
extensão. Então a rede olha o COMANDO que saiu, não a frase que entrou.

A segunda rede é irmã: `browser_screenshot` tira print do navegador DO
SERVIDOR, que não responde nada sobre a mídia do usuário. Em 0565907e isso virou
"Tô vendo nada aqui do meu lado não — seu navegador não transmite tela pra mim",
com as abas dele no `media_context` daquele mesmo turno.
"""

from __future__ import annotations

import json

import pytest

from core.router.intent_router import IntentRouter
from shared.types import IntentType


def _router():
    r = IntentRouter.__new__(IntentRouter)   # sem I/O: só o parse da saída
    r.tool_registry = None
    return r


def _decide(payload: dict, mensagem: str):
    return _router()._parse_classification(json.dumps(payload), user_message=mensagem)


# O comando verbatim do log da sessão 0565907e.
_OSASCRIPT = (
    "osascript -e 'tell application \"Google Chrome\" to set volume of tab 1 "
    "of window 1 to 0'"
)


@pytest.mark.parametrize(
    "comando",
    [
        _OSASCRIPT,
        "osascript -e 'tell application \"Microsoft Edge\" to mute tab 1'",
        "pkill -STOP chrome && echo mudo",  # 'mudo' + navegador
    ],
)
def test_comando_de_audio_de_navegador_vira_musica(comando):
    d = _decide(
        {"intent": "CLIENT_TOOL", "tool_name": "run_command", "raw_command": comando},
        "essa do podcast",
    )
    assert d.intent == IntentType.DIRECT_RESPONSE
    assert d.skill_hint == "music"
    assert d.raw_command is None  # o comando improvisado morre aqui


def test_comando_de_shell_normal_passa_intacto():
    """A rede é estreita de propósito: só comando que mexe em áudio de navegador."""
    d = _decide(
        {"intent": "CLIENT_TOOL", "tool_name": "run_command", "raw_command": "df -h"},
        "quanto de disco livre",
    )
    assert d.intent == IntentType.CLIENT_TOOL
    assert d.tool_name == "run_command"


def test_abrir_midia_dirigindo_o_navegador_volta_pro_cognitivo():
    """Interação 6 da 0565907e — a resposta ao pedido "usar a extensão de bridge".

    Sem palavra de volume, a rede de áudio não pega; e continua sendo AppleScript
    para o Chrome numa máquina que usa Edge. Aqui NÃO se força música (pode ser
    pedido legítimo de abrir uma página): mata-se o comando e o cognitivo decide
    com as tools que ele tem.
    """
    cmd = (
        "osascript -e 'tell application \"Google Chrome\" to activate' "
        "-e 'tell application \"Google Chrome\" to set URL of active tab of "
        "window 1 to \"https://www.youtube.com/watch?v=dWnToSthUfQ\"'"
    )
    d = _decide(
        {"intent": "CLIENT_TOOL", "tool_name": "run_command", "raw_command": cmd},
        "usar a extensão de bridge",
    )
    assert d.intent == IntentType.DIRECT_RESPONSE
    assert d.skill_hint is None      # não é ordem de tocar; o cérebro escolhe
    assert d.raw_command is None


def test_print_do_servidor_nao_responde_pergunta_sobre_a_midia_do_usuario():
    d = _decide(
        {"intent": "SERVER_TOOL", "tool_name": "browser_screenshot",
         "tool_arguments": {"session": "default"}},
        "ta vendo o video aberto no YouTube?",
    )
    assert d.intent == IntentType.DIRECT_RESPONSE


def test_print_do_navegador_do_servidor_continua_valendo_para_o_job():
    """Sem âncora de mídia do usuário, o print do job segue sendo o certo."""
    d = _decide(
        {"intent": "SERVER_TOOL", "tool_name": "browser_screenshot",
         "tool_arguments": {"session": "default"}},
        "tira um print da pagina que voce abriu",
    )
    assert d.intent == IntentType.SERVER_TOOL
    assert d.tool_name == "browser_screenshot"
