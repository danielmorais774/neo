"""Persistent Session Manager — sessões com topic detection e memória híbrida.

Responsabilidades:
- Criar / recuperar / fechar sessões
- Detectar mudanças de tópico via embeddings → auto-rotacionar
- Recuperar contexto de sessões anteriores via busca híbrida
- Contagem de tokens ao salvar mensagens
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, TYPE_CHECKING
from uuid import UUID

from core.session.embedding_service import EmbeddingService
from db.async_client import AsyncPostgresClient
from db.models.conversation import ConversationMessage, ConversationSession
from shared.config import get_settings
from shared.types import MemorySearchResult

if TYPE_CHECKING:
    from core.agent.token_counter import TokenCounter
    from core.memory.hybrid_search import HybridMemorySearch

logger = logging.getLogger(__name__)


class PersistentSessionManager:
    """Gerencia sessões com rotação automática e recuperação de contexto."""

    def __init__(
        self,
        db: AsyncPostgresClient,
        embedding_service: EmbeddingService,
        hybrid_search: HybridMemorySearch | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self.db = db
        self.embeddings = embedding_service
        self.hybrid_search = hybrid_search
        self.token_counter = token_counter
        self.settings = get_settings()

    # ---------------------------------------------------------------
    # Session lifecycle
    # ---------------------------------------------------------------

    async def get_or_create_session(
        self,
        session_id: UUID | None = None,
        user_id: UUID | None = None,
        service: str = "neo-desktop",
    ) -> ConversationSession:
        """Obtém sessão ativa existente ou cria uma nova."""
        session: ConversationSession | None = None

        if session_id:
            session = await self.db.get_conversation_session(session_id)

        if not session:
            session = await self.db.get_active_session(user_id=user_id, service=service)

        if session and self._is_expired(session):
            logger.info(f"Sessão {session.id} expirada por inatividade")
            await self.close_session(session.id, reason="inactivity")
            session = None

        if not session:
            session = await self.db.create_conversation_session(
                user_id=user_id, service=service,
            )
            logger.info(f"Nova sessão criada: {session.id}")

        return session

    async def close_session(self, session_id: UUID, reason: str = "manual") -> None:
        """Fecha sessão e gera resumo/embedding do tópico."""
        session = await self.db.get_conversation_session(session_id)
        if not session or session.status != "active":
            return

        messages = await self.db.fetch_session_messages(session_id, limit=50)
        if messages and self.embeddings.available:
            summary = self._build_topic_summary(messages)
            session.topic_summary = summary
            try:
                session.topic_embedding = await self.embeddings.embed(summary)
            except Exception as e:
                logger.warning(f"Falha ao gerar embedding do tópico: {e}")

        session.status = "closed"
        session.ended_at = datetime.utcnow()
        session.extra_metadata = {**(session.extra_metadata or {}), "close_reason": reason}
        await self.db.update_session(session)
        logger.info(f"Sessão {session_id} fechada (motivo={reason})")

    # ---------------------------------------------------------------
    # Message management
    # ---------------------------------------------------------------

    async def add_message(
        self,
        session_id: UUID,
        role: str,
        content: str,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        generate_embedding: bool = False,
    ) -> ConversationMessage:
        """Adiciona mensagem com contagem de tokens e embedding opcional."""
        embedding = None
        if generate_embedding and self.embeddings.available and role in ("user", "assistant"):
            try:
                embedding = await self.embeddings.embed(content)
            except Exception as e:
                logger.warning(f"Falha ao gerar embedding: {e}")

        # Contagem de tokens
        token_count = None
        if self.token_counter:
            token_count = self.token_counter.count(content)

        return await self.db.save_message(
            session_id=session_id,
            role=role,
            content=content,
            embedding=embedding,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            metadata=metadata,
            token_count=token_count,
        )

    async def get_history(self, session_id: UUID, limit: int = 20) -> list[dict[str, str]]:
        """Obtém histórico de conversação para o LLM."""
        return await self.db.fetch_session_history(session_id, limit=limit)

    # ---------------------------------------------------------------
    # Topic change detection
    # ---------------------------------------------------------------

    async def should_rotate_session(
        self,
        session: ConversationSession,
        new_message: str,
    ) -> dict[str, Any]:
        """Detecta mudança de tópico via similaridade de embeddings.

        Conservative approach: only rotate on extreme topic changes after a
        significant number of messages AND a cooldown period.

        Returns a dict with rotation details:
            should_rotate (bool), similarity (float|None), threshold (float),
            reason (str), message_count (int), min_required (int),
            cooldown_minutes (int), elapsed_minutes (float|None),
            context_text (str), duration_ms (float)
        """
        import time as _time
        t0 = _time.monotonic()

        base = {
            "should_rotate": False,
            "similarity": None,
            "threshold": self.settings.session_topic_similarity_threshold,
            "reason": "",
            "message_count": session.message_count,
            "min_required": getattr(self.settings, 'session_rotation_min_messages', 8),
            "cooldown_minutes": getattr(self.settings, 'session_rotation_cooldown_minutes', 5),
            "elapsed_minutes": None,
            "context_text": "",
            "new_message": new_message,
            "duration_ms": 0,
        }

        if not self.embeddings.available:
            base["reason"] = "embeddings indisponíveis"
            base["duration_ms"] = (_time.monotonic() - t0) * 1000
            return base

        min_msgs = base["min_required"]
        if session.message_count < min_msgs:
            base["reason"] = f"poucas mensagens ({session.message_count} < {min_msgs})"
            base["duration_ms"] = (_time.monotonic() - t0) * 1000
            return base

        # Short messages (game moves, yes/no, numbers) are likely continuations.
        if len(new_message.strip()) < 30:
            base["reason"] = f"mensagem curta ({len(new_message.strip())} chars < 30)"
            base["duration_ms"] = (_time.monotonic() - t0) * 1000
            return base

        # Time-based cooldown: don't rotate if session was active recently.
        cooldown = base["cooldown_minutes"]
        if cooldown > 0 and session.last_activity_at:
            elapsed = (datetime.utcnow() - session.last_activity_at).total_seconds() / 60
            base["elapsed_minutes"] = elapsed
            if elapsed < cooldown:
                base["reason"] = f"cooldown ativo ({elapsed:.1f}min < {cooldown}min)"
                base["duration_ms"] = (_time.monotonic() - t0) * 1000
                logger.debug(f"Topic rotation skipped: session active {elapsed:.1f}min ago (cooldown={cooldown}min)")
                return base

        recent = await self.db.fetch_session_messages(session.id, limit=10)
        if not recent:
            base["reason"] = "sem mensagens recentes para comparar"
            base["duration_ms"] = (_time.monotonic() - t0) * 1000
            return base

        context_text = " ".join(
            m.content for m in recent[-5:] if m.role in ("user", "assistant")
        )
        base["context_text"] = context_text

        try:
            embeddings = await self.embeddings.embed_batch([context_text, new_message])
            similarity = EmbeddingService.cosine_similarity(embeddings[0], embeddings[1])
            base["similarity"] = similarity
            base["duration_ms"] = (_time.monotonic() - t0) * 1000
            logger.debug(f"Similaridade de tópico: {similarity:.3f}")

            if similarity < self.settings.session_topic_similarity_threshold:
                base["should_rotate"] = True
                base["reason"] = f"similaridade {similarity:.4f} < threshold {self.settings.session_topic_similarity_threshold}"
            else:
                base["reason"] = f"similaridade {similarity:.4f} >= threshold {self.settings.session_topic_similarity_threshold}"
            return base
        except Exception as e:
            logger.warning(f"Detecção de tópico falhou: {e}")
            base["reason"] = f"erro: {e}"
            base["duration_ms"] = (_time.monotonic() - t0) * 1000
            return base

    # ---------------------------------------------------------------
    # Context recovery — busca híbrida
    # ---------------------------------------------------------------

    async def recover_context(
        self,
        message: str,
        user_id: UUID | None = None,
        service: str = "neo-desktop",
    ) -> list[MemorySearchResult]:
        """Recupera contexto de sessões e mensagens anteriores.

        Usa busca híbrida (vetor + FTS + temporal) se disponível,
        senão fallback para busca vetorial pura.
        """
        # Busca híbrida
        if self.hybrid_search:
            try:
                return await self.hybrid_search.search(
                    query=message,
                    user_id=user_id,
                    service=service,
                    limit=self.settings.session_history_recovery_limit,
                )
            except Exception as e:
                logger.warning(f"Busca híbrida falhou, usando fallback: {e}")

        # Fallback: busca vetorial pura
        return await self._vector_only_recovery(message, user_id, service)

    async def _vector_only_recovery(
        self,
        message: str,
        user_id: UUID | None,
        service: str,
    ) -> list[MemorySearchResult]:
        """Fallback: recuperação por vetor puro (comportamento original)."""
        if not self.embeddings.available:
            return []

        try:
            msg_embedding = await self.embeddings.embed(message)
        except Exception as e:
            logger.warning(f"Falha ao gerar embedding para recovery: {e}")
            return []

        similar_sessions = await self.db.find_similar_sessions(
            embedding=msg_embedding,
            user_id=user_id,
            service=service,
            limit=self.settings.session_history_recovery_limit,
        )

        results: list[MemorySearchResult] = []
        for session, similarity in similar_sessions:
            results.append(
                MemorySearchResult(
                    content=session.topic_summary or "",
                    source="session",
                    similarity=similarity,
                    final_score=similarity,
                    session_id=session.id,
                    created_at=session.started_at,
                )
            )
        return results

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    def _is_expired(self, session: ConversationSession) -> bool:
        """Verifica se sessão expirou por inatividade."""
        if session.status != "active":
            return True
        threshold = datetime.utcnow() - timedelta(
            minutes=self.settings.session_inactivity_minutes,
        )
        return session.last_activity_at < threshold

    @staticmethod
    def _build_topic_summary(messages: list[ConversationMessage]) -> str:
        """Resumo conciso do tópico a partir das mensagens."""
        user_msgs = [m.content for m in messages if m.role == "user"]
        if len(user_msgs) <= 3:
            summary_parts = user_msgs
        else:
            summary_parts = [user_msgs[0], user_msgs[len(user_msgs) // 2], user_msgs[-1]]
        return " | ".join(summary_parts)[:500]
