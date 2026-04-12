"""Knowledge Graph API routes — expõe entidades e relações para visualização."""

from fastapi import APIRouter, Query
from sqlalchemy import select

from db.models.knowledge_graph import KnowledgeEntity, KnowledgeRelation

router = APIRouter(prefix="/knowledge-graph", tags=["knowledge-graph"])

_db = None


def set_db(db) -> None:
    global _db
    _db = db


@router.get("/graph")
async def get_graph(user_id: str | None = Query(default=None)):
    """Retorna todas as entidades e relações do grafo de conhecimento."""
    async with _db.session_factory() as session:
        # Entidades
        q_entities = select(KnowledgeEntity)
        if user_id:
            from uuid import UUID
            q_entities = q_entities.where(KnowledgeEntity.user_id == UUID(user_id))
        result = await session.execute(q_entities)
        entities = result.scalars().all()

        # Relações
        q_relations = select(KnowledgeRelation)
        if user_id:
            q_relations = q_relations.where(KnowledgeRelation.user_id == UUID(user_id))
        result = await session.execute(q_relations)
        relations = result.scalars().all()

        return {
            "entities": [
                {
                    "id": str(e.id),
                    "type": e.type,
                    "name": e.name,
                    "user_id": str(e.user_id) if e.user_id else None,
                    "properties": e.properties or {},
                    "created_at": e.created_at.isoformat(),
                }
                for e in entities
            ],
            "relations": [
                {
                    "id": str(r.id),
                    "source_id": str(r.source_id),
                    "target_id": str(r.target_id),
                    "type": r.type,
                    "user_id": str(r.user_id) if r.user_id else None,
                    "properties": r.properties or {},
                }
                for r in relations
            ],
        }


@router.get("/users")
async def list_users_with_graph():
    """Lista usuários que têm dados no grafo de conhecimento."""
    async with _db.session_factory() as session:
        result = await session.execute(
            select(KnowledgeEntity.user_id).distinct().where(KnowledgeEntity.user_id.isnot(None))
        )
        user_ids = [str(row[0]) for row in result.all()]
        return {"user_ids": user_ids}
