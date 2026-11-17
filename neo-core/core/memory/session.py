"""Session manager - gerencia sessões ativas."""

from uuid import UUID, uuid4

from core.memory.conversation import ConversationMemory


class SessionManager:
    """Gerencia sessões de conversação ativas."""

    def __init__(self) -> None:
        """Inicializa o manager."""
        self._sessions: dict[UUID, ConversationMemory] = {}

    def create_session(self, session_id: UUID | None = None) -> ConversationMemory:
        """
        Cria uma nova sessão.

        Args:
            session_id: ID da sessão (gera novo se não fornecido)

        Returns:
            Memória da nova sessão
        """
        sid = session_id or uuid4()
        memory = ConversationMemory(session_id=sid)
        self._sessions[sid] = memory
        return memory

    def get_session(self, session_id: UUID) -> ConversationMemory | None:
        """
        Retorna uma sessão existente.

        Args:
            session_id: ID da sessão

        Returns:
            Memória da sessão ou None se não existe
        """
        return self._sessions.get(session_id)

    def get_or_create_session(self, session_id: UUID | None = None) -> ConversationMemory:
        """
        Retorna sessão existente ou cria nova.

        Args:
            session_id: ID da sessão

        Returns:
            Memória da sessão
        """
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        return self.create_session(session_id)

    def delete_session(self, session_id: UUID) -> bool:
        """
        Remove uma sessão.

        Args:
            session_id: ID da sessão

        Returns:
            True se removida, False se não existia
        """
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False

    def list_sessions(self) -> list[UUID]:
        """Lista IDs de todas as sessões ativas."""
        return list(self._sessions.keys())

    def count_sessions(self) -> int:
        """Retorna número de sessões ativas."""
        return len(self._sessions)

    def clear_all(self) -> None:
        """Remove todas as sessões."""
        self._sessions.clear()
