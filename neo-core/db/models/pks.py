"""Personal Knowledge Store models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, SQLModel

try:
    from pgvector.sqlalchemy import Vector

    HAS_PGVECTOR = True
except ImportError:
    HAS_PGVECTOR = False


def _now() -> datetime:
    return datetime.utcnow()


class PKSWorkspace(SQLModel, table=True):
    __tablename__ = "pks_workspace"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    owner_user_id: Optional[UUID] = Field(
        default=None,
        sa_column=Column("owner_user_id", SAUuid, ForeignKey("user.id"), nullable=True),
    )
    name: str = Field(sa_column=Column("name", Text, nullable=False))
    type: str = Field(sa_column=Column("type", String(64), nullable=False))
    extra_metadata: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSONB, nullable=False))
    external_refs: dict[str, Any] = Field(default_factory=dict, sa_column=Column("external_refs", JSONB, nullable=False))
    created_at: datetime = Field(default_factory=_now, sa_column=Column("created_at", DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=_now, sa_column=Column("updated_at", DateTime(timezone=True), nullable=False))
    deleted_at: Optional[datetime] = Field(default=None, sa_column=Column("deleted_at", DateTime(timezone=True), nullable=True))


class PKSSpace(SQLModel, table=True):
    __tablename__ = "pks_space"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    workspace_id: UUID = Field(sa_column=Column("workspace_id", SAUuid, ForeignKey("pks_workspace.id"), nullable=False))
    name: str = Field(sa_column=Column("name", Text, nullable=False))
    type: str = Field(sa_column=Column("type", String(64), nullable=False))
    extra_metadata: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSONB, nullable=False))
    created_at: datetime = Field(default_factory=_now, sa_column=Column("created_at", DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=_now, sa_column=Column("updated_at", DateTime(timezone=True), nullable=False))
    deleted_at: Optional[datetime] = Field(default=None, sa_column=Column("deleted_at", DateTime(timezone=True), nullable=True))


class PKSCollection(SQLModel, table=True):
    __tablename__ = "pks_collection"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    workspace_id: UUID = Field(sa_column=Column("workspace_id", SAUuid, ForeignKey("pks_workspace.id"), nullable=False))
    space_id: UUID = Field(sa_column=Column("space_id", SAUuid, ForeignKey("pks_space.id"), nullable=False))
    name: str = Field(sa_column=Column("name", Text, nullable=False))
    type: str = Field(sa_column=Column("type", String(64), nullable=False))
    field_schema: dict[str, Any] = Field(default_factory=dict, sa_column=Column("schema", JSONB, nullable=False))
    extra_metadata: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSONB, nullable=False))
    created_at: datetime = Field(default_factory=_now, sa_column=Column("created_at", DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=_now, sa_column=Column("updated_at", DateTime(timezone=True), nullable=False))
    deleted_at: Optional[datetime] = Field(default=None, sa_column=Column("deleted_at", DateTime(timezone=True), nullable=True))


class PKSEntity(SQLModel, table=True):
    __tablename__ = "pks_entity"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    workspace_id: UUID = Field(sa_column=Column("workspace_id", SAUuid, ForeignKey("pks_workspace.id"), nullable=False))
    space_id: Optional[UUID] = Field(default=None, sa_column=Column("space_id", SAUuid, ForeignKey("pks_space.id"), nullable=True))
    collection_id: Optional[UUID] = Field(default=None, sa_column=Column("collection_id", SAUuid, ForeignKey("pks_collection.id"), nullable=True))
    name: str = Field(sa_column=Column("name", Text, nullable=False))
    type: str = Field(sa_column=Column("type", String(64), nullable=False))
    aliases: list[str] = Field(default_factory=list, sa_column=Column("aliases", ARRAY(Text), nullable=False))
    search_text: Optional[str] = Field(default=None, sa_column=Column("search_text", Text, nullable=True))
    extra_metadata: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSONB, nullable=False))
    external_refs: dict[str, Any] = Field(default_factory=dict, sa_column=Column("external_refs", JSONB, nullable=False))
    embedding: Optional[list[float]] = Field(
        default=None,
        sa_column=Column("embedding", Vector(384) if HAS_PGVECTOR else JSONB, nullable=True),
    )
    confidence: Decimal = Field(default=Decimal("1"), sa_column=Column("confidence", Numeric, nullable=False))
    sensitivity: str = Field(default="private", sa_column=Column("sensitivity", String(32), nullable=False))
    source: Optional[str] = Field(default=None, sa_column=Column("source", String(64), nullable=True))
    source_message_id: Optional[str] = Field(default=None, sa_column=Column("source_message_id", Text, nullable=True))
    last_used_at: Optional[datetime] = Field(default=None, sa_column=Column("last_used_at", DateTime(timezone=True), nullable=True))
    last_verified_at: Optional[datetime] = Field(default=None, sa_column=Column("last_verified_at", DateTime(timezone=True), nullable=True))
    created_at: datetime = Field(default_factory=_now, sa_column=Column("created_at", DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=_now, sa_column=Column("updated_at", DateTime(timezone=True), nullable=False))
    deleted_at: Optional[datetime] = Field(default=None, sa_column=Column("deleted_at", DateTime(timezone=True), nullable=True))


class PKSEntityField(SQLModel, table=True):
    __tablename__ = "pks_entity_field"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    entity_id: UUID = Field(sa_column=Column("entity_id", SAUuid, ForeignKey("pks_entity.id"), nullable=False))
    key: str = Field(sa_column=Column("key", Text, nullable=False))
    value_type: str = Field(sa_column=Column("value_type", String(32), nullable=False))
    value_text: Optional[str] = Field(default=None, sa_column=Column("value_text", Text, nullable=True))
    value_number: Optional[Decimal] = Field(default=None, sa_column=Column("value_number", Numeric, nullable=True))
    value_boolean: Optional[bool] = None
    value_date: Optional[datetime] = Field(default=None, sa_column=Column("value_date", DateTime(timezone=True), nullable=True))
    value_json: Optional[dict[str, Any]] = Field(default=None, sa_column=Column("value_json", JSONB, nullable=True))
    unit: Optional[str] = Field(default=None, sa_column=Column("unit", Text, nullable=True))
    confidence: Decimal = Field(default=Decimal("1"), sa_column=Column("confidence", Numeric, nullable=False))
    sensitivity: str = Field(default="private", sa_column=Column("sensitivity", String(32), nullable=False))
    source: Optional[str] = Field(default=None, sa_column=Column("source", String(64), nullable=True))
    source_message_id: Optional[str] = Field(default=None, sa_column=Column("source_message_id", Text, nullable=True))
    actor: Optional[str] = Field(default=None, sa_column=Column("actor", Text, nullable=True))
    extra_metadata: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSONB, nullable=False))
    created_at: datetime = Field(default_factory=_now, sa_column=Column("created_at", DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=_now, sa_column=Column("updated_at", DateTime(timezone=True), nullable=False))
    deleted_at: Optional[datetime] = Field(default=None, sa_column=Column("deleted_at", DateTime(timezone=True), nullable=True))


class PKSEntityRelation(SQLModel, table=True):
    __tablename__ = "pks_entity_relation"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    workspace_id: UUID = Field(sa_column=Column("workspace_id", SAUuid, ForeignKey("pks_workspace.id"), nullable=False))
    from_entity_id: UUID = Field(sa_column=Column("from_entity_id", SAUuid, ForeignKey("pks_entity.id"), nullable=False))
    relation_type: str = Field(sa_column=Column("relation_type", String(64), nullable=False))
    to_entity_id: UUID = Field(sa_column=Column("to_entity_id", SAUuid, ForeignKey("pks_entity.id"), nullable=False))
    extra_metadata: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSONB, nullable=False))
    confidence: Decimal = Field(default=Decimal("1"), sa_column=Column("confidence", Numeric, nullable=False))
    sensitivity: str = Field(default="private", sa_column=Column("sensitivity", String(32), nullable=False))
    source: Optional[str] = Field(default=None, sa_column=Column("source", String(64), nullable=True))
    source_message_id: Optional[str] = Field(default=None, sa_column=Column("source_message_id", Text, nullable=True))
    valid_from: datetime = Field(default_factory=_now, sa_column=Column("valid_from", DateTime(timezone=True), nullable=False))
    valid_until: Optional[datetime] = Field(default=None, sa_column=Column("valid_until", DateTime(timezone=True), nullable=True))
    created_at: datetime = Field(default_factory=_now, sa_column=Column("created_at", DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=_now, sa_column=Column("updated_at", DateTime(timezone=True), nullable=False))
    deleted_at: Optional[datetime] = Field(default=None, sa_column=Column("deleted_at", DateTime(timezone=True), nullable=True))


class PKSWorkspaceLink(SQLModel, table=True):
    __tablename__ = "pks_workspace_link"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    from_workspace_id: UUID = Field(sa_column=Column("from_workspace_id", SAUuid, ForeignKey("pks_workspace.id"), nullable=False))
    to_workspace_id: UUID = Field(sa_column=Column("to_workspace_id", SAUuid, ForeignKey("pks_workspace.id"), nullable=False))
    relation_type: str = Field(sa_column=Column("relation_type", String(64), nullable=False))
    extra_metadata: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSONB, nullable=False))
    created_at: datetime = Field(default_factory=_now, sa_column=Column("created_at", DateTime(timezone=True), nullable=False))
    deleted_at: Optional[datetime] = Field(default=None, sa_column=Column("deleted_at", DateTime(timezone=True), nullable=True))


class PKSAuditLog(SQLModel, table=True):
    __tablename__ = "pks_audit_log"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    workspace_id: Optional[UUID] = Field(default=None, sa_column=Column("workspace_id", SAUuid, ForeignKey("pks_workspace.id"), nullable=True))
    actor_type: str = Field(sa_column=Column("actor_type", String(32), nullable=False))
    actor_id: Optional[str] = Field(default=None, sa_column=Column("actor_id", Text, nullable=True))
    action: str = Field(sa_column=Column("action", String(64), nullable=False))
    target_type: str = Field(sa_column=Column("target_type", String(64), nullable=False))
    target_id: Optional[UUID] = Field(default=None, sa_column=Column("target_id", SAUuid, nullable=True))
    before: Optional[dict[str, Any]] = Field(default=None, sa_column=Column("before", JSONB, nullable=True))
    after: Optional[dict[str, Any]] = Field(default=None, sa_column=Column("after", JSONB, nullable=True))
    reason: Optional[str] = Field(default=None, sa_column=Column("reason", Text, nullable=True))
    confidence: Optional[Decimal] = Field(default=None, sa_column=Column("confidence", Numeric, nullable=True))
    source_message_id: Optional[str] = Field(default=None, sa_column=Column("source_message_id", Text, nullable=True))
    request_id: Optional[str] = Field(default=None, sa_column=Column("request_id", Text, nullable=True))
    created_at: datetime = Field(default_factory=_now, sa_column=Column("created_at", DateTime(timezone=True), nullable=False))
