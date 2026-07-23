"""WebSearchExecutor — cadeia de backends + cache RAM + fetch/extração.

Padrão do ``engine_monitor`` do neo-voice aplicado à busca: falha/timeout do
primário → tenta o próximo NO MESMO request e marca o backend degradado por
5 min (requests seguintes pulam direto, sem pagar o timeout de novo). Cache em
RAM (dict + TTL, LRU simples, sem dependência). Ver
docs/web-search-deep-research-plan.md §3.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from modules.web_search.backends import (
    VALID_CATEGORY,
    VALID_RECENCY,
    DdgsBackend,
    SearchBackend,
    SearchResult,
    SearxngBackend,
)
from modules.web_search.fetch import FetchedPage, FetchError, fetch_readable

logger = logging.getLogger(__name__)

_DEGRADED_TTL = 300.0  # 5 min sem tentar um backend que acabou de falhar
_MAX_CACHE_ENTRIES = 200
_MAX_RESULTS_CAP = 10


class _TTLCache:
    """Cache dict + TTL com LRU simples (move_to_end via re-insert)."""

    def __init__(self, max_entries: int = _MAX_CACHE_ENTRIES) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._max = max_entries

    def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, value = item
        if time.monotonic() > expires_at:
            self._data.pop(key, None)
            return None
        # LRU: re-insere para virar o mais recente.
        self._data.pop(key, None)
        self._data[key] = (expires_at, value)
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        if len(self._data) >= self._max:
            # Descarta o mais antigo (primeiro na ordem de inserção).
            oldest = next(iter(self._data), None)
            if oldest is not None:
                self._data.pop(oldest, None)
        self._data[key] = (time.monotonic() + ttl, value)


class WebSearchExecutor:
    """Executa ``web_search``/``web_fetch`` (module executor server-side)."""

    def __init__(
        self,
        *,
        searxng_url: str = "http://searxng:8080",
        backends_order: str = "searxng,ddgs",
        cache_ttl: int = 600,
        fetch_max_chars: int = 8000,
    ) -> None:
        available: dict[str, SearchBackend] = {
            "searxng": SearxngBackend(searxng_url),
            "ddgs": DdgsBackend(),
        }
        self.backends: list[SearchBackend] = [
            available[name.strip()]
            for name in backends_order.split(",")
            if name.strip() in available
        ] or [available["searxng"], available["ddgs"]]
        self.cache_ttl = cache_ttl
        self.fetch_max_chars = fetch_max_chars
        self._degraded_until: dict[str, float] = {}
        self._search_cache = _TTLCache()
        self._fetch_cache = _TTLCache()

    # ------------------------------------------------------------------
    # Interface de module executor (como WeatherExecutor)
    # ------------------------------------------------------------------

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "web_search":
            return await self._web_search(arguments)
        if tool_name == "web_fetch":
            return await self._web_fetch(arguments)
        raise ValueError(f"Unknown web_search tool: {tool_name}")

    # ------------------------------------------------------------------
    # web_search
    # ------------------------------------------------------------------

    async def search(
        self, query: str, *, recency: str = "any", category: str = "general", n: int = 5
    ) -> dict[str, Any]:
        """Busca com cadeia de backends + cache. Também usada pelo ResearchAgent."""
        query = (query or "").strip()
        if not query:
            return {"error": "empty_query", "message": "Query vazia."}
        if recency not in VALID_RECENCY:
            recency = "any"
        if category not in VALID_CATEGORY:
            category = "general"
        n = max(1, min(int(n or 5), _MAX_RESULTS_CAP))

        cache_key = hashlib.sha1(
            f"{query.lower()}|{recency}|{category}|{n}".encode()
        ).hexdigest()
        cached = self._search_cache.get(cache_key)
        if cached is not None:
            return cached

        now = time.monotonic()
        errors: list[str] = []
        for backend in self.backends:
            if self._degraded_until.get(backend.name, 0.0) > now:
                continue
            try:
                results = await backend.search(
                    query, recency=recency, category=category, n=n
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[web_search] backend '%s' falhou (%s) — degradado por %ds; tentando o próximo",
                    backend.name, e, int(_DEGRADED_TTL),
                )
                self._degraded_until[backend.name] = now + _DEGRADED_TTL
                errors.append(f"{backend.name}: {e}")
                continue
            payload = {
                "results": [r.to_dict() for r in results],
                "backend": backend.name,
                "searched_at": datetime.now(timezone.utc).isoformat(),
            }
            if not results:
                payload["note"] = "Nenhum resultado. Tente reformular a query."
            ttl = float(self.cache_ttl)
            if category == "news" or recency == "day":
                ttl = ttl / 2
            self._search_cache.set(cache_key, payload, ttl)
            return payload

        return {
            "error": "all_backends_failed",
            "message": (
                "Nenhum backend de busca respondeu agora. NÃO invente o fato: diga "
                "ao usuário que não conseguiu verificar na web."
            ),
            "details": errors,
        }

    async def _web_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.search(
            str(arguments.get("query") or ""),
            recency=str(arguments.get("recency") or "any"),
            category=str(arguments.get("category") or "general"),
            n=int(arguments.get("max_results") or 5),
        )

    # ------------------------------------------------------------------
    # web_fetch
    # ------------------------------------------------------------------

    async def fetch(self, url: str, *, max_chars: int | None = None) -> FetchedPage | FetchError:
        """Fetch com cache por URL. Também usada pelo ResearchAgent."""
        url = (url or "").strip()
        if not url:
            return FetchError("fetch_failed", "URL vazia.")
        limit = int(max_chars or self.fetch_max_chars)
        cache_key = hashlib.sha1(f"{url}|{limit}".encode()).hexdigest()
        cached = self._fetch_cache.get(cache_key)
        if cached is not None:
            return cached
        result = await fetch_readable(url, max_chars=limit)
        # Só cacheia sucesso e js_required (erros transitórios merecem retry).
        if isinstance(result, FetchedPage) or result.error == "js_required":
            self._fetch_cache.set(cache_key, result, float(self.cache_ttl))
        return result

    async def _web_fetch(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.fetch(
            str(arguments.get("url") or ""),
            max_chars=arguments.get("max_chars"),
        )
        if isinstance(result, FetchError):
            return asdict(result)
        # Delimitado: conteúdo web é dado, não instrução (anti prompt-injection).
        return {
            "title": result.title,
            "text": f'<web_content url="{result.url}">\n{result.text}\n</web_content>',
            "truncated": result.truncated,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
