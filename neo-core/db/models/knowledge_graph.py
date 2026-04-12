"""Knowledge Graph models — generic entity/relation graph in PostgreSQL."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import Column, ForeignKey, Index, String, Text
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class KnowledgeEntity(SQLModel, table=True):
    """Nó genérico do grafo de conhecimento do usuário."""

    __tablename__ = "knowledge_entity"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: Optional[UUID] = Field(
        default=None,
        sa_column=Column("user_id", SAUuid, ForeignKey("user.id"), nullable=True),
    )
    type: str = Field(
        sa_column=Column("type", String(64), nullable=False),
    )
    name: str = Field(
        sa_column=Column("name", String(255), nullable=False),
    )
    properties: Optional[dict[str, Any]] = Field(
        default=None,
        sa_column=Column("properties", JSONB, nullable=True),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("updated_at", nullable=False),
    )

    __table_args__ = (
        Index("ix_ke_user_type", "user_id", "type"),
        Index("ix_ke_user_name", "user_id", "name"),
    )


class KnowledgeRelation(SQLModel, table=True):
    """Aresta genérica do grafo de conhecimento — liga duas entidades."""

    __tablename__ = "knowledge_relation"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: Optional[UUID] = Field(
        default=None,
        sa_column=Column("user_id", SAUuid, ForeignKey("user.id"), nullable=True),
    )
    source_id: UUID = Field(
        sa_column=Column("source_id", SAUuid, ForeignKey("knowledge_entity.id"), nullable=False),
    )
    target_id: UUID = Field(
        sa_column=Column("target_id", SAUuid, ForeignKey("knowledge_entity.id"), nullable=False),
    )
    type: str = Field(
        sa_column=Column("type", String(64), nullable=False),
    )
    properties: Optional[dict[str, Any]] = Field(
        default=None,
        sa_column=Column("properties", JSONB, nullable=True),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", nullable=False),
    )

    __table_args__ = (
        Index("ix_kr_user_type", "user_id", "type"),
        Index("ix_kr_source", "source_id"),
        Index("ix_kr_target", "target_id"),
    )
