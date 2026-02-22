"""DeepSeek provider — OpenAI-compatible API, different base URL and models."""

from typing import Any

from openai import AsyncOpenAI

from core.llm.openai_provider import OpenAIProvider
from shared import LLMProviderError, Message, StreamChunk, StreamEventType, ToolSchema


class DeepSeekProvider(OpenAIProvider):
    """Provider para DeepSeek (deepseek-chat, deepseek-reasoner).

    A API do DeepSeek é 100% compatível com a OpenAI — apenas muda o base_url
    e os nomes de modelos:
      - deepseek-chat      → equivalente ao GPT-4o (rápido, bom custo-benefício)
      - deepseek-reasoner  → equivalente ao o1 (raciocínio lento e profundo)
    """

    DEFAULT_MODEL = "deepseek-chat"
    BASE_URL = "https://api.deepseek.com"

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        # Bypass OpenAIProvider.__init__ to use DeepSeek base_url
        from core.llm.provider import LLMProvider
        LLMProvider.__init__(self, api_key, **kwargs)
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=self.BASE_URL,
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
        """Completion via DeepSeek (usa mesma lógica da OpenAI)."""
        try:
            return await super().complete(
                messages=messages,
                tools=tools,
                model=model or self.DEFAULT_MODEL,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
        except LLMProviderError as e:
            raise LLMProviderError(
                str(e.message).replace("OpenAI", "DeepSeek"),
                details={"provider": "deepseek", "model": model or self.DEFAULT_MODEL},
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
        """Streaming via DeepSeek."""
        try:
            async for chunk in super().stream_complete(
                messages=messages,
                tools=tools,
                model=model or self.DEFAULT_MODEL,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            ):
                yield chunk
        except LLMProviderError as e:
            yield StreamChunk(event=StreamEventType.ERROR, content=str(e))
            raise LLMProviderError(
                str(e.message).replace("OpenAI", "DeepSeek"),
                details={"provider": "deepseek", "model": model or self.DEFAULT_MODEL},
            ) from e
