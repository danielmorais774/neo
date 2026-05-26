"""Synchronous PKS retrieval path for DIRECT_RESPONSE."""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import text

from core.pks.bootstrap import PKSBootstrap
from core.pks.context_builder import ContextBuilder
from core.pks.entity_resolver import EntityResolver
from core.pks.repository import PKSRepository
from core.pks.schemas import EntityCandidate, Neighborhood, RetrievalResult
from db.async_client import AsyncPostgresClient


GLOBAL_TYPES = ("person", "health_state", "restriction", "routine", "preference", "goal")


class PKSRetriever:
    def __init__(
        self,
        db: AsyncPostgresClient,
        repository: PKSRepository,
        bootstrap: PKSBootstrap,
        resolver: EntityResolver,
        context_builder: ContextBuilder,
    ) -> None:
        self.db = db
        self.repository = repository
        self.bootstrap = bootstrap
        self.resolver = resolver
        self.context_builder = context_builder

    async def retrieve(
        self,
        *,
        user_id: UUID,
        user_message: str,
        keywords: list[str] | None = None,
        max_candidates: int = 5,
    ) -> tuple[RetrievalResult, str]:
        workspace_id = await self.bootstrap.ensure_workspace(user_id)
        search_terms = self._keywords(user_message, keywords)

        async with self.db.get_session() as session:
            candidates: list[EntityCandidate] = []
            seen: set[UUID] = set()
            for keyword in search_terms:
                for candidate in await self.resolver.resolve(
                    session,
                    workspace_id=workspace_id,
                    query=keyword,
                    limit=3,
                ):
                    if candidate.entity.id not in seen:
                        candidates.append(candidate)
                        seen.add(candidate.entity.id)
                    if len(candidates) >= max_candidates:
                        break
                if len(candidates) >= max_candidates:
                    break

            global_entities = await self._global_entities(session, workspace_id)
            for entity in global_entities:
                if entity.id not in seen:
                    candidates.append(EntityCandidate(entity=entity, score=0.5, matched_by="global", keyword="global"))
                    seen.add(entity.id)

            neighborhoods = [
                await self._neighborhood(session, candidate.entity)
                for candidate in candidates
            ]
            await self.repository.touch_entities(session, [n.entity.id for n in neighborhoods])
            await session.commit()

        result = RetrievalResult(
            workspace_id=workspace_id,
            keywords=search_terms,
            candidates=candidates,
            neighborhoods=neighborhoods,
            metadata={
                "workspace_id": str(workspace_id),
                "keywords_extracted": search_terms,
                "entities_matched": len(candidates),
                "neighborhoods": len(neighborhoods),
            },
        )
        return result, self.context_builder.format(result, user_message=user_message)

    async def _global_entities(self, session, workspace_id: UUID):
        result = await session.execute(
            text(
                """
                SELECT *
                FROM pks_entity
                WHERE workspace_id = :workspace_id
                  AND deleted_at IS NULL
                  AND type = ANY(CAST(:types AS text[]))
                ORDER BY type, updated_at DESC
                LIMIT 12
                """
            ),
            {"workspace_id": workspace_id, "types": list(GLOBAL_TYPES)},
        )
        from core.pks.repository import _entity_from_row

        return [_entity_from_row(row) for row in result.mappings().all()]

    async def _neighborhood(self, session, entity) -> Neighborhood:
        fields = await self.repository.get_entity_fields(session, entity.id)
        relations = await self.repository.get_relations(session, entity.id)
        return Neighborhood(entity=entity, fields=fields, relations=relations)

    def _keywords(self, user_message: str, keywords: list[str] | None) -> list[str]:
        terms = [k.strip().lower() for k in (keywords or []) if k and k.strip()]
        if terms:
            return terms[:6]
        words = re.findall(r"[\wÀ-ÿ]{4,}", user_message.lower())
        stop = {
            "para", "como", "qual", "quais", "quando", "onde", "porque", "isso",
            "essa", "esse", "minha", "meu", "meus", "minhas", "sobre", "voce",
            "você", "pode", "fazer", "quero",
        }
        deduped: list[str] = []
        for word in words:
            if word not in stop and word not in deduped:
                deduped.append(word)
            if len(deduped) >= 6:
                break
        return deduped
