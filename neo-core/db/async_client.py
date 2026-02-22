"""Async PostgreSQL client — SQLAlchemy async engine + SQLModel models."""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlmodel import select

from db.models.user import User
from db.models.user_message import UserMessage
from db.models.conversation import ConversationSession, ConversationMessage
from shared.config import get_settings

logger = logging.getLogger(__name__)


class AsyncPostgresClient:
    """Async PostgreSQL client using SQLAlchemy async engine + SQLModel models."""

    def __init__(self, dsn: str | None = None) -> None:
        settings = get_settings()
        self.dsn = dsn or settings.database_url
        self.engine = create_async_engine(self.dsn, echo=False, pool_size=10, max_overflow=20)
        self.session_factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def dispose(self) -> None:
        """Dispose engine connections."""
        await self.engine.dispose()

    # ---------------------------------------------------------------
    # Session helpers
    # ---------------------------------------------------------------

    def get_session(self) -> AsyncSession:
        """Return a new async session."""
        return self.session_factory()

    # ---------------------------------------------------------------
    # ConversationSession CRUD
    # ---------------------------------------------------------------

    async def create_conversation_session(
        self,
        user_id: UUID | None = None,
        service: str = "neo-desktop",
        metadata: dict[str, Any] | None = None,
    ) -> ConversationSession:
        """Create a new conversation session."""
        session = ConversationSession(
            user_id=user_id,
            service=service,
            extra_metadata=metadata,
        )
        async with self.get_session() as db:
            db.add(session)
            await db.commit()
            await db.refresh(session)
        return session

    async def get_conversation_session(self, session_id: UUID) -> ConversationSession | None:
        """Get session by ID."""
        async with self.get_session() as db:
            return await db.get(ConversationSession, session_id)

    async def get_active_session(self, user_id: UUID | None = None, service: str = "neo-desktop") -> ConversationSession | None:
        """Get the most recent active session for a user/service."""
        async with self.get_session() as db:
            stmt = (
                select(ConversationSession)
                .where(ConversationSession.status == "active")
                .where(ConversationSession.service == service)
            )
            if user_id:
                stmt = stmt.where(ConversationSession.user_id == user_id)
            stmt = stmt.order_by(ConversationSession.last_activity_at.desc()).limit(1)
            result = await db.execute(stmt)
            return result.scalars().first()

    async def update_session(self, session: ConversationSession) -> None:
        """Update a session (topic, status, etc.)."""
        async with self.get_session() as db:
            db.add(session)
            await db.commit()
            await db.refresh(session)

    async def close_session(self, session_id: UUID) -> None:
        """Close a session."""
        async with self.get_session() as db:
            session = await db.get(ConversationSession, session_id)
            if session:
                from datetime import datetime
                session.status = "closed"
                session.ended_at = datetime.utcnow()
                await db.commit()

    async def find_similar_sessions(
        self,
        embedding: list[float],
        user_id: UUID | None = None,
        service: str = "neo-desktop",
        limit: int = 5,
        min_similarity: float = 0.5,
    ) -> list[tuple[ConversationSession, float]]:
        """Find sessions with similar topic embeddings using cosine distance."""
        async with self.get_session() as db:
            try:
                from pgvector.sqlalchemy import Vector
                from sqlalchemy import text

                # Use pgvector cosine similarity operator
                params = {
                    "embedding": str(embedding),
                    "service": service,
                    "limit": limit,
                    "min_sim": min_similarity,
                }
                user_filter = ""
                if user_id:
                    user_filter = "AND user_id = :user_id"
                    params["user_id"] = str(user_id)

                query = text(f"""
                    SELECT id, topic_summary, 
                           1 - (topic_embedding <=> :embedding::vector) as similarity
                    FROM conversation_session
                    WHERE status = 'closed'
                      AND topic_embedding IS NOT NULL
                      AND service = :service
                      {user_filter}
                      AND 1 - (topic_embedding <=> :embedding::vector) > :min_sim
                    ORDER BY topic_embedding <=> :embedding::vector
                    LIMIT :limit
                """)
                result = await db.execute(query, params)
                rows = result.fetchall()

                # Load full session objects
                sessions = []
                for row in rows:
                    sess = await db.get(ConversationSession, row[0])
                    if sess:
                        sessions.append((sess, float(row[2])))
                return sessions
            except Exception as e:
                logger.warning(f"pgvector search failed (extension may not be installed): {e}")
                return []

    # ---------------------------------------------------------------
    # ConversationMessage CRUD
    # ---------------------------------------------------------------

    async def save_message(
        self,
        session_id: UUID,
        role: str,
        content: str,
        embedding: list[float] | None = None,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationMessage:
        """Save a message to a conversation session."""
        msg = ConversationMessage(
            session_id=session_id,
            role=role,
            content=content,
            embedding=embedding,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            extra_metadata=metadata,
        )
        async with self.get_session() as db:
            db.add(msg)

            # Update session activity & message count
            session = await db.get(ConversationSession, session_id)
            if session:
                from datetime import datetime
                session.last_activity_at = datetime.utcnow()
                session.message_count += 1

            await db.commit()
            await db.refresh(msg)
        return msg

    async def fetch_session_messages(
        self,
        session_id: UUID,
        limit: int = 50,
    ) -> list[ConversationMessage]:
        """Fetch messages for a session, ordered by creation time."""
        async with self.get_session() as db:
            stmt = (
                select(ConversationMessage)
                .where(ConversationMessage.session_id == session_id)
                .order_by(ConversationMessage.created_at.asc())
                .limit(limit)
            )
            result = await db.execute(stmt)
            return list(result.scalars().all())

    async def fetch_session_history(
        self,
        session_id: UUID,
        limit: int = 20,
    ) -> list[dict[str, str]]:
        """Fetch session messages as [{role, content}] for LLM context."""
        messages = await self.fetch_session_messages(session_id, limit=limit)
        return [{"role": m.role, "content": m.content} for m in messages]

    # ---------------------------------------------------------------
    # Legacy compatibility — UserMessage methods
    # ---------------------------------------------------------------

    async def save_user_message(
        self,
        service: str,
        content: str,
        role: str = "user",
        user_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UUID:
        """Legacy: save to user_message table."""
        async with self.get_session() as db:
            msg = UserMessage(
                user_id=user_id,
                service=service,
                role=role,
                content=content,
                extra_metadata=metadata,
            )
            db.add(msg)
            await db.commit()
            await db.refresh(msg)
            return msg.id

    # ---------------------------------------------------------------
    # User methods
    # ---------------------------------------------------------------

    async def get_user_by_username(self, username: str) -> User | None:
        """Get user by username."""
        async with self.get_session() as db:
            stmt = select(User).where(User.username == username)
            result = await db.execute(stmt)
            return result.scalars().first()
