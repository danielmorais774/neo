"""OpenAI provider implementation."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessage

from core.llm.provider import CompletionResponse, LLMProvider
from shared import LLMProviderError, Message, StreamChunk, StreamEventType, ToolCall, ToolSchema

logger = logging.getLogger(__name__)

# 429 = TPM/RPM da org estourado; renova em segundos. Backoff curto porque há um
# usuário esperando no chat — melhor 3 tentativas rápidas que um erro na cara dele.
_RATE_LIMIT_RETRIES = 3
_RATE_LIMIT_BACKOFF = (2.0, 5.0, 10.0)


def _is_rate_limit(error: Exception) -> bool:
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status == 429:
        return True
    return type(error).__name__ == "RateLimitError" or "429" in str(error)


class OpenAICompletionResponse(CompletionResponse):
    """Wrapper para resposta da OpenAI."""

    def __init__(self, response: ChatCompletion) -> None:
        self._response = response
        self._message: ChatCompletionMessage = response.choices[0].message

    def get_content(self) -> str | None:
        """Retorna o conteúdo da mensagem."""
        return self._message.content

    def get_tool_calls(self) -> list[ToolCall]:
        """Extrai tool calls da resposta."""
        if not self._message.tool_calls:
            return []

        tool_calls: list[ToolCall] = []
        for tc in self._message.tool_calls:
            tool_calls.append(
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=json.loads(tc.function.arguments),
                )
            )
        return tool_calls

    def finish_reason(self) -> str:
        """Retorna o finish_reason."""
        return self._response.choices[0].finish_reason


class OpenAIProvider(LLMProvider):
    """Provider para OpenAI (GPT-4, GPT-3.5, etc)."""

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        super().__init__(api_key, **kwargs)
        self.client = AsyncOpenAI(
            api_key=api_key,
            http_client=httpx.AsyncClient(
                limits=httpx.Limits(
                    max_keepalive_connections=5,
                    max_connections=10,
                    keepalive_expiry=60,
                ),
            ),
        )

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> CompletionResponse:
        """Realiza completion com OpenAI."""
        try:
            # Converte mensagens para formato OpenAI
            openai_messages = [
                {"role": msg.role.value, "content": msg.content} for msg in messages
            ]

            # Prepara parâmetros
            params: dict[str, Any] = {
                "model": model or "gpt-4-turbo-preview",
                "messages": openai_messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                **kwargs,
            }

            # Adiciona tools se fornecidas. O chamador pode impor `tool_choice`
            # (ex.: "required" para obrigar uma tool no primeiro round) — sem tools
            # o parâmetro é inválido na API, então cai fora.
            if tools:
                params["tools"] = self.format_tools_for_api(tools)
                params.setdefault("tool_choice", "auto")
            else:
                params.pop("tool_choice", None)

            # Rate limit (429) é transitório: o TPM da org se renova em segundos. Sem
            # retry, o turno morria virando "Erro ao processar: 429" — que ainda por
            # cima entra no histórico e envenena os turnos seguintes.
            last_error: Exception | None = None
            for attempt in range(_RATE_LIMIT_RETRIES + 1):
                try:
                    response = await self.client.chat.completions.create(**params)
                    return OpenAICompletionResponse(response)
                except Exception as e:  # noqa: BLE001 — só o 429 é retentável
                    last_error = e
                    if not _is_rate_limit(e) or attempt == _RATE_LIMIT_RETRIES:
                        raise
                    delay = _RATE_LIMIT_BACKOFF[attempt]
                    logger.warning(
                        "[OpenAI] rate limit (429) — retry %d/%d em %.1fs",
                        attempt + 1, _RATE_LIMIT_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
            raise last_error  # type: ignore[misc]  # inalcançável: o loop ou retorna ou levanta

        except Exception as e:
            raise LLMProviderError(
                f"Erro na completion OpenAI: {str(e)}",
                details={"provider": "openai", "model": model},
            ) from e

    async def stream_complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.9,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Streaming completion with OpenAI — yields StreamChunk events."""
        try:
            openai_messages = [
                {"role": msg.role.value, "content": msg.content} for msg in messages
            ]

            params: dict[str, Any] = {
                "model": model or "gpt-4o",
                "messages": openai_messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
                **kwargs,
            }

            if tools:
                params["tools"] = self.format_tools_for_api(tools)
                params["tool_choice"] = "auto"

            stream = await self.client.chat.completions.create(**params)

            # Track tool calls being assembled across chunks
            tool_calls_buffer: dict[int, dict[str, str]] = {}

            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                finish_reason = chunk.choices[0].finish_reason if chunk.choices else None

                if delta is None:
                    continue

                # Text content
                if delta.content:
                    yield StreamChunk(
                        event=StreamEventType.TOKEN,
                        content=delta.content,
                    )

                # Tool calls
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_buffer:
                            tool_calls_buffer[idx] = {
                                "id": tc.id or "",
                                "name": "",
                                "arguments": "",
                            }
                            if tc.id:
                                tool_calls_buffer[idx]["id"] = tc.id

                        if tc.function:
                            if tc.function.name:
                                tool_calls_buffer[idx]["name"] = tc.function.name
                                yield StreamChunk(
                                    event=StreamEventType.TOOL_CALL_START,
                                    tool_call_id=tool_calls_buffer[idx]["id"],
                                    tool_name=tc.function.name,
                                )
                            if tc.function.arguments:
                                tool_calls_buffer[idx]["arguments"] += tc.function.arguments
                                yield StreamChunk(
                                    event=StreamEventType.TOOL_CALL_ARGS,
                                    tool_call_id=tool_calls_buffer[idx]["id"],
                                    tool_arguments_delta=tc.function.arguments,
                                )

                # Stream end
                if finish_reason:
                    # Emit tool_call_end for completed tool calls
                    if finish_reason == "tool_calls":
                        for idx, tc_data in tool_calls_buffer.items():
                            yield StreamChunk(
                                event=StreamEventType.TOOL_CALL_END,
                                tool_call_id=tc_data["id"],
                                tool_name=tc_data["name"],
                                tool_arguments_delta=tc_data["arguments"],
                            )

                    yield StreamChunk(
                        event=StreamEventType.DONE,
                        finish_reason=finish_reason,
                    )

        except Exception as e:
            yield StreamChunk(
                event=StreamEventType.ERROR,
                content=str(e),
            )
            raise LLMProviderError(
                f"Erro no streaming OpenAI: {str(e)}",
                details={"provider": "openai", "model": model},
            ) from e

    def format_tools_for_api(self, tools: list[ToolSchema]) -> list[dict[str, Any]]:
        """Formata tools para OpenAI function calling."""
        formatted_tools: list[dict[str, Any]] = []

        for tool in tools:
            # Monta properties e required
            properties: dict[str, Any] = {}
            required: list[str] = []

            for param_name, param in tool.parameters.items():
                properties[param_name] = {
                    "type": param.type,
                    "description": param.description,
                }
                if param.enum:
                    properties[param_name]["enum"] = param.enum

                if param.required:
                    required.append(param_name)

            # Formato OpenAI
            formatted_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": {
                            "type": "object",
                            "properties": properties,
                            "required": required,
                        },
                    },
                }
            )

        return formatted_tools
