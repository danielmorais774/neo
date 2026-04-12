"""Context routes — endpoint de busca de contexto no grafo de conhecimento."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from modules.context.knowledge_retriever import KnowledgeRetriever

router = APIRouter(prefix="/context", tags=["context"])


# ── Schemas ───────────────────────────────────────────────────────
class ContextRetrieveRequest(BaseModel):
    user_id: UUID = Field(..., description="UUID do usuário")
    user_input: str = Field(..., description="Mensagem bruta do usuário")


class ContextRetrieveResponse(BaseModel):
    contexts_found: list[dict] = Field(default_factory=list)
    global_: list[dict] = Field(default_factory=list, alias="global")
    metadata: dict = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


# ── Dependency injection ──────────────────────────────────────────
_knowledge_retriever: KnowledgeRetriever | None = None


def set_knowledge_retriever(retriever: KnowledgeRetriever) -> None:
    global _knowledge_retriever
    _knowledge_retriever = retriever


def get_knowledge_retriever() -> KnowledgeRetriever:
    if _knowledge_retriever is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Knowledge retriever not initialized",
        )
    return _knowledge_retriever


# ── Endpoints ─────────────────────────────────────────────────────
@router.post("/retrieve", response_model=ContextRetrieveResponse)
async def retrieve_context(
    body: ContextRetrieveRequest,
    retriever: KnowledgeRetriever = Depends(get_knowledge_retriever),
):
    """Busca contexto relevante no grafo de conhecimento do usuário."""
    result = await retriever.retrieve(user_id=body.user_id, user_input=body.user_input)
    return ContextRetrieveResponse(
        contexts_found=result["contexts_found"],
        **{"global": result["global"]},
        metadata=result["metadata"],
    )
