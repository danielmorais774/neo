"""Low-level PostgreSQL access for PKS."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.pks.ontology import normalize_token
from core.pks.schemas import (
    EntityRecord,
    FieldRecord,
    RelationRecord,
    TraverseInput,
    UpsertEntityInput,
    UpsertFieldInput,
    UpsertRelationInput,
)

logger = logging.getLogger(__name__)


def _json_dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def build_search_text(name: str, aliases: list[str] | None = None, metadata: dict[str, Any] | None = None) -> str:
    parts = [name, *(aliases or [])]
    if metadata:
        for value in metadata.values():
            if isinstance(value, (str, int, float, bool)):
                parts.append(str(value))
    return normalize_token(" ".join(parts))


def _as_float(value: Any, default: float = 1.0) -> float:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _entity_from_row(row: Any) -> EntityRecord:
    return EntityRecord(
        id=row["id"],
        workspace_id=row["workspace_id"],
        name=row["name"],
        type=row["type"],
        aliases=list(row["aliases"] or []),
        search_text=row["search_text"],
        metadata=row["metadata"] or {},
        confidence=_as_float(row["confidence"]),
        sensitivity=row["sensitivity"] or "private",
        last_used_at=row["last_used_at"],
    )


def _field_value(row: Any) -> Any:
    value_type = row["value_type"]
    if value_type == "number":
        return _as_float(row["value_number"], 0.0) if row["value_number"] is not None else None
    if value_type == "boolean":
        return row["value_boolean"]
    if value_type == "date":
        return row["value_date"] or row["value_text"]
    if value_type == "json":
        return row["value_json"]
    return row["value_text"]


def _field_from_row(row: Any) -> FieldRecord:
    return FieldRecord(
        id=row["id"],
        entity_id=row["entity_id"],
        key=row["key"],
        value_type=row["value_type"],
        value=_field_value(row),
        unit=row["unit"],
        confidence=_as_float(row["confidence"]),
        sensitivity=row["sensitivity"] or "private",
    )


def _relation_from_row(row: Any) -> RelationRecord:
    return RelationRecord(
        id=row["id"],
        workspace_id=row["workspace_id"],
        from_entity_id=row["from_entity_id"],
        relation_type=row["relation_type"],
        to_entity_id=row["to_entity_id"],
        metadata=row["metadata"] or {},
        confidence=_as_float(row["confidence"]),
        sensitivity=row["sensitivity"] or "private",
        valid_until=row["valid_until"],
        from_name=row.get("from_name"),
        from_type=row.get("from_type"),
        to_name=row.get("to_name"),
        to_type=row.get("to_type"),
    )


class PKSRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def get_workspace_by_id(self, session: AsyncSession, workspace_id: UUID) -> dict[str, Any] | None:
        result = await session.execute(
            text(
                """
                SELECT id, owner_user_id, name, type, metadata, external_refs, created_at, updated_at
                FROM pks_workspace
                WHERE id = :workspace_id AND deleted_at IS NULL
                LIMIT 1
                """
            ),
            {"workspace_id": workspace_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def find_workspace_by_name(
        self,
        session: AsyncSession,
        *,
        name: str,
        type_: str | None = None,
    ) -> dict[str, Any] | None:
        type_filter = "AND type = :type" if type_ else ""
        result = await session.execute(
            text(
                f"""
                SELECT id, owner_user_id, name, type, metadata, external_refs, created_at, updated_at
                FROM pks_workspace
                WHERE lower(name) = lower(:name)
                  AND deleted_at IS NULL
                  {type_filter}
                ORDER BY created_at
                LIMIT 1
                """
            ),
            {"name": name, "type": type_},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def get_system_workspace(self, session: AsyncSession) -> dict[str, Any] | None:
        return await self.find_workspace_by_name(
            session,
            name="neo",
            type_="system",
        )

    async def get_personal_workspace(self, session: AsyncSession, user_id: UUID) -> EntityRecord | None:
        result = await session.execute(
            text(
                """
                SELECT id, id AS workspace_id, name, type, '{}'::text[] AS aliases,
                       NULL AS search_text, metadata, 1 AS confidence, 'private' AS sensitivity,
                       NULL AS last_used_at
                FROM pks_workspace
                WHERE owner_user_id = :user_id AND type = 'person' AND deleted_at IS NULL
                ORDER BY created_at
                LIMIT 1
                """
            ),
            {"user_id": user_id},
        )
        row = result.mappings().first()
        return _entity_from_row(row) if row else None

    async def ensure_workspace(self, session: AsyncSession, user_id: UUID, name: str) -> UUID:
        existing = await session.execute(
            text(
                """
                SELECT id
                FROM pks_workspace
                WHERE owner_user_id = :user_id AND type = 'person' AND deleted_at IS NULL
                ORDER BY created_at
                LIMIT 1
                """
            ),
            {"user_id": user_id},
        )
        row = existing.first()
        if row:
            return row[0]

        workspace = await self.create_workspace(
            session,
            name=name,
            type_="person",
            owner_user_id=user_id,
            metadata={},
            external_refs={},
        )
        return workspace["id"]

    async def create_workspace(
        self,
        session: AsyncSession,
        *,
        name: str,
        type_: str,
        owner_user_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        external_refs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        workspace_id = uuid4()
        result = await session.execute(
            text(
                """
                INSERT INTO pks_workspace (id, owner_user_id, name, type, metadata, external_refs, created_at, updated_at)
                VALUES (:id, :owner_user_id, :name, :type, CAST(:metadata AS jsonb), CAST(:external_refs AS jsonb), NOW(), NOW())
                RETURNING id, owner_user_id, name, type, metadata, external_refs, created_at, updated_at
                """
            ),
            {
                "id": workspace_id,
                "owner_user_id": owner_user_id,
                "name": name,
                "type": type_,
                "metadata": _json_dumps(metadata),
                "external_refs": _json_dumps(external_refs),
            },
        )
        return dict(result.mappings().one())

    async def merge_workspace_metadata(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
        external_refs: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        result = await session.execute(
            text(
                """
                UPDATE pks_workspace
                SET name = COALESCE(:name, name),
                    metadata = metadata || CAST(:metadata AS jsonb),
                    external_refs = external_refs || CAST(:external_refs AS jsonb),
                    updated_at = NOW()
                WHERE id = :workspace_id AND deleted_at IS NULL
                RETURNING id, owner_user_id, name, type, metadata, external_refs, created_at, updated_at
                """
            ),
            {
                "workspace_id": workspace_id,
                "name": name,
                "metadata": _json_dumps(metadata),
                "external_refs": _json_dumps(external_refs),
            },
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def ensure_space(self, session: AsyncSession, workspace_id: UUID, name: str, type_: str) -> UUID:
        result = await session.execute(
            text(
                """
                SELECT id
                FROM pks_space
                WHERE workspace_id = :workspace_id AND lower(name) = lower(:name) AND deleted_at IS NULL
                LIMIT 1
                """
            ),
            {"workspace_id": workspace_id, "name": name},
        )
        row = result.first()
        if row:
            return row[0]

        space_id = uuid4()
        await session.execute(
            text(
                """
                INSERT INTO pks_space (id, workspace_id, name, type, metadata, created_at, updated_at)
                VALUES (:id, :workspace_id, :name, :type, '{}'::jsonb, NOW(), NOW())
                """
            ),
            {"id": space_id, "workspace_id": workspace_id, "name": name, "type": type_},
        )
        return space_id

    async def ensure_collection(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        space_id: UUID,
        name: str,
        type_: str,
    ) -> UUID:
        result = await session.execute(
            text(
                """
                SELECT id
                FROM pks_collection
                WHERE space_id = :space_id AND lower(name) = lower(:name) AND deleted_at IS NULL
                LIMIT 1
                """
            ),
            {"space_id": space_id, "name": name},
        )
        row = result.first()
        if row:
            return row[0]

        collection_id = uuid4()
        await session.execute(
            text(
                """
                INSERT INTO pks_collection
                  (id, workspace_id, space_id, name, type, schema, metadata, created_at, updated_at)
                VALUES (:id, :workspace_id, :space_id, :name, :type, '{}'::jsonb, '{}'::jsonb, NOW(), NOW())
                """
            ),
            {
                "id": collection_id,
                "workspace_id": workspace_id,
                "space_id": space_id,
                "name": name,
                "type": type_,
            },
        )
        return collection_id

    async def get_entity(self, session: AsyncSession, entity_id: UUID) -> EntityRecord | None:
        result = await session.execute(
            text("SELECT * FROM pks_entity WHERE id = :id AND deleted_at IS NULL"),
            {"id": entity_id},
        )
        row = result.mappings().first()
        return _entity_from_row(row) if row else None

    async def get_primary_user_entity(self, session: AsyncSession, workspace_id: UUID) -> EntityRecord | None:
        result = await session.execute(
            text(
                """
                SELECT *
                FROM pks_entity
                WHERE workspace_id = :workspace_id
                  AND type = 'person'
                  AND deleted_at IS NULL
                  AND metadata @> '{"is_primary_user": true}'::jsonb
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            {"workspace_id": workspace_id},
        )
        row = result.mappings().first()
        return _entity_from_row(row) if row else None

    async def get_system_agent_entity(self, session: AsyncSession, workspace_id: UUID) -> EntityRecord | None:
        result = await session.execute(
            text(
                """
                SELECT *
                FROM pks_entity
                WHERE workspace_id = :workspace_id
                  AND type = 'software'
                  AND deleted_at IS NULL
                  AND metadata @> '{"is_system_agent": true}'::jsonb
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            {"workspace_id": workspace_id},
        )
        row = result.mappings().first()
        return _entity_from_row(row) if row else None

    async def get_system_personality_entity(self, session: AsyncSession, workspace_id: UUID) -> EntityRecord | None:
        result = await session.execute(
            text(
                """
                SELECT *
                FROM pks_entity
                WHERE workspace_id = :workspace_id
                  AND type = 'identity'
                  AND deleted_at IS NULL
                  AND (
                    lower(name) = 'neo-personality'
                    OR metadata @> '{"is_core_identity": true}'::jsonb
                  )
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            {"workspace_id": workspace_id},
        )
        row = result.mappings().first()
        return _entity_from_row(row) if row else None

    async def update_entity_identity(
        self,
        session: AsyncSession,
        *,
        entity_id: UUID,
        name: str,
        aliases: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        source: str | None = None,
        source_message_id: str | None = None,
        confidence: float | None = None,
    ) -> tuple[EntityRecord, dict[str, Any] | None]:
        before = await self.get_entity(session, entity_id)
        if before is None:
            raise ValueError(f"Entity {entity_id} not found")

        merged_aliases = sorted({*before.aliases, *(aliases or [])})
        merged_metadata = {**before.metadata, **(metadata or {})}
        await session.execute(
            text(
                """
                WITH duplicate AS (
                    UPDATE pks_entity
                    SET deleted_at = NOW(), updated_at = NOW()
                    WHERE workspace_id = :workspace_id
                      AND type = :type
                      AND lower(name) = lower(:name)
                      AND id <> :id
                      AND deleted_at IS NULL
                    RETURNING id
                ),
                deleted_fields AS (
                    UPDATE pks_entity_field
                    SET deleted_at = NOW(), updated_at = NOW()
                    WHERE entity_id IN (SELECT id FROM duplicate)
                      AND deleted_at IS NULL
                    RETURNING id
                )
                UPDATE pks_entity_relation
                SET deleted_at = NOW(),
                    valid_until = COALESCE(valid_until, NOW()),
                    updated_at = NOW()
                WHERE (from_entity_id IN (SELECT id FROM duplicate)
                   OR to_entity_id IN (SELECT id FROM duplicate))
                  AND deleted_at IS NULL
                """
            ),
            {
                "workspace_id": before.workspace_id,
                "type": before.type,
                "name": name,
                "id": entity_id,
            },
        )
        result = await session.execute(
            text(
                """
                UPDATE pks_entity
                SET name = :name,
                    aliases = :aliases,
                    search_text = :search_text,
                    metadata = CAST(:metadata AS jsonb),
                    confidence = GREATEST(confidence, CAST(:confidence AS numeric)),
                    source = COALESCE(:source, source),
                    source_message_id = COALESCE(:source_message_id, source_message_id),
                    updated_at = NOW()
                WHERE id = :id
                RETURNING *
                """
            ),
            {
                "id": entity_id,
                "name": name,
                "aliases": merged_aliases,
                "search_text": build_search_text(name, merged_aliases, merged_metadata),
                "metadata": _json_dumps(merged_metadata),
                "confidence": confidence,
                "source": source,
                "source_message_id": source_message_id,
            },
        )
        return _entity_from_row(result.mappings().one()), before.model_dump(mode="json")

    async def update_workspace_name(self, session: AsyncSession, workspace_id: UUID, name: str) -> None:
        await session.execute(
            text(
                """
                UPDATE pks_workspace
                SET name = :name, updated_at = NOW()
                WHERE id = :workspace_id AND deleted_at IS NULL
                """
            ),
            {"workspace_id": workspace_id, "name": name},
        )

    async def find_entity_by_name(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        name: str,
        entity_type: str | None = None,
    ) -> EntityRecord | None:
        type_filter = "AND type = :type" if entity_type else ""
        result = await session.execute(
            text(
                f"""
                SELECT *
                FROM pks_entity
                WHERE workspace_id = :workspace_id
                  AND deleted_at IS NULL
                  AND (lower(name) = lower(:name) OR lower(:name) = ANY(SELECT lower(unnest(aliases))))
                  {type_filter}
                ORDER BY confidence DESC, updated_at DESC
                LIMIT 1
                """
            ),
            {"workspace_id": workspace_id, "name": name, "type": entity_type},
        )
        row = result.mappings().first()
        return _entity_from_row(row) if row else None

    async def search_entities(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        query: str,
        entity_types: list[str] | None = None,
        limit: int = 5,
    ) -> list[tuple[EntityRecord, float, str]]:
        if not query.strip():
            return []
        params: dict[str, Any] = {
            "workspace_id": workspace_id,
            "query": query,
            "qnorm": normalize_token(query),
            "limit": limit,
        }
        type_filter = ""
        if entity_types:
            type_filter = "AND type = ANY(CAST(:types AS text[]))"
            params["types"] = entity_types
        result = await session.execute(
            text(
                f"""
                SELECT *,
                       CASE
                         WHEN lower(name) = lower(:query) THEN 1.0
                         WHEN lower(:query) = ANY(SELECT lower(unnest(aliases))) THEN 0.95
                         ELSE GREATEST(similarity(search_text, :qnorm), 0)
                       END AS match_score,
                       CASE
                         WHEN lower(name) = lower(:query) THEN 'name'
                         WHEN lower(:query) = ANY(SELECT lower(unnest(aliases))) THEN 'alias'
                         ELSE 'trigram'
                       END AS matched_by
                FROM pks_entity
                WHERE workspace_id = :workspace_id
                  AND deleted_at IS NULL
                  {type_filter}
                  AND (
                    lower(name) = lower(:query)
                    OR lower(:query) = ANY(SELECT lower(unnest(aliases)))
                    OR search_text % :qnorm
                    OR search_text LIKE '%' || :qnorm || '%'
                  )
                ORDER BY match_score DESC, last_used_at DESC NULLS LAST, confidence DESC
                LIMIT :limit
                """
            ),
            params,
        )
        rows = result.mappings().all()
        return [(_entity_from_row(row), float(row["match_score"] or 0), row["matched_by"]) for row in rows]

    async def list_entities_by_type(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        entity_type: str,
        limit: int = 10,
    ) -> list[EntityRecord]:
        result = await session.execute(
            text(
                """
                SELECT *
                FROM pks_entity
                WHERE workspace_id = :workspace_id
                  AND type = :entity_type
                  AND deleted_at IS NULL
                ORDER BY updated_at DESC, confidence DESC
                LIMIT :limit
                """
            ),
            {"workspace_id": workspace_id, "entity_type": entity_type, "limit": limit},
        )
        return [_entity_from_row(row) for row in result.mappings().all()]

    async def upsert_entity(self, session: AsyncSession, data: UpsertEntityInput) -> tuple[EntityRecord, dict[str, Any] | None]:
        aliases = sorted({a.strip() for a in data.aliases if a and a.strip() and normalize_token(a) != normalize_token(data.name)})
        search_text = build_search_text(data.name, aliases, data.metadata)
        before = await self.find_entity_by_name(session, data.workspace_id, data.name, data.type)
        if before:
            merged_aliases = sorted({*before.aliases, *aliases})
            merged_metadata = {**before.metadata, **data.metadata}
            result = await session.execute(
                text(
                    """
                    UPDATE pks_entity
                    SET aliases = :aliases,
                        space_id = COALESCE(:space_id, space_id),
                        collection_id = COALESCE(:collection_id, collection_id),
                        search_text = :search_text,
                        metadata = metadata || CAST(:metadata AS jsonb),
                        external_refs = external_refs || CAST(:external_refs AS jsonb),
                        confidence = GREATEST(confidence, :confidence),
                        sensitivity = :sensitivity,
                        source = COALESCE(:source, source),
                        source_message_id = COALESCE(:source_message_id, source_message_id),
                        updated_at = NOW(),
                        deleted_at = NULL
                    WHERE id = :id
                    RETURNING *
                    """
                ),
                {
                    "id": before.id,
                    "aliases": merged_aliases,
                    "space_id": data.space_id,
                    "collection_id": data.collection_id,
                    "search_text": build_search_text(data.name, merged_aliases, merged_metadata),
                    "metadata": _json_dumps(data.metadata),
                    "external_refs": _json_dumps(data.external_refs),
                    "confidence": data.confidence,
                    "sensitivity": data.sensitivity,
                    "source": data.source,
                    "source_message_id": data.source_message_id,
                },
            )
            row = result.mappings().one()
            return _entity_from_row(row), before.model_dump(mode="json")

        entity_id = uuid4()
        result = await session.execute(
            text(
                """
                INSERT INTO pks_entity
                  (id, workspace_id, space_id, collection_id, name, type, aliases, search_text,
                   metadata, external_refs, confidence, sensitivity, source, source_message_id,
                   created_at, updated_at)
                VALUES
                  (:id, :workspace_id, :space_id, :collection_id, :name, :type, :aliases, :search_text,
                   CAST(:metadata AS jsonb), CAST(:external_refs AS jsonb), :confidence, :sensitivity,
                   :source, :source_message_id, NOW(), NOW())
                RETURNING *
                """
            ),
            {
                "id": entity_id,
                "workspace_id": data.workspace_id,
                "space_id": data.space_id,
                "collection_id": data.collection_id,
                "name": data.name,
                "type": data.type,
                "aliases": aliases,
                "search_text": search_text,
                "metadata": _json_dumps(data.metadata),
                "external_refs": _json_dumps(data.external_refs),
                "confidence": data.confidence,
                "sensitivity": data.sensitivity,
                "source": data.source,
                "source_message_id": data.source_message_id,
            },
        )
        return _entity_from_row(result.mappings().one()), None

    async def upsert_field(self, session: AsyncSession, data: UpsertFieldInput) -> tuple[FieldRecord, dict[str, Any] | None]:
        before = await self.get_field(session, data.entity_id, data.key)
        values = self._encode_field_values(data.value_type, data.value)
        params = {
            "entity_id": data.entity_id,
            "key": data.key,
            "value_type": data.value_type,
            "unit": data.unit,
            "confidence": data.confidence,
            "sensitivity": data.sensitivity,
            "source": data.source,
            "source_message_id": data.source_message_id,
            "actor": data.actor,
            "metadata": _json_dumps(data.metadata),
            **values,
        }
        if before:
            result = await session.execute(
                text(
                    """
                    UPDATE pks_entity_field
                    SET value_type = :value_type,
                        value_text = :value_text,
                        value_number = :value_number,
                        value_boolean = :value_boolean,
                        value_date = :value_date,
                        value_json = CAST(:value_json AS jsonb),
                        unit = :unit,
                        confidence = GREATEST(confidence, :confidence),
                        sensitivity = :sensitivity,
                        source = COALESCE(:source, source),
                        source_message_id = COALESCE(:source_message_id, source_message_id),
                        actor = COALESCE(:actor, actor),
                        metadata = metadata || CAST(:metadata AS jsonb),
                        updated_at = NOW(),
                        deleted_at = NULL
                    WHERE id = :id
                    RETURNING *
                    """
                ),
                {"id": before.id, **params},
            )
            return _field_from_row(result.mappings().one()), before.model_dump(mode="json")

        field_id = uuid4()
        result = await session.execute(
            text(
                """
                INSERT INTO pks_entity_field
                  (id, entity_id, key, value_type, value_text, value_number, value_boolean, value_date,
                   value_json, unit, confidence, sensitivity, source, source_message_id, actor, metadata,
                   created_at, updated_at)
                VALUES
                  (:id, :entity_id, :key, :value_type, :value_text, :value_number, :value_boolean, :value_date,
                   CAST(:value_json AS jsonb), :unit, :confidence, :sensitivity, :source, :source_message_id,
                   :actor, CAST(:metadata AS jsonb), NOW(), NOW())
                RETURNING *
                """
            ),
            {"id": field_id, **params},
        )
        return _field_from_row(result.mappings().one()), None

    async def get_field(self, session: AsyncSession, entity_id: UUID, key: str) -> FieldRecord | None:
        result = await session.execute(
            text(
                """
                SELECT *
                FROM pks_entity_field
                WHERE entity_id = :entity_id AND lower(key) = lower(:key) AND deleted_at IS NULL
                LIMIT 1
                """
            ),
            {"entity_id": entity_id, "key": key},
        )
        row = result.mappings().first()
        return _field_from_row(row) if row else None

    async def get_entity_fields(self, session: AsyncSession, entity_id: UUID) -> list[FieldRecord]:
        result = await session.execute(
            text(
                """
                SELECT *
                FROM pks_entity_field
                WHERE entity_id = :entity_id AND deleted_at IS NULL
                ORDER BY key
                """
            ),
            {"entity_id": entity_id},
        )
        return [_field_from_row(row) for row in result.mappings().all()]

    async def upsert_relation(self, session: AsyncSession, data: UpsertRelationInput) -> tuple[RelationRecord, dict[str, Any] | None]:
        before = await self.find_relation(session, data.from_entity_id, data.relation_type, data.to_entity_id, include_deleted=True)
        if before:
            result = await session.execute(
                text(
                    """
                    UPDATE pks_entity_relation
                    SET metadata = metadata || CAST(:metadata AS jsonb),
                        confidence = GREATEST(confidence, :confidence),
                        sensitivity = :sensitivity,
                        source = COALESCE(:source, source),
                        source_message_id = COALESCE(:source_message_id, source_message_id),
                        valid_until = NULL,
                        deleted_at = NULL,
                        updated_at = NOW()
                    WHERE id = :id
                    RETURNING *
                    """
                ),
                {
                    "id": before.id,
                    "metadata": _json_dumps(data.metadata),
                    "confidence": data.confidence,
                    "sensitivity": data.sensitivity,
                    "source": data.source,
                    "source_message_id": data.source_message_id,
                },
            )
            return _relation_from_row(result.mappings().one()), before.model_dump(mode="json")

        relation_id = uuid4()
        result = await session.execute(
            text(
                """
                INSERT INTO pks_entity_relation
                  (id, workspace_id, from_entity_id, relation_type, to_entity_id, metadata,
                   confidence, sensitivity, source, source_message_id, valid_from, created_at, updated_at)
                VALUES
                  (:id, :workspace_id, :from_entity_id, :relation_type, :to_entity_id, CAST(:metadata AS jsonb),
                   :confidence, :sensitivity, :source, :source_message_id, NOW(), NOW(), NOW())
                RETURNING *
                """
            ),
            {
                "id": relation_id,
                "workspace_id": data.workspace_id,
                "from_entity_id": data.from_entity_id,
                "relation_type": data.relation_type,
                "to_entity_id": data.to_entity_id,
                "metadata": _json_dumps(data.metadata),
                "confidence": data.confidence,
                "sensitivity": data.sensitivity,
                "source": data.source,
                "source_message_id": data.source_message_id,
            },
        )
        return _relation_from_row(result.mappings().one()), None

    async def find_relation(
        self,
        session: AsyncSession,
        from_entity_id: UUID,
        relation_type: str,
        to_entity_id: UUID,
        include_deleted: bool = False,
    ) -> RelationRecord | None:
        deleted_filter = "" if include_deleted else "AND deleted_at IS NULL"
        result = await session.execute(
            text(
                f"""
                SELECT *
                FROM pks_entity_relation
                WHERE from_entity_id = :from_entity_id
                  AND relation_type = :relation_type
                  AND to_entity_id = :to_entity_id
                  {deleted_filter}
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"from_entity_id": from_entity_id, "relation_type": relation_type, "to_entity_id": to_entity_id},
        )
        row = result.mappings().first()
        return _relation_from_row(row) if row else None

    async def expire_relation(self, session: AsyncSession, relation_id: UUID) -> dict[str, Any] | None:
        before = await self.get_relation(session, relation_id)
        if not before:
            return None
        await session.execute(
            text("UPDATE pks_entity_relation SET valid_until = NOW(), updated_at = NOW() WHERE id = :id"),
            {"id": relation_id},
        )
        return before.model_dump(mode="json")

    async def get_relation(self, session: AsyncSession, relation_id: UUID) -> RelationRecord | None:
        result = await session.execute(
            text("SELECT * FROM pks_entity_relation WHERE id = :id AND deleted_at IS NULL"),
            {"id": relation_id},
        )
        row = result.mappings().first()
        return _relation_from_row(row) if row else None

    async def soft_delete_entity(self, session: AsyncSession, entity_id: UUID) -> dict[str, Any] | None:
        before = await self.get_entity(session, entity_id)
        if not before:
            return None
        await session.execute(
            text("UPDATE pks_entity SET deleted_at = NOW(), updated_at = NOW() WHERE id = :id"),
            {"id": entity_id},
        )
        await session.execute(
            text(
                """
                UPDATE pks_entity_field
                SET deleted_at = NOW(), updated_at = NOW()
                WHERE entity_id = :id AND deleted_at IS NULL
                """
            ),
            {"id": entity_id},
        )
        await session.execute(
            text(
                """
                UPDATE pks_entity_relation
                SET deleted_at = NOW(), valid_until = COALESCE(valid_until, NOW()), updated_at = NOW()
                WHERE (from_entity_id = :id OR to_entity_id = :id) AND deleted_at IS NULL
                """
            ),
            {"id": entity_id},
        )
        return before.model_dump(mode="json")

    async def get_relations(self, session: AsyncSession, entity_id: UUID) -> list[RelationRecord]:
        result = await session.execute(
            text(
                """
                SELECT r.*, fe.name AS from_name, fe.type AS from_type, te.name AS to_name, te.type AS to_type
                FROM pks_entity_relation r
                JOIN pks_entity fe ON fe.id = r.from_entity_id
                JOIN pks_entity te ON te.id = r.to_entity_id
                WHERE (r.from_entity_id = :id OR r.to_entity_id = :id)
                  AND r.deleted_at IS NULL
                  AND r.valid_until IS NULL
                ORDER BY r.created_at DESC
                """
            ),
            {"id": entity_id},
        )
        return [_relation_from_row(row) for row in result.mappings().all()]

    async def traverse(self, session: AsyncSession, data: TraverseInput) -> list[EntityRecord]:
        if not data.start_entity_ids:
            return []
        relation_filter = ""
        if data.relation_types:
            relation_filter = "AND r.relation_type = ANY(CAST(:relation_types AS text[]))"
        target_filter = ""
        if data.target_types:
            target_filter = "AND e.type = ANY(CAST(:target_types AS text[]))"
        out_leg = """
            SELECT r.to_entity_id AS entity_id, walk.depth + 1 AS depth, walk.path || r.to_entity_id
            FROM walk
            JOIN pks_entity_relation r ON r.from_entity_id = walk.entity_id
            WHERE r.workspace_id = :workspace_id
              AND r.deleted_at IS NULL
              AND r.valid_until IS NULL
              AND NOT r.to_entity_id = ANY(walk.path)
              {relation_filter}
        """.format(relation_filter=relation_filter)
        in_leg = """
            SELECT r.from_entity_id AS entity_id, walk.depth + 1 AS depth, walk.path || r.from_entity_id
            FROM walk
            JOIN pks_entity_relation r ON r.to_entity_id = walk.entity_id
            WHERE r.workspace_id = :workspace_id
              AND r.deleted_at IS NULL
              AND r.valid_until IS NULL
              AND NOT r.from_entity_id = ANY(walk.path)
              {relation_filter}
        """.format(relation_filter=relation_filter)
        legs = []
        if data.direction in {"out", "both"}:
            legs.append(out_leg)
        if data.direction in {"in", "both"}:
            legs.append(in_leg)
        recursive_union = "\nUNION ALL\n".join(legs) if legs else out_leg
        result = await session.execute(
            text(
                f"""
                WITH RECURSIVE walk(entity_id, depth, path) AS (
                    SELECT unnest(CAST(:start_entity_ids AS uuid[])) AS entity_id,
                           0 AS depth,
                           CAST(:start_entity_ids AS uuid[]) AS path
                    UNION ALL
                    {recursive_union}
                )
                SELECT DISTINCT ON (e.id) e.*
                FROM walk
                JOIN pks_entity e ON e.id = walk.entity_id
                WHERE walk.depth <= :max_depth
                  AND e.workspace_id = :workspace_id
                  AND e.deleted_at IS NULL
                  {target_filter}
                ORDER BY e.id, walk.depth
                """
            ),
            {
                "workspace_id": data.workspace_id,
                "start_entity_ids": data.start_entity_ids,
                "max_depth": data.max_depth,
                "relation_types": data.relation_types,
                "target_types": data.target_types,
            },
        )
        return [_entity_from_row(row) for row in result.mappings().all()]

    async def touch_entities(self, session: AsyncSession, entity_ids: list[UUID]) -> None:
        if not entity_ids:
            return
        await session.execute(
            text("UPDATE pks_entity SET last_used_at = NOW() WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": entity_ids},
        )

    def _encode_field_values(self, value_type: str, value: Any) -> dict[str, Any]:
        encoded = {
            "value_text": None,
            "value_number": None,
            "value_boolean": None,
            "value_date": None,
            "value_json": "{}",
        }
        if value_type == "number":
            encoded["value_number"] = value
        elif value_type == "boolean":
            encoded["value_boolean"] = bool(value)
        elif value_type == "date":
            if isinstance(value, datetime):
                encoded["value_date"] = value
            elif isinstance(value, date):
                encoded["value_date"] = datetime.combine(value, datetime.min.time())
            else:
                encoded["value_text"] = str(value)
        elif value_type == "json":
            encoded["value_json"] = _json_dumps(value)
        else:
            encoded["value_text"] = "" if value is None else str(value)
        return encoded
