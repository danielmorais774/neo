"""Subsystem LLM model configuration routes (neo-panel → Modelos page).

Generalizes ``intent_config`` to the other LLM-driven roles in neo-core (the PKS
memory planner and the Deep Research volume/writer models): an operator picks a
provider/model per role and it's picked up on the next job/plan without a restart.
Selections are persisted globally via ``modules.model_config``.

Protected by the panel JWT (wired in ``api/main.py``).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.llm.router import LLMRouter
from db.async_client import AsyncPostgresClient
from modules.model_config import (
    ROLES,
    build_catalog,
    get_all_configs,
    set_model_config,
)
from modules.model_config.store import KNOWN_PROVIDERS, is_provider_available

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/model-config", tags=["model-config"])

_db: AsyncPostgresClient | None = None
_llm_router: LLMRouter | None = None


def set_dependencies(db: AsyncPostgresClient, llm_router: LLMRouter) -> None:
    """Inject the shared DB + LLMRouter (called from the app lifespan)."""
    global _db, _llm_router
    _db = db
    _llm_router = llm_router


class ModelConfigUpdate(BaseModel):
    """Payload to switch one role's provider/model."""

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
async def get_model_config_catalog() -> dict:
    """Return every role's active provider/model plus the provider catalog for the UI."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    current = await get_all_configs(_db)
    return build_catalog(_available_providers(), current)


@router.put("/{role}")
async def update_model_config(role: str, payload: ModelConfigUpdate) -> dict:
    """Persist a new selection for ``role`` after validating the provider is usable."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    if role not in ROLES:
        raise HTTPException(
            status_code=404,
            detail=f"Role desconhecido '{role}'. Conhecidos: {', '.join(ROLES)}",
        )

    provider = payload.provider.strip().lower()
    if provider not in KNOWN_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Provider desconhecido '{provider}'. Conhecidos: {', '.join(KNOWN_PROVIDERS)}",
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

    await set_model_config(_db, role, provider, payload.model)
    current = await get_all_configs(_db)
    return build_catalog(available, current)
