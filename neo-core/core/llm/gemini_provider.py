"""Gemini provider — uses Google's OpenAI-compatible endpoint.

Google exposes Gemini via an OpenAI-compatible REST API:
  Base URL: https://generativelanguage.googleapis.com/v1beta/openai/
  Auth: Bearer <GEMINI_API_KEY>

No extra dependency needed — uses the same `openai` package as the other providers.

Available models:
  - gemini-2.5-pro-preview-03-25  → most capable, best reasoning
  - gemini-2.0-flash               → fast, multimodal, good quality
  - gemini-2.0-flash-lite          → very fast, lightweight
  - gemini-1.5-pro                 → capable, large context
  - gemini-1.5-flash               → fast
"""

from typing import Any

from openai import AsyncOpenAI

from core.llm.openai_provider import OpenAIProvider
from core.llm.provider import LLMProvider
from shared import LLMProviderError, Message, StreamChunk, StreamEventType, ToolSchema


class GeminiProvider(OpenAIProvider):
    """Provider para Google Gemini via API OpenAI-compatible."""

    DEFAULT_MODEL = "gemini-2.0-flash"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        # Bypass OpenAIProvider.__init__ to use Gemini base_url
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
        """Completion via Gemini (usa lógica OpenAI-compatible)."""
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
                str(e.message).replace("OpenAI", "Gemini"),
                details={"provider": "gemini", "model": model or self.DEFAULT_MODEL},
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
        """Streaming via Gemini."""
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
                str(e.message).replace("OpenAI", "Gemini"),
                details={"provider": "gemini", "model": model or self.DEFAULT_MODEL},
            ) from e
