"""Hybrid memory search — busca combinando vetor + FTS + decaimento temporal.

Implementa Reciprocal Rank Fusion (RRF) para combinar rankings de
diferentes fontes, MMR para diversificar resultados, e decaimento
temporal exponencial para priorizar informações recentes.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from shared.types import MemorySearchResult

if TYPE_CHECKING:
    from core.session.embedding_service import EmbeddingService
    from db.async_client import AsyncPostgresClient
    from shared.config import Settings

logger = logging.getLogger(__name__)

# Constante RRF — valor padrão da literatura (Cormack et al., 2009)
_RRF_K = 60


class HybridMemorySearch:
    """Busca híbrida na memória: vetor + FTS + temporal + MMR.

    Pipeline:
      1. Gera embedding do query
      2. Executa vetor search e FTS search em paralelo
      3. Combina via Reciprocal Rank Fusion (RRF) com pesos
      4. Aplica decaimento temporal exponencial
      5. Re-ranking via MMR para diversidade
    """

    def __init__(
        self,
        db: AsyncPostgresClient,
        embedding_service: EmbeddingService,
        settings: Settings,
    ) -> None:
        self._db = db
        self._embeddings = embedding_service
        self._settings = settings

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        user_id: UUID | None = None,
        service: str | None = None,
        limit: int | None = None,
    ) -> list[MemorySearchResult]:
        """Busca híbrida completa.

        Args:
            query: Texto de busca.
            user_id: Filtrar por usuário.
            service: Filtrar por canal. ``None`` (default) = TODOS os canais do
                usuário. O default era ``"neo-desktop"`` enquanto o app grava
                as sessões como ``"neo-desktop-tauri"`` — a busca enxergava 53
                de 3010 mensagens (1,8% do corpus) e o resto da memória entre
                sessões era invisível. Ver docs/conversation-recall-plan.md §2a.
            limit: Número de resultados (default: settings.memory_search_limit).

        Returns:
            Resultados ordenados por relevância combinada.
        """
        limit = limit or self._settings.memory_search_limit

        if not self._embeddings.available:
            logger.warning("Embeddings não disponíveis, usando FTS only")
            return await self._fts_only_search(query, user_id, service, limit)

        try:
            query_embedding = await self._embeddings.embed(query)
        except Exception as e:
            logger.error(f"Falha ao gerar embedding: {e}")
            return await self._fts_only_search(query, user_id, service, limit)

        # Executar buscas em paralelo
        vector_task = self._db.find_similar_messages(
            query_embedding, user_id, service, limit=limit * 2,
        )
        fts_task = self._db.search_messages_fts(
            query, user_id, service, limit=limit * 2,
        )
        session_task = self._db.find_similar_sessions(
            query_embedding, user_id, service, limit=5,
        )

        vector_results, fts_results, session_results = await asyncio.gather(
            vector_task, fts_task, session_task, return_exceptions=True,
        )

        # Tratar exceções individuais
        if isinstance(vector_results, BaseException):
            logger.warning(f"Vector search falhou: {vector_results}")
            vector_results = []
        if isinstance(fts_results, BaseException):
            logger.warning(f"FTS search falhou: {fts_results}")
            fts_results = []
        if isinstance(session_results, BaseException):
            logger.warning(f"Session search falhou: {session_results}")
            session_results = []

        # Converter para MemorySearchResult
        candidates: dict[str, MemorySearchResult] = {}

        for msg, sim in vector_results:
            key = str(msg.id)
            candidates[key] = MemorySearchResult(
                content=msg.content,
                source="message",
                similarity=sim,
                session_id=msg.session_id,
                message_id=msg.id,
                created_at=msg.created_at,
            )

        for msg, fts_score in fts_results:
            key = str(msg.id)
            if key in candidates:
                candidates[key].fts_score = fts_score
            else:
                candidates[key] = MemorySearchResult(
                    content=msg.content,
                    source="message",
                    fts_score=fts_score,
                    session_id=msg.session_id,
                    message_id=msg.id,
                    created_at=msg.created_at,
                )

        for sess, sim in session_results:
            key = f"session-{sess.id}"
            candidates[key] = MemorySearchResult(
                content=sess.topic_summary or "",
                source="session",
                similarity=sim,
                session_id=sess.id,
                created_at=sess.started_at,
            )

        if not candidates:
            return []

        # Aplicar RRF + temporal scoring
        scored = self._apply_scoring(candidates, vector_results, fts_results)

        # MMR re-ranking para diversidade
        if len(scored) > 1:
            scored = self._mmr_rerank(scored, limit)
        else:
            scored = scored[:limit]

        return scored

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _apply_scoring(
        self,
        candidates: dict[str, MemorySearchResult],
        vector_results: list,
        fts_results: list,
    ) -> list[MemorySearchResult]:
        """Aplica RRF + decaimento temporal em todos os candidatos."""
        w_vec = self._settings.vector_weight
        w_fts = self._settings.fts_weight
        w_time = self._settings.temporal_weight
        decay_days = self._settings.temporal_decay_days
        now = datetime.utcnow()

        # Construir rankings para RRF
        vector_ranking: dict[str, int] = {}
        for rank, (msg, _) in enumerate(vector_results):
            vector_ranking[str(msg.id)] = rank

        fts_ranking: dict[str, int] = {}
        for rank, (msg, _) in enumerate(fts_results):
            fts_ranking[str(msg.id)] = rank

        for key, result in candidates.items():
            # RRF scores
            vec_rank = vector_ranking.get(key, 999)
            fts_rank = fts_ranking.get(key, 999)
            rrf_vec = 1.0 / (_RRF_K + vec_rank + 1)
            rrf_fts = 1.0 / (_RRF_K + fts_rank + 1)

            # Decaimento temporal
            if result.created_at:
                days_ago = max(0.0, (now - result.created_at).total_seconds() / 86400)
                decay_rate = math.log(2) / max(decay_days, 1)
                temporal = math.exp(-decay_rate * days_ago)
            else:
                temporal = 0.5  # default para dados sem timestamp

            result.temporal_score = temporal
            result.final_score = w_vec * rrf_vec + w_fts * rrf_fts + w_time * temporal

        return list(candidates.values())

    def _mmr_rerank(
        self,
        results: list[MemorySearchResult],
        limit: int,
    ) -> list[MemorySearchResult]:
        """Maximal Marginal Relevance — balanceia relevância e diversidade.

        λ * relevance - (1 - λ) * max_redundancy
        """
        lam = self._settings.mmr_lambda

        if not results:
            return []

        # Ordenar por final_score como base
        results.sort(key=lambda r: r.final_score, reverse=True)

        selected: list[MemorySearchResult] = [results[0]]
        remaining = results[1:]

        while remaining and len(selected) < limit:
            best_score = -float("inf")
            best_idx = 0

            for i, candidate in enumerate(remaining):
                relevance = candidate.final_score

                # Similaridade com documentos já selecionados
                max_redundancy = max(
                    self._content_similarity(candidate.content, sel.content)
                    for sel in selected
                )

                mmr_score = lam * relevance - (1 - lam) * max_redundancy

                if mmr_score > best_score:
                    best_score = mmr_score
                    best_idx = i

            selected.append(remaining.pop(best_idx))

        return selected

    # ------------------------------------------------------------------
    # Fallback
    # ------------------------------------------------------------------

    async def _fts_only_search(
        self,
        query: str,
        user_id: UUID | None,
        service: str | None,
        limit: int,
    ) -> list[MemorySearchResult]:
        """Busca apenas FTS quando embeddings não disponíveis."""
        try:
            fts_results = await self._db.search_messages_fts(
                query, user_id, service, limit,
            )
            return [
                MemorySearchResult(
                    content=msg.content,
                    source="message",
                    fts_score=score,
                    final_score=score,
                    session_id=msg.session_id,
                    message_id=msg.id,
                    created_at=msg.created_at,
                )
                for msg, score in fts_results
            ]
        except Exception as e:
            logger.error(f"FTS-only search failed: {e}")
            return []

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------

    @staticmethod
    def _content_similarity(a: str, b: str) -> float:
        """Similaridade rápida entre conteúdos via Jaccard de palavras."""
        words_a = set(a.lower().split())
        words_b = set(b.lower().split())
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        return len(intersection) / len(union)
