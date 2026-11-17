"""Entity resolution for PKS."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.pks.ontology import OntologyResolver
from core.pks.repository import PKSRepository
from core.pks.schemas import EntityCandidate, EntityRecord


class EntityResolver:
    def __init__(
        self,
        repository: PKSRepository,
        ontology: OntologyResolver | None = None,
    ) -> None:
        self.repository = repository
        self.ontology = ontology or OntologyResolver()

    async def resolve(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        query: str,
        entity_type: str | None = None,
        limit: int = 3,
    ) -> list[EntityCandidate]:
        entity_types = [entity_type] if entity_type else None
        rows = await self.repository.search_entities(
            session,
            workspace_id=workspace_id,
            query=query,
            entity_types=entity_types,
            limit=limit,
        )
        candidates: list[EntityCandidate] = []
        for entity, score, matched_by in rows:
            recency_boost = 0.05 if entity.last_used_at else 0.0
            confidence_boost = min(entity.confidence, 1.0) * 0.05
            candidates.append(
                EntityCandidate(
                    entity=entity,
                    score=min(score + recency_boost + confidence_boost, 1.0),
                    matched_by=matched_by,
                    keyword=query,
                )
            )
        return sorted(candidates, key=lambda c: c.score, reverse=True)

    async def resolve_one(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        query: str,
        entity_type: str | None = None,
        min_score: float = 0.25,
    ) -> EntityRecord | None:
        candidates = await self.resolve(
            session,
            workspace_id=workspace_id,
            query=query,
            entity_type=entity_type,
            limit=1,
        )
        if not candidates or candidates[0].score < min_score:
            return None
        return candidates[0].entity
