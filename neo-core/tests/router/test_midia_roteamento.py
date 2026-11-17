"""Pedido sobre o CONTEÚDO de um vídeo/áudio vai ao cérebro (campo ``midia``).

Sessão e9947724 (16/11/2026): "Transcreve o vídeo ~/Downloads/superxandao-pichau.mp4
e me mostra o texto. Depois me manda a legenda .srt." saiu CLIENT_TOOL/run_command
com plano de 5 passos no Mac: ffmpeg → whisper (não instalado) → pip install
(barrado) → o turno morreu. A ``neo_transcrever`` mora no cérebro e nem foi vista.

E em 12/09 "faz a transcrição do vídeo 'Gravação de Tela…' ele é um bate-papo em
chat" queria OCR: "transcrição" serve para as duas coisas, por isso o modelo
rotula fala × tela e o código decide.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.router import intent_router as ir
from core.router.intent_router import IntentRouter, _midia
from core.tools.registry import ToolRegistry
from modules.desktop.ai_service import _AVISO_MIDIA, _aviso_midia
from modules.desktop.tools import DESKTOP_TOOLS
from shared import IntentType, RoutingDecision
from shared.types import PipelineStepSpec

PEDIDO_E9947724 = (
    "Transcreve o vídeo ~/Downloads/superxandao-pichau.mp4 e me mostra o texto. "
    "Depois me manda a legenda .srt."
)


def _router() -> IntentRouter:
    router = object.__new__(IntentRouter)
    registry = ToolRegistry()
    registry.register_many(DESKTOP_TOOLS)
    router.tool_registry = registry
    return router


@pytest.fixture
def com_transcricao(monkeypatch):
    from core.transcricao import config

    monkeypatch.setattr(config, "disponivel", lambda: True)


@pytest.fixture
def sem_transcricao(monkeypatch):
    from core.transcricao import config

    monkeypatch.setattr(config, "disponivel", lambda: False)


def _classificado(midia: str | None, **extra) -> RoutingDecision:
    """O que o classificador devolveu na e9947724, com o rótulo de mídia pedido."""
    dados = {
        "speech_act": "imperative",
        "intent": "CLIENT_TOOL",
        "confidence": 0.72,
        "tool_name": "run_command",
        "tool_arguments": {},
        "raw_command": "ls -la ~/Downloads/superxandao-pichau.mp4; which ffmpeg whisper",
        "needs_plan": True,
        "tipo_tarefa": "multi_etapas",
        "onde": "maquina",
        **extra,
    }
    if midia is not None:
        dados["midia"] = midia
    return _router()._parse_classification(json.dumps(dados), user_message=PEDIDO_E9947724)


def test_o_campo_e_lido_e_normalizado():
    assert _classificado("fala").midia == "fala"
    assert _classificado("Tela").midia == "tela"
    assert _classificado("música").midia is None
    assert _classificado(None).midia is None
    assert _midia("NENHUMA") == "nenhuma"


def test_fala_com_transcricao_vai_ao_cerebro_sem_o_plano_do_mac(com_transcricao):
    d = _router()._bias_midia(_classificado("fala"), PEDIDO_E9947724)
    assert d.intent == IntentType.COGNITIVE
    assert d.tool_name is None and d.raw_command is None and d.tool_arguments is None
    assert d.needs_plan is False
    assert d.midia == "fala"


def test_fala_sem_chave_do_groq_nao_desvia(sem_transcricao):
    d = _router()._bias_midia(_classificado("fala"), PEDIDO_E9947724)
    assert d.intent == IntentType.CLIENT_TOOL


def test_tela_vai_ao_cerebro_mesmo_sem_transcricao(sem_transcricao):
    msg = "faz a transcrição da gravação de tela do chat ~/Desktop/conversa.mov"
    d = _router()._bias_midia(_classificado("tela"), msg)
    assert d.intent == IntentType.COGNITIVE


@pytest.mark.parametrize("midia", ["tocar", "editar", "nenhuma"])
def test_o_resto_fica_como_o_classificador_decidiu(com_transcricao, midia):
    d = _router()._bias_midia(_classificado(midia), PEDIDO_E9947724)
    assert d.intent == IntentType.CLIENT_TOOL


def test_ja_no_cerebro_so_guarda_o_rotulo(com_transcricao):
    d = RoutingDecision(intent=IntentType.COGNITIVE, midia="fala")
    assert _router()._bias_midia(d, PEDIDO_E9947724) is d
    assert d.intent == IntentType.COGNITIVE


def test_sem_rotulo_a_rede_de_palavra_chave_pega_o_pedido(sem_transcricao):
    d = _router()._bias_midia(_classificado(None), PEDIDO_E9947724)
    assert d.intent == IntentType.COGNITIVE and d.midia == "conteudo"


@pytest.mark.parametrize(
    "mensagem",
    [
        "pausa o video para mim",
        "quando eu pesquiso aparece placa de video amd bc 250",
        "transcreve esse texto pro inglês",
        "legenda essa foto pro instagram",
        "baixa o audio do windows 10 connect usb e junta os dois mp3",
    ],
)
def test_rede_nao_pega_o_que_nao_e_transcricao_de_midia(sem_transcricao, mensagem):
    d = RoutingDecision(intent=IntentType.CLIENT_TOOL, tool_name="run_command", raw_command="ls")
    assert _router()._bias_midia(d, mensagem).intent == IntentType.CLIENT_TOOL
    assert d.midia is None


@pytest.mark.parametrize(
    "mensagem",
    [
        PEDIDO_E9947724,
        "faz a transcricao do video '/Users/daniel/Desktop/x/Gravação de Tela.mov'",
        "gera a legenda .srt dessa aula ~/Movies/aula.mkv",
        "transcrição da reunião gravada ~/Downloads/reuniao.m4a",
    ],
)
def test_rede_pega_transcricao_de_midia(sem_transcricao, mensagem):
    d = RoutingDecision(intent=IntentType.CLIENT_TOOL, tool_name="run_command", raw_command="ls")
    assert _router()._bias_midia(d, mensagem).intent == IntentType.COGNITIVE


def test_cascata_so_muda_o_passo_do_video(com_transcricao):
    d = _classificado("fala")
    d.pipeline_plan = [
        PipelineStepSpec(intent=IntentType.CLIENT_TOOL, input_text="transcrever o vídeo"),
        PipelineStepSpec(intent=IntentType.WHATSAPP_MESSAGING, input_text="mandar pra Ana",
                         skill_name="whatsapp_messaging"),
    ]
    d = _router()._bias_midia(d, PEDIDO_E9947724)
    assert [s.intent for s in d.pipeline_plan] == [IntentType.COGNITIVE, IntentType.WHATSAPP_MESSAGING]
    assert d.pipeline_plan[1].skill_name == "whatsapp_messaging"


def test_o_prompt_pede_o_campo_e_explica_fala_tela():
    p = ir.ROUTER_SYSTEM_PROMPT
    assert '"midia": "fala"|"tela"|"editar"|"tocar"|"nenhuma"' in p
    assert "mesmo se ele disser \"transcrever\"" in p


# ── o aviso no fim do prompt do cérebro ──────────────────────────────────────


def _tools(*nomes):
    return [SimpleNamespace(name=n) for n in nomes]


def test_aviso_de_fala_manda_trazer_do_disco_e_usar_a_tool():
    d = RoutingDecision(intent=IntentType.COGNITIVE, midia="fala")
    aviso = _aviso_midia(d, _tools("neo_bash", "neo_transcrever"))
    assert aviso == _AVISO_MIDIA["fala"]
    assert "request_user_file" in aviso and "neo_transcrever" in aviso


def test_aviso_de_fala_some_sem_a_tool():
    d = RoutingDecision(intent=IntentType.COGNITIVE, midia="fala")
    assert _aviso_midia(d, _tools("neo_bash")) is None


def test_aviso_de_tela_aponta_a_ocr_video():
    d = RoutingDecision(intent=IntentType.COGNITIVE, midia="tela")
    assert "ocr-video" in _aviso_midia(d, _tools("neo_transcrever"))


def test_conteudo_sem_a_tool_vira_aviso_de_tela():
    d = RoutingDecision(intent=IntentType.COGNITIVE, midia="conteudo")
    assert _aviso_midia(d, _tools("neo_transcrever")) == _AVISO_MIDIA["conteudo"]
    assert _aviso_midia(d, _tools()) == _AVISO_MIDIA["tela"]


@pytest.mark.parametrize("midia", [None, "tocar", "editar", "nenhuma"])
def test_sem_aviso_fora_de_fala_e_tela(midia):
    d = RoutingDecision(intent=IntentType.COGNITIVE, midia=midia)
    assert _aviso_midia(d, _tools("neo_transcrever")) is None


def test_sem_aviso_fora_do_cerebro():
    d = RoutingDecision(intent=IntentType.CLIENT_TOOL, midia="fala")
    assert _aviso_midia(d, _tools("neo_transcrever")) is None


def test_arquivo_de_midia_citado_nao_e_pergunta_referencial():
    """O atalho mandava "o que o cara fala nesse vídeo x.mov?" ao cérebro SEM rótulo."""
    assert IntentRouter._is_referential_question("o que o cara fala nesse vídeo ~/Movies/entrevista.mov?") is False
    assert IntentRouter._is_referential_question("quanto tempo tem nesse audio nota.m4a?") is False
    # o resto do atalho segue igual
    assert IntentRouter._is_referential_question("como tu chegou nessa conta de 22?") is True
    assert IntentRouter._is_referential_question("o que tem nesse vídeo que você mandou?") is True
