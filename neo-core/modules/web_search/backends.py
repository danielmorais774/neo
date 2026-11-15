"""Backends de busca web — SearXNG (self-host) primário, ddgs (DuckDuckGo) fallback.

Cada backend implementa o protocolo :class:`SearchBackend`. A cadeia (ordem,
degradação, cache) fica no :class:`~modules.web_search.executor.WebSearchExecutor`.
Ver docs/web-search-deep-research-plan.md §3.2.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

VALID_RECENCY = ("day", "week", "month", "any")
# 'science' cobre arxiv/scholar/semantic scholar/crossref/pubmed no SearXNG — é
# a matéria-prima do deep research acadêmico (docs/deep-research-v2-plan.md §6).
VALID_CATEGORY = ("news", "general", "science")


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    published_at: str | None = None
    source: str | None = None

    def to_dict(self) -> dict:
        d = {"title": self.title, "url": self.url, "snippet": self.snippet}
        if self.published_at:
            d["published_at"] = self.published_at
        if self.source:
            d["source"] = self.source
        return d


class SearchBackend(Protocol):
    name: str

    async def search(
        self, query: str, *, recency: str, category: str, n: int
    ) -> list[SearchResult]: ...


def _idioma_busca() -> str:
    """``[web] idioma_busca`` do neo.toml."""
    try:
        from shared.ajustes import ajustes

        return ajustes().web.idioma_busca
    except Exception:  # noqa: BLE001 — ajuste ilegível: sem filtro de idioma
        return "all"


class SearxngBackend:
    """SearXNG self-hosted (serviço ``searxng`` no compose, sem porta publicada).

    Gotcha: o settings.yml do SearXNG PRECISA de ``formats: [html, json]``,
    senão o ``format=json`` responde 403.
    """

    name = "searxng"

    def __init__(self, base_url: str, *, timeout: float = 8.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def search(
        self, query: str, *, recency: str = "any", category: str = "general", n: int = 5
    ) -> list[SearchResult]:
        params: dict = {
            "q": query,
            "format": "json",
            # `pt-BR` filtrava páginas em português no Google CSE (o único motor
            # que responde da VPS; Brave/DuckDuckGo/Startpage dão CAPTCHA): busca
            # em inglês voltava lixo ou nada (session-34d7f528). Ver [web] no neo.toml.
            "language": _idioma_busca(),
            "categories": category if category in ("news", "science") else "general",
            "safesearch": 0,
        }
        if recency in ("day", "week", "month"):
            params["time_range"] = recency
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/search", params=params)
            resp.raise_for_status()
            data = resp.json()
        results: list[SearchResult] = []
        for item in data.get("results") or []:
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not url or not title:
                continue
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=str(item.get("content") or "").strip(),
                    published_at=item.get("publishedDate") or None,
                    source=item.get("engine") or None,
                )
            )
            if len(results) >= n:
                break
        return results


class DdgsBackend:
    """Fallback DuckDuckGo via lib ``ddgs`` (sem chave). Lib síncrona → to_thread.

    Não tem categoria científica: ``science`` cai no ``text()`` normal (busca web
    genérica) em vez de inventar operadores de query que o DDG trata mal.
    """

    name = "ddgs"

    def __init__(self, *, timeout: float = 10.0) -> None:
        self.timeout = timeout

    async def search(
        self, query: str, *, recency: str = "any", category: str = "general", n: int = 5
    ) -> list[SearchResult]:
        return await asyncio.wait_for(
            asyncio.to_thread(self._search_sync, query, recency, category, n),
            timeout=self.timeout,
        )

    def _search_sync(
        self, query: str, recency: str, category: str, n: int
    ) -> list[SearchResult]:
        from ddgs import DDGS

        timelimit = {"day": "d", "week": "w", "month": "m"}.get(recency)
        results: list[SearchResult] = []
        with DDGS() as ddgs:
            if category == "news":
                raw = ddgs.news(query, region="br-pt", timelimit=timelimit, max_results=n)
                for item in raw or []:
                    url = str(item.get("url") or "").strip()
                    title = str(item.get("title") or "").strip()
                    if not url or not title:
                        continue
                    results.append(
                        SearchResult(
                            title=title,
                            url=url,
                            snippet=str(item.get("body") or "").strip(),
                            published_at=item.get("date") or None,
                            source=item.get("source") or None,
                        )
                    )
            else:
                raw = ddgs.text(query, region="br-pt", timelimit=timelimit, max_results=n)
                for item in raw or []:
                    url = str(item.get("href") or item.get("url") or "").strip()
                    title = str(item.get("title") or "").strip()
                    if not url or not title:
                        continue
                    results.append(
                        SearchResult(
                            title=title,
                            url=url,
                            snippet=str(item.get("body") or "").strip(),
                        )
                    )
        return results[:n]
