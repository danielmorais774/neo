"""OpenRouter provider — OpenAI-compatible gateway to many model vendors.

OpenRouter (https://openrouter.ai) expõe uma API 100% compatível com a OpenAI,
apenas mudando o ``base_url`` e usando IDs de modelo no formato ``vendor/modelo``
(ex.: ``google/gemini-3.1-flash-lite``, ``anthropic/claude-3.5-sonnet``).

Vantagem: uma única API key dá acesso a dezenas de modelos (Google, Anthropic,
Meta, Mistral, etc.) com fallback automático e roteamento por preço/disponibilidade.
"""

import logging
from typing import Any

import httpx
from openai import AsyncOpenAI

from core.llm.openai_provider import OpenAIProvider
from core.llm.provider import LLMProvider
from shared import LLMProviderError, Message, StreamChunk, StreamEventType, ToolSchema

logger = logging.getLogger(__name__)

#: Tradução de ``disable_thinking`` para o campo do OpenRouter.
#:
#: Muitos modelos servidos aqui raciocinam POR PADRÃO, e o gateway não aplica o
#: ``max_tokens`` ao orçamento de raciocínio. Medido em 2026-08-01 com
#: ``qwen/qwen3.5-flash-02-23`` (upstream Alibaba) no prompt do appraiser de afeto,
#: ``max_tokens=220``: **11-20s e 2.040-3.995 tokens de reasoning** para emitir ~180
#: chars de JSON. Com este campo: ~0,6s e zero reasoning.
#:
#: Isto é o que faltava para a intenção ``disable_thinking`` valer aqui: como este
#: provider herda o ``complete`` da OpenAI — que DESCARTA a flag por ser ruído para
#: a API dela —, quem pedia saída curta e estruturada (o ``intent_router`` pede em
#: toda classificação) estava pagando o raciocínio inteiro sem saber.
_REASONING_DISABLED = {"reasoning": {"enabled": False}}


class OpenRouterProvider(OpenAIProvider):
    """Provider para OpenRouter (acesso unificado a múltiplos vendors).

    A API do OpenRouter é compatível com a OpenAI — apenas muda o ``base_url`` e
    os IDs de modelo passam a ser ``vendor/modelo``:
      - google/gemini-3.1-flash-lite  → rápido e barato, ótimo p/ classificação
      - google/gemini-2.0-flash       → bom custo-benefício
      - anthropic/claude-3.5-sonnet   → tarefas complexas
      - meta-llama/llama-3.3-70b-instruct → open-weights

    Os headers opcionais ``HTTP-Referer``/``X-Title`` identificam o app no
    leaderboard do OpenRouter (não são obrigatórios).
    """

    DEFAULT_MODEL = "google/gemini-3.1-flash-lite"
    BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        # Bypass OpenAIProvider.__init__ to use OpenRouter base_url + keep-alive pooling
        LLMProvider.__init__(self, api_key, **kwargs)
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or self.BASE_URL,
            default_headers={
                "HTTP-Referer": "https://github.com/neo-core",
                "X-Title": "Neo Core",
            },
            http_client=httpx.AsyncClient(
                limits=httpx.Limits(
                    max_keepalive_connections=5,
                    max_connections=10,
                    keepalive_expiry=60,
                ),
            ),
        )
        self.model = self.DEFAULT_MODEL

    @staticmethod
    def _apply_reasoning_pref(kwargs: dict[str, Any]) -> None:
        """Traduz a intenção ``disable_thinking`` para o campo ``reasoning`` da API.

        Consome a flag ANTES de subir para ``OpenAIProvider``, que a descartaria.
        Um ``extra_body`` vindo do chamador vence — quem foi explícito manda.
        """
        if not kwargs.pop("disable_thinking", False):
            return
        kwargs["extra_body"] = {**_REASONING_DISABLED, **(kwargs.get("extra_body") or {})}
        logger.debug("[OpenRouter] reasoning desligado nesta chamada (saída estruturada pedida)")

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any,
    ):
        """Completion via OpenRouter (usa mesma lógica da OpenAI)."""
        self._apply_reasoning_pref(kwargs)
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
                str(e.message).replace("OpenAI", "OpenRouter"),
                details={"provider": "openrouter", "model": model or self.DEFAULT_MODEL},
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
        """Streaming via OpenRouter."""
        self._apply_reasoning_pref(kwargs)
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
                str(e.message).replace("OpenAI", "OpenRouter"),
                details={"provider": "openrouter", "model": model or self.DEFAULT_MODEL},
            ) from e
