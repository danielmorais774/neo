"""Context manager - gerencia contexto durante execução."""

from shared import AgentStep, Message, MessageRole, ToolResult


class ContextManager:
    """Gerencia contexto e histórico durante execução do agente."""

    def __init__(self, max_context_messages: int = 50) -> None:
        """
        Inicializa o manager.

        Args:
            max_context_messages: Máximo de mensagens no contexto
        """
        self.max_context_messages = max_context_messages
        self._messages: list[Message] = []
        self._current_step = 0

    def set_initial_messages(self, messages: list[Message]) -> None:
        """
        Define mensagens iniciais.

        Args:
            messages: Mensagens iniciais (system + user)
        """
        self._messages = messages.copy()
        self._current_step = 0

    def add_assistant_message(self, content: str) -> None:
        """
        Adiciona resposta do assistente.

        Args:
            content: Conteúdo da resposta
        """
        if content:  # Pode ser None se só tem tool_calls
            self._messages.append(Message(role=MessageRole.ASSISTANT, content=content))

    def add_tool_results(self, step: AgentStep) -> None:
        """
        Adiciona resultados de tools ao contexto.

        Args:
            step: Step com resultados
        """
        # Formata resultados para o LLM
        results_text = self._format_tool_results(step.tool_results)
        self._messages.append(Message(role=MessageRole.USER, content=results_text))
        self._current_step += 1

    def get_messages(self) -> list[Message]:
        """
        Retorna mensagens atuais, respeitando limite.

        Returns:
            Lista de mensagens
        """
        # Mantém system message + últimas N mensagens
        if len(self._messages) <= self.max_context_messages:
            return self._messages.copy()

        # Preserva system message
        system_msgs = [m for m in self._messages if m.role == MessageRole.SYSTEM]
        other_msgs = [m for m in self._messages if m.role != MessageRole.SYSTEM]

        # Pega as mais recentes
        recent = other_msgs[-(self.max_context_messages - len(system_msgs)) :]
        return system_msgs + recent

    def current_step_number(self) -> int:
        """Retorna número do step atual."""
        return self._current_step

    def _format_tool_results(self, results: list[ToolResult]) -> str:
        """
        Formata resultados de tools para o LLM.

        Args:
            results: Lista de resultados

        Returns:
            Texto formatado
        """
        if not results:
            return "No tool results."

        lines = ["Tool execution results:\n"]

        for result in results:
            lines.append(f"Tool: {result.name}")
            if result.error:
                lines.append(f"Status: ERROR")
                lines.append(f"Error: {result.error}")
            else:
                lines.append(f"Status: SUCCESS")
                lines.append(f"Result: {result.result}")
            lines.append(f"Execution time: {result.execution_time:.2f}s")
            lines.append("")

        return "\n".join(lines)
