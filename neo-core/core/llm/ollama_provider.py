"""Ollama provider — OpenAI-compatible local inference server."""

from typing import Any

from openai import AsyncOpenAI

from core.llm.openai_provider import OpenAIProvider
from shared import LLMProviderError, Message, StreamChunk, StreamEventType, ToolSchema


class OllamaProvider(OpenAIProvider):
    """Provider para Ollama (gemma4, llama, etc) rodando localmente.

    A API do Ollama é 100% compatível com a OpenAI — apenas muda o base_url.
    Qualquer modelo disponível no servidor Ollama pode ser usado.
    """

    DEFAULT_MODEL = "gemma4:e2b"

    def __init__(
        self,
        base_url: str = "http://neo-ollama:11434",
        model: str = DEFAULT_MODEL,
        **kwargs: Any,
    ) -> None:
        from core.llm.provider import LLMProvider
        LLMProvider.__init__(self, api_key="ollama", **kwargs)
        self.client = AsyncOpenAI(
            api_key="ollama",  # Ollama ignora o api_key mas a lib exige um valor
            base_url=f"{base_url.rstrip('/')}/v1",
        )
        self.model = model

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any,
    ):
        """Completion via Ollama."""
        try:
            return await super().complete(
                messages=messages,
                tools=tools,
                model=model or self.model,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
        except LLMProviderError as e:
            raise LLMProviderError(
                str(e.message).replace("OpenAI", "Ollama"),
                details={"provider": "ollama", "model": model or self.model},
            ) from e

    async def stream_complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.9,
        max_tokens: int = 4096,
        **kwargs: Any,
    ):
        """Streaming via Ollama."""
        try:
            async for chunk in super().stream_complete(
                messages=messages,
                tools=tools,
                model=model or self.model,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            ):
                yield chunk
        except LLMProviderError as e:
            yield StreamChunk(event=StreamEventType.ERROR, content=str(e))
            raise LLMProviderError(
                str(e.message).replace("OpenAI", "Ollama"),
                details={"provider": "ollama", "model": model or self.model},
            ) from e
