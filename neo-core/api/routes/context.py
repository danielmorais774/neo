"""Context routes — endpoint de busca de contexto no PKS."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from core.pks import PKSService

router = APIRouter(prefix="/context", tags=["context"])


# ── Schemas ───────────────────────────────────────────────────────
class ContextRetrieveRequest(BaseModel):
    user_id: UUID = Field(..., description="UUID do usuário")
    user_input: str = Field(..., description="Mensagem bruta do usuário")


class ContextRetrieveResponse(BaseModel):
    contexts_found: list[dict] = Field(default_factory=list)
    global_: list[dict] = Field(default_factory=list, alias="global")
    context_text: str = ""
    metadata: dict = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


# ── Dependency injection ──────────────────────────────────────────
_pks_service: PKSService | None = None


def set_pks_service(service: PKSService) -> None:
    global _pks_service
    _pks_service = service


def get_pks_service() -> PKSService:
    if _pks_service is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PKS service not initialized",
        )
    return _pks_service


# ── Endpoints ─────────────────────────────────────────────────────
@router.post("/retrieve", response_model=ContextRetrieveResponse)
async def retrieve_context(
    body: ContextRetrieveRequest,
    pks: PKSService = Depends(get_pks_service),
):
    """Busca contexto relevante no PKS do usuário."""
    result, context_text = await pks.retrieve_context(
        user_id=body.user_id,
        user_message=body.user_input,
        keywords=None,
    )
    result["context_text"] = context_text
    return ContextRetrieveResponse(
        contexts_found=result.get("neighborhoods", []),
        **{"global": []},
        context_text=context_text,
        metadata=result["metadata"],
    )
