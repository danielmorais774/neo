"""Synchronous PKS retrieval path for DIRECT_RESPONSE."""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import text

from core.pks.bootstrap import PKSBootstrap
from core.pks.context_builder import ContextBuilder
from core.pks.entity_resolver import EntityResolver
from core.pks.ontology import OntologyResolver
from core.pks.repository import PKSRepository
from core.pks.schemas import EntityCandidate, Neighborhood, RetrievalResult
from db.async_client import AsyncPostgresClient


# Types unconditionally loaded for every retrieval (hard health/safety constraints).
# "person" is excluded because basic identity (name) is already in the Neo system
# workspace (daniel-profile). Full person neighborhood with hardware/workout
# relations should only appear when the query is actually about those things.
# "routine", "preference" and "goal" are excluded to prevent workout/goal context
# from leaking into unrelated queries (e.g. "quanto é 2+2?").
GLOBAL_TYPES = ("health_state", "restriction")

# Entity-type fallback lookup is delegated to the canonical OntologyResolver
# (core/pks/ontology.py), which handles PT/EN aliases and plural forms. This
# avoids a second, drifting synonym map: "dispositivos", "devices", "device"
# all resolve to type "device" there.
_ONTOLOGY = OntologyResolver()

# A resolved type pulls in its sibling types for the fallback query, so recall
# isn't split by the data model's internal granularity. E.g. "treino" resolves
# to "workout" but the weekly plan is stored as "routine" and movements as
# "exercise"; the user means all three.
_TYPE_FAMILY: dict[str, tuple[str, ...]] = {
    "workout": ("workout", "routine", "exercise"),
    "routine": ("routine", "workout", "exercise"),
    "exercise": ("exercise", "workout"),
    "meal": ("meal", "food", "supplement"),
    "food": ("food", "meal"),
}


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

            # Fallback: if name/alias search returned nothing, try type-synonym expansion.
            # "quais dispositivos?" → no trigram match for "MacBook Air M4", but "dispositivos"
            # maps to type "device" and we load all device entities by type.
            if not candidates:
                synonym_types = self._synonym_types(search_terms)
                if synonym_types:
                    type_entities = await self._entities_by_types(session, workspace_id, synonym_types)
                    for entity in type_entities:
                        if entity.id not in seen:
                            candidates.append(EntityCandidate(
                                entity=entity, score=0.6, matched_by="type_synonym", keyword="type"
                            ))
                            seen.add(entity.id)
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

    def _synonym_types(self, search_terms: list[str]) -> list[str]:
        """Return entity types implied by any query term, via the ontology resolver."""
        types: list[str] = []
        seen: set[str] = set()
        for term in search_terms:
            etype = _ONTOLOGY.resolve_entity_type(term)
            if not etype:
                continue
            for t in _TYPE_FAMILY.get(etype.value, (etype.value,)):
                if t not in seen:
                    types.append(t)
                    seen.add(t)
        return types

    async def _entities_by_types(self, session, workspace_id: UUID, types: list[str]):
        result = await session.execute(
            text(
                """
                SELECT *
                FROM pks_entity
                WHERE workspace_id = :workspace_id
                  AND deleted_at IS NULL
                  AND type = ANY(CAST(:types AS text[]))
                ORDER BY updated_at DESC
                LIMIT 10
                """
            ),
            {"workspace_id": workspace_id, "types": types},
        )
        from core.pks.repository import _entity_from_row
        return [_entity_from_row(row) for row in result.mappings().all()]

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
