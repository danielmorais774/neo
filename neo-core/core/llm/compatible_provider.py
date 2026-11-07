"""Provider genérico para qualquer API compatível com OpenAI Chat Completions.

É o adapter ``openai_compatible`` do catálogo (Configurações → Providers): o
provider que o operador cadastra no painel sem precisar de classe nova —
Together, Fireworks direto, um vLLM/LM Studio na rede. Mesmo molde do Groq e do
Grok: herda o ``complete``/``stream_complete`` do OpenAIProvider e troca o
``base_url``.
"""

from typing import Any

import httpx
from openai import AsyncOpenAI

from core.llm.openai_provider import OpenAIProvider
from core.llm.provider import LLMProvider
from shared import Message, ToolSchema

#: Sem chave o SDK cairia na ``OPENAI_API_KEY`` do ambiente — e mandaria a chave
#: da OpenAI para um servidor qualquer. Servidor local que não pede chave recebe
#: este enchimento em vez dela.
_NO_KEY = "not-needed"


class OpenAICompatibleProvider(OpenAIProvider):
    """OpenAI Chat Completions num ``base_url`` arbitrário."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> None:
        if not base_url:
            raise ValueError("openai_compatible exige base_url")
        LLMProvider.__init__(self, api_key, **kwargs)
        self.client = AsyncOpenAI(
            api_key=api_key or _NO_KEY,
            base_url=base_url,
            http_client=httpx.AsyncClient(
                limits=httpx.Limits(
                    max_keepalive_connections=5,
                    max_connections=10,
                    keepalive_expiry=60,
                ),
            ),
        )
        # O OpenAIProvider cai em "gpt-4-turbo-preview" quando ninguém passa
        # modelo; num servidor que não é a OpenAI isso é um 404 garantido.
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
        return await super().complete(
            messages=messages,
            tools=tools,
            model=model or self.model,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    async def stream_complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.9,
        max_tokens: int = 4096,
        **kwargs: Any,
    ):
        async for chunk in super().stream_complete(
            messages=messages,
            tools=tools,
            model=model or self.model,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        ):
            yield chunk
