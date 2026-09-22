"""Agente do SERVER_TOOL: o que a session-d35696d4 (2026-09-21) mostrou.

"faz um select em todas as tabelas e colunas" → 5 consultas SQL a 31-61s por
ida ao modelo, e o turno morreu em "Timeout: execução excedeu 300s." com o log
dizendo que o agente "não chamou ferramenta nenhuma". Três defeitos, testados
pelo COMPORTAMENTO (o teste anterior conferia a string ``"iteration == 0"`` no
código-fonte e por isso aprovava justamente o bug):

1. ``force_tool_first`` nunca disparava (o laço incrementa antes de testar 0);
2. o raciocínio ficava ligado em todo round (só o round forçado desligava);
3. o prazo era de relógio e o corte jogava fora os passos já feitos.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.agent.agent_executor import AgentExecutor
from core.liveness import TurnLiveness, bind_liveness
from shared import ToolCall
from shared.types import ToolResult, ToolSchema


class _Resp:
    def __init__(self, content: str | None = None, tool_calls: list[ToolCall] | None = None):
        self._content = content
        self._calls = tool_calls or []

    def get_content(self) -> str | None:
        return self._content

    def get_tool_calls(self) -> list[ToolCall]:
        return self._calls


class _Provider:
    """Devolve as respostas roteirizadas; ``delays[i]`` segura a i-ésima chamada."""

    def __init__(self, script: list[_Resp], delays: list[float] | None = None):
        self.script = list(script)
        self.delays = list(delays or [])
        self.calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> _Resp:
        i = len(self.calls)
        self.calls.append(kwargs)
        if i < len(self.delays) and self.delays[i]:
            await asyncio.sleep(self.delays[i])
        return self.script[i]


class _Router:
    def __init__(self, provider: _Provider):
        self.provider = provider

    def get_provider(self, _name: str | None = None) -> _Provider:
        return self.provider

    def get_default_model(self, _name: str | None = None) -> str:
        return "fake-model"

    def list_available_providers(self) -> list[str]:
        return ["fake"]


class _Tools:
    def __init__(self, delay: float = 0.0):
        self.delay = delay

    async def execute_tool_call(self, tc: ToolCall) -> ToolResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        return ToolResult(
            tool_call_id=tc.id, name=tc.name, result=f"linhas de {tc.arguments['q']}", execution_time=0.0
        )


_SQL = ToolSchema(name="sql_query_ro", description="select", parameters={}, module="sqlro")


def _call(q: str) -> _Resp:
    return _Resp(tool_calls=[ToolCall(name="sql_query_ro", arguments={"q": q})])


def _agent(provider: _Provider, tools: _Tools, *, stall: float = 30, reasoning: bool = False) -> AgentExecutor:
    agent = AgentExecutor(llm_router=_Router(provider), tool_registry=None, tool_executor=tools)
    agent.settings = agent.settings.model_copy(
        update={"agent_stall_timeout": stall, "cognitive_reasoning_enabled": reasoning}
    )
    return agent


async def _run(agent: AgentExecutor, **kw: Any) -> dict[str, Any]:
    return await agent.execute(
        task="faz um select", provider="fake", model="fake-model", available_tools=[_SQL], **kw
    )


# ── 1 + 2: o round forçado existe, e nenhum round raciocina ─────────────────


@pytest.mark.asyncio
async def test_primeiro_round_e_forcado_e_os_seguintes_nao():
    prov = _Provider([_call("tabelas"), _Resp(content="pronto")])
    result = await _run(_agent(prov, _Tools()), force_tool_first=True)

    assert result["status"] == "completed"
    primeiro, segundo = prov.calls
    assert primeiro.get("tool_choice") == "required"
    assert primeiro["disable_thinking"] is True
    # Depois do primeiro o agente precisa poder CONCLUIR em texto.
    assert "tool_choice" not in segundo


@pytest.mark.asyncio
async def test_sem_forcamento_nenhum_round_forca():
    prov = _Provider([_call("tabelas"), _Resp(content="pronto")])
    await _run(_agent(prov, _Tools()))
    assert all("tool_choice" not in c for c in prov.calls)


@pytest.mark.asyncio
async def test_raciocinio_segue_a_flag_do_cognitivo_em_todo_round():
    prov = _Provider([_call("a"), _call("b"), _Resp(content="pronto")])
    await _run(_agent(prov, _Tools(), reasoning=False))
    assert [c["disable_thinking"] for c in prov.calls] == [True, True, True]

    prov = _Provider([_call("a"), _Resp(content="pronto")])
    await _run(_agent(prov, _Tools(), reasoning=True))
    assert [c["disable_thinking"] for c in prov.calls] == [False, False]


@pytest.mark.asyncio
async def test_resultado_diz_qual_modelo_rodou():
    prov = _Provider([_Resp(content="oi")])
    result = await _run(_agent(prov, _Tools()))
    assert result["metadata"]["provider"] == "fake"
    assert result["metadata"]["model"] == "fake-model"


# ── 3: corte por INATIVIDADE, sem perder o que já foi apurado ───────────────


@pytest.mark.asyncio
async def test_lento_mas_andando_nao_e_cortado():
    """Cada ida ao modelo cabe na janela; o total passa dela com folga — era
    exatamente o caso que o relógio de 300s matava."""
    prov = _Provider(
        [_call("a"), _call("b"), _call("c"), _Resp(content="achei")],
        delays=[0.12, 0.12, 0.12, 0.12],
    )
    agent = _agent(prov, _Tools())
    agent.settings = agent.settings.model_copy(update={"agent_stall_timeout": 0.3})

    result = await _run(agent)

    assert result["status"] == "completed"
    assert result["final_response"] == "achei"


@pytest.mark.asyncio
async def test_parado_e_cortado_e_os_passos_sobrevivem():
    prov = _Provider(
        [_call("tabelas"), _call("colunas"), _Resp(content="nunca chega")],
        delays=[0, 0, 5.0],
    )
    agent = _agent(prov, _Tools())
    agent.settings = agent.settings.model_copy(update={"agent_stall_timeout": 0.2})

    result = await _run(agent)

    assert result["status"] == "timeout"
    assert len(result["steps"]) == 2, "o trabalho feito antes do travamento não pode sumir"
    assert result["iterations"] == 2
    assert result["metadata"]["stalled_in"] == "esperando a resposta do modelo"
    texto = result["final_response"]
    assert "sem avançar" in texto
    assert "sql_query_ro ×2" in texto
    assert "linhas de colunas" in texto, "o último resultado vai no texto para o turno seguinte partir dele"


@pytest.mark.asyncio
async def test_travado_sem_nenhuma_tool_diz_isso():
    prov = _Provider([_Resp(content="nunca")], delays=[5.0])
    agent = _agent(prov, _Tools())
    agent.settings = agent.settings.model_copy(update={"agent_stall_timeout": 0.1})

    result = await _run(agent)

    assert result["status"] == "timeout"
    assert result["steps"] == []
    assert "Nenhuma ferramenta" in result["final_response"]


@pytest.mark.asyncio
async def test_progresso_do_turno_conta_como_avanco():
    """Uma tool longa que avança (o probe do sandbox toca o TurnLiveness do
    turno) não pode ser cortada só porque o loop do agente está esperando."""
    liveness = TurnLiveness()
    bind_liveness(liveness)

    async def _batendo() -> None:
        for _ in range(10):
            await asyncio.sleep(0.05)
            liveness.touch()

    prov = _Provider([_call("longa"), _Resp(content="ok")])
    agent = _agent(prov, _Tools(delay=0.5))
    agent.settings = agent.settings.model_copy(update={"agent_stall_timeout": 0.2})

    batedor = asyncio.create_task(_batendo())
    result = await _run(agent)
    await batedor

    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_avanco_do_agente_mantem_o_turno_vivo():
    """Cada passo do agente toca o TurnLiveness: é isso que mantém o keepalive
    do gRPC saindo (sem ele o desktop desiste aos 240s+90s)."""
    liveness = TurnLiveness()
    bind_liveness(liveness)
    prov = _Provider([_call("a"), _Resp(content="ok")], delays=[0.2, 0])

    await _run(_agent(prov, _Tools()))

    assert liveness.idle_seconds() < 0.15
