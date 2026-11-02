"""`solicitar_dado` no trilho do SERVER_TOOL (sessão 1a605544, 2026-11-01).

O agente desse trilho fala ``ToolCall`` → ``ToolResult``; a tool só existia no
despacho inline do cognitivo. O adaptador é o que faz o pedido nascer de verdade
em vez de ser narrado.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from modules.release import ReleaseToolExecutor
from shared import ToolCall


class _Servico:
    def __init__(self):
        self.criados: list[dict] = []

    async def create(self, **kwargs):
        self.criados.append(kwargs)
        return SimpleNamespace(
            id=str(uuid4()), status="pendente", campos=kwargs["campos"],
            destino=kwargs["destino"], expira_em=None,
        )


def _call(**args) -> ToolCall:
    return ToolCall(id="t1", name="solicitar_dado", arguments=args)


@pytest.mark.asyncio
async def test_o_pedido_nasce_pelo_dialeto_do_agente():
    servico = _Servico()
    ex = ReleaseToolExecutor(servico)
    ex.set_context(uuid4(), uuid4())
    resultado = await ex.execute_tool_call(
        _call(campos=["cep"], destino="loja.com.br", destino_tipo="dominio",
              finalidade="calcular o frete")
    )
    assert resultado.error is None and resultado.result["ok"] is True
    assert servico.criados[0]["campos"] == ["pks:cep"]


@pytest.mark.asyncio
async def test_sem_usuario_o_erro_volta_como_dado_e_nao_derruba_o_agente():
    ex = ReleaseToolExecutor(_Servico())
    resultado = await ex.execute_tool_call(
        _call(campos=["cep"], destino="loja.com.br", destino_tipo="dominio", finalidade="x")
    )
    assert resultado.result is None and "usuário" in (resultado.error or "")
