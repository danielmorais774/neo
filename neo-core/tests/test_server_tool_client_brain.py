"""O agente do SERVER_TOOL roda no modelo que o usuário escolheu no desktop.

Na session-d35696d4 (2026-09-21) a MESMA conversa falava por dois cérebros: o
chat em ``deepseek/deepseek-v4.1-flash`` (seletor do desktop) e o agente do
SERVER_TOOL em ``deepseek/deepseek-v4-flash@baidu`` (role do painel), num pool
lotado que devolveu 502 em 2 das 7 chamadas. A regra agora: escolha do desktop
→ senão, painel.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from modules.desktop.ai_service import AIService
from shared import IntentType
from shared.config import get_settings
from shared.types import RoutingDecision


class _Agent:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
        return {"status": "completed", "final_response": "ok", "steps": [], "iterations": 1,
                "metadata": {"provider": kwargs.get("provider"), "model": kwargs.get("model")}}


class _Sessions:
    async def add_message(self, **_kw: Any) -> None:
        return None


class _LLM:
    def list_available_providers(self) -> list[str]:
        return ["openrouter"]


@pytest.fixture
def painel(monkeypatch):
    import modules.model_config as mc

    async def _fake(db, role, **kw):
        return {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash@baidu"}

    monkeypatch.setattr(mc, "get_model_config", _fake)


def _svc() -> tuple[AIService, _Agent]:
    svc = AIService.__new__(AIService)
    svc.settings = get_settings()
    svc.db = object()
    svc.llm = _LLM()
    svc.pks = None
    svc.agent = _Agent()
    svc.sessions = _Sessions()
    svc._voice_input_sessions = set()
    svc.build_request_executor = lambda **_kw: object()
    svc._server_tool_toolset = lambda: []
    svc._build_server_tool_memory = lambda *_a, **_kw: None
    return svc, svc.agent


async def _drive(svc: AIService, metadata: dict[str, Any]) -> None:
    decision = RoutingDecision(
        intent=IntentType.SERVER_TOOL, tool_name="sql_query_ro",
        tool_arguments={"query": "SELECT 1"}, needs_memory=False,
    )
    async for _ in svc._handle_server_tool(uuid4(), "faz um select", [], decision, metadata=metadata):
        pass


@pytest.mark.asyncio
async def test_agente_usa_o_modelo_do_desktop(painel):
    svc, agent = _svc()
    await _drive(svc, {"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"})
    assert agent.kwargs["provider"] == "openrouter"
    assert agent.kwargs["model"] == "deepseek/deepseek-v4.1-flash"


@pytest.mark.asyncio
async def test_pin_do_painel_vale_quando_o_modelo_e_o_mesmo(monkeypatch):
    import modules.model_config as mc

    async def _fake(db, role, **kw):
        return {"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash@fireworks"}

    monkeypatch.setattr(mc, "get_model_config", _fake)
    svc, agent = _svc()
    await _drive(svc, {"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"})
    assert agent.kwargs["model"] == "deepseek/deepseek-v4.1-flash@fireworks"


@pytest.mark.asyncio
async def test_sem_escolha_do_cliente_cai_no_painel(painel):
    svc, agent = _svc()
    await _drive(svc, {})
    # None/None = o AgentExecutor resolve pelo role `cognitive` do painel.
    assert agent.kwargs["provider"] is None
    assert agent.kwargs["model"] is None


@pytest.mark.asyncio
async def test_provider_sem_chave_cai_no_painel(painel):
    svc, agent = _svc()
    await _drive(svc, {"provider": "anthropic", "model": "claude-x"})
    assert agent.kwargs["provider"] is None
    assert agent.kwargs["model"] is None


@pytest.mark.asyncio
async def test_provider_deduzido_do_catalogo_quando_o_cliente_nao_manda(painel):
    svc, agent = _svc()
    await _drive(svc, {"model": "deepseek/deepseek-v4.1-flash"})
    assert agent.kwargs["provider"] == "openrouter"
    assert agent.kwargs["model"] == "deepseek/deepseek-v4.1-flash"
