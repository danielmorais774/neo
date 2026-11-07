"""Intent classifier configuration routes (neo-panel → Config page).

Lets an operator pick which LLM provider/model runs the IntentRouter without a
restart. The selection is persisted globally via ``modules.intent_config`` and
picked up by the router on the next message (cache invalidated on write).

Protected by the panel JWT (wired in ``api/main.py``).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.llm.router import LLMRouter
from db.async_client import AsyncPostgresClient
from modules.intent_config import (
    build_catalog,
    get_classifier_config,
    set_classifier_config,
)
from modules.intent_config.store import is_provider_available, known_providers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/intent-config", tags=["intent-config"])

_db: AsyncPostgresClient | None = None
_llm_router: LLMRouter | None = None


def set_dependencies(db: AsyncPostgresClient, llm_router: LLMRouter) -> None:
    """Inject the shared DB + LLMRouter (called from the app lifespan)."""
    global _db, _llm_router
    _db = db
    _llm_router = llm_router


class IntentConfigUpdate(BaseModel):
    """Payload to switch the intent classifier provider/model."""

    provider: str = Field(..., description="LLM provider, e.g. 'deepseek', 'groq', 'openai'")
    model: str = Field(..., min_length=1, description="Model id for that provider")


def _available_providers() -> list[str]:
    if _llm_router is None:
        return []
    try:
        return _llm_router.list_available_providers()
    except Exception:  # pragma: no cover - defensive
        return []


@router.get("")
async def get_intent_config() -> dict:
    """Return the active classifier plus the provider/model catalog for the UI."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    current = await get_classifier_config(_db)
    return build_catalog(_available_providers(), current)


@router.put("")
async def update_intent_config(payload: IntentConfigUpdate) -> dict:
    """Persist a new classifier selection after validating the provider is usable."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Service not ready")

    provider = payload.provider.strip().lower()
    if provider not in known_providers():
        raise HTTPException(
            status_code=400,
            detail=f"Provider desconhecido '{provider}'. Conhecidos: {', '.join(known_providers())}",
        )

    available = _available_providers()
    if not is_provider_available(provider, available):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Provider '{provider}' não está configurado (defina a API key correspondente). "
                f"Disponíveis: {', '.join(available) or 'nenhum'}"
            ),
        )

    config = await set_classifier_config(_db, provider, payload.model)
    return build_catalog(available, config)
