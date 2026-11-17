"""Vídeo que já está no Mac não sobe para o catálogo só para virar botão (17/11).

Sessions b4f87e4d e 20fb0a92: o yt-dlp do Mac baixou o vídeo, e o cérebro fez
`request_user_file(mode=path, stage=false)` + `file_offer` — 3,8 MB e 12 MB
indo para a VPS para voltar como segunda cópia — em vez do `[[FILE:…]]`.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from core.cognitive import cognitive_tools_for
from modules.desktop.ai_service import ClientActuationRequested, _upload_so_para_entregar
from shared import ToolCall

from tests.test_cognitive_url_de_memoria_e_imagem import _Executor, _Provider, _Resp, _svc
from shared.types import Message, MessageRole

_VIDEO = "/Users/daniel/Downloads/Kelsea Ballerini - Yeah Boy (Official Music Video) [z30z8ZnM73U].mp4"
_CHAMADA_REAL = {
    "mode": "path",
    "path": _VIDEO,
    "reason": "Subir o vídeo baixado pra te entregar o botão de download no chat.",
    "stage": False,
}


def test_detector_pega_a_chamada_real():
    assert _upload_so_para_entregar("request_user_file", _CHAMADA_REAL) == _VIDEO


@pytest.mark.parametrize(
    "nome,args",
    [
        ("request_user_file", {**_CHAMADA_REAL, "stage": True}),  # vai processar
        ("request_user_file", {**_CHAMADA_REAL, "path": "/Users/daniel/Downloads/nota.pdf"}),
        ("request_user_file", {"mode": "picker"}),
        ("save_generated_file", {"file_id": "x"}),
    ],
)
def test_detector_deixa_o_resto(nome, args):
    assert _upload_so_para_entregar(nome, args) is None


async def _rodar(respostas, *, no_chat: bool):
    svc = _svc()
    provider = _Provider(respostas)
    await svc._run_cognitive_tool_loop(
        provider=provider,
        messages=[Message(role=MessageRole.USER, content="baixa o vídeo e me manda aqui")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=_Executor({}),
        slog=None, trace=None, arquivo_local_no_chat=no_chat,
    )
    return provider


def _pedido():
    return ToolCall(id="1", name="request_user_file", arguments=dict(_CHAMADA_REAL))


@pytest.mark.asyncio
async def test_primeira_chamada_nao_roda_e_o_modelo_recebe_o_aviso():
    provider = await _rodar([_Resp(tool_calls=[_pedido()]), _Resp(content="ok")], no_chat=True)
    aviso = provider.rodadas[-1][-1].content
    assert "NÃO rodou" in aviso
    assert f"[[FILE:{_VIDEO}]]" in aviso


@pytest.mark.asyncio
async def test_se_insistir_a_segunda_passa():
    with pytest.raises(ClientActuationRequested):
        await _rodar(
            [_Resp(tool_calls=[_pedido()]), _Resp(tool_calls=[_pedido()]), _Resp(content="x")],
            no_chat=True,
        )


@pytest.mark.asyncio
async def test_cliente_que_nao_desenha_o_cartao_nao_e_barrado():
    with pytest.raises(ClientActuationRequested):
        await _rodar([_Resp(tool_calls=[_pedido()]), _Resp(content="x")], no_chat=False)
