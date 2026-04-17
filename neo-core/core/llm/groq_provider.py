"""Groq provider — ultra-fast inference via Groq API (OpenAI-compatible)."""

from typing import Any

import httpx
from openai import AsyncOpenAI

from core.llm.openai_provider import OpenAIProvider
from core.llm.provider import LLMProvider
from shared import LLMProviderError, Message, ToolSchema


class GroqProvider(OpenAIProvider):
    """Provider para Groq (llama-3.3-70b-versatile, llama-3.1-8b-instant, etc.).

    A API Groq é OpenAI-compatível com base_url diferente.
    Groq é conhecido por latência extremamente baixa (~200-400ms) graças ao LPU.

    Modelos disponíveis no plano free:
      - llama-3.3-70b-versatile  → principal, 70B params, muito capaz
      - llama-3.1-8b-instant     → ultra-rápido, bom para classificação
      - llama3-70b-8192          → contexto longo
      - mixtral-8x7b-32768       → contexto de 32k tokens
      - gemma2-9b-it             → Google Gemma2
    """

    DEFAULT_MODEL = "llama-3.3-70b-versatile"
    BASE_URL = "https://api.groq.com/openai/v1"

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
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
        """Completion via Groq."""
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
                f"Groq completion failed: {e}",
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
        """Streaming via Groq."""
        async for chunk in super().stream(
            messages=messages,
            tools=tools,
            model=model or self.DEFAULT_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        ):
            yield chunk
