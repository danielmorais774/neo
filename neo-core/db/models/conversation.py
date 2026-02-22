"""Conversation session & message models with embedding support."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import Column, Float, ForeignKey, Index, Integer, String, Text, JSON
from sqlalchemy import Uuid as SAUuid
from sqlmodel import Field, SQLModel

try:
    from pgvector.sqlalchemy import Vector
    HAS_PGVECTOR = True
except ImportError:
    HAS_PGVECTOR = False


class ConversationSession(SQLModel, table=True):
    """Uma sessão de conversa — agrupa mensagens por tópico/tempo."""

    __tablename__ = "conversation_session"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: Optional[UUID] = Field(
        default=None,
        sa_column=Column("user_id", SAUuid, ForeignKey("user.id"), nullable=True, index=True),
    )
    service: str = Field(
        default="neo-desktop",
        sa_column=Column("service", String(64), nullable=False),
    )
    status: str = Field(
        default="active",
        sa_column=Column("status", String(16), nullable=False, index=True),
    )
    topic_summary: Optional[str] = Field(
        default=None,
        sa_column=Column("topic_summary", Text, nullable=True),
    )
    topic_embedding: Optional[list[float]] = Field(
        default=None,
        sa_column=Column(
            "topic_embedding",
            Vector(1536) if HAS_PGVECTOR else JSON,
            nullable=True,
        ),
    )
    message_count: int = Field(
        default=0,
        sa_column=Column("message_count", Integer, nullable=False, server_default="0"),
    )
    started_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("started_at", nullable=False),
    )
    last_activity_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("last_activity_at", nullable=False, index=True),
    )
    ended_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("ended_at", nullable=True),
    )
    extra_metadata: Optional[dict[str, Any]] = Field(
        default=None,
        sa_column=Column("session_metadata", JSON, nullable=True),
    )


class ConversationMessage(SQLModel, table=True):
    """Mensagem individual dentro de uma sessão."""

    __tablename__ = "conversation_message"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    session_id: UUID = Field(
        sa_column=Column(
            "session_id", SAUuid,
            ForeignKey("conversation_session.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
    )
    role: str = Field(
        sa_column=Column("role", String(16), nullable=False),
    )
    content: str = Field(
        sa_column=Column("content", Text, nullable=False),
    )
    embedding: Optional[list[float]] = Field(
        default=None,
        sa_column=Column(
            "embedding",
            Vector(1536) if HAS_PGVECTOR else JSON,
            nullable=True,
        ),
    )
    tool_name: Optional[str] = Field(
        default=None,
        sa_column=Column("tool_name", String(128), nullable=True),
    )
    tool_call_id: Optional[str] = Field(
        default=None,
        sa_column=Column("tool_call_id", String(64), nullable=True),
    )
    token_count: Optional[int] = Field(
        default=None,
        sa_column=Column("token_count", Integer, nullable=True),
    )
    extra_metadata: Optional[dict[str, Any]] = Field(
        default=None,
        sa_column=Column("msg_metadata", JSON, nullable=True),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", nullable=False),
    )

    __table_args__ = (
        Index("ix_conv_msg_session_created", "session_id", "created_at"),
    )
