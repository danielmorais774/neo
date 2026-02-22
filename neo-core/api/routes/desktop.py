"""Desktop routes - endpoints da API para o módulo desktop."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from api.schemas.requests import (
    ErrorResponse,
    ExecuteTaskRequest,
    ExecuteTaskResponse,
    ListToolsResponse,
    SessionHistoryResponse,
    ToolInfoSchema,
)
from modules.desktop import DesktopService
from shared import NeoError

router = APIRouter(prefix="/desktop", tags=["desktop"])


# Dependency injection do service (será configurado no main.py)
_desktop_service: DesktopService | None = None


def set_desktop_service(service: DesktopService) -> None:
    """Define o service instance."""
    global _desktop_service
    _desktop_service = service


def get_desktop_service() -> DesktopService:
    """Dependency para injetar o service."""
    if _desktop_service is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Desktop service not initialized",
        )
    return _desktop_service


@router.post("/execute", response_model=ExecuteTaskResponse)
async def execute_task(
    request: ExecuteTaskRequest,
    service: DesktopService = Depends(get_desktop_service),
) -> ExecuteTaskResponse:
    """
    Executa uma task do usuário.

    O neo-core processa a task, decide quais tools usar,
    e retorna tool_calls para o neo-desktop executar.
    """
    try:
        print(f"📥 Received task: {request.task[:50]}...")
        
        result = await service.execute_task(
            task=request.task,
            session_id=request.session_id,
            provider=request.provider,
            model=request.model,
        )
        
        print(f"📤 Result status: {result.get('status')}, steps: {len(result.get('steps', []))}")
        print(f"📤 Final response: {result.get('final_response')}")
        
        return ExecuteTaskResponse(**result)

    except NeoError as e:
        print(f"❌ NeoError: {e.message}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": e.message, "details": e.details},
        )
    except Exception as e:
        print(f"❌ Exception: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": str(e)},
        )


@router.get("/tools", response_model=ListToolsResponse)
async def list_tools(
    service: DesktopService = Depends(get_desktop_service),
) -> ListToolsResponse:
    """Lista todas as tools disponíveis para desktop."""
    tools = service.get_available_tools()

    tool_schemas = [
        ToolInfoSchema(
            name=t.name,
            description=t.description,
            module=t.module,
            execution=t.execution,
            parameters={
                name: {
                    "type": p.type,
                    "description": p.description,
                    "required": p.required,
                    "enum": p.enum,
                }
                for name, p in t.parameters.items()
            },
        )
        for t in tools
    ]

    return ListToolsResponse(tools=tool_schemas, count=len(tool_schemas))


@router.get("/session/{session_id}", response_model=SessionHistoryResponse)
async def get_session_history(
    session_id: UUID,
    service: DesktopService = Depends(get_desktop_service),
) -> SessionHistoryResponse:
    """Retorna histórico de uma sessão."""
    history = service.get_session_history(session_id)

    if not history:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    return SessionHistoryResponse(**history)


@router.delete("/session/{session_id}")
async def clear_session(
    session_id: UUID,
    service: DesktopService = Depends(get_desktop_service),
) -> dict[str, str]:
    """Limpa uma sessão."""
    deleted = service.clear_session(session_id)

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    return {"message": "Session cleared successfully"}


@router.get("/sessions")
async def list_sessions(
    service: DesktopService = Depends(get_desktop_service),
) -> dict[str, list[str]]:
    """Lista todas as sessões ativas."""
    sessions = service.list_active_sessions()
    return {"sessions": sessions, "count": len(sessions)}
