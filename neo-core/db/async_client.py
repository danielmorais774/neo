"""Async PostgreSQL client — SQLAlchemy async engine + SQLModel models."""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import text, func
from sqlmodel import select

from db.models.user import User
from db.models.user_message import UserMessage
from db.models.conversation import ConversationSession, ConversationMessage
from db.models.user_preference import UserPreference
from db.models.mission import Mission
from db.models.whatsapp_contact import WhatsAppContact
from db.models.user_notification import UserNotification
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
        session_id: UUID | None = None,
        user_id: UUID | None = None,
        service: str = "neo-desktop",
        metadata: dict[str, Any] | None = None,
    ) -> ConversationSession:
        """Create a new conversation session."""
        if user_id is not None:
            await self.ensure_user_exists(user_id)
        session = ConversationSession(
            id=session_id if session_id else None,
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
                           1 - (topic_embedding <=> CAST(:embedding AS vector)) as similarity
                    FROM conversation_session
                    WHERE status = 'closed'
                      AND topic_embedding IS NOT NULL
                      AND service = :service
                      {user_filter}
                      AND 1 - (topic_embedding <=> CAST(:embedding AS vector)) > :min_sim
                    ORDER BY topic_embedding <=> CAST(:embedding AS vector)
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
    # Vector Search — Messages (pgvector)
    # ---------------------------------------------------------------

    async def find_similar_messages(
        self,
        embedding: list[float],
        user_id: UUID | None = None,
        service: str = "neo-desktop",
        limit: int = 20,
        min_similarity: float = 0.4,
    ) -> list[tuple[ConversationMessage, float]]:
        """Busca mensagens individuais por similaridade de embedding."""
        async with self.get_session() as db:
            try:
                params: dict[str, Any] = {
                    "embedding": str(embedding),
                    "service": service,
                    "limit": limit,
                    "min_sim": min_similarity,
                }
                user_filter = ""
                if user_id:
                    user_filter = "AND cm.user_id = :user_id"
                    params["user_id"] = str(user_id)

                query = text(f"""
                    SELECT cm.id,
                           1 - (cm.embedding <=> CAST(:embedding AS vector)) AS similarity
                    FROM conversation_message cm
                    JOIN conversation_session cs ON cs.id = cm.session_id
                    WHERE cm.embedding IS NOT NULL
                      AND cs.service = :service
                      {user_filter}
                      AND 1 - (cm.embedding <=> CAST(:embedding AS vector)) > :min_sim
                    ORDER BY cm.embedding <=> CAST(:embedding AS vector)
                    LIMIT :limit
                """)
                result = await db.execute(query, params)
                rows = result.fetchall()

                messages = []
                for row in rows:
                    msg = await db.get(ConversationMessage, row[0])
                    if msg:
                        messages.append((msg, float(row[1])))
                return messages
            except Exception as e:
                logger.warning(f"pgvector message search failed: {e}")
                return []

    # ---------------------------------------------------------------
    # Full-Text Search — Messages (PostgreSQL FTS)
    # ---------------------------------------------------------------

    async def search_messages_fts(
        self,
        query_text: str,
        user_id: UUID | None = None,
        service: str = "neo-desktop",
        limit: int = 20,
    ) -> list[tuple[ConversationMessage, float]]:
        """Busca mensagens por Full-Text Search (FTS) no PostgreSQL.

        Usa tsquery com fallback de idioma: português → inglês.
        """
        async with self.get_session() as db:
            try:
                params: dict[str, Any] = {
                    "query": query_text,
                    "service": service,
                    "limit": limit,
                }
                user_filter = ""
                if user_id:
                    user_filter = "AND cm.user_id = :user_id"
                    params["user_id"] = str(user_id)

                query = text(f"""
                    WITH fts AS (
                        SELECT cm.id,
                               ts_rank_cd(cm.content_tsv, query) AS rank
                        FROM conversation_message cm
                        JOIN conversation_session cs ON cs.id = cm.session_id,
                             plainto_tsquery('portuguese', :query) query
                        WHERE cm.content_tsv @@ query
                          AND cs.service = :service
                          {user_filter}

                        UNION ALL

                        SELECT cm.id,
                               ts_rank_cd(cm.content_tsv, query) AS rank
                        FROM conversation_message cm
                        JOIN conversation_session cs ON cs.id = cm.session_id,
                             plainto_tsquery('english', :query) query
                        WHERE cm.content_tsv @@ query
                          AND cs.service = :service
                          {user_filter}
                    )
                    SELECT DISTINCT ON (id) id, rank
                    FROM fts
                    ORDER BY id, rank DESC
                    LIMIT :limit
                """)
                result = await db.execute(query, params)
                rows = result.fetchall()

                messages = []
                for row in rows:
                    msg = await db.get(ConversationMessage, row[0])
                    if msg:
                        messages.append((msg, float(row[1])))
                return messages
            except Exception as e:
                logger.warning(f"FTS search failed: {e}")
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
        token_count: int | None = None,
        user_id: UUID | None = None,
    ) -> ConversationMessage:
        """Save a message to a conversation session."""
        msg = ConversationMessage(
            session_id=session_id,
            role=role,
            content=content,
            embedding=embedding,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            token_count=token_count,
            extra_metadata=metadata,
            user_id=user_id,
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
        roles: list[str] | None = None,
        exclude_tool_calls: bool = False,
        min_content_length: int = 0,
    ) -> list[ConversationMessage]:
        """Fetch the *last* N messages for a session, ordered chronologically.

        Uses a subquery with DESC to pick the most recent rows, then
        re-orders ASC so the conversation flows naturally.
        """
        from sqlalchemy import func

        async with self.get_session() as db:
            stmt = (
                select(ConversationMessage)
                .where(ConversationMessage.session_id == session_id)
            )

            # Filter by allowed roles (e.g. ['user', 'assistant'])
            if roles:
                stmt = stmt.where(ConversationMessage.role.in_(roles))

            # Exclude intermediate tool-call result messages
            if exclude_tool_calls:
                stmt = stmt.where(ConversationMessage.tool_call_id.is_(None))

            # Exclude very short messages (game moves, "ok", numbers)
            if min_content_length > 0:
                stmt = stmt.where(
                    func.length(ConversationMessage.content) > min_content_length
                )

            # Pick the LAST N rows: order DESC + limit, then wrap to re-order ASC
            stmt = stmt.order_by(ConversationMessage.created_at.desc()).limit(limit)
            subq = stmt.subquery()
            final_stmt = (
                select(ConversationMessage)
                .join(subq, ConversationMessage.id == subq.c.id)
                .order_by(ConversationMessage.created_at.asc())
            )
            result = await db.execute(final_stmt)
            return list(result.scalars().all())

    async def fetch_session_history(
        self,
        session_id: UUID,
        limit: int = 20,
    ) -> list[dict[str, str]]:
        """Fetch the last N user/assistant messages as [{role, content, token_count}] for LLM context."""
        messages = await self.fetch_session_messages(
            session_id,
            limit=limit,
            roles=["user", "assistant"],
            exclude_tool_calls=True,
            min_content_length=5,
        )
        return [
            {"role": m.role, "content": m.content, "token_count": m.token_count or 0}
            for m in messages
        ]

    async def fetch_similar_session_messages(
        self,
        session_id: UUID,
        embedding: list[float],
        limit: int = 5,
        min_similarity: float = 0.5,
    ) -> list[dict[str, str]]:
        """Fetch top-K semantically similar messages within a session via pgvector."""
        async with self.get_session() as db:
            try:
                query = text("""
                    SELECT cm.id, cm.role, cm.content, cm.token_count,
                           1 - (cm.embedding <=> CAST(:embedding AS vector)) AS similarity
                    FROM conversation_message cm
                    WHERE cm.session_id = :session_id
                      AND cm.embedding IS NOT NULL
                      AND cm.role IN ('user', 'assistant')
                      AND cm.tool_call_id IS NULL
                      AND 1 - (cm.embedding <=> CAST(:embedding AS vector)) > :min_sim
                    ORDER BY cm.embedding <=> CAST(:embedding AS vector)
                    LIMIT :limit
                """)
                result = await db.execute(query, {
                    "session_id": str(session_id),
                    "embedding": str(embedding),
                    "min_sim": min_similarity,
                    "limit": limit,
                })
                rows = result.fetchall()
                return [
                    {"role": r[1], "content": r[2], "token_count": r[3] or 0}
                    for r in rows
                ]
            except Exception as e:
                logger.warning(f"pgvector session similarity search failed: {e}")
                return []

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

    async def ensure_user_exists(self, user_id: UUID) -> None:
        """Create user record with given ID if it doesn't exist yet."""
        async with self.get_session() as db:
            existing = await db.get(User, user_id)
            if existing is None:
                db.add(User(id=user_id))
                await db.commit()
                logger.info(f"[DB] Auto-created user {user_id}")

    # ---------------------------------------------------------------
    # User Preferences CRUD
    # ---------------------------------------------------------------

    async def upsert_preference(
        self,
        category: str,
        key: str,
        value: str,
        user_id: UUID | None = None,
        service: str = "neo-desktop",
        source_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UserPreference:
        """Create or update a user preference (upsert by user+service+category+key)."""
        from datetime import datetime
        async with self.get_session() as db:
            stmt = select(UserPreference).where(
                UserPreference.service == service,
                UserPreference.category == category,
                UserPreference.key == key,
            )
            if user_id:
                stmt = stmt.where(UserPreference.user_id == user_id)
            else:
                stmt = stmt.where(UserPreference.user_id.is_(None))

            result = await db.execute(stmt)
            existing = result.scalars().first()

            if existing:
                existing.value = value
                existing.updated_at = datetime.utcnow()
                if source_message:
                    existing.source_message = source_message
                if metadata:
                    existing.extra_metadata = {**(existing.extra_metadata or {}), **metadata}
                db.add(existing)
                await db.commit()
                await db.refresh(existing)
                return existing

            pref = UserPreference(
                user_id=user_id,
                service=service,
                category=category,
                key=key,
                value=value,
                source_message=source_message,
                extra_metadata=metadata,
            )
            db.add(pref)
            await db.commit()
            await db.refresh(pref)
            return pref

    async def get_preferences(
        self,
        user_id: UUID | None = None,
        service: str = "neo-desktop",
        category: str | None = None,
    ) -> list[UserPreference]:
        """Get all preferences for a user/service, optionally filtered by category."""
        async with self.get_session() as db:
            stmt = select(UserPreference).where(UserPreference.service == service)
            if user_id:
                stmt = stmt.where(UserPreference.user_id == user_id)
            else:
                stmt = stmt.where(UserPreference.user_id.is_(None))
            if category:
                stmt = stmt.where(UserPreference.category == category)
            stmt = stmt.order_by(UserPreference.category, UserPreference.key)
            result = await db.execute(stmt)
            return list(result.scalars().all())

    async def delete_preference(
        self,
        category: str,
        key: str,
        user_id: UUID | None = None,
        service: str = "neo-desktop",
    ) -> bool:
        """Delete a specific preference. Returns True if found and deleted."""
        async with self.get_session() as db:
            stmt = select(UserPreference).where(
                UserPreference.service == service,
                UserPreference.category == category,
                UserPreference.key == key,
            )
            if user_id:
                stmt = stmt.where(UserPreference.user_id == user_id)
            else:
                stmt = stmt.where(UserPreference.user_id.is_(None))
            result = await db.execute(stmt)
            pref = result.scalars().first()
            if pref:
                await db.delete(pref)
                await db.commit()
                return True
            return False

    # ---------------------------------------------------------------
    # Mission CRUD
    # ---------------------------------------------------------------

    async def create_mission(
        self,
        user_id: UUID,
        session_id: UUID,
        parent_session_id: UUID | None,
        title: str,
        goal: str,
        contact_jid: str,
        channel: str = "whatsapp",
    ) -> Mission:
        """Create a new mission record."""
        mission = Mission(
            user_id=user_id,
            session_id=session_id,
            parent_session_id=parent_session_id,
            title=title,
            goal=goal,
            contact_jid=contact_jid,
            channel=channel,
        )
        async with self.get_session() as db:
            db.add(mission)
            await db.commit()
            await db.refresh(mission)
        return mission

    async def get_mission(self, mission_id: UUID) -> Mission | None:
        """Get a mission by ID."""
        async with self.get_session() as db:
            return await db.get(Mission, mission_id)

    async def find_active_mission_by_contact(self, contact_jid: str) -> Mission | None:
        """Find the active mission for a contact (awaiting_reply or awaiting_confirmation)."""
        async with self.get_session() as db:
            stmt = (
                select(Mission)
                .where(Mission.contact_jid == contact_jid)
                .where(Mission.status.in_(["awaiting_reply", "awaiting_confirmation"]))
                .order_by(Mission.last_activity_at.desc())
                .limit(1)
            )
            result = await db.execute(stmt)
            return result.scalars().first()

    async def find_active_confirmation_mission(self, user_id: UUID) -> Mission | None:
        """Find the most recent awaiting_confirmation mission for a user."""
        async with self.get_session() as db:
            stmt = (
                select(Mission)
                .where(Mission.user_id == user_id)
                .where(Mission.status == "awaiting_confirmation")
                .order_by(Mission.last_activity_at.desc())
                .limit(1)
            )
            result = await db.execute(stmt)
            return result.scalars().first()

    async def lock_mission_for_update(self, mission_id: UUID) -> Mission | None:
        """Lock a mission row for update to prevent concurrent agent runs."""
        async with self.get_session() as db:
            stmt = (
                select(Mission)
                .where(Mission.id == mission_id)
                .with_for_update()
            )
            result = await db.execute(stmt)
            return result.scalars().first()

    async def list_missions(
        self,
        user_id: UUID | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Mission]:
        """List missions optionally filtered by user_id and/or status."""
        async with self.get_session() as db:
            stmt = select(Mission)
            if user_id:
                stmt = stmt.where(Mission.user_id == user_id)
            if status:
                stmt = stmt.where(Mission.status == status)
            stmt = stmt.order_by(Mission.last_activity_at.desc()).limit(limit)
            result = await db.execute(stmt)
            return list(result.scalars().all())

    async def update_mission_status(
        self,
        mission_id: UUID,
        status: str,
        last_inbound_message_id: str | None = None,
        result_summary: str | None = None,
        extra_metadata: dict | None = None,
    ) -> Mission | None:
        """Update mission status and related fields."""
        async with self.get_session() as db:
            mission = await db.get(Mission, mission_id)
            if not mission:
                return None
            from datetime import datetime
            mission.status = status
            mission.last_activity_at = datetime.utcnow()
            if last_inbound_message_id is not None:
                mission.last_inbound_message_id = last_inbound_message_id
            if result_summary is not None:
                mission.result_summary = result_summary
            if extra_metadata is not None:
                mission.extra_metadata = {**mission.extra_metadata, **extra_metadata}
            if status in ("completed", "failed", "cancelled"):
                mission.completed_at = datetime.utcnow()
            db.add(mission)
            await db.commit()
            await db.refresh(mission)
        return mission

    # ---------------------------------------------------------------
    # WhatsAppContact CRUD
    # ---------------------------------------------------------------

    async def upsert_whatsapp_contact(
        self,
        user_id: UUID,
        jid: str,
        label: str,
        phone_e164: str,
        notes: str | None = None,
    ) -> WhatsAppContact:
        """Upsert a WhatsApp contact by user_id + jid."""
        async with self.get_session() as db:
            stmt = select(WhatsAppContact).where(
                WhatsAppContact.user_id == user_id,
                WhatsAppContact.jid == jid,
            )
            result = await db.execute(stmt)
            existing = result.scalars().first()
            from datetime import datetime
            if existing:
                existing.label = label
                existing.phone_e164 = phone_e164
                if notes is not None:
                    existing.notes = notes
                existing.last_used_at = datetime.utcnow()
                db.add(existing)
                await db.commit()
                await db.refresh(existing)
                return existing
            contact = WhatsAppContact(
                user_id=user_id,
                jid=jid,
                label=label,
                phone_e164=phone_e164,
                notes=notes,
                last_used_at=datetime.utcnow(),
            )
            db.add(contact)
            await db.commit()
            await db.refresh(contact)
            return contact

    async def get_whatsapp_contact_by_label(
        self,
        user_id: UUID,
        label: str,
    ) -> WhatsAppContact | None:
        """Find a WhatsApp contact by label (case-insensitive exact match)."""
        async with self.get_session() as db:
            stmt = (
                select(WhatsAppContact)
                .where(WhatsAppContact.user_id == user_id)
                .where(func.lower(WhatsAppContact.label) == label.lower())
                .limit(1)
            )
            result = await db.execute(stmt)
            return result.scalars().first()

    async def get_whatsapp_contact_by_jid(self, jid: str) -> WhatsAppContact | None:
        """Find any WhatsApp contact (across users) matching this JID.

        Used by inbound webhook to notify the contact's owner when there is
        no active mission. Returns the most recently used contact when the
        same JID is saved by multiple users.
        """
        async with self.get_session() as db:
            stmt = (
                select(WhatsAppContact)
                .where(WhatsAppContact.jid == jid)
                .order_by(WhatsAppContact.last_used_at.desc())
                .limit(1)
            )
            result = await db.execute(stmt)
            return result.scalars().first()

    async def search_whatsapp_contact(
        self,
        user_id: UUID,
        query: str,
    ) -> WhatsAppContact | None:
        """Fuzzy search a WhatsApp contact by label, phone_e164, or JID.

        Matches beginning of label (case-insensitive), full phone_e164,
        or JID prefix. Returns the best match ordered by last_used_at desc.
        """
        q = query.strip()
        if not q:
            return None
        async with self.get_session() as db:
            # Try exact JID first
            stmt = (
                select(WhatsAppContact)
                .where(WhatsAppContact.user_id == user_id)
                .where(WhatsAppContact.jid == q)
                .limit(1)
            )
            result = await db.execute(stmt)
            contact = result.scalars().first()
            if contact:
                return contact

            # Try phone (digits only)
            digits = "".join(filter(str.isdigit, q))
            if digits:
                stmt = (
                    select(WhatsAppContact)
                    .where(WhatsAppContact.user_id == user_id)
                    .where(func.replace(func.replace(WhatsAppContact.phone_e164, " ", ""), "-", "") == digits)
                    .order_by(WhatsAppContact.last_used_at.desc())
                    .limit(1)
                )
                result = await db.execute(stmt)
                contact = result.scalars().first()
                if contact:
                    return contact

            # Fuzzy label match (case-insensitive, starts-with)
            stmt = (
                select(WhatsAppContact)
                .where(WhatsAppContact.user_id == user_id)
                .where(func.lower(WhatsAppContact.label).like(f"{q.lower()}%"))
                .order_by(WhatsAppContact.last_used_at.desc())
                .limit(1)
            )
            result = await db.execute(stmt)
            return result.scalars().first()

    async def list_whatsapp_contacts(self, user_id: UUID) -> list[WhatsAppContact]:
        """List all WhatsApp contacts for a user, ordered by last_used_at desc."""
        async with self.get_session() as db:
            stmt = (
                select(WhatsAppContact)
                .where(WhatsAppContact.user_id == user_id)
                .order_by(WhatsAppContact.last_used_at.desc())
            )
            result = await db.execute(stmt)
            return list(result.scalars().all())

    # ---------------------------------------------------------------
    # UserNotification CRUD
    # ---------------------------------------------------------------

    async def create_notification(
        self,
        user_id: UUID,
        mission_id: UUID | None,
        kind: str,
        title: str,
        body: str,
        payload: dict | None = None,
    ) -> UserNotification:
        """Create a new notification."""
        notif = UserNotification(
            user_id=user_id,
            mission_id=mission_id,
            kind=kind,
            title=title,
            body=body,
            payload=payload or {},
        )
        async with self.get_session() as db:
            db.add(notif)
            await db.commit()
            await db.refresh(notif)
        return notif

    async def get_pending_notifications(self, user_id: UUID) -> list[UserNotification]:
        """Get all undelivered notifications for a user."""
        async with self.get_session() as db:
            stmt = (
                select(UserNotification)
                .where(UserNotification.user_id == user_id)
                .where(UserNotification.delivered_at.is_(None))
                .order_by(UserNotification.created_at.asc())
            )
            result = await db.execute(stmt)
            return list(result.scalars().all())

    async def list_notifications_for_mission(self, mission_id: UUID) -> list[UserNotification]:
        """All notifications tied to a mission, oldest first."""
        async with self.get_session() as db:
            stmt = (
                select(UserNotification)
                .where(UserNotification.mission_id == mission_id)
                .order_by(UserNotification.created_at.asc())
            )
            result = await db.execute(stmt)
            return list(result.scalars().all())

    async def mark_notification_delivered(self, notification_id: UUID) -> None:
        """Mark a notification as delivered."""
        async with self.get_session() as db:
            notif = await db.get(UserNotification, notification_id)
            if notif:
                from datetime import datetime
                notif.delivered_at = datetime.utcnow()
                db.add(notif)
                await db.commit()

    async def mark_notification_acked(self, notification_id: UUID) -> None:
        """Mark a notification as acked by the desktop client."""
        async with self.get_session() as db:
            notif = await db.get(UserNotification, notification_id)
            if notif:
                from datetime import datetime
                notif.acked_at = datetime.utcnow()
                db.add(notif)
                await db.commit()
