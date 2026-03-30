"""test_context_compactor — testa compactação de contexto."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.agent.context_compactor import ContextCompactor, _extract_identifiers
from core.agent.token_counter import TokenCounter
from shared.types import Message


@pytest.fixture
def token_counter() -> TokenCounter:
    return TokenCounter()


@pytest.fixture
def mock_llm_router() -> MagicMock:
    router = MagicMock()
    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=MagicMock(get_content=lambda: "Resumo: o usuário pediu ajuda com Python.")
    )
    router.get_provider.return_value = provider
    router.get_default_model.return_value = "gpt-4"
    return router


@pytest.fixture
def compactor(mock_llm_router: MagicMock, token_counter: TokenCounter) -> ContextCompactor:
    return ContextCompactor(llm_router=mock_llm_router, token_counter=token_counter)


class TestContextCompactor:
    """Testes para ContextCompactor."""

    def test_should_compact_below_threshold(
        self, compactor: ContextCompactor,
    ) -> None:
        """Mensagens curtas não precisam de compactação."""
        msgs = [Message(role="user", content="Hello")]
        assert compactor.should_compact(msgs, max_tokens=10000, threshold=0.75) is False

    def test_should_compact_above_threshold(
        self, compactor: ContextCompactor,
    ) -> None:
        """Muitas mensagens longas devem disparar compactação."""
        msgs = [
            Message(role="user", content="A" * 2000)
            for _ in range(50)
        ]
        assert compactor.should_compact(msgs, max_tokens=1000, threshold=0.5) is True

    @pytest.mark.asyncio
    async def test_compact_returns_result(
        self, compactor: ContextCompactor,
    ) -> None:
        """compact() retorna CompactionResult com resumo."""
        msgs = [
            Message(role="user", content=f"Mensagem número {i} sobre Python") for i in range(15)
        ]

        result = await compactor.compact(msgs, target_tokens=5000)

        assert result.summary is not None
        assert len(result.summary) > 0
        assert result.original_token_count > 0
        assert result.compacted_token_count >= 0

    @pytest.mark.asyncio
    async def test_compact_preserves_identifiers(
        self, compactor: ContextCompactor,
    ) -> None:
        """Identificadores como UUIDs e URLs devem ser preservados."""
        msgs = [
            Message(role="user", content="Veja o arquivo /home/user/test.py"),
            Message(role="assistant", content="ID: 550e8400-e29b-41d4-a716-446655440000"),
            Message(role="user", content="Acesse https://example.com/api/v1"),
        ]

        result = await compactor.compact(msgs, target_tokens=5000)
        assert result.preserved_identifiers is not None
        assert len(result.preserved_identifiers) > 0

    @pytest.mark.asyncio
    async def test_compact_fallback_on_llm_failure(self, token_counter: TokenCounter) -> None:
        """Se LLM falhar, fallback para truncamento simples."""
        failing_router = MagicMock()
        failing_provider = MagicMock()
        failing_provider.complete = AsyncMock(side_effect=Exception("LLM offline"))
        failing_router.get_provider.return_value = failing_provider
        failing_router.get_default_model.return_value = "gpt-4"

        compactor = ContextCompactor(llm_router=failing_router, token_counter=token_counter)
        msgs = [
            Message(role="user", content=f"Mensagem {i}") for i in range(10)
        ]

        result = await compactor.compact(msgs, target_tokens=5000)
        assert result.summary is not None
        assert len(result.summary) > 0


class TestIdentifierExtraction:
    """Testes para extração de identificadores (função de módulo)."""

    def test_extract_uuids(self) -> None:
        text = "ID do recurso: 550e8400-e29b-41d4-a716-446655440000"
        ids = _extract_identifiers(text)
        assert "550e8400-e29b-41d4-a716-446655440000" in ids

    def test_extract_urls(self) -> None:
        text = "Veja https://api.example.com/v1/users"
        ids = _extract_identifiers(text)
        assert any("example.com" in i for i in ids)

    def test_extract_paths(self) -> None:
        text = "Arquivo em /home/user/project/main.py"
        ids = _extract_identifiers(text)
        assert any("main.py" in i for i in ids)
