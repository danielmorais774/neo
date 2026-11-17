"""User notification model — push notifications sent to desktop."""

import uuid
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, String, Text, Uuid as SAUuid, JSON


class UserNotification(SQLModel, table=True):
    __tablename__ = "user_notification"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        sa_column=Column("user_id", SAUuid, nullable=False, index=True),
    )
    mission_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column("mission_id", SAUuid, nullable=True, index=True),
    )
    kind: str = Field(
        sa_column=Column("kind", String(64), nullable=False, index=True),
    )
    title: str = Field(
        sa_column=Column("title", String(256), nullable=False),
    )
    body: str = Field(
        sa_column=Column("body", Text, nullable=False),
    )
    payload: dict = Field(
        default_factory=dict,
        sa_column=Column("payload", JSON, nullable=False),
    )
    delivered_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("delivered_at", nullable=True),
    )
    acked_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("acked_at", nullable=True),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", nullable=False),
    )