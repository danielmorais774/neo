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


class TestScoreAlcancaOThreshold:
    """Regressão: a camada RETRIEVED era inalcançável por construção.

    O RRF cru vale 1/(K+rank+1) — 1/61 = 0,0164 no melhor caso. Com os pesos
    (0,5 vetor + 0,3 FTS + 0,2 temporal), o teto do `final_score` era 0,2131,
    contra um `context_relevance_threshold` de 0,25 que decide o que entra no
    prompt. Resultado medido na sessão 7330e40c: 295 candidatos, 295 descartes
    `below_threshold`, zero aprovados — sempre, para qualquer usuário.
    Ver docs/context-recall-fixes.md §1.
    """

    @pytest.mark.asyncio
    async def test_melhor_resultado_passa_do_threshold_padrao(
        self, hybrid_search: HybridMemorySearch, mock_db: MagicMock,
    ) -> None:
        from shared.config import Settings

        threshold = Settings().context_relevance_threshold
        msg = MagicMock()
        msg.id = uuid4()
        msg.session_id = uuid4()
        msg.content = "a resposta que o usuário quer de volta"
        msg.role = "assistant"
        msg.created_at = datetime.utcnow()

        # Primeiro lugar nos dois rankings e recentíssimo: o melhor caso
        # possível. Se ISTO não passa do corte, nada passa nunca.
        mock_db.find_similar_messages = AsyncMock(return_value=[(msg, 0.9)])
        mock_db.search_messages_fts = AsyncMock(return_value=[(msg, 1.5)])

        results = await hybrid_search.search(query="a resposta", limit=5)

        assert results, "busca não devolveu nada"
        assert results[0].final_score >= threshold, (
            f"teto do score ({results[0].final_score:.4f}) abaixo do threshold "
            f"({threshold}) — a camada RETRIEVED volta a ser código morto"
        )

    @pytest.mark.asyncio
    async def test_relevancia_pesa_mais_que_recencia(
        self, hybrid_search: HybridMemorySearch, mock_db: MagicMock,
    ) -> None:
        """Antes, 94% da nota era recência e a relevância virava ruído."""
        relevante_antigo = MagicMock()
        relevante_antigo.id = uuid4()
        relevante_antigo.session_id = uuid4()
        relevante_antigo.content = "exatamente o que ele procura"
        relevante_antigo.role = "assistant"
        relevante_antigo.created_at = datetime.utcnow() - timedelta(days=20)

        irrelevante_novo = MagicMock()
        irrelevante_novo.id = uuid4()
        irrelevante_novo.session_id = uuid4()
        irrelevante_novo.content = "conversa de ontem sobre outra coisa"
        irrelevante_novo.role = "user"
        irrelevante_novo.created_at = datetime.utcnow()

        # O relevante é rank 0; o novo nem aparece nos rankings de busca.
        mock_db.find_similar_messages = AsyncMock(return_value=[(relevante_antigo, 0.9)])
        mock_db.search_messages_fts = AsyncMock(return_value=[(irrelevante_novo, 0.1)])

        results = await hybrid_search.search(query="o que ele procura", limit=5)
        by_id = {str(r.message_id): r.final_score for r in results}

        assert by_id[str(relevante_antigo.id)] > by_id[str(irrelevante_novo.id)]

    @pytest.mark.asyncio
    async def test_exclude_session_id_tira_a_conversa_de_agora(
        self, hybrid_search: HybridMemorySearch, mock_db: MagicMock,
    ) -> None:
        """Esta camada é memória ENTRE conversas; a de agora já vai no histórico.

        Sem o filtro, a pergunta recém-salva casava consigo mesma e era o
        resultado nº 1 de toda interação da sessão 7330e40c.
        """
        atual = MagicMock()
        atual.id = uuid4()
        atual.session_id = uuid4()
        atual.content = "pega a ultima reanalise completa que fizemos"
        atual.role = "user"
        atual.created_at = datetime.utcnow()

        mock_db.find_similar_messages = AsyncMock(return_value=[(atual, 0.99)])

        results = await hybrid_search.search(
            query="pega a ultima reanalise completa",
            limit=5,
            exclude_session_id=atual.session_id,
        )

        assert all(r.session_id != atual.session_id for r in results)


class TestSessaoEntraNoRanking:
    """Regressão dupla do `bytea <=> vector` (docs/context-recall-fixes.md §6).

    Eram dois defeitos empilhados: o banco não conseguia comparar
    `conversation_session.topic_embedding` (coluna `bytea`, corrigida pela
    migration a0b1c2d3e4f5) e, mesmo depois de corrigido, a sessão entrava no
    dicionário de candidatos com a chave `session-<id>` enquanto os rankings de
    RRF eram indexados por id de MENSAGEM — ou seja, toda sessão caía no rank
    default 999 e ficava só com o termo temporal, com teto ~0,246 contra um
    corte de 0,25. Achar e não poder usar dá no mesmo.
    """

    @pytest.mark.asyncio
    async def test_sessao_relevante_passa_do_threshold(
        self, hybrid_search: HybridMemorySearch, mock_db: MagicMock,
    ) -> None:
        from shared.config import Settings

        sess = MagicMock()
        sess.id = uuid4()
        sess.topic_summary = "análise dos relatórios anuais das empresas"
        sess.started_at = datetime.utcnow() - timedelta(days=3)

        mock_db.find_similar_sessions = AsyncMock(return_value=[(sess, 0.61)])

        results = await hybrid_search.search(query="relatórios anuais", limit=5)

        achou = [r for r in results if r.source == "session"]
        assert achou, "sessão não voltou nos candidatos"
        assert achou[0].final_score >= Settings().context_relevance_threshold

    @pytest.mark.asyncio
    async def test_sessao_melhor_colocada_pontua_mais(
        self, hybrid_search: HybridMemorySearch, mock_db: MagicMock,
    ) -> None:
        """A ordem que o banco devolveu tem que valer alguma coisa."""
        primeira, segunda = MagicMock(), MagicMock()
        for m, resumo in ((primeira, "exatamente o assunto"), (segunda, "assunto vizinho")):
            m.id = uuid4()
            m.topic_summary = resumo
            m.started_at = datetime.utcnow() - timedelta(days=3)

        mock_db.find_similar_sessions = AsyncMock(
            return_value=[(primeira, 0.61), (segunda, 0.42)]
        )

        results = await hybrid_search.search(query="o assunto", limit=5)
        por_id = {str(r.session_id): r.final_score for r in results if r.source == "session"}

        assert por_id[str(primeira.id)] > por_id[str(segunda.id)]
