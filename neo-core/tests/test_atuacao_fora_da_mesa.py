"""Atuação no Mac chamada pelo nome num turno sem ninguém na frente (session-0bdca023).

A continuação do job 4a5c6069 roda `unattended`, sem as tools client-side na mesa.
O modelo chamou `request_user_file` assim mesmo (o nome estava no texto do aviso)
e o loop estacionou o turno à espera de um desktop que não estava conectado: o
Daniel nunca recebeu a planilha nem o cartão.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from core.cognitive import cognitive_tools_for
from modules.desktop.tools import DESKTOP_TOOLS
from modules.desktop.ai_service import ClientActuationRequested
from shared import ToolCall
from shared.types import Message, MessageRole

from tests.test_cognitive_url_de_memoria_e_imagem import _Executor, _Provider, _Resp, _svc

_XLSX = "/Users/daniel/Neo/claude/4a5c6069/top10_paises_area_populacao.xlsx"


def _pedido():
    return ToolCall(
        id="1", name="request_user_file", arguments={"mode": "path", "stage": True, "path": _XLSX}
    )


async def _rodar(respostas, tools):
    provider = _Provider(respostas)
    await _svc()._run_cognitive_tool_loop(
        provider=provider,
        messages=[Message(role=MessageRole.USER, content="[Continuação automática] entregue o xlsx")],
        req_model=None, model_name="m", provider_name="p",
        tools=tools, tool_executor=_Executor({}),
        slog=None, trace=None,
    )
    return provider


@pytest.mark.asyncio
async def test_fora_da_mesa_nao_estaciona_e_manda_o_cartao():
    mesa = [t for t in cognitive_tools_for(uuid4()) + DESKTOP_TOOLS if getattr(t, "execution", "") != "client-side"]
    assert not any(t.name == "request_user_file" for t in mesa)
    provider = await _rodar([_Resp(tool_calls=[_pedido()]), _Resp(content=f"[[FILE:{_XLSX}]]")], mesa)
    aviso = provider.rodadas[-1][-1].content
    assert "NÃO está disponível neste turno" in aviso
    assert "[[FILE:<caminho absoluto>]]" in aviso


@pytest.mark.asyncio
async def test_na_mesa_continua_estacionando_no_cliente():
    with pytest.raises(ClientActuationRequested):
        await _rodar([_Resp(tool_calls=[_pedido()]), _Resp(content="x")], cognitive_tools_for(uuid4()) + DESKTOP_TOOLS)
