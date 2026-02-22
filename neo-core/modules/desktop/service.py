"""Desktop service - lógica de negócio do módulo desktop."""

from typing import Any
from uuid import UUID

from core import AgentExecutor, LLMRouter, ToolExecutor, ToolRegistry
from core.memory import SessionManager
from modules.desktop.tools import DESKTOP_TOOLS
from shared import ToolSchema


class DesktopService:
    """Service layer para o módulo desktop."""

    def __init__(
        self,
        agent_executor: AgentExecutor,
        session_manager: SessionManager,
        tool_registry: ToolRegistry,
    ) -> None:
        """
        Inicializa o service.

        Args:
            agent_executor: Executor do agente
            session_manager: Manager de sessões
            tool_registry: Registry de tools
        """
        self.agent = agent_executor
        self.sessions = session_manager
        self.registry = tool_registry

        # Registra tools do desktop
        self._register_desktop_tools()

    def _register_desktop_tools(self) -> None:
        """Registra todas as tools do desktop no registry."""
        for tool in DESKTOP_TOOLS:
            if not self.registry.exists(tool.name):
                self.registry.register(tool)

    async def execute_task(
        self,
        task: str,
        session_id: UUID | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """
        Executa uma task do usuário.

        Args:
            task: Descrição da task
            session_id: ID da sessão
            provider: Provider LLM (opcional)
            model: Modelo específico (opcional)

        Returns:
            Resultado da execução
        """
        # Obtém ou cria sessão
        memory = self.sessions.get_or_create_session(session_id)

        # Executa com o agente
        result = await self.agent.execute(
            task=task,
            session_id=session_id,
            memory=memory,
            provider=provider,
            model=model,
        )

        return result

    def get_available_tools(self) -> list[ToolSchema]:
        """
        Retorna lista de tools disponíveis no desktop.

        Returns:
            Lista de schemas de tools
        """
        return self.registry.get_by_module("desktop")

    def get_session_history(self, session_id: UUID) -> dict[str, Any] | None:
        """
        Retorna histórico de uma sessão.

        Args:
            session_id: ID da sessão

        Returns:
            Histórico da sessão ou None
        """
        memory = self.sessions.get_session(session_id)
        if not memory:
            return None

        return {
            "session_id": str(memory.session_id),
            "created_at": memory.created_at.isoformat(),
            "updated_at": memory.updated_at.isoformat(),
            "message_count": memory.message_count(),
            "step_count": memory.step_count(),
            "messages": [
                {"role": m.role.value, "content": m.content} for m in memory.messages
            ],
        }

    def clear_session(self, session_id: UUID) -> bool:
        """
        Limpa uma sessão.

        Args:
            session_id: ID da sessão

        Returns:
            True se removida, False se não existia
        """
        return self.sessions.delete_session(session_id)

    def list_active_sessions(self) -> list[str]:
        """
        Lista sessões ativas.

        Returns:
            Lista de IDs de sessões
        """
        return [str(sid) for sid in self.sessions.list_sessions()]
