"""User Preference model — persistent key/value preferences per user/service."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import Column, ForeignKey, Index, String, Text, JSON, UniqueConstraint
from sqlalchemy import Uuid as SAUuid
from sqlmodel import Field, SQLModel


class UserPreference(SQLModel, table=True):
    """Preferência do usuário — ex: 'use emoji 🔴 para X no jogo da velha'."""

    __tablename__ = "user_preference"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: Optional[UUID] = Field(
        default=None,
        sa_column=Column("user_id", SAUuid, ForeignKey("user.id"), nullable=True, index=True),
    )
    service: str = Field(
        default="neo-desktop",
        sa_column=Column("service", String(64), nullable=False),
    )
    category: str = Field(
        sa_column=Column("category", String(64), nullable=False, index=True),
    )
    key: str = Field(
        sa_column=Column("key", String(128), nullable=False),
    )
    value: str = Field(
        sa_column=Column("value", Text, nullable=False),
    )
    source_message: Optional[str] = Field(
        default=None,
        sa_column=Column("source_message", Text, nullable=True),
    )
    extra_metadata: Optional[dict[str, Any]] = Field(
        default=None,
        sa_column=Column("pref_metadata", JSON, nullable=True),
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
        UniqueConstraint("user_id", "service", "category", "key", name="uq_user_pref"),
        Index("ix_user_pref_lookup", "user_id", "service"),
    )
