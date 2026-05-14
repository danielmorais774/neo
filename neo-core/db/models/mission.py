"""Mission model — represents an async WhatsApp order mission."""

import uuid
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, String, Text, Uuid as SAUuid, JSON


class Mission(SQLModel, table=True):
    __tablename__ = "mission"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        sa_column=Column("user_id", SAUuid, nullable=False, index=True),
    )
    session_id: uuid.UUID = Field(
        sa_column=Column("session_id", SAUuid, nullable=False, index=True),
    )
    parent_session_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column("parent_session_id", SAUuid, nullable=True, index=True),
    )
    title: str = Field(
        sa_column=Column("title", String(256), nullable=False),
    )
    goal: str = Field(
        sa_column=Column("goal", Text, nullable=False),
    )
    contact_jid: str = Field(
        sa_column=Column("contact_jid", String(128), nullable=False, index=True),
    )
    channel: str = Field(
        default="whatsapp",
        sa_column=Column("channel", String(32), nullable=False),
    )
    status: str = Field(
        default="pending",
        sa_column=Column("status", String(32), nullable=False, index=True),
    )
    next_action_type: Optional[str] = Field(
        default=None,
        sa_column=Column("next_action_type", String(64), nullable=True),
    )
    next_action_params: dict = Field(
        default_factory=dict,
        sa_column=Column("next_action_params", JSON, nullable=False),
    )
    last_inbound_message_id: Optional[str] = Field(
        default=None,
        sa_column=Column("last_inbound_message_id", String(128), nullable=True),
    )
    last_activity_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("last_activity_at", nullable=False),
    )
    timeout_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("timeout_at", nullable=True),
    )
    result_summary: Optional[str] = Field(
        default=None,
        sa_column=Column("result_summary", Text, nullable=True),
    )
    extra_metadata: dict = Field(
        default_factory=dict,
        sa_column=Column("extra_metadata", JSON, nullable=False),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", nullable=False),
    )
    completed_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("completed_at", nullable=True),
    )
