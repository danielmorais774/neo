"""A linha de progresso leva a tool que anuncia (F5 do chat em três colunas).

O painel "Agentes e canais" do desktop precisa saber QUEM está trabalhando. Até
aqui o core mandava só a frase pronta ("olhando a caixa de email"), e o desktop
casava a frase numa tabela copiada do `_TOOL_PROGRESS_LABEL` — frágil, e cego no
trilho do servidor: o agente do SERVER_TOOL não mandava progresso nenhum, só uma
linha "Executando ferramenta no servidor: email_inbox..." no começo
(session-0e232e1b, 20/11/2026). docs/desktop-chat-tres-colunas-plan.md, F5.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from core.agent.agent_executor import AgentExecutor
from grpc_server import NeoDesktopServicer
from modules.desktop.ai_service import (
    CYCLE_PROGRESS_MARKER,
    AIService,
    AIServiceResponse,
    _LoopRun,
)
from shared import IntentType, StreamChunk, StreamEventType, ToolCall
from shared.config import get_settings
from shared.types import RoutingDecision, ToolResult, ToolSchema

# ── a linha em si ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_linha_de_tool_continua_sendo_o_texto_e_sabe_a_tool():
    fila: asyncio.Queue[str] = asyncio.Queue()
    await AIService._emit_progress(fila, "  olhando a caixa de email\n", tool_name="email_inbox")
    linha = fila.get_nowait()
    # Quem só lê texto (a bolha, os testes antigos) não percebe diferença.
    assert linha == "olhando a caixa de email"
    assert linha.tool_name == "email_inbox"


@pytest.mark.asyncio
async def test_texto_do_modelo_nao_tem_tool():
    fila: asyncio.Queue[str] = asyncio.Queue()
    await AIService._emit_progress(fila, "Vou olhar a sua caixa")
    assert fila.get_nowait().tool_name == ""


# ── o loop cognitivo: anúncio no START e na execução ──────────────────────────


class _S:
    cognitive_max_tool_rounds = 3
    default_max_tokens = 1024
    cognitive_stream_enabled = True
    cognitive_reasoning_enabled = False


def _svc_cognitivo(**overrides: Any) -> AIService:
    svc = object.__new__(AIService)
    svc.settings = _S()
    for k, v in overrides.items():
        setattr(svc.settings, k, v)
    return svc


class _Stream:
    def __init__(self, chunks: list[StreamChunk]):
        self._chunks = chunks

    async def stream_complete(self, **_kw: Any):
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_anuncio_no_start_leva_a_tool():
    provider = _Stream([
        StreamChunk(event=StreamEventType.TOOL_CALL_START, tool_call_id="c1", tool_name="neo_bash"),
        StreamChunk(event=StreamEventType.TOOL_CALL_ARGS, tool_call_id="c1", tool_arguments_delta='{"command": "ls"}'),
        StreamChunk(event=StreamEventType.DONE, finish_reason="tool_calls"),
    ])
    fila: asyncio.Queue[str] = asyncio.Queue()
    await _svc_cognitivo()._cognitive_round(
        provider=provider, messages=[], tools=[object()], req_model="m",
        max_tokens=100, extra={}, progress=fila,
    )
    linha = fila.get_nowait()
    assert (linha, linha.tool_name) == ("rodando no sandbox", "neo_bash")


@pytest.mark.asyncio
async def test_anuncio_da_execucao_leva_a_tool():
    class _Resp:
        def __init__(self, content: str = "", tool_calls: list[ToolCall] | None = None):
            self._content, self._tc = content, tool_calls or []

        def get_content(self) -> str:
            return self._content

        def get_tool_calls(self) -> list[ToolCall]:
            return self._tc

    class _P:
        def __init__(self, respostas: list[_Resp]):
            self._r = list(respostas)

        async def complete(self, **_kw: Any) -> _Resp:
            return self._r.pop(0)

    class _E:
        async def execute_tool_call(self, tc: ToolCall) -> ToolResult:
            return ToolResult(tool_call_id=tc.id, name=tc.name, result="ok", execution_time=0.0)

    fila: asyncio.Queue[str] = asyncio.Queue()
    await _svc_cognitivo(cognitive_stream_enabled=False)._run_cognitive_tool_loop(
        provider=_P([
            _Resp(tool_calls=[ToolCall(id="1", name="email_message", arguments={"id": "42"})]),
            _Resp(content="pronto"),
        ]),
        messages=[], req_model="m", model_name="m", provider_name="fake",
        tools=[object()], tool_executor=_E(), slog=None, trace=None, progress=fila,
    )
    linha = fila.get_nowait()
    assert (linha, linha.tool_name) == ("lendo o email", "email_message")


@pytest.mark.asyncio
async def test_bolha_do_drenador_leva_a_tool_no_metadata():
    svc = _svc_cognitivo()

    async def _loop(*, progress=None, **_kw: Any) -> str:
        await AIService._emit_progress(progress, "olhando a caixa de email", tool_name="email_inbox")
        await asyncio.sleep(0)
        await AIService._emit_progress(progress, "Achei dois emails")
        return "resposta"

    svc._run_cognitive_tool_loop = _loop
    bolhas = [b async for b in svc._drive_cognitive_loop(_LoopRun(), session_id=uuid4())]

    assert [b.message for b in bolhas] == [
        f"{CYCLE_PROGRESS_MARKER}olhando a caixa de email",
        f"{CYCLE_PROGRESS_MARKER}Achei dois emails",
    ]
    assert bolhas[0].metadata == {"tool_name": "email_inbox"}
    # Texto do modelo não inventa tool: o desktop cai na frase (ou ignora).
    assert "tool_name" not in bolhas[1].metadata


# ── o agente do SERVER_TOOL avisa cada rodada antes de rodar ──────────────────


class _AResp:
    def __init__(self, content: str | None = None, tool_calls: list[ToolCall] | None = None):
        self._content, self._calls = content, tool_calls or []

    def get_content(self) -> str | None:
        return self._content

    def get_tool_calls(self) -> list[ToolCall]:
        return self._calls


class _AProvider:
    def __init__(self, script: list[_AResp]):
        self.script = list(script)

    async def complete(self, **_kw: Any) -> _AResp:
        return self.script.pop(0)


class _ARouter:
    def __init__(self, provider: _AProvider):
        self.provider = provider

    def get_provider(self, _name: str | None = None) -> _AProvider:
        return self.provider

    def get_default_model(self, _name: str | None = None) -> str:
        return "fake-model"

    def list_available_providers(self) -> list[str]:
        return ["fake"]


class _ATools:
    def __init__(self, ordem: list[str]):
        self.ordem = ordem

    async def execute_tool_call(self, tc: ToolCall) -> ToolResult:
        self.ordem.append(f"rodou {tc.name}")
        return ToolResult(tool_call_id=tc.id, name=tc.name, result="ok", execution_time=0.0)


_INBOX = ToolSchema(name="email_inbox", description="caixa", parameters={}, module="email")


def _agente(script: list[_AResp], ordem: list[str]) -> AgentExecutor:
    return AgentExecutor(llm_router=_ARouter(_AProvider(script)), tool_registry=None, tool_executor=_ATools(ordem))


@pytest.mark.asyncio
async def test_agente_avisa_as_tools_antes_de_rodar():
    ordem: list[str] = []

    async def aviso(chamadas: list[ToolCall]) -> None:
        ordem.append("aviso " + ",".join(c.name for c in chamadas))

    agente = _agente([
        _AResp(tool_calls=[ToolCall(name="email_inbox", arguments={})]),
        _AResp(tool_calls=[ToolCall(name="email_message", arguments={"id": "1"})]),
        _AResp(content="pronto"),
    ], ordem)
    result = await agente.execute(
        task="olha meus emails", provider="fake", model="fake-model",
        available_tools=[_INBOX], on_tool_calls=aviso,
    )

    assert result["status"] == "completed"
    assert ordem == ["aviso email_inbox", "rodou email_inbox", "aviso email_message", "rodou email_message"]


@pytest.mark.asyncio
async def test_aviso_que_explode_nao_derruba_o_agente():
    ordem: list[str] = []

    async def aviso(_chamadas: list[ToolCall]) -> None:
        raise RuntimeError("fila morta")

    agente = _agente([_AResp(tool_calls=[ToolCall(name="email_inbox", arguments={})]), _AResp(content="ok")], ordem)
    result = await agente.execute(
        task="x", provider="fake", model="fake-model", available_tools=[_INBOX], on_tool_calls=aviso,
    )
    assert result["status"] == "completed"
    assert ordem == ["rodou email_inbox"]


# ── o trilho do servidor manda a linha de cada passo, com a tool ──────────────


class _Sessions:
    async def add_message(self, **_kw: Any) -> None:
        return None


class _LLM:
    def list_available_providers(self) -> list[str]:
        return ["openrouter"]


class _AgentePasso:
    """Agente falso: anuncia duas rodadas e responde. ``segura`` trava no meio."""

    def __init__(self, segura: asyncio.Event | None = None):
        self.segura = segura
        self.terminou = False

    async def execute(self, *, on_tool_calls=None, **_kw: Any) -> dict[str, Any]:
        await on_tool_calls([ToolCall(name="email_inbox", arguments={})])
        if self.segura is not None:
            await self.segura.wait()
        await on_tool_calls([ToolCall(name="email_message", arguments={"id": "1"})])
        self.terminou = True
        return {"status": "completed", "final_response": "Dois emails do LinkedIn.", "steps": [], "iterations": 3}


def _svc_servidor(agente: _AgentePasso) -> AIService:
    svc = AIService.__new__(AIService)
    svc.settings = get_settings()
    svc.db = object()
    svc.llm = _LLM()
    svc.pks = None
    svc.agent = agente
    svc.sessions = _Sessions()
    svc._voice_input_sessions = set()
    svc.build_request_executor = lambda **_kw: object()
    svc._server_tool_toolset = lambda escolhida=None: []
    svc._build_server_tool_memory = lambda *_a, **_kw: None
    return svc


def _decisao() -> RoutingDecision:
    return RoutingDecision(
        intent=IntentType.SERVER_TOOL, tool_name="email_inbox",
        tool_arguments={"minutes": 1440}, needs_memory=False,
    )


@pytest.fixture
def sem_painel(monkeypatch):
    import modules.model_config as mc

    async def _fake(db, role, **kw):
        return None

    monkeypatch.setattr(mc, "get_model_config", _fake)


@pytest.mark.asyncio
async def test_trilho_do_servidor_manda_cada_passo_com_a_tool(sem_painel):
    svc = _svc_servidor(_AgentePasso())
    respostas = [r async for r in svc._handle_server_tool(uuid4(), "olha meus emails", [], _decisao(), metadata={})]

    progresso = [r for r in respostas if not r.is_final]
    assert [(r.message, (r.metadata or {}).get("tool_name")) for r in progresso] == [
        ("Executando ferramenta no servidor: email_inbox...", "email_inbox"),
        (f"{CYCLE_PROGRESS_MARKER}olhando a caixa de email", "email_inbox"),
        (f"{CYCLE_PROGRESS_MARKER}lendo o email", "email_message"),
    ]
    assert respostas[-1].is_final


@pytest.mark.asyncio
async def test_stop_no_meio_do_trilho_do_servidor_cancela_o_agente(sem_painel):
    """Antes o agente era `await`-ado inteiro; agora é task — e não pode sobrar."""
    agente = _AgentePasso(segura=asyncio.Event())
    gen = _svc_servidor(agente)._handle_server_tool(uuid4(), "olha", [], _decisao(), metadata={})
    vistos = []
    async for resp in gen:
        vistos.append(resp)
        if len(vistos) == 2:  # a 1ª linha do trilho e a do 1º passo
            break
    await gen.aclose()
    agente.segura.set()
    await asyncio.sleep(0.05)
    assert agente.terminou is False


# ── o gRPC leva o campo ───────────────────────────────────────────────────────


def test_grpc_leva_a_tool_no_thinking():
    resp = AIServiceResponse(
        intent=IntentType.SERVER_TOOL, session_id=uuid4(),
        message=f"{CYCLE_PROGRESS_MARKER}lendo o email", is_final=False,
        metadata={"tool_name": "email_message"},
    )
    msg = NeoDesktopServicer._to_core_message(str(resp.session_id), resp)
    assert msg.thinking.tool_name == "email_message"


def test_grpc_sem_tool_manda_vazio():
    resp = AIServiceResponse(intent=IntentType.DIRECT_RESPONSE, session_id=uuid4(), message="pensando", is_final=False)
    msg = NeoDesktopServicer._to_core_message(str(resp.session_id), resp)
    assert msg.thinking.tool_name == ""
