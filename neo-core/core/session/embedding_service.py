"""Embedding service — gera embeddings via modelo local (sentence-transformers) ou OpenAI API.

Provider padrão: local (all-MiniLM-L6-v2 via sentence-transformers).
Para usar a API OpenAI, configure EMBEDDING_PROVIDER=openai no .env.
"""

import asyncio
import hashlib
import logging
from typing import Any

from shared.config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache em memória (evita recalcular textos iguais, max 512 entradas)
# ---------------------------------------------------------------------------
_CACHE_MAX = 512
_embedding_cache: dict[str, list[float]] = {}
_embedding_cache_keys: list[str] = []  # fila FIFO para limitar tamanho


def _cache_key(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


def _cache_get(text: str) -> list[float] | None:
    return _embedding_cache.get(_cache_key(text))


def _cache_set(text: str, embedding: list[float]) -> None:
    key = _cache_key(text)
    if key not in _embedding_cache:
        if len(_embedding_cache_keys) >= _CACHE_MAX:
            oldest = _embedding_cache_keys.pop(0)
            _embedding_cache.pop(oldest, None)
        _embedding_cache_keys.append(key)
    _embedding_cache[key] = embedding


# ---------------------------------------------------------------------------
# EmbeddingService
# ---------------------------------------------------------------------------

class EmbeddingService:
    """Gera embeddings usando modelo local (padrão) ou OpenAI API (alternativa).

    Provider selecionado via EMBEDDING_PROVIDER no .env:
      - "local"  → all-MiniLM-L6-v2 via sentence-transformers (padrão)
      - "openai" → OpenAI text-embedding-3-small via API remota
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._local_model: Any | None = None
        # OpenAI client — mantido para uso futuro se EMBEDDING_PROVIDER=openai
        self._openai_client: Any | None = None

    # ------------------------------------------------------------------
    # Modelo local
    # ------------------------------------------------------------------

    def _get_local_model(self) -> Any:
        """Carrega o modelo sentence-transformers na primeira chamada (lazy load)."""
        if self._local_model is None:
            try:
                from sentence_transformers import SentenceTransformer
                model_name = getattr(self.settings, "embedding_local_model", "all-MiniLM-L6-v2")
                logger.info(f"[EMBEDDING] Carregando modelo local: {model_name}")
                self._local_model = SentenceTransformer(model_name)
                logger.info(f"[EMBEDDING] Modelo local pronto: {model_name}")
            except ImportError:
                raise RuntimeError(
                    "sentence-transformers não instalado. "
                    "Execute: pip install sentence-transformers"
                )
        return self._local_model

    def _embed_local_sync(self, texts: list[str]) -> list[list[float]]:
        """Executa embedding local de forma síncrona (chamado via executor)."""
        model = self._get_local_model()
        vectors = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return [v.tolist() for v in vectors]

    async def _embed_local(self, texts: list[str]) -> list[list[float]]:
        """Gera embeddings locais sem bloquear o event loop."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._embed_local_sync, texts)

    # ------------------------------------------------------------------
    # OpenAI API (mantido para uso futuro — não é o provider padrão)
    # ------------------------------------------------------------------

    def _get_openai_client(self) -> Any:
        if self._openai_client is None:
            from openai import AsyncOpenAI
            self._openai_client = AsyncOpenAI(api_key=self.settings.openai_api_key)
        return self._openai_client

    async def _embed_openai(self, texts: list[str]) -> list[list[float]]:
        """Gera embeddings via OpenAI API. Ativar com EMBEDDING_PROVIDER=openai."""
        if not self.settings.openai_api_key:
            raise RuntimeError("OpenAI API key não configurada")
        truncated = [t[:8000] for t in texts]
        client = self._get_openai_client()
        response = await client.embeddings.create(
            input=truncated,
            model=self.settings.embedding_model,
        )
        return [item.embedding for item in response.data]

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        provider = getattr(self.settings, "embedding_provider", "local")
        if provider == "openai":
            return bool(self.settings.openai_api_key)
        return True  # local sempre disponível se sentence-transformers instalado

    async def embed(self, text: str) -> list[float]:
        """Gera embedding para um único texto com cache em memória."""
        text = text[:8000]
        cached = _cache_get(text)
        if cached is not None:
            return cached
        result = (await self._embed_batch_internal([text]))[0]
        _cache_set(text, result)
        return result

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Gera embeddings para múltiplos textos, usando cache quando possível."""
        truncated = [t[:8000] for t in texts]
        results: list[list[float] | None] = [None] * len(truncated)
        uncached_indices: list[int] = []
        uncached_texts: list[str] = []

        for i, t in enumerate(truncated):
            cached = _cache_get(t)
            if cached is not None:
                results[i] = cached
            else:
                uncached_indices.append(i)
                uncached_texts.append(t)

        if uncached_texts:
            new_vectors = await self._embed_batch_internal(uncached_texts)
            for idx, vec in zip(uncached_indices, new_vectors):
                _cache_set(truncated[idx], vec)
                results[idx] = vec

        return results  # type: ignore[return-value]

    async def _embed_batch_internal(self, texts: list[str]) -> list[list[float]]:
        """Roteia para o provider configurado."""
        provider = getattr(self.settings, "embedding_provider", "local")
        if provider == "openai":
            return await self._embed_openai(texts)
        return await self._embed_local(texts)

    # ------------------------------------------------------------------
    # Utilitários
    # ------------------------------------------------------------------

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """Calcula similaridade de cosseno entre dois vetores."""
        import math

        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
