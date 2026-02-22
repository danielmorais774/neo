from __future__ import annotations
from datetime import datetime
from typing import Optional
from uuid import uuid4, UUID

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String


class User(SQLModel, table=True):
    """Usuário do sistema."""
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    username: Optional[str] = Field(default=None, sa_column=Column("username", String, nullable=True, unique=True, index=True))
    email: Optional[str] = Field(default=None, sa_column=Column("email", String, nullable=True))
    display_name: Optional[str] = Field(default=None, sa_column=Column("display_name", String, nullable=True))
    password_hash: Optional[str] = Field(default=None, sa_column=Column("password_hash", String, nullable=True))
    created_at: datetime = Field(default_factory=datetime.utcnow, sa_column=Column("created_at", nullable=False))
