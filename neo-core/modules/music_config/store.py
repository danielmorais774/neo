"""Persist Spotify OAuth tokens per user (refresh + cached access token).

Stored as a per-user ``user_preference`` row (service="spotify", category="oauth",
key="tokens"). The value is JSON ``{refresh_token, access_token, expires_at, scopes}``.
The refresh token is long-lived; the access token is short-lived and refreshed by
:class:`core.skills.music.adapter_spotify.SpotifyAdapter` when it nears expiry.

Mirrors the persistence style of ``modules.intent_config`` / ``modules.voice`` — a
thin layer over ``AsyncPostgresClient.{get_preferences,upsert_preference}``.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

PREF_SERVICE = "spotify"
PREF_CATEGORY = "oauth"
PREF_KEY = "tokens"


async def get_tokens(db: Any, user_id: UUID) -> dict[str, Any] | None:
    """Return the stored Spotify token bundle for ``user_id`` or None.

    Shape: ``{"refresh_token","access_token","expires_at","scopes"}``. Never
    raises — a read failure degrades to None (treated as "not connected").
    """
    if db is None or user_id is None:
        return None
    try:
        prefs = await db.get_preferences(
            user_id=user_id, service=PREF_SERVICE, category=PREF_CATEGORY
        )
        for pref in prefs:
            if pref.key == PREF_KEY:
                return json.loads(pref.value)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("get_tokens failed for user=%s: %s", user_id, e)
    return None


async def save_tokens(
    db: Any,
    user_id: UUID,
    *,
    refresh_token: str | None,
    access_token: str | None,
    expires_at: float | None,
    scopes: str | None = None,
) -> None:
    """Upsert the Spotify token bundle for ``user_id``.

    Spotify only returns a ``refresh_token`` on the first authorization (and
    sometimes on rotation); when absent we keep the previously stored one.
    """
    existing = await get_tokens(db, user_id) or {}
    bundle = {
        "refresh_token": refresh_token or existing.get("refresh_token"),
        "access_token": access_token,
        "expires_at": expires_at,
        "scopes": scopes if scopes is not None else existing.get("scopes"),
    }
    await db.upsert_preference(
        service=PREF_SERVICE,
        category=PREF_CATEGORY,
        key=PREF_KEY,
        value=json.dumps(bundle),
        user_id=user_id,
    )


async def clear_tokens(db: Any, user_id: UUID) -> None:
    """Disconnect Spotify for ``user_id`` (blank the bundle)."""
    await db.upsert_preference(
        service=PREF_SERVICE,
        category=PREF_CATEGORY,
        key=PREF_KEY,
        value=json.dumps({}),
        user_id=user_id,
    )


async def is_connected(db: Any, user_id: UUID) -> bool:
    """True when a usable refresh token is stored for ``user_id``."""
    tokens = await get_tokens(db, user_id)
    return bool(tokens and tokens.get("refresh_token"))


def access_token_valid(tokens: dict[str, Any] | None, *, skew: float = 30.0) -> bool:
    """True when ``tokens`` carries an access token not within ``skew`` of expiry."""
    if not tokens or not tokens.get("access_token"):
        return False
    expires_at = tokens.get("expires_at")
    if not expires_at:
        return False
    return (float(expires_at) - skew) > time.time()
