"""Grok provider — xAI API (OpenAI-compatible), different base URL and models."""

from typing import Any

import httpx
from openai import AsyncOpenAI

from core.llm.openai_provider import OpenAIProvider
from core.llm.provider import LLMProvider
from shared import LLMProviderError, Message, StreamChunk, StreamEventType, ToolSchema


class GrokProvider(OpenAIProvider):
    """Provider para xAI Grok (grok-3-mini, grok-3-mini-fast, grok-2-1212, etc.).

    A API xAI é 100% compatível com a OpenAI — apenas muda o base_url.

    Modelos disponíveis no plano free:
      - grok-3-mini        → rápido, barato, bom para classificação e tarefas simples
      - grok-3-mini-fast   → mais rápido ainda, menor custo
      - grok-2-1212        → modelo completo para tarefas complexas

    Modelos pagos:
      - grok-3             → modelo mais poderoso
      - grok-3-fast        → grok-3 otimizado para velocidade
    """

    DEFAULT_MODEL = "grok-3-mini"
    BASE_URL = "https://api.x.ai/v1"

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        # Bypass OpenAIProvider.__init__ to use xAI base_url + keep-alive pooling
        LLMProvider.__init__(self, api_key, **kwargs)
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=self.BASE_URL,
            http_client=httpx.AsyncClient(
                limits=httpx.Limits(
                    max_keepalive_connections=5,
                    max_connections=10,
                    keepalive_expiry=60,
                ),
            ),
        )
        self.model = self.DEFAULT_MODEL

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any,
    ):
        """Completion via xAI Grok (usa mesma lógica da OpenAI)."""
        try:
            return await super().complete(
                messages=messages,
                tools=tools,
                model=model or self.DEFAULT_MODEL,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
        except Exception as e:
            raise LLMProviderError(
                f"Grok completion failed: {e}",
                details={"model": model or self.DEFAULT_MODEL},
            ) from e

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any,
    ):
        """Streaming via xAI Grok."""
        async for chunk in super().stream(
            messages=messages,
            tools=tools,
            model=model or self.DEFAULT_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        ):
            yield chunk
