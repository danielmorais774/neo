"""Embedding service — generates text embeddings via OpenAI API."""

import logging
from typing import Any

from openai import AsyncOpenAI

from shared.config import get_settings

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Generates embeddings using OpenAI text-embedding models."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._client: AsyncOpenAI | None = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(api_key=self.settings.openai_api_key)
        return self._client

    @property
    def available(self) -> bool:
        return bool(self.settings.openai_api_key)

    async def embed(self, text: str) -> list[float]:
        """Generate embedding for a single text.

        Args:
            text: Input text (will be truncated to ~8000 chars).

        Returns:
            Embedding vector as list[float].

        Raises:
            RuntimeError: If API key not configured.
        """
        if not self.available:
            raise RuntimeError("OpenAI API key not configured for embeddings")

        # Truncate very long texts
        text = text[:8000]

        response = await self.client.embeddings.create(
            input=text,
            model=self.settings.embedding_model,
        )
        return response.data[0].embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts in one API call.

        Args:
            texts: List of input texts.

        Returns:
            List of embedding vectors.
        """
        if not self.available:
            raise RuntimeError("OpenAI API key not configured for embeddings")

        # Truncate
        truncated = [t[:8000] for t in texts]

        response = await self.client.embeddings.create(
            input=truncated,
            model=self.settings.embedding_model,
        )
        return [item.embedding for item in response.data]

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        import math

        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
