"""Synchronous PKS retrieval path for DIRECT_RESPONSE."""

from __future__ import annotations

import logging
import re
import time
from uuid import UUID

from sqlalchemy import text

from core.pks.bootstrap import PKSBootstrap
from core.pks.context_builder import ContextBuilder
from core.pks.entity_resolver import EntityResolver
from core.pks.ledger_guard import is_ledger_memory
from core.pks.ontology import OntologyResolver
from core.pks.ranking import RankConfig, RankOutcome, WorkspaceIndex, build_workspace_index, rank_pks
from core.pks.repository import PKSRepository
from core.pks.schemas import EntityCandidate, Neighborhood, RetrievalResult
from db.async_client import AsyncPostgresClient
from shared.config import get_settings


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
logger = logging.getLogger(__name__)

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

# Score assigned to an entity recalled because the query named its type
# ("dispositivos" → all device entities). Must stay above
# context_relevance_threshold (0.25) so the PKS block enters the prompt.
_TYPE_MATCH_SCORE = 0.6


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
        recent_messages: list[str] | None = None,
    ) -> tuple[RetrievalResult, str]:
        """Bloco de PKS do turno. Com ``pks_ranker_v2_enabled`` vem da nota de
        prioridade (``core/pks/ranking.py``); senão, do caminho antigo — e, com
        ``pks_ranker_v2_shadow``, o v2 roda junto só para o log comparar."""
        settings = get_settings()
        if settings.pks_ranker_v2_enabled:
            return await self.retrieve_ranked(
                user_id=user_id,
                user_message=user_message,
                keywords=keywords,
                recent_messages=recent_messages,
            )
        result, text_out = await self._retrieve_legacy(
            user_id=user_id,
            user_message=user_message,
            keywords=keywords,
            max_candidates=max_candidates,
        )
        if settings.pks_ranker_v2_shadow:
            try:
                shadow, _ = await self.retrieve_ranked(
                    user_id=user_id,
                    user_message=user_message,
                    keywords=keywords,
                    recent_messages=recent_messages,
                    touch=False,
                )
                result.metadata["ranker_v2_shadow"] = self._shadow_diff(text_out, shadow)
            except Exception:  # noqa: BLE001 — a sombra nunca derruba o turno
                logger.exception("[PKS][rank-shadow] falhou; seguindo com o bloco antigo")
        return result, text_out

    async def retrieve_ranked(
        self,
        *,
        user_id: UUID,
        user_message: str,
        keywords: list[str] | None = None,
        recent_messages: list[str] | None = None,
        budget_tokens: int | None = None,
        relative_cutoff: float | None = None,
        touch: bool = True,
    ) -> tuple[RetrievalResult, str]:
        """Nota de prioridade sobre o workspace inteiro (docs/pks-ranking-plan.md)."""
        workspace_id, outcome, entities, fields, relations = await self.rank(
            user_id=user_id,
            user_message=user_message,
            keywords=keywords,
            recent_messages=recent_messages,
            budget_tokens=budget_tokens,
            relative_cutoff=relative_cutoff,
            touch=touch,
        )
        by_id = {e.id: e for e in entities}
        neighborhoods = [
            Neighborhood(
                entity=g.head.entity,
                fields=fields.get(g.head.entity.id, []),
                relations=[
                    r for r in relations
                    if g.head.entity.id in (r.from_entity_id, r.to_entity_id)
                    and r.from_entity_id in by_id and r.to_entity_id in by_id
                ],
            )
            for g in outcome.groups
        ]
        candidates = [
            EntityCandidate(
                entity=g.head.entity,
                score=round(g.score, 4),
                matched_by=_dominant_signal(g.head.signals),
                keyword="rank_v2",
            )
            for g in outcome.groups
        ]
        result = RetrievalResult(
            workspace_id=workspace_id,
            keywords=outcome.query_terms,
            candidates=candidates,
            neighborhoods=neighborhoods,
            metadata={
                "ranker": "v2",
                "workspace_id": str(workspace_id),
                "keywords_extracted": outcome.query_terms,
                "asked_types": outcome.asked_types,
                "entities_matched": len(candidates),
                "neighborhoods": len(neighborhoods),
                "block_tokens": outcome.tokens,
                "explain": outcome.explain(15),
                "groups": [
                    {"entity": g.head.entity.name, "level": g.level, "score": round(g.score, 3),
                     "members": [by_id[m].name for m in g.members if m in by_id]}
                    for g in outcome.groups
                ],
            },
        )
        return result, outcome.text

    async def rank(
        self,
        *,
        user_id: UUID,
        user_message: str,
        keywords: list[str] | None = None,
        recent_messages: list[str] | None = None,
        budget_tokens: int | None = None,
        relative_cutoff: float | None = None,
        touch: bool = True,
    ) -> tuple[UUID, RankOutcome, list, dict, list]:
        """Carrega o workspace e roda o ranking. Usado pelo bloco do turno e pela
        ``search_pks`` — a mesma função, para os dois não se contradizerem."""
        workspace_id = await self.bootstrap.ensure_workspace(user_id)
        async with self.db.get_session() as session:
            entities, fields, relations, windex = await self._workspace_graph(session, workspace_id)
            outcome = rank_pks(
                workspace=windex,
                message=user_message,
                keywords=keywords,
                recent_messages=recent_messages,
                config=self.rank_config(budget_tokens=budget_tokens, relative_cutoff=relative_cutoff),
                count_tokens=_count_tokens,
            )
            if touch and outcome.groups:
                await self.repository.touch_entities(session, outcome.included_ids)
                await session.commit()
        return workspace_id, outcome, entities, fields, relations

    async def _workspace_graph(
        self, session, workspace_id: UUID
    ) -> tuple[list, dict, list, WorkspaceIndex]:
        """Workspace + índice do ranking, em cache enquanto o PKS não muda."""
        fp = await self.repository.workspace_fingerprint(session, workspace_id)
        hit = _WS_CACHE.get(workspace_id)
        if hit and hit[0] == fp and time.monotonic() - hit[1] < _WS_CACHE_TTL_S:
            return hit[2], hit[3], hit[4], hit[5]
        entities, fields, relations = await self.repository.load_workspace_graph(session, workspace_id)
        windex = build_workspace_index(entities, fields, relations)
        _WS_CACHE[workspace_id] = (fp, time.monotonic(), entities, fields, relations, windex)
        return entities, fields, relations, windex

    @staticmethod
    def rank_config(
        *, budget_tokens: int | None = None, relative_cutoff: float | None = None
    ) -> RankConfig:
        s = get_settings()
        budget_max = 0
        if budget_tokens is None:
            # O bloco entra no seletor embrulhado neste título e é UM candidato
            # só: se passar do orçamento, some inteiro. Por isso o ranking
            # empacota por dentro, e o TETO desconta o título e uma folga — o
            # seletor aceita até `pks_rank_budget_max` quando o v2 está ligado.
            budget_tokens = s.pks_rank_budget_base
            budget_max = s.pks_rank_budget_max - _count_tokens("## Conhecimento do Usuário\n") - 12
        return RankConfig(
            weights=dict(s.pks_rank_weights or {}),
            min_score=s.pks_rank_min_score,
            relative_cutoff=s.pks_rank_relative_cutoff if relative_cutoff is None else relative_cutoff,
            kappa=s.pks_rank_kappa,
            history_turns=s.pks_rank_history_turns,
            list_cap=s.pks_rank_list_cap,
            budget_tokens=max(0, budget_tokens),
            budget_max_tokens=max(0, budget_max),
            strong_ratio=s.pks_rank_strong_ratio,
        )

    @staticmethod
    def _shadow_diff(old_text: str, shadow: RetrievalResult) -> dict:
        antigas = _block_entities(old_text)
        novas = [g["entity"] for g in shadow.metadata.get("groups", [])]
        return {
            "entraria": [n for n in novas if n not in antigas],
            "sairia": [n for n in antigas if n not in novas],
            "v2_bloco": novas,
            "v2_tokens": shadow.metadata.get("block_tokens"),
            "explain": shadow.metadata.get("explain", []),
        }

    async def _retrieve_legacy(
        self,
        *,
        user_id: UUID,
        user_message: str,
        keywords: list[str] | None = None,
        max_candidates: int = 5,
    ) -> tuple[RetrievalResult, str]:
        workspace_id = await self.bootstrap.ensure_workspace(user_id)
        search_terms = self._keywords(user_message, keywords)
        synonym_types = self._synonym_types(search_terms)

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

            # Type-synonym recall — runs whenever the query names an entity type
            # ("quais dispositivos?" → type "device"), NOT only as a fallback.
            # A weak, incidental trigram match (e.g. score 0.18 on a device name)
            # used to suppress this and leave the PKS block below the relevance
            # threshold, so the device knowledge never reached the LLM
            # (session 384236e9). We now (a) load all entities of the type and
            # (b) upgrade any already-collected entity of that type to the
            # type-match score so the block clears the threshold.
            if synonym_types:
                type_set = set(synonym_types)
                by_id = {c.entity.id: c for c in candidates}
                # Upgrade weak matches that happen to be of the requested type.
                for cand in candidates:
                    if cand.entity.type in type_set and cand.score < _TYPE_MATCH_SCORE:
                        cand.score = _TYPE_MATCH_SCORE
                        cand.matched_by = "type_synonym"
                type_cap = max(max_candidates, 10)
                type_entities = await self._entities_by_types(session, workspace_id, synonym_types)
                for entity in type_entities:
                    if entity.id in by_id or entity.id in seen:
                        continue
                    candidates.append(EntityCandidate(
                        entity=entity, score=_TYPE_MATCH_SCORE, matched_by="type_synonym", keyword="type"
                    ))
                    seen.add(entity.id)
                    if len(candidates) >= type_cap:
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
            # Dinheiro não vai para o prompt: valor/saldo guardado aqui é cópia velha
            # do extrato (tabelas fin_*), e chega ao modelo SEM passar por tool nenhuma.
            # Ver core/pks/ledger_guard.
            ledger_ids = {
                n.entity.id
                for n in neighborhoods
                if is_ledger_memory(n.entity.name, [f.key for f in n.fields])
            }
            if ledger_ids:
                logger.info("[PKS][retrieve] %d entidade(s) de extrato fora do contexto", len(ledger_ids))
                neighborhoods = [n for n in neighborhoods if n.entity.id not in ledger_ids]
                candidates = [c for c in candidates if c.entity.id not in ledger_ids]
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
        # 3 letras, não 4: este caminho só roda quando o classificador não
        # devolveu keywords (foi o caso no 403 da sessão 369d812c), e é
        # justamente aí que "cep", "cpf" e "rg" — a palavra que carrega o
        # pedido inteiro — eram descartadas por serem curtas demais.
        words = re.findall(r"[\wÀ-ÿ]{3,}", user_message.lower())
        stop = {
            "para", "como", "qual", "quais", "quando", "onde", "porque", "isso",
            "essa", "esse", "minha", "meu", "meus", "minhas", "sobre", "voce",
            "você", "pode", "fazer", "quero",
            # ruído de 3 letras que o corte anterior escondia
            "que", "com", "por", "uma", "uns", "dos", "das", "nos", "nas",
            "num", "sem", "sim", "nao", "não", "tem", "ter", "foi", "era",
            "ele", "ela", "seu", "sua", "tua", "teu", "mim", "voc", "vai",
            "vem", "faz", "fez", "diz", "ver", "vou", "sou", "tao", "tão",
            "aos", "ate", "até", "lhe", "pra", "pro", "mas", "qua", "eai",
            "oi", "ola", "olá",
        }
        deduped: list[str] = []
        for word in words:
            if word not in stop and word not in deduped:
                deduped.append(word)
            if len(deduped) >= 6:
                break
        return deduped


_TOKEN_COUNTER = None

# workspace_id → (impressão digital, quando, entidades, fields, relações, índice).
# A impressão digital invalida na hora; o TTL é só rede de segurança.
_WS_CACHE: dict[UUID, tuple] = {}
_WS_CACHE_TTL_S = 600.0


def _count_tokens(text_in: str) -> int:
    """Mesmo contador (tiktoken) do PromptContextSelector — o orçamento do bloco
    é conferido por ele, então o ranking tem que contar igual."""
    global _TOKEN_COUNTER
    if _TOKEN_COUNTER is None:
        # Import tardio: core.agent importa o executor inteiro no __init__.
        from core.agent.token_counter import TokenCounter

        _TOKEN_COUNTER = TokenCounter()
    return _TOKEN_COUNTER.count(text_in)


def _dominant_signal(signals: dict[str, float]) -> str:
    direct = {k: v for k, v in signals.items() if k in ("nome", "lex", "conv", "grafo", "sem")}
    if not direct or max(direct.values()) <= 0:
        return "tipo"
    return max(direct, key=direct.get)


_HEADER = re.compile(r"^### (.+?) \[[\w_]+\]\s*$", re.M)


def _block_entities(block: str) -> list[str]:
    return _HEADER.findall(block or "")
