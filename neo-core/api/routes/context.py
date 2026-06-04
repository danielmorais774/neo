"""Context routes — endpoint de busca de contexto no PKS."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from core.pks import PKSService
from shared.types import IntentType, RoutingDecision

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


class PromptPreviewRequest(BaseModel):
    user_id: UUID = Field(..., description="UUID do usuário")
    user_input: str = Field(..., description="Mensagem bruta do usuário")


class CandidateTrace(BaseModel):
    layer: str
    source: str
    key: str
    pin: bool
    est_tokens: int
    relevance: float
    final_score: float
    included: bool
    skip_reason: str


class PromptPreviewResponse(BaseModel):
    system_prompt: str
    trace: list[CandidateTrace]
    prompt_tokens_est: int


# ── Dependency injection ──────────────────────────────────────────
_pks_service: PKSService | None = None
_context_selector = None


def set_pks_service(service: PKSService) -> None:
    global _pks_service
    _pks_service = service


def set_context_selector(selector) -> None:
    global _context_selector
    _context_selector = selector


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


@router.post("/prompt-preview", response_model=PromptPreviewResponse)
async def prompt_preview(body: PromptPreviewRequest):
    """Monta o system prompt completo via PromptContextSelector sem chamar o LLM.

    Retorna o texto exato que seria enviado ao modelo, mais o trace de seleção
    com score, tokens estimados e motivo de inclusão/exclusão de cada empresa.
    """
    if _context_selector is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ContextSelector não inicializado",
        )

    decision = RoutingDecision(
        intent=IntentType.DIRECT_RESPONSE,
        confidence=1.0,
        needs_memory=False,  # sem busca cross-session no preview
        knowledge_keywords=None,
    )

    system_prompt, candidates = await _context_selector.build_prompt(
        user_message=body.user_input,
        user_id=body.user_id,
        decision=decision,
        system_context=None,
        recovered_context=[],
        is_neo_training=False,
    )

    trace = [
        CandidateTrace(
            layer=c.layer.value,
            source=c.source,
            key=c.key,
            pin=c.pin,
            est_tokens=c.est_tokens,
            relevance=round(c.relevance, 3),
            final_score=round(c.final_score, 3),
            included=c.included,
            skip_reason=c.skip_reason,
        )
        for c in candidates
    ]

    prompt_tokens_est = sum(c.est_tokens for c in candidates if c.included)

    return PromptPreviewResponse(
        system_prompt=system_prompt,
        trace=trace,
        prompt_tokens_est=prompt_tokens_est,
    )
