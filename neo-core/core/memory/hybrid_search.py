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

# O RRF cru vale 1/(K+rank+1) — no melhor caso possível, rank 0, isso dá 1/61 =
# 0,0164. Somado com os pesos (0,5 vetor + 0,3 FTS + 0,2 temporal) o teto do
# `final_score` era 0,5·0,0164 + 0,3·0,0164 + 0,2·1,0 = **0,2131**, enquanto o
# `context_relevance_threshold` que decide o que entra no prompt vale 0,25. Ou
# seja: a camada RETRIEVED era inalcançável por construção — na sessão
# 7330e40c foram 295 candidatos e 295 descartes `below_threshold`, zero
# aprovados, sempre, para qualquer usuário. Pior: com o RRF esmagado em 0,016 e
# o temporal valendo 0,2 inteiros, 94% da nota era RECÊNCIA e a relevância
# virava ruído — o topo da lista era sempre a própria mensagem que o usuário
# acabou de mandar. Normalizar por (K+1) devolve rank 0 → 1,0 e faz o
# `final_score` voltar a ser um 0..1 de verdade, com os pesos significando o
# que dizem significar. Ver docs/context-recall-fixes.md §1.
_RRF_NORM = float(_RRF_K + 1)


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
        exclude_session_id: UUID | None = None,
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
            exclude_session_id: Tira a sessão ATUAL do resultado. Esta camada é
                memória ENTRE conversas — a conversa de agora já vai inteira no
                histórico. Sem o filtro, a mensagem que o usuário acabou de
                mandar casa consigo mesma, ganha rank 0 no vetor e no FTS e
                ainda leva o temporal cheio: ela era o resultado nº 1 em toda
                interação da sessão 7330e40c, gastando um slot para devolver ao
                modelo o que ele acabou de ler.

        Returns:
            Resultados ordenados por relevância combinada.
        """
        limit = limit or self._settings.memory_search_limit

        if not self._embeddings.available:
            logger.warning("Embeddings não disponíveis, usando FTS only")
            return await self._fts_only_search(
                query, user_id, service, limit, exclude_session_id,
            )

        try:
            query_embedding = await self._embeddings.embed(query)
        except Exception as e:
            logger.error(f"Falha ao gerar embedding: {e}")
            return await self._fts_only_search(
                query, user_id, service, limit, exclude_session_id,
            )

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

        # A sessão atual sai ANTES do RRF: se saísse depois, ela continuaria
        # ocupando o rank 0 e empurraria todo mundo um degrau para baixo.
        if exclude_session_id is not None:
            _drop = str(exclude_session_id)
            vector_results = [
                (m, sc) for m, sc in vector_results if str(m.session_id) != _drop
            ]
            fts_results = [
                (m, sc) for m, sc in fts_results if str(m.session_id) != _drop
            ]
            session_results = [
                (sess, sc) for sess, sc in session_results if str(sess.id) != _drop
            ]

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
        scored = self._apply_scoring(
            candidates, vector_results, fts_results, session_results,
        )

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
        session_results: list | None = None,
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

        # Sessões entram no dicionário com a chave `session-<id>`, mas os
        # rankings acima são indexados por id de MENSAGEM — então toda sessão
        # caía no rank default 999 e ficava só com o termo temporal. Mesmo com
        # o RRF normalizado, o teto de uma sessão seria ~0,246, abaixo do corte
        # de 0,25: consertar o `bytea <=> vector` no banco (migration
        # a0b1c2d3e4f5) fazia a busca VOLTAR a achar sessão, e ela continuaria
        # sem conseguir entrar no prompt. A busca por sessão é vetorial; o
        # ranking dela é a própria ordem que o banco devolveu.
        for rank, (sess, _) in enumerate(session_results or []):
            vector_ranking[f"session-{sess.id}"] = rank

        for key, result in candidates.items():
            # RRF scores
            vec_rank = vector_ranking.get(key, 999)
            fts_rank = fts_ranking.get(key, 999)
            rrf_vec = _RRF_NORM / (_RRF_K + vec_rank + 1)
            rrf_fts = _RRF_NORM / (_RRF_K + fts_rank + 1)

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
        exclude_session_id: UUID | None = None,
    ) -> list[MemorySearchResult]:
        """Busca apenas FTS quando embeddings não disponíveis."""
        try:
            fts_results = await self._db.search_messages_fts(
                query, user_id, service, limit,
            )
            if exclude_session_id is not None:
                _drop = str(exclude_session_id)
                fts_results = [
                    (m, sc) for m, sc in fts_results if str(m.session_id) != _drop
                ]
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
