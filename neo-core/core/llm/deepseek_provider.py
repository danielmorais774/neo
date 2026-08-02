"""DeepSeek provider — OpenAI-compatible API, different base URL and models."""

import logging
from typing import Any

from openai import AsyncOpenAI

from core.llm.openai_provider import OpenAIProvider, forces_tool_call
from shared import LLMProviderError, Message, StreamChunk, StreamEventType, ToolSchema

logger = logging.getLogger(__name__)

#: Desliga o raciocínio APENAS na chamada que exige tool. Os modelos v4 pensam
#: por padrão e a API recusa `tool_choice` forçado sob thinking mode com
#: 400 "Thinking mode does not support this tool_choice" — o que matava o turno
#: inteiro sempre que uma guarda do núcleo cognitivo (anti-mentira,
#: anti-promessa, anti-alegação-de-download) ou uma skill tool_backed
#: autocarregada tentava obrigar a tool. Sondando a API de verdade (2026-07-31),
#: só duas formas passam junto com `tool_choice="required"`:
#:   extra_body={"thinking": {"type": "disabled"}}   ← esta
#:   reasoning_effort="none"
#: As demais (`enable_thinking`, `reasoning.enabled`, `chat_template_kwargs`,
#: `reasoning_effort="minimal"`) continuam dando 400, e não existe variante
#: "-non-thinking" do modelo. Escolhida a primeira por ser explícita: o erro de
#: `thinking: false` ("expected struct") confirma que é um campo de objeto da
#: própria API, não um chute.
#:
#: Perder o raciocínio nessa chamada é o comportamento DESEJADO: o único
#: trabalho dela é emitir a tool call que já foi decidida.
_THINKING_DISABLED = {"thinking": {"type": "disabled"}}


class DeepSeekProvider(OpenAIProvider):
    """Provider para DeepSeek (deepseek-v4-flash, deepseek-v4-pro).

    A API do DeepSeek é 100% compatível com a OpenAI — apenas muda o base_url
    e os nomes de modelos:
      - deepseek-v4-flash  → rápido, bom custo-benefício (default)
      - deepseek-v4-pro    → forte, raciocínio mais profundo
    NOTA: os nomes antigos "deepseek-chat"/"deepseek-reasoner" foram aposentados
    pela API (400 invalid_request) — não usar como default.
    """

    DEFAULT_MODEL = "deepseek-v4-flash"
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
        # Quem chama só diz "quero a tool garantida" (tool_choice); saber que
        # isso exige desligar o thinking é conhecimento DESTE provider.
        forced_tool = bool(tools) and forces_tool_call(kwargs.get("tool_choice"))
        # Idem para quem pede saída curta e estruturada (`disable_thinking=True`,
        # ex.: o classificador de intenção): nesses casos o raciocínio só compete
        # por orçamento com o JSON que precisa sair. O chamador expressa a
        # INTENÇÃO; a tradução para o campo da API mora aqui.
        wants_no_thinking = bool(kwargs.pop("disable_thinking", False))
        if forced_tool or wants_no_thinking:
            kwargs["extra_body"] = {**_THINKING_DISABLED, **(kwargs.get("extra_body") or {})}
            logger.debug(
                "[DeepSeek] thinking desligado nesta chamada (%s)",
                "tool_choice forçado" if forced_tool else "saída estruturada pedida",
            )
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
