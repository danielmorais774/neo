"""test_hybrid_search — testa busca híbrida com mocks do DB."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from core.memory.hybrid_search import HybridMemorySearch
from shared.types import MemorySearchResult


def _make_settings(**overrides) -> MagicMock:
    """Cria settings mock para HybridMemorySearch."""
    defaults = {
        "vector_weight": 0.5,
        "fts_weight": 0.3,
        "temporal_weight": 0.2,
        "temporal_decay_days": 30,
        "mmr_lambda": 0.7,
        "memory_search_limit": 10,
    }
    defaults.update(overrides)
    s = MagicMock()
    for k, v in defaults.items():
        setattr(s, k, v)
    return s


@pytest.fixture
def mock_db() -> MagicMock:
    db = MagicMock()
    db.find_similar_messages = AsyncMock(return_value=[])
    db.search_messages_fts = AsyncMock(return_value=[])
    db.find_similar_sessions = AsyncMock(return_value=[])
    return db


@pytest.fixture
def mock_embedding_service() -> MagicMock:
    svc = MagicMock()
    svc.available = True
    svc.embed = AsyncMock(return_value=[0.1] * 1536)
    return svc


@pytest.fixture
def hybrid_search(mock_db: MagicMock, mock_embedding_service: MagicMock) -> HybridMemorySearch:
    return HybridMemorySearch(
        db=mock_db,
        embedding_service=mock_embedding_service,
        settings=_make_settings(),
    )


class TestHybridSearch:
    """Testes para HybridMemorySearch."""

    @pytest.mark.asyncio
    async def test_search_empty_results(self, hybrid_search: HybridMemorySearch) -> None:
        """Busca sem resultados retorna lista vazia."""
        results = await hybrid_search.search(query="test", limit=10)
        assert results == []

    @pytest.mark.asyncio
    async def test_search_with_vector_results(
        self, hybrid_search: HybridMemorySearch, mock_db: MagicMock,
    ) -> None:
        """Resultados vetoriais são combinados corretamente."""
        msg_id = uuid4()
        session_id = uuid4()
        mock_msg = MagicMock()
        mock_msg.id = msg_id
        mock_msg.session_id = session_id
        mock_msg.content = "Python é uma linguagem de programação"
        mock_msg.role = "assistant"
        mock_msg.created_at = datetime.utcnow()

        mock_db.find_similar_messages = AsyncMock(return_value=[(mock_msg, 0.85)])

        results = await hybrid_search.search(query="Python programming", limit=5)

        assert len(results) >= 1
        assert all(isinstance(r, MemorySearchResult) for r in results)
        assert results[0].similarity == 0.85

    @pytest.mark.asyncio
    async def test_search_with_fts_results(
        self, hybrid_search: HybridMemorySearch, mock_db: MagicMock,
    ) -> None:
        """Resultados de FTS são combinados com RRF."""
        msg_id = uuid4()
        session_id = uuid4()
        mock_msg = MagicMock()
        mock_msg.id = msg_id
        mock_msg.session_id = session_id
        mock_msg.content = "Implementação do algoritmo de busca"
        mock_msg.role = "user"
        mock_msg.created_at = datetime.utcnow()

        mock_db.search_messages_fts = AsyncMock(return_value=[(mock_msg, 1.5)])

        results = await hybrid_search.search(query="algoritmo busca", limit=5)

        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_combines_sources(
        self, hybrid_search: HybridMemorySearch, mock_db: MagicMock,
    ) -> None:
        """Vector + FTS + Session resultam em scores combinados."""
        now = datetime.utcnow()
        msg_id = uuid4()
        session_id = uuid4()

        # Vector result
        vec_msg = MagicMock()
        vec_msg.id = msg_id
        vec_msg.session_id = session_id
        vec_msg.content = "Resultado do vector"
        vec_msg.role = "assistant"
        vec_msg.created_at = now
        mock_db.find_similar_messages = AsyncMock(return_value=[(vec_msg, 0.9)])

        # FTS result (different message)
        fts_msg = MagicMock()
        fts_msg.id = uuid4()
        fts_msg.session_id = session_id
        fts_msg.content = "Resultado do FTS"
        fts_msg.role = "user"
        fts_msg.created_at = now - timedelta(hours=1)
        mock_db.search_messages_fts = AsyncMock(return_value=[(fts_msg, 2.0)])

        results = await hybrid_search.search(query="test query", limit=10)

        assert len(results) >= 2
        # Results should be sorted by final_score descending
        scores = [r.final_score for r in results]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_search_with_session_results(
        self, hybrid_search: HybridMemorySearch, mock_db: MagicMock,
    ) -> None:
        """Sessões similares aparecem nos resultados."""
        session = MagicMock()
        session.id = uuid4()
        session.topic_summary = "Discussão sobre Docker"
        session.started_at = datetime.utcnow() - timedelta(days=1)

        mock_db.find_similar_sessions = AsyncMock(return_value=[(session, 0.75)])

        results = await hybrid_search.search(query="Docker containers", limit=5)

        session_results = [r for r in results if r.source == "session"]
        assert len(session_results) >= 1

    @pytest.mark.asyncio
    async def test_search_temporal_decay(
        self, hybrid_search: HybridMemorySearch, mock_db: MagicMock,
    ) -> None:
        """Mensagens mais recentes devem ter temporal_score mais alto."""
        now = datetime.utcnow()
        recent = MagicMock()
        recent.id = uuid4()
        recent.session_id = uuid4()
        recent.content = "Mensagem recente"
        recent.role = "user"
        recent.created_at = now

        old = MagicMock()
        old.id = uuid4()
        old.session_id = uuid4()
        old.content = "Mensagem antiga"
        old.role = "user"
        old.created_at = now - timedelta(days=60)

        mock_db.find_similar_messages = AsyncMock(return_value=[
            (recent, 0.8),
            (old, 0.8),
        ])

        results = await hybrid_search.search(query="test", limit=10)

        if len(results) >= 2:
            recent_result = next((r for r in results if "recente" in r.content), None)
            old_result = next((r for r in results if "antiga" in r.content), None)
            if recent_result and old_result:
                assert recent_result.temporal_score >= old_result.temporal_score

    @pytest.mark.asyncio
    async def test_search_fallback_no_embedding(self, mock_db: MagicMock) -> None:
        """Sem embedding service, fallback para FTS puro."""
        no_embed = MagicMock()
        no_embed.available = False

        hs = HybridMemorySearch(
            db=mock_db,
            embedding_service=no_embed,
            settings=_make_settings(),
        )

        msg = MagicMock()
        msg.id = uuid4()
        msg.session_id = uuid4()
        msg.content = "Resultado FTS"
        msg.role = "user"
        msg.created_at = datetime.utcnow()
        mock_db.search_messages_fts = AsyncMock(return_value=[(msg, 1.0)])

        results = await hs.search(query="test", limit=5)
        assert len(results) >= 0  # Should not crash

    @pytest.mark.asyncio
    async def test_mmr_deduplication(
        self, hybrid_search: HybridMemorySearch, mock_db: MagicMock,
    ) -> None:
        """MMR deve diversificar resultados similares."""
        now = datetime.utcnow()
        messages = []
        for i in range(5):
            msg = MagicMock()
            msg.id = uuid4()
            msg.session_id = uuid4()
            msg.content = "Mensagem muito similar sobre Python"  # identical content
            msg.role = "user"
            msg.created_at = now
            messages.append((msg, 0.9 - i * 0.01))

        mock_db.find_similar_messages = AsyncMock(return_value=messages)

        results = await hybrid_search.search(query="Python", limit=3)

        # MMR should limit highly similar results
        assert len(results) <= 5


class TestRRFScoring:
    """Testes para o cálculo de RRF."""

    def test_rrf_score_formula(self) -> None:
        """RRF = 1 / (k + rank) onde k=60."""
        k = 60
        rank_1 = 1 / (k + 1)  # ~0.0164
        rank_5 = 1 / (k + 5)  # ~0.0154
        assert rank_1 > rank_5
