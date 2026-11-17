"""test_token_counter — testa contagem de tokens via tiktoken."""

import pytest
from core.agent.token_counter import TokenCounter
from shared.types import Message


@pytest.fixture
def counter() -> TokenCounter:
    return TokenCounter()


class TestTokenCounter:
    """Testes para TokenCounter."""

    def test_count_empty(self, counter: TokenCounter) -> None:
        assert counter.count("") == 0

    def test_count_simple_text(self, counter: TokenCounter) -> None:
        tokens = counter.count("Hello, world!")
        assert tokens > 0
        assert isinstance(tokens, int)

    def test_count_portuguese_text(self, counter: TokenCounter) -> None:
        tokens = counter.count("Olá, como você está? Tudo bem com a implementação.")
        assert tokens > 5

    def test_count_messages_empty(self, counter: TokenCounter) -> None:
        # Empty list still has REPLY_OVERHEAD (3 tokens)
        count = counter.count_messages([])
        assert count == 3  # reply priming overhead

    def test_count_messages_single(self, counter: TokenCounter) -> None:
        msgs = [Message(role="user", content="Hello")]
        count = counter.count_messages(msgs)
        # content tokens + MSG_OVERHEAD (4) + REPLY_OVERHEAD (3)
        assert count >= 7

    def test_count_messages_multiple(self, counter: TokenCounter) -> None:
        msgs = [
            Message(role="user", content="Hello"),
            Message(role="assistant", content="Hi there!"),
            Message(role="user", content="How are you?"),
        ]
        count = counter.count_messages(msgs)
        single_count = counter.count_messages(msgs[:1])
        assert count > single_count

    def test_estimate_remaining(self, counter: TokenCounter) -> None:
        msgs = [
            Message(role="user", content="Hello"),
            Message(role="assistant", content="Hi"),
        ]
        remaining = counter.estimate_remaining(msgs, max_tokens=1000)
        assert remaining > 0
        assert remaining < 1000

    def test_estimate_remaining_tight_budget(self, counter: TokenCounter) -> None:
        msgs = [
            Message(role="user", content="A" * 5000),
        ]
        remaining = counter.estimate_remaining(msgs, max_tokens=10)
        assert remaining < 10  # should be negative or very small

    def test_different_models(self) -> None:
        c1 = TokenCounter(model="gpt-4")
        c2 = TokenCounter(model="gpt-3.5-turbo")
        text = "Hello, this is a test."
        # Both should produce valid token counts
        assert c1.count(text) > 0
        assert c2.count(text) > 0
