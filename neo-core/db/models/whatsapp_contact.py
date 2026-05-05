"""WhatsApp contact model — saved contacts for WhatsApp messaging."""

import uuid
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, Index, String, Text, Uuid as SAUuid


class WhatsAppContact(SQLModel, table=True):
    __tablename__ = "whatsapp_contact"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        sa_column=Column("user_id", SAUuid, nullable=False, index=True),
    )
    jid: str = Field(
        sa_column=Column("jid", String(128), nullable=False),
    )
    label: str = Field(
        sa_column=Column("label", String(256), nullable=False),
    )
    phone_e164: str = Field(
        sa_column=Column("phone_e164", String(32), nullable=False),
    )
    notes: Optional[str] = Field(
        default=None,
        sa_column=Column("notes", Text, nullable=True),
    )
    last_used_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("last_used_at", nullable=True),
    )

    __table_args__ = (
        Index("ix_whatsapp_contact_user_jid", "user_id", "jid", unique=True),
    )