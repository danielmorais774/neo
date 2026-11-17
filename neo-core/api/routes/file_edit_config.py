"""Aba Arquivos do neo-panel: como o Neo edita arquivos do disco do usuário.

GET devolve a config atual + defaults; PUT grava para todos os usuários e o
registro global (mesmo padrão do voice-config), e vale a partir do próximo turno
(o cache de ``get_file_edit_config`` é invalidado na hora).

Protegido pelo JWT do painel (ligado em ``api/main.py``).
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from db.async_client import AsyncPostgresClient
from modules.desktop.file_edit_config import (
    DEFAULTS,
    ENGINES,
    PREF_CATEGORY,
    PREF_KEY,
    PREF_SERVICE,
    get_file_edit_config,
    invalidate_file_edit_config_cache,
    normalize_file_edit_config,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/file-edit-config", tags=["file-edit-config"])

_db: AsyncPostgresClient | None = None


def set_dependencies(db: AsyncPostgresClient) -> None:
    """Injeta o cliente de banco compartilhado (chamado no lifespan do app)."""
    global _db
    _db = db


class FileEditConfigUpdate(BaseModel):
    engine: str = Field(..., description="'neo' (ferramentas do próprio Neo) ou 'opencode'")
    opencode_model: str | None = Field(None, description="provider/modelo do opencode")
    opencode_timeout_s: int | None = Field(None, ge=30, le=1800)


async def _payload() -> dict:
    invalidate_file_edit_config_cache()
    return {
        "current": await get_file_edit_config(_db, None),
        "defaults": DEFAULTS,
        "engines": list(ENGINES),
    }


@router.get("")
async def get_config() -> dict:
    if _db is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    return await _payload()


@router.put("")
async def update_config(payload: FileEditConfigUpdate) -> dict:
    if _db is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    engine = payload.engine.strip().lower()
    if engine not in ENGINES:
        raise HTTPException(status_code=422, detail=f"engine deve ser um de {list(ENGINES)}")

    cfg = normalize_file_edit_config(
        {
            "engine": engine,
            "opencode_model": payload.opencode_model or DEFAULTS["opencode_model"],
            "opencode_timeout_s": payload.opencode_timeout_s or DEFAULTS["opencode_timeout_s"],
        }
    )
    value = json.dumps(cfg, ensure_ascii=False)

    try:
        async with _db.get_session() as s:
            rows = (await s.execute(text('SELECT id FROM "user"'))).all()
        targets: list = [r[0] for r in rows] + [None]
    except Exception as e:  # noqa: BLE001
        logger.warning("file-edit-config: não listei usuários (%s); gravando só o global", e)
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
    logger.info("[file-edit-config] atualizado: %s", cfg)
    return await _payload()
