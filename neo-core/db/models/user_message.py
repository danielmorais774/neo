from __future__ import annotations
from typing import Optional, Dict, Any
from datetime import datetime
from uuid import uuid4, UUID

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, Text, ForeignKey, JSON
from sqlalchemy import Uuid


class UserMessage(SQLModel, table=True):
    """Mensagem enviada por um usuário — registra a origem (service) como `neo-desktop`, etc.

    `role` indica se a mensagem foi enviada pelo `user` ou `assistant` (ou `system`).
    """
    __tablename__ = "user_message"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: Optional[UUID] = Field(default=None, sa_column=Column("user_id", Uuid, ForeignKey("user.id"), nullable=True, index=True))
    service: str = Field(sa_column=Column("service", String, nullable=False))
    role: str = Field(default="user", sa_column=Column("role", String, nullable=False))
    content: str = Field(sa_column=Column("content", Text, nullable=False))
    extra_metadata: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column("metadata", JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow, sa_column=Column("created_at", nullable=False))
