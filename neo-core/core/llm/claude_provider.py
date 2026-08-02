"""Anthropic Claude provider implementation."""

import json
from collections.abc import AsyncIterator
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types import Message as ClaudeMessage

from core.llm.provider import CompletionResponse, LLMProvider
from shared import LLMProviderError, Message, MessageRole, StreamChunk, StreamEventType, ToolCall, ToolSchema


class ClaudeCompletionResponse(CompletionResponse):
    """Wrapper para resposta do Claude."""

    def __init__(self, response: ClaudeMessage) -> None:
        self._response = response

    def get_content(self) -> str | None:
        """Extrai conteúdo de texto da resposta."""
        for block in self._response.content:
            if block.type == "text":
                return block.text
        return None

    def get_tool_calls(self) -> list[ToolCall]:
        """Extrai tool calls da resposta."""
        tool_calls: list[ToolCall] = []

        for block in self._response.content:
            if block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input,
                    )
                )

        return tool_calls

    def finish_reason(self) -> str:
        """Retorna stop_reason do Claude."""
        return self._response.stop_reason or "stop"


class ClaudeProvider(LLMProvider):
    """Provider para Anthropic Claude."""

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        super().__init__(api_key, **kwargs)
        self.client = AsyncAnthropic(api_key=api_key)

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> CompletionResponse:
        """Realiza completion com Claude."""
        try:
            # Intenção do chamador ("quero a saída estruturada, não o raciocínio"),
            # não parâmetro da API: aqui não há o que traduzir (o extended thinking
            # do Claude é opt-in), então some antes de virar 400 no corpo.
            kwargs.pop("disable_thinking", None)

            # Separa system message
            system_content: str | None = None
            claude_messages: list[dict[str, Any]] = []

            for msg in messages:
                if msg.role == MessageRole.SYSTEM:
                    system_content = msg.content
                else:
                    claude_messages.append(
                        {"role": msg.role.value, "content": msg.content}
                    )

            # Prepara parâmetros
            params: dict[str, Any] = {
                "model": model or "claude-3-opus-20240229",
                "messages": claude_messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                **kwargs,
            }

            if system_content:
                params["system"] = system_content

            # Adiciona tools
            if tools:
                params["tools"] = self.format_tools_for_api(tools)

            # Faz a chamada
            response = await self.client.messages.create(**params)
            return ClaudeCompletionResponse(response)

        except Exception as e:
            raise LLMProviderError(
                f"Erro na completion Claude: {str(e)}",
                details={"provider": "anthropic", "model": model},
            ) from e

    async def stream_complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Streaming completion with Claude — yields StreamChunk events."""
        try:
            system_content: str | None = None
            claude_messages: list[dict[str, Any]] = []

            for msg in messages:
                if msg.role == MessageRole.SYSTEM:
                    system_content = msg.content
                else:
                    claude_messages.append(
                        {"role": msg.role.value, "content": msg.content}
                    )

            params: dict[str, Any] = {
                "model": model or "claude-3-opus-20240229",
                "messages": claude_messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                **kwargs,
            }

            if system_content:
                params["system"] = system_content

            if tools:
                params["tools"] = self.format_tools_for_api(tools)

            async with self.client.messages.stream(**params) as stream:
                current_tool_id: str | None = None
                current_tool_name: str | None = None

                async for event in stream:
                    # Text delta
                    if event.type == "content_block_delta":
                        if hasattr(event.delta, "text"):
                            yield StreamChunk(
                                event=StreamEventType.TOKEN,
                                content=event.delta.text,
                            )
                        elif hasattr(event.delta, "partial_json"):
                            yield StreamChunk(
                                event=StreamEventType.TOOL_CALL_ARGS,
                                tool_call_id=current_tool_id,
                                tool_arguments_delta=event.delta.partial_json,
                            )

                    # Content block start (tool_use)
                    elif event.type == "content_block_start":
                        if hasattr(event.content_block, "type") and event.content_block.type == "tool_use":
                            current_tool_id = event.content_block.id
                            current_tool_name = event.content_block.name
                            yield StreamChunk(
                                event=StreamEventType.TOOL_CALL_START,
                                tool_call_id=current_tool_id,
                                tool_name=current_tool_name,
                            )

                    # Content block stop
                    elif event.type == "content_block_stop":
                        if current_tool_id:
                            yield StreamChunk(
                                event=StreamEventType.TOOL_CALL_END,
                                tool_call_id=current_tool_id,
                                tool_name=current_tool_name,
                            )
                            current_tool_id = None
                            current_tool_name = None

                    # Message stop
                    elif event.type == "message_stop":
                        yield StreamChunk(
                            event=StreamEventType.DONE,
                            finish_reason="stop",
                        )

        except Exception as e:
            yield StreamChunk(
                event=StreamEventType.ERROR,
                content=str(e),
            )
            raise LLMProviderError(
                f"Erro no streaming Claude: {str(e)}",
                details={"provider": "anthropic", "model": model},
            ) from e

    def format_tools_for_api(self, tools: list[ToolSchema]) -> list[dict[str, Any]]:
        """Formata tools para Claude tool use."""
        formatted_tools: list[dict[str, Any]] = []

        for tool in tools:
            # Monta input_schema
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

            # Formato Claude
            formatted_tools.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                }
            )

        return formatted_tools
