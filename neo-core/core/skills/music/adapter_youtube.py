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
