"""Spotify adapter — search (app token) + playback control (user OAuth token).

Two credential modes:
  - **Client Credentials** (app-only) for ``/v1/search`` — no user login needed.
  - **Authorization Code** (per-user, stored via ``modules.music_config``) for the
    Connect playback endpoints (``/v1/me/player/*``). Requires the user to connect
    Spotify in the neo-panel; playback control also requires a Premium account.

Token lifecycle: the app token is cached in-process; the user access token is
cached in the DB and refreshed here when it nears expiry, using the long-lived
refresh token saved at OAuth time.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx

from modules import music_config
from shared.config import get_settings

logger = logging.getLogger(__name__)

ACCOUNTS_BASE = "https://accounts.spotify.com"
API_BASE = "https://api.spotify.com/v1"
# Minimum scopes for reading the active device and starting playback.
SPOTIFY_SCOPES = "user-read-playback-state user-modify-playback-state"


class SpotifyNotConfiguredError(RuntimeError):
    """Raised when the Spotify app credentials (client id/secret) are missing."""


class SpotifyNotConnectedError(RuntimeError):
    """Raised when the user hasn't connected their Spotify account (no refresh token)."""


@dataclass
class SpotifyTrack:
    id: str
    uri: str
    name: str
    artist: str

    @property
    def label(self) -> str:
        return f"{self.name} — {self.artist}" if self.artist else self.name


class SpotifyAdapter:
    """Async HTTP client for Spotify Web API + Connect playback."""

    def __init__(self, db: Any | None = None) -> None:
        settings = get_settings()
        self._client_id = settings.spotify_client_id
        self._client_secret = settings.spotify_client_secret
        self._redirect_uri = settings.spotify_redirect_uri
        self._timeout = float(settings.spotify_timeout)
        self.db = db
        self._app_token: str | None = None
        self._app_token_exp: float = 0.0
        self._http: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    # ── OAuth helpers ────────────────────────────────────────────────────────
    def authorize_url(self, state: str) -> str:
        """Build the Spotify authorize URL the panel redirects the user to."""
        if not self.is_configured:
            raise SpotifyNotConfiguredError("Spotify client_id/secret não configurados.")
        params = {
            "client_id": self._client_id,
            "response_type": "code",
            "redirect_uri": self._redirect_uri,
            "scope": SPOTIFY_SCOPES,
            "state": state,
            "show_dialog": "false",
        }
        return f"{ACCOUNTS_BASE}/authorize?{urlencode(params)}"

    def _basic_auth(self) -> str:
        raw = f"{self._client_id}:{self._client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout))
        return self._http

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        """POST to the token endpoint; returns the parsed JSON or raises ValueError."""
        if not self.is_configured:
            raise SpotifyNotConfiguredError("Spotify client_id/secret não configurados.")
        http = await self._get_http()
        resp = await http.post(
            f"{ACCOUNTS_BASE}/api/token",
            data=data,
            headers={
                "Authorization": self._basic_auth(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if resp.status_code >= 400:
            raise ValueError(f"Spotify token error ({resp.status_code}): {resp.text[:200]}")
        return resp.json()

    async def exchange_code(self, code: str) -> dict[str, Any]:
        """Exchange an authorization code for tokens (OAuth callback path).

        Returns a bundle ready for ``music_config.save_tokens``.
        """
        data = await self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri,
        })
        return {
            "refresh_token": data.get("refresh_token"),
            "access_token": data.get("access_token"),
            "expires_at": time.time() + float(data.get("expires_in", 3600)),
            "scopes": data.get("scope", SPOTIFY_SCOPES),
        }

    # ── Bearer tokens ────────────────────────────────────────────────────────
    async def _app_bearer(self) -> str:
        """Client-credentials token for search (cached in-process)."""
        if self._app_token and (self._app_token_exp - 30) > time.time():
            return self._app_token
        data = await self._token_request({"grant_type": "client_credentials"})
        self._app_token = data["access_token"]
        self._app_token_exp = time.time() + float(data.get("expires_in", 3600))
        return self._app_token

    async def _user_bearer(self, user_id: UUID) -> str:
        """Valid user access token, refreshing via the stored refresh token if needed."""
        tokens = await music_config.get_tokens(self.db, user_id)
        if not tokens or not tokens.get("refresh_token"):
            raise SpotifyNotConnectedError("Spotify não conectado para este usuário.")
        if music_config.access_token_valid(tokens):
            return tokens["access_token"]
        refreshed = await self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        })
        access_token = refreshed["access_token"]
        await music_config.save_tokens(
            self.db,
            user_id,
            refresh_token=refreshed.get("refresh_token") or tokens["refresh_token"],
            access_token=access_token,
            expires_at=time.time() + float(refreshed.get("expires_in", 3600)),
            scopes=refreshed.get("scope") or tokens.get("scopes"),
        )
        return access_token

    # ── API calls ────────────────────────────────────────────────────────────
    async def search_track(self, query: str, limit: int = 5) -> SpotifyTrack | None:
        """Search a track by free text; returns the top match or None."""
        http = await self._get_http()
        resp = await http.get(
            f"{API_BASE}/search",
            params={"q": query, "type": "track", "limit": limit},
            headers={"Authorization": f"Bearer {await self._app_bearer()}"},
        )
        if resp.status_code >= 400:
            raise ValueError(f"Spotify search error ({resp.status_code}): {resp.text[:200]}")
        items = (resp.json().get("tracks") or {}).get("items") or []
        if not items:
            return None
        top = items[0]
        artists = ", ".join(a.get("name", "") for a in (top.get("artists") or [])).strip(", ")
        return SpotifyTrack(
            id=top.get("id", ""),
            uri=top.get("uri", ""),
            name=top.get("name", ""),
            artist=artists,
        )

    async def get_devices(self, user_id: UUID) -> list[dict[str, Any]]:
        """List the user's Spotify Connect devices (requires user token)."""
        http = await self._get_http()
        resp = await http.get(
            f"{API_BASE}/me/player/devices",
            headers={"Authorization": f"Bearer {await self._user_bearer(user_id)}"},
        )
        if resp.status_code >= 400:
            raise ValueError(f"Spotify devices error ({resp.status_code}): {resp.text[:200]}")
        return resp.json().get("devices") or []

    async def play(
        self, user_id: UUID, uris: list[str], device_id: str | None = None
    ) -> None:
        """Start playback of ``uris`` on the active (or given) device."""
        http = await self._get_http()
        params = {"device_id": device_id} if device_id else {}
        resp = await http.put(
            f"{API_BASE}/me/player/play",
            params=params,
            json={"uris": uris},
            headers={"Authorization": f"Bearer {await self._user_bearer(user_id)}"},
        )
        # 204 = success, 202 = accepted. 404 (no active device) / 403 (non-premium)
        # are meaningful failures the caller should surface.
        if resp.status_code >= 400:
            raise ValueError(f"Spotify play error ({resp.status_code}): {resp.text[:200]}")

    async def control(self, user_id: UUID, action: str) -> None:
        """Pause / resume / skip on the active device. action: pause|resume|next|previous."""
        http = await self._get_http()
        headers = {"Authorization": f"Bearer {await self._user_bearer(user_id)}"}
        method, path = {
            "pause": ("PUT", "pause"),
            "resume": ("PUT", "play"),
            "next": ("POST", "next"),
            "previous": ("POST", "previous"),
        }.get(action, ("", ""))
        if not method:
            raise ValueError(f"Ação de playback desconhecida: {action}")
        resp = await http.request(method, f"{API_BASE}/me/player/{path}", headers=headers)
        if resp.status_code >= 400:
            raise ValueError(f"Spotify control error ({resp.status_code}): {resp.text[:200]}")
