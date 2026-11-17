"""YouTube adapter — video search via the YouTube Data API v3 (app key, no OAuth).

Quota note: ``search.list`` costs 100 units; the default project quota is 10k/day
(~100 searches/day). Cache/measure if it becomes a constraint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from shared.config import get_settings

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


class YouTubeNotConfiguredError(RuntimeError):
    """Raised when no YouTube Data API key is configured."""


@dataclass
class YouTubeVideo:
    id: str
    title: str
    channel: str

    @property
    def watch_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"

    @property
    def label(self) -> str:
        return f"{self.title} — {self.channel}" if self.channel else self.title


class YouTubeAdapter:
    """Async client for the YouTube Data API v3 search endpoint."""

    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.youtube_api_key
        self._timeout = float(settings.youtube_timeout)
        self._http: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout))
        return self._http

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def video_by_id(self, video_id: str) -> YouTubeVideo | None:
        """O vídeo EXATO, pelo id — sem passar pela busca.

        Existe porque jogar um LINK na busca devolve outro vídeo: medido em
        2026-09-09, a fila mandou tocar `watch?v=YdQ5NKjbd2U` e o `search.list`
        do texto da URL respondeu `USELWXVcQUQ`. Como a inscrição da fila espera
        uma faixa específica, o vídeo errado derrubava a fila inteira.

        De quebra, `videos.list` custa **1** unidade de quota contra 100 da
        busca. Devolve None quando não dá para enriquecer (sem chave, id
        inexistente, API fora) — quem chama toca pelo id mesmo assim.
        """
        if not video_id or not self.is_configured:
            return None
        try:
            http = await self._get_http()
            resp = await http.get(
                VIDEOS_URL,
                params={"part": "snippet", "id": video_id, "key": self._api_key},
            )
            if resp.status_code >= 400:
                logger.warning(
                    "YouTube videos.list %s: %s", resp.status_code, resp.text[:200]
                )
                return None
            items = resp.json().get("items") or []
            if not items:
                return None
            snippet = items[0].get("snippet") or {}
            return YouTubeVideo(
                id=video_id,
                title=snippet.get("title", ""),
                channel=snippet.get("channelTitle", ""),
            )
        except Exception as e:  # noqa: BLE001 — enriquecer é opcional, tocar não
            logger.warning("YouTube videos.list falhou para %s: %s", video_id, e)
            return None

    async def search_video(self, query: str, limit: int = 5) -> YouTubeVideo | None:
        """Search a video by free text; returns the top match or None."""
        if not self.is_configured:
            raise YouTubeNotConfiguredError("YOUTUBE_API_KEY não configurada.")
        http = await self._get_http()
        resp = await http.get(
            SEARCH_URL,
            params={
                "part": "snippet",
                "type": "video",
                "maxResults": limit,
                "q": query,
                "key": self._api_key,
            },
        )
        if resp.status_code >= 400:
            raise ValueError(f"YouTube search error ({resp.status_code}): {resp.text[:200]}")
        items = resp.json().get("items") or []
        if not items:
            return None
        top = items[0]
        video_id = (top.get("id") or {}).get("videoId", "")
        snippet = top.get("snippet") or {}
        return YouTubeVideo(
            id=video_id,
            title=snippet.get("title", ""),
            channel=snippet.get("channelTitle", ""),
        )
