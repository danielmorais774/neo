"""Knowledge Retriever — busca contexto relevante no grafo de conhecimento."""

import json
import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

# Relações que ligam o perfil a nós globais (sempre incluídos)
GLOBAL_RELATION_TYPES = ("TEM_ESTADO", "TEM_RESTRICAO", "TEM_PERFIL", "TEM_ROTINA")

_KEYWORD_EXTRACTION_PROMPT = """\
You are a keyword extractor for a personal AI assistant.

Given a user's message, extract the main topics, activities, or subjects that are relevant \
to personal context (e.g. food, exercise, work, health, social events, routines).

Rules:
- Return only lowercase keywords in Portuguese
- Maximum 5 keywords
- Ignore filler words, articles, pronouns
- Focus on nouns and activities that could map to user context categories
- Always return the SINGULAR form of each word (e.g. "treinos" → "treino", "refeições" → "refeição")
- Return ONLY a JSON array of strings, nothing else

Example: "quero comer algo saudável antes do treino" → ["treino", "alimentação", "saúde"]
Example: "quais são meus treinos?" → ["treino"]
"""


def _stem_pt(word: str) -> str:
    """Stemming muito simples para português — retorna a raiz provável."""
    w = word.lower().strip()
    # Plural → singular
    if w.endswith("ões"):
        return w[:-3] + "ão"
    if w.endswith("ães"):
        return w[:-3] + "ão"
    if w.endswith("ais"):
        return w[:-2] + "al"
    if w.endswith("éis"):
        return w[:-3] + "el"
    if w.endswith("eis"):
        return w[:-2] + "el"
    if w.endswith("ns"):
        return w[:-2] + "m"
    if w.endswith("s") and len(w) > 3:
        return w[:-1]
    return w


def _expand_keywords(keywords: list[str]) -> list[str]:
    """Expande keywords com variantes (singular/plural/stem) para match mais flexível."""
    expanded: set[str] = set()
    for kw in keywords:
        kw = kw.lower().strip()
        expanded.add(kw)
        stem = _stem_pt(kw)
        if stem != kw:
            expanded.add(stem)
        # Se a word já é singular, adicionar plural simples
        if not kw.endswith("s"):
            expanded.add(kw + "s")
    return list(expanded)


class KnowledgeRetriever:
    """Busca entidades no grafo PostgreSQL com base em categorias do intent."""

    def __init__(self, session_factory: async_sessionmaker, llm_router: Any | None = None) -> None:
        self.session_factory = session_factory
        self._llm_router = llm_router

    async def _extract_keywords(self, user_input: str) -> list[str]:
        """Usa o LLM para extrair keywords relevantes do input do usuário."""
        if not self._llm_router:
            # Fallback: split simples sem LLM
            return [w.lower() for w in user_input.split() if len(w) > 3][:5]

        from shared.types import Message, MessageRole

        try:
            provider = self._llm_router.get_provider()
            response = await provider.complete(
                messages=[
                    Message(role=MessageRole.SYSTEM, content=_KEYWORD_EXTRACTION_PROMPT),
                    Message(role=MessageRole.USER, content=user_input),
                ],
                temperature=0.0,
                max_tokens=128,
            )
            content = response.get_content() or "[]"
            # Remove markdown code fences if present
            content = content.strip().strip("```json").strip("```").strip()
            keywords = json.loads(content)
            if isinstance(keywords, list):
                return [str(k).lower() for k in keywords]
        except Exception:
            logger.exception("Falha ao extrair keywords via LLM, usando fallback")

        return [w.lower() for w in user_input.split() if len(w) > 3][:5]

    async def retrieve(
        self,
        user_id: UUID,
        user_input: str,
        keywords: list[str] | None = None,
    ) -> dict[str, Any]:
        """Busca contextos relevantes no grafo para o usuário.

        Args:
            user_id: UUID do usuário.
            user_input: Mensagem bruta do usuário (usada apenas se keywords não fornecidas).
            keywords: Keywords pré-extraídas (e.g. do intent classifier). Se fornecidas,
                      evita uma chamada extra ao LLM.

        Returns:
            Dict com contexts_found + global + metadata.
        """
        if keywords:
            logger.debug("Keywords recebidas do intent para '%s': %s", user_input, keywords)
        else:
            keywords = await self._extract_keywords(user_input)
            logger.debug("Keywords extraídas via LLM para '%s': %s", user_input, keywords)

        async with self.session_factory() as session:
            contexts = await self._find_matching_contexts(session, user_id, keywords)
            context_trees = []
            for ctx in contexts:
                # If the context name itself matched keywords, return all children (no filtering)
                search_terms = _expand_keywords(keywords)
                ctx_name_matched = any(
                    kw in ctx["name"].lower() for kw in search_terms
                )
                tree = await self._expand_context(
                    session, user_id, ctx["id"],
                    keywords=None if ctx_name_matched else keywords,
                )
                context_trees.append({
                    "context": ctx,
                    "children": tree,
                })

            globals_ = await self._get_global_entities(session, user_id)

            return {
                "contexts_found": context_trees,
                "global": globals_,
                "metadata": {
                    "user_id": str(user_id),
                    "user_input": user_input,
                    "keywords_extracted": keywords,
                    "contexts_matched": len(context_trees),
                    "global_nodes": len(globals_),
                },
            }

    async def _find_matching_contexts(
        self,
        session: AsyncSession,
        user_id: UUID,
        keywords: list[str],
    ) -> list[dict[str, Any]]:
        """Busca nós do tipo 'contexto' cujo nome ou filhos batem com keywords."""
        if not keywords:
            return []

        # Expandir keywords com variantes (singular/plural/stem)
        search_terms = _expand_keywords(keywords)
        logger.debug("Search terms (expanded): %s", search_terms)

        # Busca contextos que tenham nome contendo alguma keyword
        # OU que contenham filhos com nome contendo alguma keyword
        like_clauses = " OR ".join(
            f"LOWER(e.name) LIKE :kw{i}" for i in range(len(search_terms))
        )
        child_like_clauses = " OR ".join(
            f"LOWER(child.name) LIKE :kw{i}" for i in range(len(search_terms))
        )

        query = text(f"""
            SELECT DISTINCT ctx.id, ctx.name, ctx.properties
            FROM knowledge_entity ctx
            WHERE ctx.user_id = :user_id
              AND ctx.type = 'contexto'
              AND (
                -- Match direto no nome do contexto
                ({like_clauses.replace('e.name', 'ctx.name')})
                OR
                -- Match em filhos diretos do contexto
                EXISTS (
                    SELECT 1
                    FROM knowledge_relation r
                    JOIN knowledge_entity child ON child.id = r.target_id
                    WHERE r.source_id = ctx.id
                      AND r.user_id = :user_id
                      AND ({child_like_clauses})
                )
                OR
                -- Match em netos (filhos de filhos)
                EXISTS (
                    SELECT 1
                    FROM knowledge_relation r1
                    JOIN knowledge_entity child ON child.id = r1.target_id
                    JOIN knowledge_relation r2 ON r2.source_id = child.id AND r2.user_id = :user_id
                    JOIN knowledge_entity grandchild ON grandchild.id = r2.target_id
                    WHERE r1.source_id = ctx.id
                      AND r1.user_id = :user_id
                      AND ({child_like_clauses.replace('child.name', 'grandchild.name')})
                )
              )
        """)

        params: dict[str, Any] = {"user_id": user_id}
        for i, kw in enumerate(search_terms):
            params[f"kw{i}"] = f"%{kw.lower()}%"

        result = await session.execute(query, params)
        rows = result.fetchall()

        return [{"id": row[0], "name": row[1], "properties": row[2]} for row in rows]

    async def _expand_context(
        self,
        session: AsyncSession,
        user_id: UUID,
        context_id: UUID,
        keywords: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Expande um contexto recursivamente (2 níveis máximo) via WITH RECURSIVE.

        Se keywords forem fornecidas, retorna apenas os nós (e seus pais diretos)
        que batem com as keywords. Fallback para todos os filhos se nenhum bater.
        """
        query = text("""
            WITH RECURSIVE tree AS (
                -- Nível 0: filhos diretos do contexto
                SELECT
                    e.id,
                    e.type,
                    e.name,
                    e.properties,
                    r.type AS relation_type,
                    1 AS depth
                FROM knowledge_relation r
                JOIN knowledge_entity e ON e.id = r.target_id
                WHERE r.source_id = :context_id
                  AND r.user_id = :user_id

                UNION ALL

                -- Níveis seguintes (até depth 2)
                SELECT
                    e.id,
                    e.type,
                    e.name,
                    e.properties,
                    r.type AS relation_type,
                    t.depth + 1 AS depth
                FROM tree t
                JOIN knowledge_relation r ON r.source_id = t.id AND r.user_id = :user_id
                JOIN knowledge_entity e ON e.id = r.target_id
                WHERE t.depth < 2
            )
            SELECT id, type, name, properties, relation_type, depth
            FROM tree
            ORDER BY depth, type, name
        """)

        result = await session.execute(query, {"context_id": context_id, "user_id": user_id})
        rows = result.fetchall()

        all_children = [
            {
                "id": str(row[0]),
                "type": row[1],
                "name": row[2],
                "properties": row[3],
                "relation": row[4],
                "depth": row[5],
            }
            for row in rows
        ]

        # Filtra apenas nós relevantes às keywords quando há match específico
        if keywords:
            search_terms = _expand_keywords(keywords)
            matched = [
                c for c in all_children
                if any(kw in c["name"].lower() or kw in str(c.get("properties", "")).lower()
                       for kw in search_terms)
            ]
            if matched:
                return matched

        return all_children

    async def _get_global_entities(
        self,
        session: AsyncSession,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        """Busca nós globais (saúde, perfil, restrições, rotina) + o próprio perfil."""
        # Primeiro busca o nó de perfil do usuário
        profile_query = text("""
            SELECT id, type, name, properties
            FROM knowledge_entity
            WHERE user_id = :user_id AND type = 'perfil'
            LIMIT 1
        """)
        profile_result = await session.execute(profile_query, {"user_id": user_id})
        profile_row = profile_result.fetchone()

        entities: list[dict[str, Any]] = []

        if profile_row:
            entities.append({
                "id": str(profile_row[0]),
                "type": profile_row[1],
                "name": profile_row[2],
                "properties": profile_row[3],
                "relation": "PERFIL",
            })

        # Busca os nós globais ligados ao perfil
        query = text("""
            SELECT e.id, e.type, e.name, e.properties, r.type AS relation_type
            FROM knowledge_entity profile
            JOIN knowledge_relation r ON r.source_id = profile.id AND r.user_id = :user_id
            JOIN knowledge_entity e ON e.id = r.target_id
            WHERE profile.user_id = :user_id
              AND profile.type = 'perfil'
              AND r.type = ANY(:global_types)
        """)

        result = await session.execute(
            query,
            {"user_id": user_id, "global_types": list(GLOBAL_RELATION_TYPES)},
        )
        rows = result.fetchall()

        entities.extend([
            {
                "id": str(row[0]),
                "type": row[1],
                "name": row[2],
                "properties": row[3],
                "relation": row[4],
            }
            for row in rows
        ])

        return entities
