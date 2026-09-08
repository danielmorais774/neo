"""Async PostgreSQL client — SQLAlchemy async engine + SQLModel models."""

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import text, func
from sqlalchemy import update
from sqlmodel import select

from db.models.user import User
from db.models.user_message import UserMessage
from db.models.conversation import ConversationSession, ConversationMessage
from db.models.user_preference import UserPreference
from db.models.mission import Mission
from db.models.whatsapp_contact import WhatsAppContact
from db.models.user_notification import UserNotification
from db.models.connected_device import ConnectedDevice
from db.models.agent_job import AgentJob
from shared.config import get_settings

logger = logging.getLogger(__name__)


def _normalize_embedding_vector(value: Any) -> list[float] | None:
    """Normalize embedding payloads to pgvector-compatible list[float]."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        try:
            return [float(v) for v in value]
        except (TypeError, ValueError):
            logger.warning("[DB] Invalid embedding sequence; dropping value")
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [float(v) for v in parsed]
        except Exception:
            pass
        if raw.startswith("[") and raw.endswith("]"):
            parts = [p.strip() for p in raw[1:-1].split(",")]
            try:
                return [float(p) for p in parts if p]
            except ValueError:
                logger.warning("[DB] Invalid embedding string; dropping value")
                return None
    logger.warning("[DB] Unsupported embedding type %s; dropping value", type(value).__name__)
    return None


def _embedding_vector_literal(value: Any) -> str | None:
    vector = _normalize_embedding_vector(value)
    if vector is None:
        return None
    return str(vector)


def _embedding_bytes_payload(value: Any) -> bytes | None:
    vector = _normalize_embedding_vector(value)
    if vector is None:
        return None
    return json.dumps(vector, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _jsonb_payload(value: Any) -> str:
    if value is None:
        return "{}"
    return json.dumps(value, ensure_ascii=False, default=str)


def _digits_from_whatsapp_jid(jid: str | None) -> str:
    """Return phone digits for phone-backed WhatsApp JIDs."""
    if not jid:
        return ""
    local = jid.split("@", 1)[0]
    return local if local.isdigit() else ""


def _whatsapp_jid_candidates(jid: str) -> list[str]:
    """Equivalent JID variants for a phone-backed WhatsApp contact."""
    candidates = {jid}
    digits = _digits_from_whatsapp_jid(jid)
    if digits:
        candidates.add(f"{digits}@c.us")
        candidates.add(f"{digits}@s.whatsapp.net")
    return list(candidates)


def _mission_matches_whatsapp_contact(mission: Mission, contact_jid: str) -> bool:
    """Return whether a mission can be associated with an inbound WhatsApp JID."""
    inbound_candidates = set(_whatsapp_jid_candidates(contact_jid))
    if mission.contact_jid in inbound_candidates:
        return True

    phone_digits = _digits_from_whatsapp_jid(contact_jid)
    candidate_digits = _digits_from_whatsapp_jid(mission.contact_jid)
    meta = mission.extra_metadata or {}
    if contact_jid and contact_jid in {
        str(meta.get("contact_lid") or ""),
        str(meta.get("last_inbound_jid") or ""),
    }:
        return True
    meta_phone = meta.get("contact_phone_e164", "")
    meta_digits = "".join(filter(str.isdigit, meta_phone or ""))
    if phone_digits and phone_digits in {candidate_digits, meta_digits}:
        return True

    # WPPConnect sometimes emits inbound messages as @lid while the mission
    # contact was resolved as @c.us. The outbound message id carries that @lid:
    # true_234020537397350@lid_3EB0...
    last_outbound_id = str(meta.get("last_outbound_id") or "")
    return bool(contact_jid and contact_jid in last_outbound_id)


class AsyncPostgresClient:
    """Async PostgreSQL client using SQLAlchemy async engine + SQLModel models."""

    def __init__(self, dsn: str | None = None) -> None:
        settings = get_settings()
        self.dsn = dsn or settings.database_url
        self.engine = create_async_engine(self.dsn, echo=False, pool_size=10, max_overflow=20)
        self.session_factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self._topic_embedding_column_type: str | None = None

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
            result = await db.execute(
                text(
                    """
                    SELECT id, user_id, service, status, topic_summary, message_count,
                           started_at, last_activity_at, ended_at, session_metadata
                    FROM conversation_session
                    WHERE id = :session_id
                    LIMIT 1
                    """
                ),
                {"session_id": session_id},
            )
            row = result.mappings().first()
            if not row:
                return None
            return ConversationSession(
                id=row["id"],
                user_id=row["user_id"],
                service=row["service"],
                status=row["status"],
                topic_summary=row["topic_summary"],
                topic_embedding=None,
                message_count=row["message_count"] or 0,
                started_at=row["started_at"],
                last_activity_at=row["last_activity_at"],
                ended_at=row["ended_at"],
                extra_metadata=row["session_metadata"],
            )

    async def get_active_session(self, user_id: UUID | None = None, service: str = "neo-desktop") -> ConversationSession | None:
        """Get the most recent active session for a user/service."""
        async with self.get_session() as db:
            user_filter = "AND user_id = :user_id" if user_id else ""
            params: dict[str, Any] = {"service": service}
            if user_id:
                params["user_id"] = user_id
            result = await db.execute(
                text(
                    f"""
                    SELECT id, user_id, service, status, topic_summary, message_count,
                           started_at, last_activity_at, ended_at, session_metadata
                    FROM conversation_session
                    WHERE status = 'active'
                      AND service = :service
                      {user_filter}
                    ORDER BY last_activity_at DESC
                    LIMIT 1
                    """
                ),
                params,
            )
            row = result.mappings().first()
            if not row:
                return None
            return ConversationSession(
                id=row["id"],
                user_id=row["user_id"],
                service=row["service"],
                status=row["status"],
                topic_summary=row["topic_summary"],
                topic_embedding=None,
                message_count=row["message_count"] or 0,
                started_at=row["started_at"],
                last_activity_at=row["last_activity_at"],
                ended_at=row["ended_at"],
                extra_metadata=row["session_metadata"],
            )

    async def merge_session_metadata(
        self, session_id: UUID, patch: dict[str, Any]
    ) -> None:
        """Funde chaves em ``session_metadata`` sem tocar no resto da linha.

        ``update_session`` reescreve a linha inteira (status, tópico, embedding):
        usá-la para guardar um detalhe de skill faria o turno concorrente que
        rotaciona o tópico perder a atualização. O ``||`` do jsonb resolve no
        próprio banco — sem leitura prévia, sem corrida.

        O ``CASE`` não é paranoia: ``COALESCE`` só cobre o NULL do SQL, e a
        coluna também guarda o **null do JSON**. E ``'null'::jsonb || '{...}'``
        no Postgres não dá objeto — dá ``[null, {...}]``, um ARRAY. Uma linha
        assim quebra TODO turno da sessão daí em diante (`'list' object has no
        attribute 'get'`), e foi exatamente o que aconteceu em 2026-09-08.
        """
        if not patch:
            return
        async with self.get_session() as db:
            await db.execute(
                text(
                    """
                    UPDATE conversation_session
                    SET session_metadata =
                        CASE
                            WHEN jsonb_typeof(session_metadata) = 'object'
                            THEN session_metadata
                            ELSE '{}'::jsonb
                        END || CAST(:patch AS jsonb)
                    WHERE id = :session_id
                    """
                ),
                {"patch": json.dumps(patch), "session_id": session_id},
            )
            await db.commit()

    async def update_session(self, session: ConversationSession) -> None:
        """Update a session (topic, status, etc.)."""
        async with self.get_session() as db:
            column_type = await self._get_topic_embedding_column_type(db)
            params = {
                "status": session.status,
                "topic_summary": session.topic_summary,
                "last_activity_at": session.last_activity_at,
                "ended_at": session.ended_at,
                "session_metadata": _jsonb_payload(session.extra_metadata),
                "session_id": session.id,
            }
            if column_type == "vector":
                params["topic_embedding"] = _embedding_vector_literal(session.topic_embedding)
                await db.execute(
                    text(
                        """
                        UPDATE conversation_session
                        SET status = :status,
                            topic_summary = :topic_summary,
                            topic_embedding = CASE
                                WHEN :topic_embedding IS NULL THEN NULL
                                ELSE CAST(:topic_embedding AS vector)
                            END,
                            last_activity_at = :last_activity_at,
                            ended_at = :ended_at,
                            session_metadata = CAST(:session_metadata AS jsonb)
                        WHERE id = :session_id
                        """
                    ),
                    params,
                )
            elif column_type == "bytea":
                params["topic_embedding"] = _embedding_bytes_payload(session.topic_embedding)
                await db.execute(
                    text(
                        """
                        UPDATE conversation_session
                        SET status = :status,
                            topic_summary = :topic_summary,
                            topic_embedding = :topic_embedding,
                            last_activity_at = :last_activity_at,
                            ended_at = :ended_at,
                            session_metadata = CAST(:session_metadata AS jsonb)
                        WHERE id = :session_id
                        """
                    ),
                    params,
                )
            else:
                logger.warning(
                    "[DB] Unknown conversation_session.topic_embedding type '%s'; persisting NULL",
                    column_type,
                )
                await db.execute(
                    text(
                        """
                        UPDATE conversation_session
                        SET status = :status,
                            topic_summary = :topic_summary,
                            topic_embedding = NULL,
                            last_activity_at = :last_activity_at,
                            ended_at = :ended_at,
                            session_metadata = CAST(:session_metadata AS jsonb)
                        WHERE id = :session_id
                        """
                    ),
                    params,
                )
            await db.commit()

    async def _get_topic_embedding_column_type(self, db: AsyncSession) -> str:
        if self._topic_embedding_column_type:
            return self._topic_embedding_column_type
        result = await db.execute(
            text(
                """
                SELECT COALESCE(NULLIF(udt_name, ''), data_type) AS column_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'conversation_session'
                  AND column_name = 'topic_embedding'
                LIMIT 1
                """
            )
        )
        row = result.mappings().first()
        self._topic_embedding_column_type = str((row or {}).get("column_type") or "unknown").lower()
        logger.info(
            "[DB] conversation_session.topic_embedding type detected: %s",
            self._topic_embedding_column_type,
        )
        return self._topic_embedding_column_type

    async def close_session(self, session_id: UUID) -> None:
        """Close a session."""
        async with self.get_session() as db:
            session = await self.get_conversation_session(session_id)
            if session:
                from datetime import datetime
                await db.execute(
                    text(
                        """
                        UPDATE conversation_session
                        SET status = 'closed',
                            ended_at = :ended_at
                        WHERE id = :session_id
                        """
                    ),
                    {"session_id": session_id, "ended_at": datetime.utcnow()},
                )
                await db.commit()

    async def find_similar_sessions(
        self,
        embedding: list[float],
        user_id: UUID | None = None,
        service: str | None = "neo-desktop",
        limit: int = 5,
        min_similarity: float = 0.5,
    ) -> list[tuple[ConversationSession, float]]:
        """Find sessions with similar topic embeddings using cosine distance.

        ``service=None`` busca em TODOS os canais do usuário (desktop, painel,
        WhatsApp). O escopo real de privacidade é o ``user_id``, não o app.
        """
        async with self.get_session() as db:
            try:
                from pgvector.sqlalchemy import Vector
                from sqlalchemy import text

                # Use pgvector cosine similarity operator
                params = {
                    "embedding": str(embedding),
                    "limit": limit,
                    "min_sim": min_similarity,
                }
                service_filter = ""
                if service:
                    service_filter = "AND service = :service"
                    params["service"] = service
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
                      {service_filter}
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
                    sess = await self.get_conversation_session(row[0])
                    if sess:
                        sessions.append((sess, float(row[2])))
                return sessions
            except Exception as e:
                logger.warning(f"pgvector search failed (extension may not be installed): {e}")
                return []

    async def fetch_recent_sessions(
        self,
        user_id: UUID | None = None,
        service: str = "neo-desktop",
        limit: int = 5,
        statuses: list[str] | None = None,
        exclude_session_id: UUID | None = None,
    ) -> list[ConversationSession]:
        """Fetch recent sessions for context-continuity decisions."""
        async with self.get_session() as db:
            stmt = select(ConversationSession).where(
                ConversationSession.service == service
            )
            if user_id:
                stmt = stmt.where(ConversationSession.user_id == user_id)
            if statuses:
                stmt = stmt.where(ConversationSession.status.in_(statuses))
            if exclude_session_id:
                stmt = stmt.where(ConversationSession.id != exclude_session_id)
            stmt = (
                stmt.order_by(ConversationSession.last_activity_at.desc())
                .limit(limit)
            )
            result = await db.execute(stmt)
            return list(result.scalars().all())

    # ---------------------------------------------------------------
    # Vector Search — Messages (pgvector)
    # ---------------------------------------------------------------

    async def find_similar_messages(
        self,
        embedding: list[float],
        user_id: UUID | None = None,
        service: str | None = "neo-desktop",
        limit: int = 20,
        min_similarity: float = 0.4,
    ) -> list[tuple[ConversationMessage, float]]:
        """Busca mensagens individuais por similaridade de embedding.

        ``service=None`` busca em TODOS os canais do usuário — ver
        ``find_similar_sessions``.
        """
        async with self.get_session() as db:
            try:
                params: dict[str, Any] = {
                    "embedding": str(embedding),
                    "limit": limit,
                    "min_sim": min_similarity,
                }
                service_filter = ""
                if service:
                    service_filter = "AND cs.service = :service"
                    params["service"] = service
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
                      {service_filter}
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
        service: str | None = "neo-desktop",
        limit: int = 20,
    ) -> list[tuple[ConversationMessage, float]]:
        """Busca mensagens por Full-Text Search (FTS) no PostgreSQL.

        Usa tsquery com fallback de idioma: português → inglês.
        ``service=None`` busca em TODOS os canais do usuário.
        """
        async with self.get_session() as db:
            try:
                params: dict[str, Any] = {
                    "query": query_text,
                    "limit": limit,
                }
                service_filter = ""
                if service:
                    service_filter = "AND cs.service = :service"
                    params["service"] = service
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
                          {service_filter}
                          {user_filter}

                        UNION ALL

                        SELECT cm.id,
                               ts_rank_cd(cm.content_tsv, query) AS rank
                        FROM conversation_message cm
                        JOIN conversation_session cs ON cs.id = cm.session_id,
                             plainto_tsquery('english', :query) query
                        WHERE cm.content_tsv @@ query
                          {service_filter}
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

    async def set_message_embedding(
        self,
        message_id: UUID,
        embedding: list[float],
    ) -> None:
        """Preenche o embedding de uma mensagem já salva.

        Usado pelo preenchimento em background das respostas do assistant
        (ver PersistentSessionManager._embed_saved_message) e pelo backfill
        de scripts/backfill_assistant_embeddings.py.
        """
        async with self.get_session() as db:
            await db.execute(
                text(
                    "UPDATE conversation_message SET embedding = CAST(:emb AS vector) "
                    "WHERE id = :id"
                ),
                {"emb": str(embedding), "id": str(message_id)},
            )
            await db.commit()

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
        mission_id: UUID | None = None,
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
            mission_id=mission_id,
        )
        async with self.get_session() as db:
            db.add(msg)

            # Update session activity & message count
            from datetime import datetime
            await db.execute(
                text(
                    """
                    UPDATE conversation_session
                    SET last_activity_at = :last_activity_at,
                        message_count = COALESCE(message_count, 0) + 1
                    WHERE id = :session_id
                    """
                ),
                {"session_id": session_id, "last_activity_at": datetime.utcnow()},
            )

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

    async def fetch_mission_messages(
        self,
        mission_id: UUID,
        limit: int = 200,
        roles: list[str] | None = None,
    ) -> list[ConversationMessage]:
        """Fetch messages explicitly tied to a mission, ordered chronologically."""
        async with self.get_session() as db:
            stmt = (
                select(ConversationMessage)
                .where(ConversationMessage.mission_id == mission_id)
            )
            if roles:
                stmt = stmt.where(ConversationMessage.role.in_(roles))
            stmt = stmt.order_by(ConversationMessage.created_at.asc()).limit(limit)
            result = await db.execute(stmt)
            return list(result.scalars().all())

    async def attach_message_to_mission(
        self,
        message_id: UUID,
        mission_id: UUID,
    ) -> ConversationMessage | None:
        """Attach an already-saved conversation message to a mission."""
        async with self.get_session() as db:
            msg = await db.get(ConversationMessage, message_id)
            if not msg:
                return None
            msg.mission_id = mission_id
            db.add(msg)
            await db.commit()
            await db.refresh(msg)
            return msg

    async def fetch_session_history(
        self,
        session_id: UUID,
        limit: int = 20,
    ) -> list[dict[str, str]]:
        """Fetch the last N user/assistant messages as [{role, content, token_count}] for LLM context.

        ``extra_metadata`` também vem junto (sem custo extra de query — já é uma
        coluna do mesmo SELECT via ORM): session_manager.get_history() usa ela pra
        reinjetar um resumo técnico do que as tools fizeram no turno mais recente.
        """
        messages = await self.fetch_session_messages(
            session_id,
            limit=limit,
            roles=["user", "assistant"],
            exclude_tool_calls=True,
            min_content_length=5,
        )
        return [
            {
                "role": m.role, "content": m.content, "token_count": m.token_count or 0,
                "extra_metadata": m.extra_metadata,
            }
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

    async def list_user_ids(self) -> list[UUID]:
        """IDs de todos os usuários. Usado pelo pareamento: com um único usuário
        (o caso real hoje) o painel não precisa pedir UUID digitado à mão."""
        async with self.get_session() as db:
            result = await db.execute(select(User.id))
            return [row[0] for row in result.all()]

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
        """Find the active mission for a contact (awaiting_reply or awaiting_confirmation).

        WhatsApp gateways may report the same 1:1 contact as @c.us or
        @s.whatsapp.net depending on the API path. Match by equivalent phone
        JID variants first, then fall back to a small active-mission scan.
        """
        jid_candidates = _whatsapp_jid_candidates(contact_jid)
        async with self.get_session() as db:
            stmt = (
                select(Mission)
                .where(Mission.contact_jid.in_(jid_candidates))
                .where(Mission.status.in_(["awaiting_reply", "awaiting_confirmation"]))
                .order_by(Mission.last_activity_at.desc())
                .limit(1)
            )
            result = await db.execute(stmt)
            mission = result.scalars().first()
            if mission:
                return mission

            fallback_stmt = (
                select(Mission)
                .where(Mission.status.in_(["awaiting_reply", "awaiting_confirmation"]))
                .order_by(Mission.last_activity_at.desc())
                .limit(50)
            )
            fallback_result = await db.execute(fallback_stmt)
            for candidate in fallback_result.scalars().all():
                if _mission_matches_whatsapp_contact(candidate, contact_jid):
                    return candidate
            return None

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

    async def find_active_whatsapp_mission_by_session(
        self,
        session_id: UUID,
        user_id: UUID | None = None,
    ) -> Mission | None:
        """Find the most recent active WhatsApp mission in a chat session."""
        async with self.get_session() as db:
            stmt = (
                select(Mission)
                .where(Mission.session_id == session_id)
                .where(Mission.channel == "whatsapp")
                .where(Mission.status.in_(["awaiting_reply", "awaiting_confirmation", "processing"]))
            )
            if user_id:
                stmt = stmt.where(Mission.user_id == user_id)
            stmt = stmt.order_by(Mission.last_activity_at.desc()).limit(1)
            result = await db.execute(stmt)
            return result.scalars().first()

    async def list_recent_whatsapp_missions_by_session(
        self,
        session_id: UUID,
        user_id: UUID | None = None,
        limit: int = 5,
    ) -> list[Mission]:
        """List recent WhatsApp missions associated with a chat session."""
        async with self.get_session() as db:
            stmt = (
                select(Mission)
                .where(Mission.session_id == session_id)
                .where(Mission.channel == "whatsapp")
                .where(Mission.status != "cancelled")
                .order_by(Mission.last_activity_at.desc())
                .limit(limit)
            )
            if user_id:
                stmt = stmt.where(Mission.user_id == user_id)
            result = await db.execute(stmt)
            return list(result.scalars().all())

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

    async def update_mission_last_inbound_message_id(
        self,
        mission_id: UUID,
        message_id: str,
    ) -> Mission | None:
        """Update inbound idempotency marker without changing mission status."""
        async with self.get_session() as db:
            mission = await db.get(Mission, mission_id)
            if not mission:
                return None
            from datetime import datetime
            mission.last_inbound_message_id = message_id
            mission.last_activity_at = datetime.utcnow()
            db.add(mission)
            await db.commit()
            await db.refresh(mission)
        return mission

    async def set_mission_next_action(
        self,
        mission_id: UUID,
        action_type: str,
        action_params: dict[str, Any] | None = None,
    ) -> Mission | None:
        """Set next action to be executed by structured mission commands."""
        async with self.get_session() as db:
            mission = await db.get(Mission, mission_id)
            if not mission:
                return None
            from datetime import datetime

            mission.next_action_type = action_type
            mission.next_action_params = action_params or {}
            mission.last_activity_at = datetime.utcnow()
            db.add(mission)
            await db.commit()
            await db.refresh(mission)
        return mission

    async def clear_mission_next_action(self, mission_id: UUID) -> Mission | None:
        """Clear pending next action after command execution/cancel."""
        async with self.get_session() as db:
            mission = await db.get(Mission, mission_id)
            if not mission:
                return None
            from datetime import datetime

            mission.next_action_type = None
            mission.next_action_params = {}
            mission.last_activity_at = datetime.utcnow()
            db.add(mission)
            await db.commit()
            await db.refresh(mission)
        return mission

    async def merge_mission_extra_metadata(
        self,
        mission_id: UUID,
        metadata: dict[str, Any],
    ) -> Mission | None:
        """Merge mission metadata keys without changing mission state."""
        async with self.get_session() as db:
            mission = await db.get(Mission, mission_id)
            if not mission:
                return None
            from datetime import datetime

            mission.extra_metadata = {**(mission.extra_metadata or {}), **(metadata or {})}
            mission.last_activity_at = datetime.utcnow()
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

    async def mark_notifications_acked_for_mission(self, mission_id: UUID) -> int:
        """Ack all unacked notifications tied to a mission."""
        from datetime import datetime

        async with self.get_session() as db:
            stmt = (
                update(UserNotification)
                .where(UserNotification.mission_id == mission_id)
                .where(UserNotification.acked_at.is_(None))
                .values(acked_at=datetime.utcnow())
            )
            result = await db.execute(stmt)
            await db.commit()
            return int(result.rowcount or 0)

    # ---------------------------------------------------------------
    # ConnectedDevice CRUD
    # ---------------------------------------------------------------

    async def upsert_connected_device(
        self,
        user_id: UUID,
        device_id: str,
        device_type: str,
        model: str,
        hardware_id: str,
        os_platform: str,
        os_version: str,
        client_version: str = "",
        ip_address: str = "",
        last_heartbeat_unix: int = 0,
        is_online: bool = True,
    ) -> None:
        """Insert or update a connected device record."""
        import time as _time
        async with self.get_session() as db:
            stmt = select(ConnectedDevice).where(ConnectedDevice.device_id == device_id)
            result = await db.execute(stmt)
            existing = result.scalars().first()

            now_unix = int(_time.time())

            if existing:
                existing.user_id = user_id
                existing.device_type = device_type
                existing.model = model
                existing.hardware_id = hardware_id
                existing.os_platform = os_platform
                existing.os_version = os_version
                existing.client_version = client_version
                existing.ip_address = ip_address
                existing.last_heartbeat_unix = last_heartbeat_unix or now_unix
                existing.is_online = is_online
                db.add(existing)
            else:
                device = ConnectedDevice(
                    user_id=user_id,
                    device_id=device_id,
                    device_type=device_type,
                    model=model,
                    hardware_id=hardware_id,
                    os_platform=os_platform,
                    os_version=os_version,
                    client_version=client_version,
                    ip_address=ip_address,
                    last_heartbeat_unix=last_heartbeat_unix or now_unix,
                    is_online=is_online,
                )
                db.add(device)
            await db.commit()

    async def get_connected_devices(self, user_id: UUID | None = None) -> list[ConnectedDevice]:
        """List all connected devices, optionally filtered by user_id."""
        async with self.get_session() as db:
            stmt = select(ConnectedDevice)
            if user_id:
                stmt = stmt.where(ConnectedDevice.user_id == user_id)
            stmt = stmt.order_by(ConnectedDevice.created_at.desc())
            result = await db.execute(stmt)
            return list(result.scalars().all())

    async def update_device_heartbeat(self, device_id: str, timestamp_unix: int) -> None:
        """Update last_heartbeat_unix and set is_online=True for a device."""
        async with self.get_session() as db:
            stmt = select(ConnectedDevice).where(ConnectedDevice.device_id == device_id)
            result = await db.execute(stmt)
            device = result.scalars().first()
            if device:
                device.last_heartbeat_unix = timestamp_unix
                device.is_online = True
                db.add(device)
                await db.commit()

    async def mark_device_offline(self, device_id: str) -> None:
        """Mark a device as offline."""
        async with self.get_session() as db:
            stmt = select(ConnectedDevice).where(ConnectedDevice.device_id == device_id)
            result = await db.execute(stmt)
            device = result.scalars().first()
            if device:
                device.is_online = False
                db.add(device)
                await db.commit()

    async def get_device_by_id(self, device_id: str) -> ConnectedDevice | None:
        """Get a device by its device_id."""
        async with self.get_session() as db:
            stmt = select(ConnectedDevice).where(ConnectedDevice.device_id == device_id)
            result = await db.execute(stmt)
            return result.scalars().first()

    async def get_device_by_hardware_id(
        self,
        user_id: UUID,
        hardware_id: str,
    ) -> ConnectedDevice | None:
        """Get the most recent device for a user/hardware_id pair."""
        async with self.get_session() as db:
            stmt = (
                select(ConnectedDevice)
                .where(ConnectedDevice.user_id == user_id)
                .where(ConnectedDevice.hardware_id == hardware_id)
                .order_by(ConnectedDevice.created_at.desc())
            )
            result = await db.execute(stmt)
            return result.scalars().first()

    async def mark_device_online(self, device_id: str) -> None:
        """Mark a device as online (used by WS registry)."""
        async with self.get_session() as db:
            stmt = select(ConnectedDevice).where(ConnectedDevice.device_id == device_id)
            result = await db.execute(stmt)
            device = result.scalars().first()
            if device:
                device.is_online = True
                device.last_heartbeat_unix = int(time.time())
                db.add(device)
                await db.commit()

    async def set_device_ws_connection(self, device_id: str, ws_connection_id: str) -> None:
        """Set the WS connection ID for a device (connection open)."""
        async with self.get_session() as db:
            stmt = select(ConnectedDevice).where(ConnectedDevice.device_id == device_id)
            result = await db.execute(stmt)
            device = result.scalars().first()
            if device:
                device.ws_connection_id = ws_connection_id
                device.is_online = True
                db.add(device)
                await db.commit()

    async def clear_device_ws_connection(self, device_id: str) -> None:
        """Clear the WS connection ID for a device (connection closed)."""
        async with self.get_session() as db:
            stmt = select(ConnectedDevice).where(ConnectedDevice.device_id == device_id)
            result = await db.execute(stmt)
            device = result.scalars().first()
            if device:
                device.ws_connection_id = None
                device.is_online = False
                db.add(device)
                await db.commit()

    # ── Pareamento de device (docs/device-control-plan.md §2) ───────────

    async def pair_device(
        self,
        *,
        user_id: UUID,
        device_id: str,
        pairing_token_hash: str,
        device_type: str,
        model: str,
        hardware_id: str,
        os_platform: str,
        os_version: str,
        client_version: str = "",
        display_name: str | None = None,
        capabilities: dict | None = None,
    ) -> ConnectedDevice:
        """Cria/atualiza o device e grava o vínculo de dono (hash do token).

        Re-parear um device existente ROTACIONA o token (o antigo deixa de valer
        na hora) e mantém o ``display_name`` já dado pelo usuário, a menos que um
        novo venha explicitamente.
        """
        from datetime import datetime as _dt
        import time as _time

        async with self.get_session() as db:
            stmt = select(ConnectedDevice).where(ConnectedDevice.device_id == device_id)
            result = await db.execute(stmt)
            device = result.scalars().first()

            if device is None:
                device = ConnectedDevice(
                    user_id=user_id,
                    device_id=device_id,
                    device_type=device_type,
                    model=model,
                    hardware_id=hardware_id,
                    os_platform=os_platform,
                    os_version=os_version,
                    client_version=client_version,
                    last_heartbeat_unix=int(_time.time()),
                    is_online=False,
                )

            device.user_id = user_id
            device.device_type = device_type
            device.model = model
            device.hardware_id = hardware_id
            device.os_platform = os_platform
            device.os_version = os_version
            device.client_version = client_version
            device.pairing_token_hash = pairing_token_hash
            device.paired_at = _dt.utcnow()
            device.capabilities = capabilities
            # Nome só é sobrescrito quando vem um novo — o pareamento não pode
            # apagar o "PC da sala" que o usuário já batizou.
            if display_name is not None:
                device.display_name = display_name
            elif not device.display_name:
                device.display_name = None

            db.add(device)
            await db.commit()
            await db.refresh(device)
            return device

    async def delete_device(self, device_id: str) -> bool:
        """Apaga um device. Apagar a linha invalida o token junto (só existia o hash)."""
        async with self.get_session() as db:
            stmt = select(ConnectedDevice).where(ConnectedDevice.device_id == device_id)
            result = await db.execute(stmt)
            device = result.scalars().first()
            if device is None:
                return False
            await db.delete(device)
            await db.commit()
            return True

    async def get_device_by_token_hash(self, token_hash: str) -> ConnectedDevice | None:
        """Resolve um device pelo hash do token — a fonte de verdade do dono.

        É por aqui que o core descobre ``user_id`` e ``device_id`` sem confiar em
        nada que o cliente afirme.
        """
        if not token_hash:
            return None
        async with self.get_session() as db:
            stmt = select(ConnectedDevice).where(
                ConnectedDevice.pairing_token_hash == token_hash
            )
            result = await db.execute(stmt)
            return result.scalars().first()

    async def set_device_display_name(
        self, device_id: str, display_name: str | None
    ) -> ConnectedDevice | None:
        """Renomeia um device. Devolve o registro atualizado (ou None)."""
        async with self.get_session() as db:
            stmt = select(ConnectedDevice).where(ConnectedDevice.device_id == device_id)
            result = await db.execute(stmt)
            device = result.scalars().first()
            if device is None:
                return None
            device.display_name = display_name
            db.add(device)
            await db.commit()
            await db.refresh(device)
            return device

    async def set_device_capabilities(
        self, device_id: str, capabilities: dict | None
    ) -> None:
        """Atualiza o que o device declara saber fazer (informado na conexão)."""
        async with self.get_session() as db:
            stmt = select(ConnectedDevice).where(ConnectedDevice.device_id == device_id)
            result = await db.execute(stmt)
            device = result.scalars().first()
            if device:
                device.capabilities = capabilities
                db.add(device)
                await db.commit()

    async def mark_stale_devices_offline(self, threshold_seconds: int = 60) -> int:
        """Mark all devices with last_heartbeat older than threshold as offline. Returns count."""
        import time as _time
        cutoff = int(_time.time()) - threshold_seconds
        async with self.get_session() as db:
            stmt = (
                select(ConnectedDevice)
                .where(ConnectedDevice.last_heartbeat_unix > 0)
                .where(ConnectedDevice.last_heartbeat_unix < cutoff)
                .where(ConnectedDevice.is_online == True)
            )
            result = await db.execute(stmt)
            stale = list(result.scalars().all())
            for device in stale:
                device.is_online = False
                db.add(device)
            await db.commit()
            return len(stale)

    # ---------------------------------------------------------------
    # AgentJob CRUD (workbench / scheduler — background work)
    # ---------------------------------------------------------------

    async def create_agent_job(self, job: "AgentJob") -> "AgentJob":
        """Persist a new AgentJob (status defaults to 'queued')."""
        async with self.get_session() as db:
            db.add(job)
            await db.commit()
            await db.refresh(job)
        return job

    async def get_agent_job(self, job_id: UUID) -> "AgentJob | None":
        async with self.get_session() as db:
            return await db.get(AgentJob, job_id)

    async def claim_due_agent_jobs(self, limit: int) -> list["AgentJob"]:
        """Atomically claim up to ``limit`` queued jobs whose ``next_run_at`` is due.

        Each claim is an atomic ``queued → running`` UPDATE guarded on the current
        status, so a job is never picked twice (safe even if two ticks overlap).
        Returns the claimed jobs (already marked running, with started_at set).
        """
        from datetime import datetime

        if limit <= 0:
            return []
        now = datetime.utcnow()
        async with self.get_session() as db:
            stmt = (
                select(AgentJob.id)
                .where(AgentJob.status == "queued")
                .where(
                    (AgentJob.next_run_at.is_(None)) | (AgentJob.next_run_at <= now)
                )
                .order_by(AgentJob.created_at.asc())
                .limit(limit)
            )
            candidate_ids = list((await db.execute(stmt)).scalars().all())

        claimed: list[AgentJob] = []
        for job_id in candidate_ids:
            async with self.get_session() as db:
                upd = (
                    update(AgentJob)
                    .where(AgentJob.id == job_id)
                    .where(AgentJob.status == "queued")
                    .values(status="running", started_at=now, heartbeat_at=now)
                )
                result = await db.execute(upd)
                await db.commit()
                if result.rowcount:
                    job = await db.get(AgentJob, job_id)
                    if job is not None:
                        claimed.append(job)
        return claimed

    async def update_agent_job(self, job_id: UUID, **fields) -> "AgentJob | None":
        """Patch an AgentJob's columns. ``spec_json``/``extra_metadata`` overwrite."""
        async with self.get_session() as db:
            job = await db.get(AgentJob, job_id)
            if job is None:
                return None
            for key, value in fields.items():
                setattr(job, key, value)
            db.add(job)
            await db.commit()
            await db.refresh(job)
            return job

    async def reconcile_orphan_agent_jobs(self) -> list["AgentJob"]:
        """On boot: mark jobs stuck in 'running' as failed (interrupted by restart).

        Returns the reconciled jobs so the dispatcher can notify the user. A job is
        orphaned when the process that owned it died (only one neo-core instance
        runs, so any 'running' row at boot is orphaned).
        """
        from datetime import datetime

        from db.models.research_task import RESEARCH_TERMINAL_STATUSES, ResearchTask

        async with self.get_session() as db:
            stmt = select(AgentJob).where(AgentJob.status == "running")
            orphans = list((await db.execute(stmt)).scalars().all())
            now = datetime.utcnow()
            for job in orphans:
                job.status = "failed"
                job.finished_at = now
                job.result_summary = (
                    job.result_summary
                    or "Job interrompido por reinício do Neo antes de concluir."
                )
                db.add(job)

            # Terminal do job ⇒ terminal da task, SEMPRE: sem isso a research_task
            # do job órfão fica presa na fase em que morreu (ex.: 'searching') e o
            # painel mostra uma pesquisa "ativa" que ninguém está mais rodando.
            research_job_ids = [job.id for job in orphans if job.kind == "research"]
            if research_job_ids:
                task_stmt = select(ResearchTask).where(
                    ResearchTask.job_id.in_(research_job_ids),
                    ResearchTask.status.notin_(list(RESEARCH_TERMINAL_STATUSES)),
                )
                for task in (await db.execute(task_stmt)).scalars().all():
                    task.status = "failed"
                    task.finished_at = now
                    task.error = task.error or "Interrompida por reinício do Neo."
                    db.add(task)

            await db.commit()
            for job in orphans:
                await db.refresh(job)
            return orphans

    async def list_agent_jobs(
        self,
        user_id: UUID | None = None,
        *,
        statuses: list[str] | None = None,
        limit: int = 20,
    ) -> list["AgentJob"]:
        async with self.get_session() as db:
            stmt = select(AgentJob)
            if user_id is not None:
                stmt = stmt.where(AgentJob.user_id == user_id)
            if statuses:
                stmt = stmt.where(AgentJob.status.in_(statuses))
            stmt = stmt.order_by(AgentJob.created_at.desc()).limit(limit)
            return list((await db.execute(stmt)).scalars().all())

    async def list_undelivered_terminal_jobs(self) -> list["AgentJob"]:
        """Terminal jobs whose result was never delivered (dispatcher catch-up)."""
        async with self.get_session() as db:
            stmt = (
                select(AgentJob)
                .where(AgentJob.status.in_(["completed", "failed", "timeout"]))
                .where(AgentJob.notified_at.is_(None))
                .order_by(AgentJob.finished_at.asc())
            )
            return list((await db.execute(stmt)).scalars().all())

    # ---------------------------------------------------------------
    # ResearchTask CRUD (deep_research — docs/web-search-deep-research-plan.md)
    # ---------------------------------------------------------------

    async def create_research_task(self, task: "ResearchTask") -> "ResearchTask":
        """Persist a new ResearchTask (status defaults to 'queued')."""
        from db.models.research_task import ResearchTask  # noqa: F401

        async with self.get_session() as db:
            db.add(task)
            await db.commit()
            await db.refresh(task)
        return task

    async def get_research_task(self, task_id: UUID) -> "ResearchTask | None":
        from db.models.research_task import ResearchTask

        async with self.get_session() as db:
            return await db.get(ResearchTask, task_id)

    async def get_research_task_by_job(self, job_id: UUID) -> "ResearchTask | None":
        from db.models.research_task import ResearchTask

        async with self.get_session() as db:
            stmt = select(ResearchTask).where(ResearchTask.job_id == job_id).limit(1)
            return (await db.execute(stmt)).scalars().first()

    async def update_research_task(self, task_id: UUID, **fields) -> "ResearchTask | None":
        """Patch a ResearchTask's columns. ``stats_json``/``sources_json`` overwrite."""
        from db.models.research_task import ResearchTask

        async with self.get_session() as db:
            task = await db.get(ResearchTask, task_id)
            if task is None:
                return None
            for key, value in fields.items():
                setattr(task, key, value)
            db.add(task)
            await db.commit()
            await db.refresh(task)
            return task

    async def list_research_tasks(
        self,
        user_id: UUID | None = None,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list["ResearchTask"]:
        """Lista compacta para o painel — NÃO retorna report_markdown (payload
        pesado só no get). Colunas explícitas para não carregar o texto."""
        from db.models.research_task import ResearchTask

        cols = (
            ResearchTask.id, ResearchTask.job_id, ResearchTask.user_id,
            ResearchTask.session_id, ResearchTask.topic, ResearchTask.depth,
            ResearchTask.status, ResearchTask.model, ResearchTask.backend,
            ResearchTask.stats_json, ResearchTask.quality_json,
            ResearchTask.report_path,
            ResearchTask.error, ResearchTask.created_at, ResearchTask.started_at,
            ResearchTask.finished_at,
            (ResearchTask.report_markdown.isnot(None)).label("has_report"),
        )
        async with self.get_session() as db:
            stmt = select(*cols)
            if user_id is not None:
                stmt = stmt.where(ResearchTask.user_id == user_id)
            if status:
                stmt = stmt.where(ResearchTask.status == status)
            stmt = stmt.order_by(ResearchTask.created_at.desc()).limit(limit)
            rows = (await db.execute(stmt)).all()
        out = []
        for row in rows:
            m = row._mapping
            out.append(
                {
                    "id": m[ResearchTask.id],
                    "job_id": m[ResearchTask.job_id],
                    "user_id": m[ResearchTask.user_id],
                    "session_id": m[ResearchTask.session_id],
                    "topic": m[ResearchTask.topic],
                    "depth": m[ResearchTask.depth],
                    "status": m[ResearchTask.status],
                    "model": m[ResearchTask.model],
                    "backend": m[ResearchTask.backend],
                    "stats_json": m[ResearchTask.stats_json] or {},
                    "quality_json": m[ResearchTask.quality_json] or {},
                    "report_path": m[ResearchTask.report_path],
                    "error": m[ResearchTask.error],
                    "created_at": m[ResearchTask.created_at],
                    "started_at": m[ResearchTask.started_at],
                    "finished_at": m[ResearchTask.finished_at],
                    "has_report": bool(m["has_report"]),
                }
            )
        return out
