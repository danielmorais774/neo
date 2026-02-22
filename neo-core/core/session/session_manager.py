"""Persistent Session Manager — manages conversation sessions with topic detection.

Responsibilities:
- Create / retrieve / close sessions
- Detect topic changes via embeddings → auto-rotate sessions
- Recover context from previous sessions when needed
- Track inactivity timeouts
"""

import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from core.session.embedding_service import EmbeddingService
from db.async_client import AsyncPostgresClient
from db.models.conversation import ConversationMessage, ConversationSession
from shared.config import get_settings

logger = logging.getLogger(__name__)


class PersistentSessionManager:
    """Manages conversation sessions with automatic topic rotation."""

    def __init__(
        self,
        db: AsyncPostgresClient,
        embedding_service: EmbeddingService,
    ) -> None:
        self.db = db
        self.embeddings = embedding_service
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
        """Get existing active session or create a new one.

        If session_id is given, looks it up directly.
        Otherwise finds the most recent active session for user/service.
        Creates a new session if none found or existing one is expired.
        """
        session: ConversationSession | None = None

        if session_id:
            session = await self.db.get_conversation_session(session_id)

        if not session:
            session = await self.db.get_active_session(user_id=user_id, service=service)

        # Check if session is expired by inactivity
        if session and self._is_expired(session):
            logger.info(f"Session {session.id} expired by inactivity, closing")
            await self.close_session(session.id, reason="inactivity")
            session = None

        if not session:
            session = await self.db.create_conversation_session(
                user_id=user_id,
                service=service,
            )
            logger.info(f"Created new session {session.id}")

        return session

    async def close_session(
        self,
        session_id: UUID,
        reason: str = "manual",
    ) -> None:
        """Close a session and generate topic summary/embedding."""
        session = await self.db.get_conversation_session(session_id)
        if not session or session.status != "active":
            return

        # Generate topic summary from conversation
        messages = await self.db.fetch_session_messages(session_id, limit=50)
        if messages and self.embeddings.available:
            summary = self._build_topic_summary(messages)
            session.topic_summary = summary
            try:
                session.topic_embedding = await self.embeddings.embed(summary)
            except Exception as e:
                logger.warning(f"Failed to generate topic embedding: {e}")

        session.status = "closed"
        session.ended_at = datetime.utcnow()
        session.extra_metadata = {**(session.extra_metadata or {}), "close_reason": reason}
        await self.db.update_session(session)
        logger.info(f"Closed session {session_id} (reason={reason})")

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
        """Add a message to the session and optionally generate its embedding."""
        embedding = None
        if generate_embedding and self.embeddings.available and role in ("user", "assistant"):
            try:
                embedding = await self.embeddings.embed(content)
            except Exception as e:
                logger.warning(f"Failed to generate message embedding: {e}")

        return await self.db.save_message(
            session_id=session_id,
            role=role,
            content=content,
            embedding=embedding,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            metadata=metadata,
        )

    async def get_history(
        self,
        session_id: UUID,
        limit: int = 20,
    ) -> list[dict[str, str]]:
        """Get conversation history as [{role, content}] for LLM context."""
        return await self.db.fetch_session_history(session_id, limit=limit)

    # ---------------------------------------------------------------
    # Topic change detection
    # ---------------------------------------------------------------

    async def should_rotate_session(
        self,
        session: ConversationSession,
        new_message: str,
    ) -> bool:
        """Determine if the new message is different enough to warrant a new session.

        Uses embedding similarity between the conversation context and the new message.
        """
        if not self.embeddings.available:
            return False

        # Don't rotate on first few messages
        if session.message_count < 3:
            return False

        # Build context from recent messages
        recent = await self.db.fetch_session_messages(session.id, limit=10)
        if not recent:
            return False

        context_text = " ".join(m.content for m in recent[-5:] if m.role in ("user", "assistant"))

        try:
            embeddings = await self.embeddings.embed_batch([context_text, new_message])
            similarity = EmbeddingService.cosine_similarity(embeddings[0], embeddings[1])
            logger.debug(f"Topic similarity: {similarity:.3f} (threshold: {self.settings.session_topic_similarity_threshold})")

            return similarity < self.settings.session_topic_similarity_threshold
        except Exception as e:
            logger.warning(f"Topic change detection failed: {e}")
            return False

    # ---------------------------------------------------------------
    # Context recovery from previous sessions
    # ---------------------------------------------------------------

    async def recover_context(
        self,
        message: str,
        user_id: UUID | None = None,
        service: str = "neo-desktop",
    ) -> list[dict[str, Any]]:
        """Search previous closed sessions for relevant context.

        Returns summaries + key messages from similar past sessions.
        """
        if not self.embeddings.available:
            return []

        try:
            msg_embedding = await self.embeddings.embed(message)
        except Exception as e:
            logger.warning(f"Failed to embed message for context recovery: {e}")
            return []

        similar_sessions = await self.db.find_similar_sessions(
            embedding=msg_embedding,
            user_id=user_id,
            service=service,
            limit=self.settings.session_history_recovery_limit,
        )

        context_items: list[dict[str, Any]] = []
        for session, similarity in similar_sessions:
            messages = await self.db.fetch_session_messages(session.id, limit=10)
            context_items.append({
                "session_id": str(session.id),
                "topic_summary": session.topic_summary,
                "similarity": round(similarity, 3),
                "started_at": session.started_at.isoformat() if session.started_at else None,
                "messages": [
                    {"role": m.role, "content": m.content}
                    for m in messages
                ],
            })

        return context_items

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    def _is_expired(self, session: ConversationSession) -> bool:
        """Check if session is expired by inactivity timeout."""
        if session.status != "active":
            return True
        threshold = datetime.utcnow() - timedelta(minutes=self.settings.session_inactivity_minutes)
        return session.last_activity_at < threshold

    @staticmethod
    def _build_topic_summary(messages: list[ConversationMessage]) -> str:
        """Build a concise topic summary from messages for embedding."""
        user_msgs = [m.content for m in messages if m.role == "user"]
        # Take first + last user messages for topic range
        if len(user_msgs) <= 3:
            summary_parts = user_msgs
        else:
            summary_parts = [user_msgs[0], user_msgs[len(user_msgs) // 2], user_msgs[-1]]
        return " | ".join(summary_parts)[:500]
