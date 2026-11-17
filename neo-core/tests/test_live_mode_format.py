"""Modo Live: a formatação da resposta deve mudar quando o turno é falado.

Cobre o site CLIENT_TOOL (`_format_tool_result`): com ``live=True`` o prompt pede
texto corrido sem markdown; sem live mantém o comportamento atual (markdown). Os
outros dois sites (sys_context do DIRECT_RESPONSE e o task do SERVER_TOOL) vivem
dentro de métodos pesados (DB/LLM/agent) e são verificados ponta-a-ponta.
"""

from __future__ import annotations

import pytest

from modules.desktop.ai_service import AIService
from shared.config import get_settings


class _FakeResp:
    def get_content(self) -> str:
        return "resposta"


class _CaptureProvider:
    """Provider falso que captura as mensagens enviadas ao LLM."""

    model = "test-model"

    def __init__(self) -> None:
        self.messages = None

    async def complete(self, messages, model=None, temperature=0.0,
                       max_tokens=0, **kwargs):
        self.messages = messages
        return _FakeResp()


class _FakeLLM:
    def __init__(self, provider) -> None:
        self._p = provider

    def get_provider(self):
        return self._p


def _service(provider) -> AIService:
    # Evita o __init__ pesado (router/db/agent/...); só precisamos de llm + settings.
    svc = AIService.__new__(AIService)
    svc.llm = _FakeLLM(provider)
    svc.settings = get_settings()
    return svc


def _user_prompt(provider) -> str:
    return provider.messages[-1].content


@pytest.mark.asyncio
async def test_format_tool_result_live_pede_texto_corrido():
    prov = _CaptureProvider()
    await _service(prov)._format_tool_result("e aí?", "ls -la", "a\nb\nc", live=True)
    p = _user_prompt(prov)
    assert "TEXTO CORRIDO" in p
    assert "será falado" in p
    assert "Use markdown" not in p


@pytest.mark.asyncio
async def test_format_tool_result_default_mantem_markdown():
    prov = _CaptureProvider()
    await _service(prov)._format_tool_result("e aí?", "ls -la", "a\nb\nc", live=False)
    p = _user_prompt(prov)
    assert "Use markdown" in p
    assert "TEXTO CORRIDO" not in p
