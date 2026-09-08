"""Num turno de música, quem atua é o bloco — não o navegador nem o terminal.

Medido em produção (07/09 e 06/09): com `browser_local_task`, `browser_task` e
`run_command` no toolset, o modelo escolhia a TOOL e não o bloco
[[HANDOFF:music]]. Resultado: "toca duality do slipknot" virou job dirigindo o
Edge passo a passo (26 s), "coloca uma musica do meu mix" virou Chromium
headless na VPS (morreu no anti-bot do YouTube) e "mutar essa aba" virou
`osascript` para o Chrome numa máquina que usa Edge.

O bloco chega na skill, que manda `media_command` para a extensão — instantâneo
e reusando a aba aberta. Este teste guarda a subtração; o contrato em texto
(_MUSIC_HANDOFF_INSTRUCTION) sozinho não segurou.
"""

from __future__ import annotations

from types import SimpleNamespace

from modules.desktop.ai_service import _drop_music_turn_temptations


def _tools(*nomes):
    return [SimpleNamespace(name=n) for n in nomes]


def _nomes(tools):
    return [t.name for t in tools]


def test_turno_de_musica_perde_navegador_e_terminal():
    tools = _tools("search_pks", "browser_local_task", "browser_task", "run_command", "get_weather")
    assert _nomes(_drop_music_turn_temptations(tools, "music", "toca slipknot")) == ["search_pks", "get_weather"]


def test_pedido_sobre_a_aba_tira_o_controle_da_maquina():
    """Mutar a ABA não é mutar o MacBook.

    Sessão 0565907e: "consegue mutar essa aba tocando esse video" chamou
    `device_control` e silenciou o computador inteiro (volume 81% → mudo) — e
    AINDA emitiu o handoff depois. Duas atuações, uma delas errada.
    """
    tools = _tools("device_control", "get_weather")
    ficou = _nomes(
        _drop_music_turn_temptations(tools, "music", "consegue mutar essa aba tocando esse video")
    )
    assert ficou == ["get_weather"]


def test_volume_sem_falar_de_aba_mantem_o_controle_da_maquina():
    """"abaixa o volume" pode ser a máquina mesmo — tirar a tool trocaria um
    erro por outro."""
    tools = _tools("device_control", "get_weather")
    assert "device_control" in _nomes(
        _drop_music_turn_temptations(tools, "music", "abaixa o volume")
    )


def test_turno_normal_nao_muda_nada():
    """A subtração é do turno de música — em qualquer outro o toolset é o mesmo."""
    tools = _tools("browser_local_task", "run_command", "get_weather")
    assert _nomes(_drop_music_turn_temptations(tools, None, "muta essa aba")) == _nomes(tools)
    assert _nomes(_drop_music_turn_temptations(tools, "home_assistant", "muta essa aba")) == _nomes(tools)


def test_tool_sem_nome_nao_quebra_a_montagem():
    """O toolset é montado por camadas; nem todo item tem `name` legível."""
    tools = [SimpleNamespace(), *_tools("run_command")]
    assert len(_drop_music_turn_temptations(tools, "music", "toca slipknot")) == 1
