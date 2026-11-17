"""Voice (TTS) configuration routes (neo-panel → Settings page).

Lets an operator pick the active TTS engine + voice from the neo-voice catalog,
without a restart. The selection is persisted in ``user_preference``
(service="neo-voice", category="tts", key="active") for every user + global, and
read back per-request by the desktop renderer (Pattern A, see
``modules.voice.executor.get_active_voice``).

Protected by the panel JWT (wired in ``api/main.py``).
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from db.async_client import AsyncPostgresClient
from modules.voice.catalog import VoiceCatalog
from modules.voice.executor import (
    PREF_CATEGORY,
    PREF_KEY,
    PREF_SERVICE,
    get_active_voice,
)
from shared.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice-config", tags=["voice-config"])

_db: AsyncPostgresClient | None = None


def set_dependencies(db: AsyncPostgresClient) -> None:
    """Inject the shared DB client (called from the app lifespan)."""
    global _db
    _db = db


class VoiceConfigUpdate(BaseModel):
    """Payload to switch the active TTS engine/voice."""

    engine: str = Field(..., min_length=1, description="Engine/backend, e.g. 'chatterbox', 'f5', 'coqui'")
    voice: str = Field(..., min_length=1, description="Speaker/clone id sent to the engine")
    language: str | None = Field(None, description="Language code, e.g. 'pt'")
    label: str | None = Field(None, description="Friendly name for display")


async def _current() -> dict:
    """The latest persisted active voice (any user), else the engine default."""
    assert _db is not None
    try:
        async with _db.get_session() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT value FROM user_preference "
                        "WHERE service = :svc AND category = :cat AND key = :key "
                        "ORDER BY updated_at DESC LIMIT 1"
                    ),
                    {"svc": PREF_SERVICE, "cat": PREF_CATEGORY, "key": PREF_KEY},
                )
            ).first()
        if row:
            return json.loads(row[0])
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("voice-config current read failed: %s", e)
    return await get_active_voice(_db, None)


async def _catalog() -> list[dict]:
    """Compact neo-voice catalog (engine + voices). Empty if neo-voice is unreachable."""
    try:
        cat = VoiceCatalog(get_settings())
        return cat.compact(await cat.fetch())
    except Exception as e:  # pragma: no cover - defensive
        logger.info("voice-config catalog unavailable: %s", e)
        return []


async def _payload() -> dict:
    voices = await _catalog()
    engines = sorted({v["engine"] for v in voices})
    return {"current": await _current(), "voices": voices, "engines": engines}


@router.get("")
async def get_voice_config() -> dict:
    """Return the active voice + the catalog (engines/voices) for the UI."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    return await _payload()


@router.put("")
async def update_voice_config(payload: VoiceConfigUpdate) -> dict:
    """Persist the active engine/voice for every user + global; desktop reads per-request."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Service not ready")

    value = json.dumps(
        {
            "engine": payload.engine.strip(),
            "voice": payload.voice.strip(),
            "language": payload.language,
            "label": payload.label,
        },
        ensure_ascii=False,
    )

    # Write for each existing user + the global (NULL) row, mirroring scripts/set_active_voice.
    try:
        async with _db.get_session() as s:
            rows = (await s.execute(text('SELECT id FROM "user"'))).all()
        targets: list = [r[0] for r in rows] + [None]
    except Exception as e:
        logger.warning("voice-config: could not list users (%s); writing global only", e)
        targets = [None]

    for uid in targets:
        await _db.upsert_preference(
            category=PREF_CATEGORY,
            key=PREF_KEY,
            value=value,
            user_id=uid,
            service=PREF_SERVICE,
            source_message="neo-panel settings",
        )

    return await _payload()
