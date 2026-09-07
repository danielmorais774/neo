"""Dupla atuação de música num turno só (sessão a691f1e0).

O pedido foi um: "abre uma aba no meu navegador com uma playlist de rock e
coloca para tocar no meu macbook". Ele casou com DUAS regras obrigatórias que
não se conheciam:

  - "no meu navegador"  → regra 14 do system prompt: CHAME `browser_local_task`
    no MESMO turno, nunca diga que vai abrir sem chamar;
  - "coloca para tocar" → `skill_hint="music"`: TOCAR sempre exige o bloco
    [[HANDOFF:music]], nunca diga que tocou sem emitir.

O modelo obedeceu às duas e o turno atuou duas vezes: o agente de navegador
abriu uma aba nova com uma playlist ("CLASSIC ROCK Music Videos") e a skill de
música mandou a extensão tocar OUTRO vídeo (`5ZCB1ty7qBU`) numa segunda aba.
Duas playlists de rock tocando juntas.

O contrato de música só sabia checar se FALTAVA handoff — e o verificador ainda
sintetizava um quando faltava. Nunca perguntava se o turno já tinha atuado por
outro canal.
"""

from __future__ import annotations

import pytest

from core.pipeline.types import Handoff
from shared import IntentType
from modules.desktop.ai_service import (
    _USER_BROWSER_TOOL,
    _drop_music_handoff_after_browser,
)


def _music() -> Handoff:
    return Handoff(
        target_intent=IntentType.MUSIC,
        input_text="playlist rock clássico anos 80 YouTube",
        target_skill="music",
    )


def _casa() -> Handoff:
    return Handoff(
        target_intent=IntentType.HOME_ASSISTANT,
        input_text="apaga a luz do quarto",
        target_skill="home_assistant",
    )


# ── a guarda ────────────────────────────────────────────────────────────────

def test_aba_ja_aberta_no_navegador_descarta_o_handoff_de_musica():
    """O caso real: browser_local_task já abriu a aba tocando."""
    assert _drop_music_handoff_after_browser([_music()], True) == []


def test_sem_atuacao_no_navegador_o_handoff_sobrevive():
    """Turno de música puro ("toca AC/DC") continua tocando — este é o caminho
    normal, e quebrá-lo seria trocar duas abas por nenhuma."""
    handoffs = [_music()]
    assert _drop_music_handoff_after_browser(handoffs, False) == handoffs


def test_handoff_de_outra_skill_nao_e_atingido():
    """A trava é sobre a atuação DUPLICADA (navegador × música), não sobre
    handoff em geral: apagar a luz no mesmo turno segue valendo."""
    kept = _drop_music_handoff_after_browser([_casa(), _music()], True)
    assert [h.target_skill for h in kept] == ["home_assistant"]


def test_handoff_de_musica_sem_target_skill_tambem_cai():
    """`extract_handoffs` pode devolver o intent sem o nome da skill; a guarda
    olha os dois — senão o descarte passa reto e a segunda aba volta."""
    h = Handoff(target_intent=IntentType.MUSIC, input_text="AC/DC", target_skill=None)
    assert _drop_music_handoff_after_browser([h], True) == []


def test_so_o_navegador_do_usuario_conta():
    """`browser_task` roda escondida no servidor e não abre aba nenhuma na tela
    dele: se ela contasse, um turno que só pesquisou na web perderia a música."""
    assert _USER_BROWSER_TOOL == "browser_local_task"


# ── o sinal que alimenta a guarda ───────────────────────────────────────────
#
# `browser_actuation` é preenchido no loop cognitivo com o nome PÓS-portão e só
# quando a ordem foi aceita. Os dois casos abaixo são exatamente os que um
# `used_tools & {"browser_local_task"}` erraria — e errar aqui é pior que o bug
# original: o usuário pede música e não toca NADA.

class _ToolResultFake:
    def __init__(self, name, result, error=""):
        self.name = name
        self.result = result
        self.error = error


def _atuou(tr) -> bool:
    """Mesma condição do loop cognitivo (ai_service, após execute_tool_call)."""
    return bool(
        not tr.error
        and (getattr(tr, "name", None) or "") == _USER_BROWSER_TOOL
        and isinstance(tr.result, dict)
        and tr.result.get("ok")
    )


def test_sinal_liga_quando_o_job_local_foi_aceito():
    tr = _ToolResultFake(
        "browser_local_task",
        {"ok": True, "job_id": "cfeeb987-e3d0-4942-9d90-fb5dce5c37dd", "status": "queued"},
    )
    assert _atuou(tr) is True


def test_portao_rebaixou_para_o_servidor_nao_conta():
    """O modelo escolheu `browser_local_task`, o portão rodou `browser_task`
    (usuário não pediu a tela dele). Nada abriu na cara dele → a música vale."""
    tr = _ToolResultFake("browser_task", {"ok": True, "job_id": "x", "status": "queued"})
    assert _atuou(tr) is False


def test_chamada_local_que_falhou_nao_conta():
    """`{'ok': False}` é o que volta sem device_id ou com o local desligado."""
    tr = _ToolResultFake("browser_local_task", {"ok": False, "error": "não sei em qual máquina abrir"})
    assert _atuou(tr) is False


# ── o tabId da decisão viaja até a extensão ─────────────────────────────────

class _VideoFake:
    id = "5ZCB1ty7qBU"
    title = "Classic Rock Hits"
    channel = "Rock"
    label = "Classic Rock Hits (YouTube)"
    watch_url = "https://www.youtube.com/watch?v=5ZCB1ty7qBU"


class _YouTubeFake:
    is_configured = True

    async def search_video(self, query: str):
        return _VideoFake()


def _pipeline():
    from core.skills.music.pipeline import MusicPipeline

    p = MusicPipeline(agent_executor=None, db=None, broker=None)
    p.youtube = _YouTubeFake()
    return p


# As abas que estavam abertas na sessão a691f1e0. A extensão escolhia sozinha a
# PRIMEIRA que casasse com youtube.com — nem sempre a mesma sobre a qual o
# neo-core decidiu o reuso.
ABAS = {
    "browser_tabs": [
        {"site": "youtube", "tabId": 1976597174, "url": "https://www.youtube.com/watch?v=09y8THtoqJc", "playing": False, "kind": "watch", "hasMedia": True},
        {"site": "youtube", "tabId": 1976597316, "url": "https://www.youtube.com/", "playing": False, "kind": "home", "hasMedia": False},
    ]
}


@pytest.mark.asyncio
async def test_play_manda_o_tabid_da_aba_escolhida():
    """O ponto original segue de pé: a decisão de QUAL aba é do neo-core e viaja
    no `tabId`. O que mudou é a escolha — a aba de vídeo do usuário (parada, mas
    dele) não é mais candidata; a home é."""
    res = await _pipeline()._play_youtube("rock", ABAS, available=True)
    args = res.extra["actuation"]["args"]
    assert args["reuseTab"] is True
    assert args["tabId"] == 1976597316


def test_controle_manda_o_tabid_da_aba_escolhida():
    res = _pipeline()._control("pause", ABAS)
    assert res.extra["actuation"]["args"]["tabId"] == 1976597174


@pytest.mark.asyncio
async def test_sem_aba_aberta_nao_ha_tabid_e_abre_url():
    """Nada aberto → `open_url`, o caminho que não passa pela extensão."""
    res = await _pipeline()._play_youtube("rock", {"browser_tabs": []}, available=True)
    act = res.extra["actuation"]
    assert act["tool"] == "open_url"
    assert "tabId" not in act["args"]
