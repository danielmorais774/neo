"""Context manager — gerencia contexto com compactação inteligente.

Substitui o sliding window simples por um sistema token-aware que:
  1. Conta tokens precisamente via tiktoken
  2. Compacta mensagens antigas quando excede o budget
  3. Preserva: system messages, últimas N mensagens, última tool call
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from shared.types import AgentStep, CompactionResult, Message, MessageRole, ToolResult

if TYPE_CHECKING:
    from core.agent.context_compactor import ContextCompactor
    from core.agent.token_counter import TokenCounter

logger = logging.getLogger(__name__)

# Mensagens recentes que NUNCA são compactadas
_PROTECTED_TAIL_SIZE = 6


class ContextManager:
    """Gerencia contexto com budget de tokens e compactação automática."""

    def __init__(
        self,
        token_counter: TokenCounter | None = None,
        compactor: ContextCompactor | None = None,
        max_context_tokens: int = 120_000,
        compaction_threshold: float = 0.75,
    ) -> None:
        self._tokens = token_counter
        self._compactor = compactor
        self._max_tokens = max_context_tokens
        self._threshold = compaction_threshold

        self._messages: list[Message] = []
        self._current_step = 0
        self._compaction_applied = False

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def set_initial_messages(self, messages: list[Message]) -> None:
        """Define mensagens iniciais (system + user task)."""
        self._messages = messages.copy()
        self._current_step = 0
        self._compaction_applied = False

    # ------------------------------------------------------------------
    # Adicionar conteúdo
    # ------------------------------------------------------------------

    def add_assistant_message(self, content: str) -> None:
        """Adiciona resposta do assistente."""
        if content:
            self._messages.append(Message(role=MessageRole.ASSISTANT, content=content))

    def add_user_message(self, content: str) -> None:
        """Adiciona um aviso do próprio loop ao modelo (não é fala do usuário)."""
        if content:
            self._messages.append(Message(role=MessageRole.USER, content=content))

    def add_tool_results(self, step: AgentStep) -> None:
        """Adiciona resultados de tools ao contexto."""
        results_text = self._format_tool_results(step.tool_results)
        self._messages.append(Message(role=MessageRole.USER, content=results_text))
        self._current_step += 1

    # ------------------------------------------------------------------
    # Obter contexto
    # ------------------------------------------------------------------

    async def get_messages(self) -> list[Message]:
        """Retorna mensagens respeitando budget de tokens.

        Se o total exceder o threshold, compacta mensagens antigas
        automaticamente. Sempre preserva system messages e as últimas
        N mensagens.
        """
        if self._tokens and self._compactor:
            return await self._get_messages_with_compaction()
        return self._get_messages_sliding_window()

    def get_messages_sync(self) -> list[Message]:
        """Versão síncrona — sempre usa sliding window."""
        return self._get_messages_sliding_window()

    @property
    def compaction_applied(self) -> bool:
        """True se compactação foi aplicada nesta execução."""
        return self._compaction_applied

    def current_step_number(self) -> int:
        """Retorna número do step atual."""
        return self._current_step

    # ------------------------------------------------------------------
    # Estratégias de contexto
    # ------------------------------------------------------------------

    async def _get_messages_with_compaction(self) -> list[Message]:
        """Contexto token-aware com compactação automática."""
        assert self._tokens is not None
        assert self._compactor is not None

        current_count = self._tokens.count_messages(self._messages)

        # Dentro do budget? Retorna tudo
        if current_count <= int(self._max_tokens * self._threshold):
            return self._messages.copy()

        logger.info(
            f"Contexto excede threshold: {current_count} tokens "
            f"(limite: {int(self._max_tokens * self._threshold)}). Compactando..."
        )

        # Separar mensagens protegidas
        system_msgs = [m for m in self._messages if m.role == MessageRole.SYSTEM]
        other_msgs = [m for m in self._messages if m.role != MessageRole.SYSTEM]

        if len(other_msgs) <= _PROTECTED_TAIL_SIZE:
            return self._messages.copy()

        # Mensagens antigas (para compactar) vs recentes (protegidas)
        to_compact = other_msgs[:-_PROTECTED_TAIL_SIZE]
        protected = other_msgs[-_PROTECTED_TAIL_SIZE:]

        # Budget para o resumo
        system_tokens = self._tokens.count_messages(system_msgs)
        protected_tokens = self._tokens.count_messages(protected)
        summary_budget = self._max_tokens - system_tokens - protected_tokens - 500

        try:
            result: CompactionResult = await self._compactor.compact(to_compact, summary_budget)

            # Reconstruir contexto: system + resumo + mensagens recentes
            summary_msg = Message(
                role=MessageRole.USER,
                content=f"[Resumo do contexto anterior]\n{result.summary}",
            )

            self._compaction_applied = True
            return system_msgs + [summary_msg] + protected

        except Exception as e:
            logger.error(f"Compactação falhou, usando sliding window: {e}")
            return self._get_messages_sliding_window()

    def _get_messages_sliding_window(self) -> list[Message]:
        """Fallback: sliding window por contagem (comportamento original)."""
        max_msgs = 50

        if len(self._messages) <= max_msgs:
            return self._messages.copy()

        system_msgs = [m for m in self._messages if m.role == MessageRole.SYSTEM]
        other_msgs = [m for m in self._messages if m.role != MessageRole.SYSTEM]

        recent = other_msgs[-(max_msgs - len(system_msgs)):]
        return system_msgs + recent

    # ------------------------------------------------------------------
    # Formatação
    # ------------------------------------------------------------------

    @staticmethod
    def _format_tool_results(results: list[ToolResult]) -> str:
        """Formata resultados de tools para o LLM."""
        if not results:
            return "No tool results."

        lines = ["Tool execution results:\n"]
        for result in results:
            lines.append(f"Tool: {result.name}")
            if result.error:
                lines.append("Status: ERROR")
                lines.append(f"Error: {result.error}")
            else:
                lines.append("Status: SUCCESS")
                lines.append(f"Result: {result.result}")
            lines.append(f"Execution time: {result.execution_time:.2f}s")
            lines.append("")
        return "\n".join(lines)
