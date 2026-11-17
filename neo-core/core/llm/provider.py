"""Interface base para LLM providers."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from shared import Message, StreamChunk, ToolCall, ToolSchema


class CompletionResponse(ABC):
    """Resposta de uma completion do LLM."""

    @abstractmethod
    def get_content(self) -> str | None:
        """Retorna o conteúdo da resposta."""
        pass

    @abstractmethod
    def get_tool_calls(self) -> list[ToolCall]:
        """Retorna as tool calls solicitadas."""
        pass

    @abstractmethod
    def finish_reason(self) -> str:
        """Retorna o motivo de término (stop, tool_calls, length, etc)."""
        pass

    def reasoning_tokens(self) -> int | None:
        """Tokens que o modelo gastou raciocinando; ``None`` se o provider não diz.

        Saem do mesmo ``max_tokens`` da resposta — ver ``core/llm/teto_adaptativo``.
        """
        return None


class LLMProvider(ABC):
    """Interface base para providers de LLM."""

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        """
        Inicializa o provider.

        Args:
            api_key: Chave de API do provider
            **kwargs: Argumentos adicionais específicos do provider
        """
        self.api_key = api_key
        self.config = kwargs

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> CompletionResponse:
        """
        Realiza uma completion com o LLM.

        Args:
            messages: Histórico de mensagens
            tools: Lista de tools disponíveis
            model: Nome do modelo a usar
            temperature: Temperatura da geração
            max_tokens: Máximo de tokens
            **kwargs: Parâmetros adicionais do provider

        Returns:
            CompletionResponse com a resposta do LLM

        Raises:
            LLMProviderError: Em caso de erro na comunicação
        """
        pass

    @abstractmethod
    async def stream_complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """
        Realiza completion com streaming.

        Args:
            messages: Histórico de mensagens
            tools: Lista de tools disponíveis
            model: Nome do modelo
            temperature: Temperatura
            max_tokens: Máximo de tokens
            **kwargs: Parâmetros adicionais

        Yields:
            StreamChunk with event type and content
        """
        pass

    @abstractmethod
    def format_tools_for_api(self, tools: list[ToolSchema]) -> Any:
        """
        Formata tools para o formato esperado pela API do provider.

        Args:
            tools: Lista de schemas de tools

        Returns:
            Tools formatadas no formato do provider
        """
        pass
