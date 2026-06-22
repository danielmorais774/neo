"""Spotify OAuth routes — connect a user's Spotify account from the neo-panel.

Flow (Authorization Code):
  1. Panel opens ``GET /spotify/authorize?user_id=...`` → 302 to Spotify's consent.
  2. Spotify redirects back to ``GET /spotify/callback?code=...&state=...``.
  3. We exchange the code for tokens and persist them via ``modules.music_config``.

These endpoints are NOT behind the panel JWT: the callback is a browser redirect
from Spotify and can't carry it. CSRF/identity is bound through a signed ``state``
(HMAC over the user_id with the panel secret). The music skill then uses the stored
refresh token to control playback. See neo-music-feature.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from core.skills.music.adapter_spotify import (
    SpotifyAdapter,
    SpotifyNotConfiguredError,
)
from db.async_client import AsyncPostgresClient
from modules import music_config
from shared.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/spotify", tags=["spotify"])

_db: AsyncPostgresClient | None = None


def set_dependencies(db: AsyncPostgresClient) -> None:
    """Inject the shared DB (called from the app lifespan)."""
    global _db
    _db = db


def _sign(user_id: str) -> str:
    secret = (get_settings().panel_jwt_secret or "neo").encode()
    mac = hmac.new(secret, user_id.encode(), hashlib.sha256).hexdigest()
    return f"{user_id}.{mac}"


def _verify(state: str) -> str | None:
    """Return the user_id from a signed state, or None when tampered/invalid."""
    if not state or "." not in state:
        return None
    user_id, _, mac = state.rpartition(".")
    expected = hmac.new(
        (get_settings().panel_jwt_secret or "neo").encode(),
        user_id.encode(),
        hashlib.sha256,
    ).hexdigest()
    return user_id if hmac.compare_digest(mac, expected) else None


def _html(message: str) -> HTMLResponse:
    body = (
        "<!doctype html><meta charset='utf-8'>"
        "<style>body{font-family:system-ui;background:#111;color:#eee;"
        "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
        "div{text-align:center}</style>"
        f"<div><h2>{message}</h2><p>Pode fechar esta aba.</p></div>"
    )
    return HTMLResponse(content=body)


@router.get("/authorize")
async def spotify_authorize(user_id: str) -> RedirectResponse:
    """Redirect the user to Spotify's consent screen (state binds the user_id)."""
    try:
        UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="user_id inválido")
    adapter = SpotifyAdapter(_db)
    try:
        url = adapter.authorize_url(_sign(user_id))
    except SpotifyNotConfiguredError:
        raise HTTPException(
            status_code=503,
            detail="Spotify não configurado no neo-core (defina SPOTIFY_CLIENT_ID/SECRET).",
        )
    finally:
        await adapter.close()
    return RedirectResponse(url)


@router.get("/callback")
async def spotify_callback(
    code: str | None = None, state: str | None = None, error: str | None = None
) -> HTMLResponse:
    """Exchange the auth code for tokens and persist them for the user."""
    if error:
        return _html(f"Autorização negada: {error}")
    if not code or not state:
        return _html("Faltou o código de autorização.")
    user_id = _verify(state)
    if not user_id:
        return _html("State inválido — tente conectar novamente.")
    if _db is None:
        raise HTTPException(status_code=503, detail="Service not ready")

    adapter = SpotifyAdapter(_db)
    try:
        bundle = await adapter.exchange_code(code)
        await music_config.save_tokens(
            _db,
            UUID(user_id),
            refresh_token=bundle["refresh_token"],
            access_token=bundle["access_token"],
            expires_at=bundle["expires_at"],
            scopes=bundle["scopes"],
        )
    except Exception as e:
        logger.exception("Spotify callback failed")
        return _html(f"Erro ao conectar o Spotify: {e}")
    finally:
        await adapter.close()
    return _html("Spotify conectado! 🎵")


@router.get("/status")
async def spotify_status(user_id: str) -> dict:
    """Whether Spotify is configured (app creds) and connected (user tokens)."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    try:
        uid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="user_id inválido")
    adapter = SpotifyAdapter(_db)
    configured = adapter.is_configured
    await adapter.close()
    connected = configured and await music_config.is_connected(_db, uid)
    return {"configured": configured, "connected": connected}


@router.post("/disconnect")
async def spotify_disconnect(user_id: str) -> dict:
    """Forget the stored Spotify tokens for the user."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    try:
        uid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="user_id inválido")
    await music_config.clear_tokens(_db, uid)
    return {"connected": False}
