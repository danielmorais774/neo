"""Typed contracts for PKS services."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


Direction = Literal["in", "out", "both"]
PKSOperationType = Literal[
    "upsert_entity",
    "upsert_field",
    "upsert_relation",
    "expire_relation",
    "soft_delete_entity",
]


class EntityRecord(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    type: str
    aliases: list[str] = Field(default_factory=list)
    search_text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0
    sensitivity: str = "private"
    last_used_at: datetime | None = None


class FieldRecord(BaseModel):
    id: UUID
    entity_id: UUID
    key: str
    value_type: str
    value: Any = None
    unit: str | None = None
    confidence: float = 1.0
    sensitivity: str = "private"


class RelationRecord(BaseModel):
    id: UUID
    workspace_id: UUID
    from_entity_id: UUID
    relation_type: str
    to_entity_id: UUID
    metadata: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0
    sensitivity: str = "private"
    valid_until: datetime | None = None
    from_name: str | None = None
    from_type: str | None = None
    to_name: str | None = None
    to_type: str | None = None


class EntityCandidate(BaseModel):
    entity: EntityRecord
    score: float
    matched_by: str
    keyword: str


class TraverseInput(BaseModel):
    workspace_id: UUID
    start_entity_ids: list[UUID]
    max_depth: int = Field(default=1, ge=0, le=3)
    relation_types: list[str] | None = None
    target_types: list[str] | None = None
    direction: Direction = "both"


class UpsertEntityInput(BaseModel):
    workspace_id: UUID
    name: str
    type: str
    aliases: list[str] = Field(default_factory=list)
    space_id: UUID | None = None
    collection_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    external_refs: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0
    sensitivity: str = "private"
    source: str | None = "conversation"
    source_message_id: str | None = None


class UpsertFieldInput(BaseModel):
    entity_id: UUID
    key: str
    value_type: str
    value: Any
    unit: str | None = None
    confidence: float = 1.0
    sensitivity: str = "private"
    source: str | None = "conversation"
    source_message_id: str | None = None
    actor: str | None = "agent"
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpsertRelationInput(BaseModel):
    workspace_id: UUID
    from_entity_id: UUID
    relation_type: str
    to_entity_id: UUID
    metadata: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0
    sensitivity: str = "private"
    source: str | None = "conversation"
    source_message_id: str | None = None


class PKSOperation(BaseModel):
    op: PKSOperationType
    confidence: float = 1.0
    reason: str | None = None
    entity: dict[str, Any] | None = None
    field: dict[str, Any] | None = None
    relation: dict[str, Any] | None = None


class Neighborhood(BaseModel):
    entity: EntityRecord
    fields: list[FieldRecord] = Field(default_factory=list)
    relations: list[RelationRecord] = Field(default_factory=list)


class RetrievalResult(BaseModel):
    workspace_id: UUID
    keywords: list[str] = Field(default_factory=list)
    candidates: list[EntityCandidate] = Field(default_factory=list)
    neighborhoods: list[Neighborhood] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
