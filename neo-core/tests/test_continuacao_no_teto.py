"""Teto de rodadas sem entrega vira continuação (session-3b19d27a).

O pedido era "passa OCR na notinha do e-mail, põe numa planilha e me devolve o
arquivo". A nota estava lida na 10ª rodada; o turno gastou as outras relendo,
bateu o teto de 32 e o aviso final pediu "o que faltou". O modelo fechou com "me
dá um 'vai' e eu gero a planilha": pergunta ao usuário, que o verificador de
promessa não continua. O Daniel mandou "gera ai" e a planilha saiu em 33 s.

Estes testes fixam:

* o loop diz ao chamador que parou no teto (e só no teto, não na repetição);
* com a continuação ligada, a rodada final pede só o status, sem pergunta;
* a continuação nasce no código só sem arquivo entregue e sem job aberto;
* a ordem da continuação não cai na guarda de quase-repetição do orquestrador.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from core.cognitive import cognitive_tools_for
from core.pipeline.types import order_is_near_repeat
from modules.desktop.ai_service import (
    AIService,
    _AVISO_FIM_DAS_RODADAS,
    _AVISO_FIM_DAS_RODADAS_CONTINUA,
    _ORDEM_DO_TETO,
    _continuacao_do_teto,
    _e_continuacao_do_teto,
)
from shared import ToolCall, ToolResult
from shared.types import IntentType, Message, MessageRole

_PEDIDO = (
    "te encaminhei um email com a notinha de supermercado quero que passe um OCR "
    "nela, coloque em uma planilha e me retorne o arquivo para eu baixar."
)


class _Resp:
    def __init__(self, content="", tool_calls=None):
        self._content = content
        self._tc = tool_calls or []

    def get_content(self):
        return self._content

    def get_tool_calls(self):
        return self._tc


class _Provider:
    def __init__(self, responses):
        self._responses = list(responses)
        self.rodadas: list[list[Message]] = []

    async def complete(self, *, messages, tools=None, **kwargs):
        self.rodadas.append(list(messages))
        return self._responses.pop(0)


class _Executor:
    async def execute_tool_call(self, tool_call):
        return ToolResult(
            tool_call_id=tool_call.id, name=tool_call.name, result="saída",
            execution_time=0.0,
        )


def _svc(max_rounds=1):
    svc = object.__new__(AIService)

    class _S:
        cognitive_max_tool_rounds = max_rounds
        default_max_tokens = 1024
        cognitive_stream_enabled = False
        cognitive_reasoning_enabled = False

    svc.settings = _S()
    return svc


def _chamada(cid: str, consulta: str = "notinha") -> ToolCall:
    return ToolCall(id=cid, name="search_pks", arguments={"query": consulta})


async def _rodar(svc, provider, **kwargs):
    return await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content=_PEDIDO)],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=_Executor(), slog=None, trace=None,
        **kwargs,
    )


# ── o loop ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_teto_com_continuacao_pede_so_o_status():
    fim: dict = {}
    provider = _Provider([
        _Resp(tool_calls=[_chamada("1")]),
        _Resp(content="Li a nota inteira; falta montar a planilha."),
    ])
    out = await _rodar(_svc(), provider, continuar_no_teto=True, fim_do_loop=fim)
    assert out.startswith("Li a nota")
    assert fim == {"teto": True}
    ultima = provider.rodadas[-1][-1]
    assert ultima.role == MessageRole.SYSTEM
    assert ultima.content == _AVISO_FIM_DAS_RODADAS_CONTINUA
    assert "Não pergunte nada ao usuário" in ultima.content


@pytest.mark.asyncio
async def test_teto_sem_continuacao_mantem_o_aviso_antigo():
    fim: dict = {}
    provider = _Provider([
        _Resp(tool_calls=[_chamada("1")]),
        _Resp(content="Parei no meio."),
    ])
    await _rodar(_svc(), provider, fim_do_loop=fim)
    assert fim == {"teto": True}
    assert provider.rodadas[-1][-1].content == _AVISO_FIM_DAS_RODADAS


@pytest.mark.asyncio
async def test_teto_com_arquivo_entregue_mantem_o_aviso_antigo():
    """Já entregou: não vai continuar, então o aviso não pode prometer isso."""
    fim: dict = {}
    arquivos = [{"file_id": "x", "name": "notinha.xlsx", "size_bytes": 10}]
    provider = _Provider([
        _Resp(tool_calls=[_chamada("1")]),
        _Resp(content="Planilha pronta."),
    ])
    await _rodar(
        _svc(), provider, continuar_no_teto=True, fim_do_loop=fim, produced_files=arquivos,
    )
    assert fim == {"teto": True}
    assert provider.rodadas[-1][-1].content == _AVISO_FIM_DAS_RODADAS


@pytest.mark.asyncio
async def test_parada_por_repeticao_nao_marca_teto():
    """Repetição é o modelo travado: continuar repetiria a trava."""
    fim: dict = {}
    provider = _Provider([
        _Resp(tool_calls=[_chamada("1")]),
        _Resp(tool_calls=[_chamada("2")]),
        _Resp(tool_calls=[_chamada("3")]),
        _Resp(tool_calls=[_chamada("4")]),
        _Resp(content="Travei."),
    ])
    await _rodar(_svc(max_rounds=32), provider, continuar_no_teto=True, fim_do_loop=fim)
    assert fim == {}
    assert "MESMA chamada" in provider.rodadas[-1][-1].content


@pytest.mark.asyncio
async def test_turno_que_responde_antes_do_teto_nao_marca_nada():
    fim: dict = {}
    provider = _Provider([
        _Resp(tool_calls=[_chamada("1")]),
        _Resp(content="Pronto."),
    ])
    assert await _rodar(_svc(max_rounds=5), provider, continuar_no_teto=True, fim_do_loop=fim) == "Pronto."
    assert fim == {}


# ── a decisão de continuar ──────────────────────────────────────────────────

def test_teto_sem_entrega_cria_a_continuacao():
    h = _continuacao_do_teto({"teto": True}, ligado=True, arquivos=[], tool_trace=[])
    assert h is not None
    assert h.is_self is True
    assert h.target_intent == IntentType.DIRECT_RESPONSE
    assert h.input_text == _ORDEM_DO_TETO
    assert h.reason == "teto_de_rodadas"


@pytest.mark.parametrize(
    "fim, ligado, arquivos, trace",
    [
        ({}, True, [], []),                                         # não bateu o teto
        ({"teto": True}, False, [], []),                            # desligado
        ({"teto": True}, True, [{"file_id": "x", "name": "a.xlsx"}], []),  # já entregou
        ({"teto": True}, True, [], [{"async_job": True, "job_id": "j1", "tool": "neo_bash"}]),
    ],
    ids=["sem-teto", "desligado", "ja-entregou", "job-aberto"],
)
def test_sem_continuacao(fim, ligado, arquivos, trace):
    assert _continuacao_do_teto(fim, ligado=ligado, arquivos=arquivos, tool_trace=trace) is None


def test_fim_do_loop_ausente_nao_continua():
    assert _continuacao_do_teto(None, ligado=True, arquivos=[], tool_trace=[]) is None


# ── a ordem ─────────────────────────────────────────────────────────────────

def test_a_ordem_se_reconhece_como_continuacao_do_teto():
    assert _e_continuacao_do_teto(_ORDEM_DO_TETO)
    assert _e_continuacao_do_teto("  " + _ORDEM_DO_TETO)
    assert not _e_continuacao_do_teto(_PEDIDO)
    assert not _e_continuacao_do_teto("")
    assert not _e_continuacao_do_teto(None)


def test_a_ordem_nao_e_quase_repeticao_do_pedido():
    """O orquestrador compara cada ordem do ciclo com as que já rodaram."""
    assert order_is_near_repeat(_ORDEM_DO_TETO, [_PEDIDO]) is None
