"""Context routes — endpoint de busca de contexto no PKS.

**Autenticação (docs/api-auth-plan.md).** Estas duas rotas despejam o bloco do
PKS e o prompt montado — ou seja, dado pessoal cru. O router é montado com
``require_owner`` em ``api/main.py``, e o ``user_id`` do CORPO é advisório: quem
manda é o dono da credencial (``owner_of``). Antes, mandar o UUID de alguém no
corpo entregava a memória dessa pessoa a quem alcançasse a porta.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from api.auth import Principal, owner_of, require_owner
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


class PromptPreviewRequest(BaseModel):
    user_id: UUID = Field(..., description="UUID do usuário")
    user_input: str = Field(..., description="Mensagem bruta do usuário")
    # Metadata do turno (o mesmo que o cliente manda no gRPC). Serve para prever o
    # prompt de um turno de VOZ (`{"input_source": "voice", "os_platform": "darwin"}`)
    # sem precisar falar de verdade — ver docs/voice-input-normalization-plan.md.
    metadata: dict = Field(default_factory=dict)


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


class RecoveredMemory(BaseModel):
    content: str
    source: str
    score: float


class DecisionInfo(BaseModel):
    intent: str
    needs_memory: bool
    knowledge_keywords: list[str] = Field(default_factory=list)


class PromptPreviewResponse(BaseModel):
    system_prompt: str
    trace: list[CandidateTrace]
    prompt_tokens_est: int
    # Full-fidelity fields: the panel renders the same layers the live turn
    # injects — the PKS neighborhoods, the semantic-memory (RETRIEVED) layer and
    # the real routing decision — instead of a stripped-down preview.
    decision: DecisionInfo
    contexts_found: list[dict] = Field(default_factory=list)
    context_text: str = ""
    recovered: list[RecoveredMemory] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


# ── Dependency injection ──────────────────────────────────────────
_pks_service: PKSService | None = None
_context_selector = None
_ai_service = None


def set_pks_service(service: PKSService) -> None:
    global _pks_service
    _pks_service = service


def set_context_selector(selector) -> None:
    global _context_selector
    _context_selector = selector


def set_ai_service(service) -> None:
    global _ai_service
    _ai_service = service


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
    request: Request,
    pks: PKSService = Depends(get_pks_service),
    principal: Principal = Depends(require_owner),
):
    """Busca contexto relevante no PKS do usuário."""
    result, context_text = await pks.retrieve_context(
        user_id=owner_of(principal, body.user_id, request=request) or body.user_id,
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
async def prompt_preview(
    body: PromptPreviewRequest,
    request: Request,
    principal: Principal = Depends(require_owner),
):
    """Reproduz a montagem do prompt do turno real, sem escrever nem chamar o LLM.

    Roda o MESMO pré-processamento de ``_handle_direct_response`` — classificação
    real de intent (keywords/needs_memory reais), recuperação semântica
    cross-sessão (camada RETRIEVED) e o registro situacional — e então monta o
    prompt. Sem os efeitos colaterais do turno vivo: NÃO grava no PKS
    (``learn_before_response``) e nunca chama o modelo. Retorna o texto exato que
    iria ao LLM, o trace de seleção, as entidades do PKS, a memória semântica
    recuperada e a decisão de roteamento.
    """
    if _ai_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AIService não inicializado",
        )

    result = await _ai_service.preview_prompt(
        user_id=owner_of(principal, body.user_id, request=request) or body.user_id,
        user_message=body.user_input,
        metadata=body.metadata or None,
    )

    candidates = result["candidates"]
    decision = result["decision"]

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

    return PromptPreviewResponse(
        system_prompt=result["system_prompt"],
        trace=trace,
        prompt_tokens_est=result["prompt_tokens_est"],
        decision=DecisionInfo(
            intent=decision.intent.value,
            needs_memory=bool(decision.needs_memory),
            knowledge_keywords=list(decision.knowledge_keywords or []),
        ),
        contexts_found=result["contexts_found"],
        context_text=result["context_text"],
        recovered=[
            RecoveredMemory(
                content=r.content,
                source=r.source,
                score=round(r.final_score, 3),
            )
            for r in result["recovered"]
        ],
        keywords=result["keywords"],
    )
