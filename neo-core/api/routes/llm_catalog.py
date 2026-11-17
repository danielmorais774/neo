"""Catálogo de LLM — CRUD de providers, chaves e modelos (neo-panel → Providers).

Toda gravação relê o banco e remonta o ``LLMRouter`` (``modules.llm_catalog``):
vale no próximo turno, sem reiniciar. A chave de API só ENTRA por aqui — nenhuma
rota a devolve, nem cifrada.

Protected by the panel JWT (wired in ``api/main.py``).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.llm import catalog
from core.llm.router import LLMRouter
from db.async_client import AsyncPostgresClient
from modules.llm_catalog import store
from modules.llm_catalog.store import CatalogError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm", tags=["llm-catalog"])

_db: AsyncPostgresClient | None = None
_llm_router: LLMRouter | None = None


def set_dependencies(db: AsyncPostgresClient, llm_router: LLMRouter) -> None:
    """Inject the shared DB + LLMRouter (called from the app lifespan)."""
    global _db, _llm_router
    _db = db
    _llm_router = llm_router


class ProviderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    label: str | None = Field(default=None, max_length=80)
    adapter: str = Field(..., min_length=1, max_length=30)
    base_url: str | None = Field(default=None, max_length=300)


class ProviderUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=80)
    adapter: str | None = Field(default=None, max_length=30)
    base_url: str | None = Field(default=None, max_length=300)
    enabled: bool | None = None
    default_model: str | None = Field(default=None, max_length=200)


class KeyUpdate(BaseModel):
    # Vazio/None apaga a chave do banco (volta a valer a do .env, se houver).
    api_key: str | None = Field(default=None, max_length=500)


class ProbeRequest(BaseModel):
    model: str | None = Field(default=None, max_length=200)


class ModelCreate(BaseModel):
    model_id: str = Field(..., min_length=1, max_length=200)
    label: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=300)
    vision: bool | None = None
    in_selector: bool | None = None
    notes: str | None = None


class ModelUpdate(BaseModel):
    model_id: str | None = Field(default=None, max_length=200)
    label: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=300)
    vision: bool | None = None
    in_selector: bool | None = None
    notes: str | None = None


class Order(BaseModel):
    ids: list[str]


def _ready() -> tuple[AsyncPostgresClient, LLMRouter]:
    if _db is None or _llm_router is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    return _db, _llm_router


async def _payload() -> dict[str, Any]:
    db, llm = _ready()
    return store.admin_payload(llm, await store.usage(db))


async def _write(action: Callable[[], Awaitable[None]]) -> dict[str, Any]:
    """Roda a gravação e devolve o catálogo inteiro já atualizado."""
    try:
        await action()
    except CatalogError as e:
        raise HTTPException(status_code=e.status, detail=e.message) from e
    return await _payload()


@router.get("/catalog")
async def get_catalog() -> dict[str, Any]:
    """Providers (com origem da chave, nunca o valor), modelos, adapters e usos.

    Enquanto o catálogo estiver na semente (o core subiu antes da migração), cada
    leitura tenta o banco de novo: rodou o `alembic upgrade head`, basta abrir a
    aba — sem reiniciar o core.
    """
    db, llm = _ready()
    if catalog.current().source != "db":
        await store.refresh(db, llm)
    return await _payload()


@router.post("/providers")
async def create_provider(payload: ProviderCreate) -> dict[str, Any]:
    db, llm = _ready()
    return await _write(lambda: store.create_provider(db, llm, payload.model_dump()))


@router.put("/providers/order")
async def reorder_providers(payload: Order) -> dict[str, Any]:
    db, llm = _ready()
    return await _write(lambda: store.reorder_providers(db, llm, payload.ids))


@router.put("/providers/{name}")
async def update_provider(name: str, payload: ProviderUpdate) -> dict[str, Any]:
    db, llm = _ready()
    # Só o que veio no corpo: `base_url: null` explícito limpa, ausente não mexe.
    data = payload.model_dump(exclude_unset=True)
    return await _write(lambda: store.update_provider(db, llm, name, data))


@router.delete("/providers/{name}")
async def delete_provider(name: str) -> dict[str, Any]:
    db, llm = _ready()
    return await _write(lambda: store.delete_provider(db, llm, name))


@router.put("/providers/{name}/key")
async def set_provider_key(name: str, payload: KeyUpdate) -> dict[str, Any]:
    db, llm = _ready()
    return await _write(lambda: store.set_key(db, llm, name, payload.api_key))


@router.delete("/providers/{name}/key")
async def delete_provider_key(name: str) -> dict[str, Any]:
    db, llm = _ready()
    return await _write(lambda: store.set_key(db, llm, name, None))


@router.post("/providers/{name}/test")
async def probe_provider(name: str, payload: ProbeRequest) -> dict[str, Any]:
    """Chamada mínima de verdade com a chave em uso — confirma antes do chat quebrar."""
    _, llm = _ready()
    return await store.probe_provider(llm, name, payload.model)


@router.post("/providers/{name}/models")
async def create_model(name: str, payload: ModelCreate) -> dict[str, Any]:
    db, llm = _ready()
    return await _write(lambda: store.create_model(db, llm, name, payload.model_dump()))


@router.put("/providers/{name}/models/order")
async def reorder_models(name: str, payload: Order) -> dict[str, Any]:
    db, llm = _ready()
    return await _write(lambda: store.reorder_models(db, llm, name, payload.ids))


@router.put("/models/{model_row_id}")
async def update_model(model_row_id: str, payload: ModelUpdate) -> dict[str, Any]:
    db, llm = _ready()
    data = payload.model_dump(exclude_unset=True)
    return await _write(lambda: store.update_model(db, llm, model_row_id, data))


@router.delete("/models/{model_row_id}")
async def delete_model(model_row_id: str) -> dict[str, Any]:
    db, llm = _ready()
    return await _write(lambda: store.delete_model(db, llm, model_row_id))
