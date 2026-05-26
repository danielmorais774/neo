"""PKS graph API routes — expõe entidades e relações para visualização."""

from fastapi import APIRouter, Query
from sqlalchemy import text

from core.pks import PKSService

router = APIRouter(prefix="/knowledge-graph", tags=["knowledge-graph"])

_pks: PKSService | None = None


def set_pks_service(service: PKSService) -> None:
    global _pks
    _pks = service


@router.get("/graph")
async def get_graph(user_id: str | None = Query(default=None)):
    """Retorna entidades e relações do PKS."""
    if _pks is None:
        return {"entities": [], "relations": []}

    workspace_filter = ""
    params = {}
    if user_id:
        from uuid import UUID

        workspace_id = await _pks.bootstrap.ensure_workspace(UUID(user_id))
        workspace_filter = "AND workspace_id = :workspace_id"
        params["workspace_id"] = workspace_id

    async with _pks.db.session_factory() as session:
        entities_result = await session.execute(
            text(
                f"""
                SELECT id, workspace_id, type, name, aliases, metadata, confidence, sensitivity, created_at
                FROM pks_entity
                WHERE deleted_at IS NULL {workspace_filter}
                ORDER BY updated_at DESC
                LIMIT 500
                """
            ),
            params,
        )
        relation_result = await session.execute(
            text(
                f"""
                SELECT id, workspace_id, from_entity_id, to_entity_id, relation_type, metadata, confidence, sensitivity
                FROM pks_entity_relation
                WHERE deleted_at IS NULL AND valid_until IS NULL {workspace_filter}
                ORDER BY updated_at DESC
                LIMIT 1000
                """
            ),
            params,
        )
        entities = entities_result.mappings().all()
        relations = relation_result.mappings().all()

        return {
            "entities": [
                {
                    "id": str(e["id"]),
                    "type": e["type"],
                    "name": e["name"],
                    "workspace_id": str(e["workspace_id"]),
                    "aliases": list(e["aliases"] or []),
                    "metadata": e["metadata"] or {},
                    "confidence": float(e["confidence"] or 0),
                    "sensitivity": e["sensitivity"],
                    "created_at": e["created_at"].isoformat(),
                }
                for e in entities
            ],
            "relations": [
                {
                    "id": str(r["id"]),
                    "source_id": str(r["from_entity_id"]),
                    "target_id": str(r["to_entity_id"]),
                    "type": r["relation_type"],
                    "workspace_id": str(r["workspace_id"]),
                    "metadata": r["metadata"] or {},
                    "confidence": float(r["confidence"] or 0),
                    "sensitivity": r["sensitivity"],
                }
                for r in relations
            ],
        }


@router.get("/users")
async def list_users_with_graph():
    """Lista usuários com workspace PKS."""
    if _pks is None:
        return {"user_ids": []}
    async with _pks.db.session_factory() as session:
        result = await session.execute(
            text(
                """
                SELECT DISTINCT owner_user_id
                FROM pks_workspace
                WHERE owner_user_id IS NOT NULL AND deleted_at IS NULL
                """
            )
        )
        user_ids = [str(row[0]) for row in result.fetchall()]
        return {"user_ids": user_ids}


@router.get("/workspaces")
async def list_workspaces():
    """Lista workspaces PKS disponíveis."""
    if _pks is None:
        return {"workspaces": []}
    async with _pks.db.session_factory() as session:
        result = await session.execute(
            text(
                """
                SELECT id, owner_user_id, name, type, created_at, updated_at
                FROM pks_workspace
                WHERE deleted_at IS NULL
                ORDER BY updated_at DESC
                LIMIT 50
                """
            )
        )
        rows = result.mappings().all()
        return {
            "workspaces": [
                {
                    "id": str(row["id"]),
                    "owner_user_id": str(row["owner_user_id"]) if row["owner_user_id"] else None,
                    "name": row["name"],
                    "type": row["type"],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                }
                for row in rows
            ]
        }


@router.get("/entities")
async def list_entities(
    workspace_id: str | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
):
    """Lista entidades de um workspace PKS."""
    if _pks is None:
        return {"entities": []}

    params: dict = {"limit": limit}
    type_filter = ""
    if entity_type:
        type_filter = "AND type = :entity_type"
        params["entity_type"] = entity_type

    workspace_filter = ""
    if workspace_id:
        from uuid import UUID
        params["workspace_id"] = workspace_id
        workspace_filter = "AND workspace_id = :workspace_id"

    async with _pks.db.session_factory() as session:
        result = await session.execute(
            text(
                f"""
                SELECT id, workspace_id, space_id, collection_id, name, type, aliases,
                       metadata, external_refs, confidence, sensitivity, source,
                       source_message_id, last_used_at, created_at, updated_at
                FROM pks_entity
                WHERE deleted_at IS NULL {type_filter} {workspace_filter}
                ORDER BY updated_at DESC
                LIMIT :limit
                """
            ),
            params,
        )
        rows = result.mappings().all()
        return {
            "entities": [
                {
                    "id": str(row["id"]),
                    "workspace_id": str(row["workspace_id"]),
                    "space_id": str(row["space_id"]) if row["space_id"] else None,
                    "collection_id": str(row["collection_id"]) if row["collection_id"] else None,
                    "name": row["name"],
                    "type": row["type"],
                    "aliases": list(row["aliases"] or []),
                    "metadata": row["metadata"] or {},
                    "external_refs": row["external_refs"] or {},
                    "confidence": float(row["confidence"] or 0),
                    "sensitivity": row["sensitivity"] or "private",
                    "source": row["source"],
                    "source_message_id": row["source_message_id"],
                    "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                }
                for row in rows
            ]
        }


@router.get("/entities/{entity_id}")
async def get_entity(entity_id: str):
    """Retorna uma entidade com seus fields e relações."""
    if _pks is None:
        return {"entity": None, "fields": [], "relations": []}

    from uuid import UUID
    eid = UUID(entity_id)

    async with _pks.db.session_factory() as session:
        # Entity
        ent_result = await session.execute(
            text(
                """
                SELECT id, workspace_id, space_id, collection_id, name, type, aliases,
                       metadata, external_refs, confidence, sensitivity, source,
                       source_message_id, last_used_at, created_at, updated_at
                FROM pks_entity
                WHERE id = :id AND deleted_at IS NULL
                """
            ),
            {"id": eid},
        )
        ent_row = ent_result.mappings().first()
        if not ent_row:
            return {"entity": None, "fields": [], "relations": []}

        entity = {
            "id": str(ent_row["id"]),
            "workspace_id": str(ent_row["workspace_id"]),
            "space_id": str(ent_row["space_id"]) if ent_row["space_id"] else None,
            "collection_id": str(ent_row["collection_id"]) if ent_row["collection_id"] else None,
            "name": ent_row["name"],
            "type": ent_row["type"],
            "aliases": list(ent_row["aliases"] or []),
            "metadata": ent_row["metadata"] or {},
            "external_refs": ent_row["external_refs"] or {},
            "confidence": float(ent_row["confidence"] or 0),
            "sensitivity": ent_row["sensitivity"] or "private",
            "source": ent_row["source"],
            "source_message_id": ent_row["source_message_id"],
            "last_used_at": ent_row["last_used_at"].isoformat() if ent_row["last_used_at"] else None,
            "created_at": ent_row["created_at"].isoformat() if ent_row["created_at"] else None,
            "updated_at": ent_row["updated_at"].isoformat() if ent_row["updated_at"] else None,
        }

        # Fields
        fields_result = await session.execute(
            text(
                """
                SELECT id, entity_id, key, value_type, value_text, value_number,
                       value_boolean, value_date, value_json, unit, confidence,
                       sensitivity, source, source_message_id, actor, metadata,
                       created_at, updated_at
                FROM pks_entity_field
                WHERE entity_id = :entity_id AND deleted_at IS NULL
                ORDER BY key
                """
            ),
            {"entity_id": eid},
        )
        fields = []
        for row in fields_result.mappings().all():
            if row["value_type"] == "number" and row["value_number"] is not None:
                value = float(row["value_number"])
            elif row["value_type"] == "boolean":
                value = bool(row["value_boolean"])
            elif row["value_type"] == "date" and row["value_date"]:
                value = row["value_date"].isoformat()
            elif row["value_type"] == "json":
                value = row["value_json"]
            else:
                value = row["value_text"]
            fields.append({
                "id": str(row["id"]),
                "entity_id": str(row["entity_id"]),
                "key": row["key"],
                "value_type": row["value_type"],
                "value": value,
                "unit": row["unit"],
                "confidence": float(row["confidence"] or 0),
                "sensitivity": row["sensitivity"] or "private",
                "source": row["source"],
                "source_message_id": row["source_message_id"],
                "actor": row["actor"],
                "metadata": row["metadata"] or {},
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            })

        # Relations (both directions)
        rel_result = await session.execute(
            text(
                """
                SELECT r.id, r.workspace_id, r.from_entity_id, r.relation_type,
                       r.to_entity_id, r.metadata, r.confidence, r.sensitivity,
                       r.valid_from, r.valid_until, r.created_at, r.updated_at,
                       fe.name AS from_name, fe.type AS from_type,
                       te.name AS to_name, te.type AS to_type
                FROM pks_entity_relation r
                JOIN pks_entity fe ON fe.id = r.from_entity_id
                JOIN pks_entity te ON te.id = r.to_entity_id
                WHERE (r.from_entity_id = :entity_id OR r.to_entity_id = :entity_id)
                  AND r.deleted_at IS NULL
                ORDER BY r.created_at DESC
                """
            ),
            {"entity_id": eid},
        )
        relations = []
        for row in rel_result.mappings().all():
            relations.append({
                "id": str(row["id"]),
                "workspace_id": str(row["workspace_id"]),
                "from_entity_id": str(row["from_entity_id"]),
                "relation_type": row["relation_type"],
                "to_entity_id": str(row["to_entity_id"]),
                "metadata": row["metadata"] or {},
                "confidence": float(row["confidence"] or 0),
                "sensitivity": row["sensitivity"] or "private",
                "valid_from": row["valid_from"].isoformat() if row["valid_from"] else None,
                "valid_until": row["valid_until"].isoformat() if row["valid_until"] else None,
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                "from_name": row["from_name"],
                "from_type": row["from_type"],
                "to_name": row["to_name"],
                "to_type": row["to_type"],
            })

        return {"entity": entity, "fields": fields, "relations": relations}


@router.get("/entities/{entity_id}/raw")
async def get_entity_raw(entity_id: str):
    """Retorna o registro bruto da tabela pks_entity (select * where id = ...)."""
    if _pks is None:
        return {"row": None}

    from uuid import UUID
    eid = UUID(entity_id)

    async with _pks.db.session_factory() as session:
        result = await session.execute(
            text("SELECT * FROM pks_entity WHERE id = :id AND deleted_at IS NULL"),
            {"id": eid},
        )
        row = result.mappings().first()
        if not row:
            return {"row": None}

        def _format(v):
            if hasattr(v, 'isoformat'):
                return v.isoformat()
            if hasattr(v, '__bytes__'):
                return str(v)
            return v

        return {
            "row": {
                k: _format(v) for k, v in row.items()
            }
        }


@router.get("/collections")
async def list_collections(
    workspace_id: str | None = Query(default=None),
):
    """Lista collections de um workspace PKS."""
    if _pks is None:
        return {"collections": []}

    params: dict = {}
    workspace_filter = ""
    if workspace_id:
        from uuid import UUID
        params["workspace_id"] = UUID(workspace_id)
        workspace_filter = "AND workspace_id = :workspace_id"

    async with _pks.db.session_factory() as session:
        result = await session.execute(
            text(
                f"""
                SELECT id, workspace_id, space_id, name, type, schema, metadata,
                       created_at, updated_at
                FROM pks_collection
                WHERE deleted_at IS NULL {workspace_filter}
                ORDER BY updated_at DESC
                LIMIT 100
                """
            ),
            params,
        )
        rows = result.mappings().all()
        return {
            "collections": [
                {
                    "id": str(row["id"]),
                    "workspace_id": str(row["workspace_id"]),
                    "space_id": str(row["space_id"]) if row["space_id"] else None,
                    "name": row["name"],
                    "type": row["type"],
                    "schema": row["schema"] or {},
                    "metadata": row["metadata"] or {},
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                }
                for row in rows
            ]
        }


@router.get("/audit")
async def get_audit_log(
    workspace_id: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
):
    """Retorna histórico de auditoria do PKS."""
    if _pks is None:
        return {"entries": []}

    params: dict = {"limit": limit}
    workspace_filter = ""
    if workspace_id:
        from uuid import UUID
        params["workspace_id"] = UUID(workspace_id)
        workspace_filter = "AND workspace_id = :workspace_id"

    async with _pks.db.session_factory() as session:
        result = await session.execute(
            text(
                f"""
                SELECT id, workspace_id, actor_type, actor_id, action, target_type,
                       target_id, before, after, reason, confidence, source_message_id,
                       request_id, created_at
                FROM pks_audit_log
                WHERE workspace_id IS NOT NULL {workspace_filter}
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            params,
        )
        rows = result.mappings().all()
        return {
            "entries": [
                {
                    "id": str(row["id"]),
                    "workspace_id": str(row["workspace_id"]) if row["workspace_id"] else None,
                    "actor_type": row["actor_type"],
                    "actor_id": row["actor_id"],
                    "action": row["action"],
                    "target_type": row["target_type"],
                    "target_id": str(row["target_id"]) if row["target_id"] else None,
                    "before": row["before"],
                    "after": row["after"],
                    "reason": row["reason"],
                    "confidence": float(row["confidence"]) if row["confidence"] else None,
                    "source_message_id": row["source_message_id"],
                    "request_id": row["request_id"],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                }
                for row in rows
            ]
        }
