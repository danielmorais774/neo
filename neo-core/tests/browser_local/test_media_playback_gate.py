"""Tocar mídia não tem casa no servidor (sessão 0939a295).

O pedido foi UM: "to de boa, coloca uma musica do meu mix para tocar ai". O
turno atuou DUAS vezes, por dois orquestradores diferentes:

  1. o núcleo cognitivo chamou `browser_local_task` com a URL do mix — e o
     portão REBAIXOU para `browser_task`, porque a frase tem a âncora de posse
     ("meu mix") mas nenhuma palavra de navegador e a herança da sessão (10min)
     tinha vencido havia 11 horas. Job 1998e5fb, kind=browser: rodou 5min38 num
     Chromium headless da VPS e morreu no "Sign in to confirm you're not a bot".
     Ninguém ouviria nada de qualquer jeito;
  2. o verificador de música (`_verify_music_decision`), que só arma quando o
     turno NÃO atuou no navegador do usuário, viu `browser_actuation` vazia —
     porque ela conta o nome PÓS-portão, e o pós-portão era `browser_task` —
     sintetizou o `[[HANDOFF:music]]` que o modelo não emitiu, e o desktop abriu
     OUTRO vídeo por `open_url`.

A guarda de dupla atuação (`_drop_music_handoff_after_browser`) já existia e
estava certa; ela só era cega porque o rebaixamento acontecia antes. Consertar o
verificador seria consertar o sintoma: o erro é o rebaixamento, que manda "dar
play" para o único lugar onde play não significa nada.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.jobs.executor import _NO_SERVER_PLAYBACK, JobToolExecutor


# A frase e a tarefa REAIS do turno, verbatim do log.
_FRASE = "to de boa, coloca uma musica do meu mix para tocar ai"
_TAREFA = {
    "task": (
        "Abrir a URL https://www.youtube.com/watch?v=c-g6X5nsbn8&list=RDc-g6X5nsbn8"
        "&start_radio=1 e dar play no vídeo. É um mix/radio de rock do YouTube."
    ),
    "title": "Tocar música do mix do Daniel",
}


class _FakeDB:
    def __init__(self, jobs=None) -> None:
        self.jobs = jobs or []
        self.criados = []

    async def list_agent_jobs(self, user_id, *, statuses=None, limit=20):
        return self.jobs

    async def create_agent_job(self, job):
        if getattr(job, "id", None) is None:
            job.id = uuid4()
        self.criados.append(job)
        return job


def _executor(message: str, *, device_id: str | None = "device-1"):
    return JobToolExecutor(
        db=_FakeDB(),
        job_manager=SimpleNamespace(poke=lambda: None),
        user_id=uuid4(),
        session_id=uuid4(),
        user_message=message,
        device_id=device_id,
    )


@pytest.fixture(autouse=True)
def _liga_o_local(monkeypatch):
    from shared.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "browser_local_enabled", True, raising=False)
    monkeypatch.setattr("shared.config.get_settings", lambda: settings)
    yield


# ── o caso real ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_o_turno_da_sessao_0939a295_vai_para_a_extensao():
    """O que o bug fazia: rebaixava. O que tem que acontecer: fica no local."""
    ex = _executor(_FRASE)
    assert await ex._gate_browser("browser_local_task", _TAREFA) == "browser_local_task"


@pytest.mark.asyncio
async def test_play_escolhido_no_servidor_e_promovido():
    """O cérebro pedindo o headless para tocar é sempre engano — promove."""
    ex = _executor(_FRASE)
    assert await ex._gate_browser("browser_task", _TAREFA) == "browser_local_task"


@pytest.mark.asyncio
async def test_a_tarefa_do_cerebro_basta_mesmo_com_a_frase_muda():
    """§5.2 só olhava a frase do usuário. Quem escreve "dar play" é o cérebro:
    "põe pra rodar" não tem verbo de reprodução nenhum, a tarefa tem."""
    ex = _executor("põe pra rodar")
    assert await ex._gate_browser("browser_task", _TAREFA) == "browser_local_task"


@pytest.mark.asyncio
async def test_a_frase_do_usuario_basta_mesmo_com_a_tarefa_muda():
    """E o contrário também: tarefa genérica, frase inequívoca."""
    ex = _executor(_FRASE)
    tarefa = {"task": "Abrir https://www.youtube.com/watch?v=c-g6X5nsbn8"}
    assert await ex._gate_browser("browser_local_task", tarefa) == "browser_local_task"


# ── sem navegador do usuário: recusa, nunca o headless ──────────────────────


@pytest.mark.asyncio
async def test_sem_device_recusa_em_vez_de_tocar_no_vazio():
    """WhatsApp/painel não identificam a máquina. Enfileirar no servidor seria
    repetir o 1998e5fb: 5min38 de nada. A recusa manda o cérebro usar o
    handoff de música, que toca de verdade."""
    ex = _executor(_FRASE, device_id=None)
    assert await ex._gate_browser("browser_local_task", _TAREFA) == _NO_SERVER_PLAYBACK


@pytest.mark.asyncio
async def test_flag_desligada_recusa_em_vez_de_rebaixar(monkeypatch):
    from shared.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "browser_local_enabled", False, raising=False)
    ex = _executor(_FRASE)
    assert await ex._gate_browser("browser_local_task", _TAREFA) == _NO_SERVER_PLAYBACK


@pytest.mark.asyncio
async def test_a_recusa_vira_resultado_que_ensina_o_caminho_certo():
    """A recusa não pode ser um erro seco: o cérebro tentaria outra vez. Ela diz
    qual é o canal que toca."""
    from shared import ToolCall

    ex = _executor(_FRASE, device_id=None)
    tr = await ex.execute_tool_call(
        ToolCall(id="c1", name="browser_local_task", arguments=_TAREFA)
    )
    assert tr.result["ok"] is False
    assert "[[HANDOFF:music]]" in tr.result["error"]
    # Nome preservado: quem lê o log precisa ver a tool que o modelo pediu.
    assert tr.name == "browser_local_task"


# ── o que NÃO pode mudar ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pesquisa_comum_fica_local_sem_janela():
    """A regra de duas partes segue valendo para tudo que não é play — mas o que
    protege a tela dele agora é o modo headless, não o rebaixamento. Play
    continua sendo o caso especial: sem tela e sem as caixas dele, ninguém ouve.
    """
    ex = _executor("acessa a amazon e acha uma promoção de iphone")
    tarefa = {"task": "Buscar promoção de iPhone na Amazon", "title": "Amazon 9.9"}
    assert await ex._gate_browser("browser_local_task", tarefa) == "browser_local_task"


@pytest.mark.asyncio
async def test_navegar_sem_tocar_nao_promove():
    """"não toque no botão" é instrução de navegação. Se contasse como play, uma
    compra guiada sequestraria o navegador do usuário."""
    ex = _executor("procura o preço do teclado e não toque no botão de comprar")
    tarefa = {"task": "Listar preços; não toque no botão de comprar"}
    assert await ex._gate_browser("browser_task", tarefa) == "browser_task"


@pytest.mark.asyncio
async def test_abrir_pagina_de_video_sem_play_nao_promove():
    """Ler metadado de um vídeo o headless faz bem — só a REPRODUÇÃO é que não
    existe lá."""
    ex = _executor("vê quantas visualizações tem esse vídeo")
    tarefa = {"task": "Abrir o vídeo no YouTube e ler o número de visualizações"}
    assert await ex._gate_browser("browser_task", tarefa) == "browser_task"


@pytest.mark.asyncio
async def test_tool_que_nao_e_de_browser_passa_intacta():
    ex = _executor(_FRASE)
    assert await ex._gate_browser("deep_research", _TAREFA) == "deep_research"


@pytest.mark.asyncio
async def test_chamada_antiga_sem_args_nao_quebra():
    """`args` é opcional: sem ele o portão volta a decidir só pela frase."""
    ex = _executor("no meu navegador, abre o github")
    assert await ex._gate_browser("browser_local_task") == "browser_local_task"


# ── a costura inteira: portão → sinal → guarda ──────────────────────────────
#
# As três peças já tinham teste separado e mesmo assim o bug passou, porque
# ninguém testava a EMENDA: o portão rebaixava, o sinal (que lê o nome
# pós-portão) não ligava, e a guarda — correta — nunca era consultada.


@pytest.mark.asyncio
async def test_o_job_criado_e_do_navegador_do_usuario():
    """O que o banco registrou no bug foi `kind=browser` (headless da VPS).
    Tem que ser `browser_local`, que é o que fala com a extensão."""
    from shared import ToolCall

    ex = _executor(_FRASE)
    tr = await ex.execute_tool_call(
        ToolCall(id="c1", name="browser_local_task", arguments=_TAREFA)
    )
    assert tr.result["ok"] is True
    assert ex.db.criados[-1].kind == "browser_local"
    assert tr.name == "browser_local_task"


@pytest.mark.asyncio
async def test_a_atuacao_local_desarma_o_verificador_de_musica():
    """O elo que faltava. Com o nome pós-portão certo, `browser_actuation`
    liga, a guarda derruba o handoff e o desktop NÃO recebe um segundo
    `open_url` com outra faixa."""
    from modules.desktop.ai_service import (
        _USER_BROWSER_TOOL,
        _drop_music_handoff_after_browser,
    )
    from core.pipeline.types import Handoff
    from shared import IntentType, ToolCall

    ex = _executor(_FRASE)
    tr = await ex.execute_tool_call(
        ToolCall(id="c1", name="browser_local_task", arguments=_TAREFA)
    )

    # Condição literal do loop cognitivo (ai_service, após execute_tool_call).
    atuou = bool(
        not tr.error
        and (getattr(tr, "name", None) or "") == _USER_BROWSER_TOOL
        and isinstance(tr.result, dict)
        and tr.result.get("ok")
    )
    assert atuou is True

    musica = Handoff(
        target_intent=IntentType.MUSIC,
        input_text="No Matter What - Papa Roach no youtube",
        target_skill="music",
    )
    assert _drop_music_handoff_after_browser([musica], atuou) == []
