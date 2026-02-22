"""Gerenciamento de contexto e memória de conversação."""

from datetime import datetime
from uuid import UUID, uuid4

from shared import AgentStep, Message


class ConversationMemory:
    """Gerencia histórico de conversação de uma sessão."""

    def __init__(self, session_id: UUID | None = None) -> None:
        """
        Inicializa a memória da conversação.

        Args:
            session_id: ID da sessão (gera novo se não fornecido)
        """
        self.session_id = session_id or uuid4()
        self.messages: list[Message] = []
        self.steps: list[AgentStep] = []
        self.created_at = datetime.now()
        self.updated_at = datetime.now()

    def add_message(self, message: Message) -> None:
        """
        Adiciona uma mensagem ao histórico.

        Args:
            message: Mensagem a adicionar
        """
        self.messages.append(message)
        self.updated_at = datetime.now()

    def add_step(self, step: AgentStep) -> None:
        """
        Adiciona um step do agente.

        Args:
            step: Step a adicionar
        """
        self.steps.append(step)
        self.updated_at = datetime.now()

    def get_messages(self) -> list[Message]:
        """Retorna todas as mensagens."""
        return self.messages.copy()

    def get_recent_messages(self, count: int = 10) -> list[Message]:
        """
        Retorna as N mensagens mais recentes.

        Args:
            count: Número de mensagens

        Returns:
            Lista de mensagens recentes
        """
        return self.messages[-count:]

    def get_steps(self) -> list[AgentStep]:
        """Retorna todos os steps."""
        return self.steps.copy()

    def clear(self) -> None:
        """Limpa todo o histórico."""
        self.messages.clear()
        self.steps.clear()
        self.updated_at = datetime.now()

    def message_count(self) -> int:
        """Retorna número de mensagens."""
        return len(self.messages)

    def step_count(self) -> int:
        """Retorna número de steps."""
        return len(self.steps)
