"""Desktop routes - endpoints da API para o módulo desktop."""

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from api.schemas.requests import (
    DesktopChatMessage,
    ErrorResponse,
    ExecuteTaskRequest,
    ExecuteTaskResponse,
    ListToolsResponse,
    SessionHistoryResponse,
    ToolInfoSchema,
)
from api.routes.missions import _execute_mission_command
from db.async_client import AsyncPostgresClient
from db.models import ConversationMessage, Mission
from modules.desktop import AIService, DesktopService
from shared import NeoError
from shared.types import MessageType, MissionCommandPayload

router = APIRouter(prefix="/desktop", tags=["desktop"])


# Dependency injection do service (será configurado no main.py)
_desktop_service: DesktopService | None = None
_ai_service: AIService | None = None
_db: AsyncPostgresClient | None = None


def set_desktop_service(service: DesktopService) -> None:
    """Define o service instance."""
    global _desktop_service
    _desktop_service = service


def set_ai_service(service: AIService) -> None:
    """Define the AI service instance for chat endpoints."""
    global _ai_service
    _ai_service = service


def set_db(db: AsyncPostgresClient) -> None:
    """Define o DB instance para endpoints de histórico persistente."""
    global _db
    _db = db


def get_desktop_service() -> DesktopService:
    """Dependency para injetar o service."""
    if _desktop_service is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Desktop service not initialized",
        )
    return _desktop_service


def get_db() -> AsyncPostgresClient:
    """Dependency para injetar o DB."""
    if _db is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="DB not initialized",
        )
    return _db


def get_ai_service() -> AIService:
    """Dependency para injetar o AIService."""
    if _ai_service is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI service not initialized",
        )
    return _ai_service


def _mission_chat_status(status_value: str | None) -> str:
    if status_value == "awaiting_confirmation":
        return "pending"
    if status_value in {"awaiting_reply", "processing", "completed"}:
        return "approved"
    if status_value == "cancelled":
        return "cancelled"
    if status_value == "failed":
        return "rejected"
    return "pending"


def _strip_mission_marker(text: str) -> str:
    marker = "[MISSION:"
    start = text.find(marker)
    if start < 0:
        return text

    payload_start = start + len(marker)
    try:
        _, payload_len = json.JSONDecoder().raw_decode(text[payload_start:])
    except json.JSONDecodeError:
        return text

    end = payload_start + payload_len
    if end < len(text) and text[end] == "]":
        end += 1
    return f"{text[:start]}{text[end:]}".strip()


def _mission_payload_from_record(
    mission: Mission,
    metadata_mission: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata_mission = metadata_mission or {}
    mission_meta = mission.extra_metadata or {}
    next_action_params = mission.next_action_params or {}
    draft_text = (
        metadata_mission.get("draftText")
        or mission_meta.get("draft_text")
        or next_action_params.get("text")
        or ""
    )
    draft_messages = (
        metadata_mission.get("draftMessages")
        or mission_meta.get("draft_messages")
        or next_action_params.get("messages")
        or ([draft_text] if draft_text else [])
    )
    status_value = _mission_chat_status(mission.status)
    if (
        mission.status == "awaiting_confirmation"
        and metadata_mission.get("draftText")
        and mission_meta.get("draft_text")
        and metadata_mission.get("draftText") != mission_meta.get("draft_text")
    ):
        status_value = "approved"

    return {
        **metadata_mission,
        "missionId": str(mission.id),
        "contactLabel": (
            mission_meta.get("contact_label")
            or metadata_mission.get("contactLabel")
            or mission.title
        ),
        "contactJid": metadata_mission.get("contactJid") or mission.contact_jid,
        "contactPhone": (
            mission_meta.get("contact_phone_e164")
            or metadata_mission.get("contactPhone")
            or ""
        ),
        "draftText": draft_text,
        "draftMessages": draft_messages,
        "status": status_value,
        "backendStatus": mission.status,
        "nextActionType": mission.next_action_type,
        "nextActionParams": next_action_params,
        "resultSummary": mission.result_summary,
    }


def _coerce_legacy_mission_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    mission_value = metadata.get("mission")
    if isinstance(mission_value, dict):
        return mission_value
    if isinstance(mission_value, str):
        try:
            parsed = json.loads(mission_value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _serialize_chat_message(
    message: ConversationMessage,
    mission: Mission | None = None,
) -> dict[str, Any]:
    metadata = message.extra_metadata or {}
    content = _strip_mission_marker(message.content)
    mission_meta = mission.extra_metadata if mission else {}
    is_whatsapp_inbound = (
        metadata.get("message_type") == "whatsapp_inbound"
        or (
            mission is not None
            and message.role == "user"
            and bool(mission_meta.get("last_inbound_text"))
            and mission_meta.get("last_inbound_text") == content
        )
    )
    payload: dict[str, Any] = {
        "id": str(message.id),
        "role": message.role,
        "content": content,
        "timestamp": message.created_at.isoformat(),
        "messageType": "text",
    }

    if is_whatsapp_inbound:
        contact_label = (
            metadata.get("contact_label")
            or mission_meta.get("contact_label")
            or (mission.title if mission else "Contato")
        )
        payload["role"] = "assistant"
        payload["content"] = f'{contact_label} respondeu: "{content}"'
        payload["details"] = {
            "eventType": "whatsapp_inbound",
            "missionId": str(message.mission_id) if message.mission_id else None,
            "contactLabel": contact_label,
            "contactJid": metadata.get("contact_jid") or (mission.contact_jid if mission else None),
            "contactPhone": metadata.get("contact_phone_e164") or mission_meta.get("contact_phone_e164"),
            "inboundText": content,
        }
        return payload

    if metadata.get("message_type") == "mission_card" or metadata.get("mission"):
        metadata_mission = metadata.get("mission")
        if not isinstance(metadata_mission, dict):
            metadata_mission = _coerce_legacy_mission_metadata(metadata) or {}
        intro = metadata.get("intro") or content
        payload["content"] = intro
        payload["messageType"] = "mission"
        if mission:
            payload["mission"] = _mission_payload_from_record(mission, metadata_mission)
        elif metadata_mission:
            payload["mission"] = metadata_mission

    return payload


def _build_pending_reply_card(message: ConversationMessage, mission: Mission) -> dict[str, Any] | None:
    mission_meta = mission.extra_metadata or {}
    if mission.status != "awaiting_confirmation" or not mission_meta.get("pending_inbound_ack"):
        return None
    if mission_meta.get("last_inbound_text") != message.content:
        return None

    contact_label = mission_meta.get("contact_label") or mission.title
    return {
        "id": f"{message.id}:reply-card",
        "role": "assistant",
        "content": f"{contact_label} respondeu. Rascunho pronto, aguardando sua aprovação para enviar.",
        "timestamp": message.created_at.isoformat(),
        "messageType": "mission",
        "mission": _mission_payload_from_record(mission),
        "details": {
            "eventType": "whatsapp_reply_draft",
            "missionId": str(mission.id),
            "inboundText": message.content,
        },
    }


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


@router.post("/chat", response_model=dict)
async def handle_desktop_chat_message(
    request: DesktopChatMessage,
    ai_service: AIService = Depends(get_ai_service),
) -> dict[str, Any]:
    """Handle structured desktop chat messages.

    Mission commands are explicit UI events, so they are dispatched directly
    without calling IntentRouter. Natural user input stays on the normal
    execution path.
    """
    if request.type == MessageType.MISSION_COMMAND:
        if not request.command:
            raise HTTPException(status_code=400, detail="command é obrigatório para mission_command")
        if not request.mission_id:
            raise HTTPException(status_code=400, detail="mission_id é obrigatório para mission_command")
        if request.command == "edit" and not (request.edited_text or "").strip():
            raise HTTPException(status_code=400, detail="edited_text é obrigatório para command=edit")

        return await _execute_mission_command(
            MissionCommandPayload(
                command=request.command,
                mission_id=request.mission_id,
                edited_text=request.edited_text,
                user_id=request.user_id,
            ),
            source="desktop-chat",
        )

    if request.type == MessageType.SYSTEM_EVENT:
        raise HTTPException(status_code=400, detail="system_event não é suportado neste endpoint")

    if not (request.user_message or "").strip():
        raise HTTPException(status_code=400, detail="user_message é obrigatório para user_input")

    final_response: str | None = None
    final_session_id: str | None = str(request.session_id) if request.session_id else None
    iterations = 1
    metadata = {
        "client": "desktop-chat",
    }
    if request.provider:
        metadata["provider"] = request.provider
    if request.model:
        metadata["model"] = request.model

    async for resp in ai_service.process_user_input(
        user_message=request.user_message.strip(),
        session_id=request.session_id,
        user_id=request.user_id,
        service="desktop-chat",
        metadata=metadata,
    ):
        final_session_id = str(resp.session_id)
        if resp.is_final:
            final_response = resp.message
            iterations = resp.iterations

    return {
        "status": "completed",
        "final_response": final_response,
        "steps": [],
        "iterations": iterations,
        "session_id": final_session_id,
    }


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


@router.get("/session/{session_id}/history", response_model=dict)
async def get_persistent_session_history(
    session_id: UUID,
    limit: int = Query(default=100, ge=1, le=300),
    db: AsyncPostgresClient = Depends(get_db),
) -> dict[str, Any]:
    """Retorna histórico persistente da sessão, incluindo cards de missão."""
    messages = await db.fetch_session_messages(
        session_id=session_id,
        limit=limit,
        roles=["user", "assistant"],
        exclude_tool_calls=True,
    )

    serialized: list[dict[str, Any]] = []
    mission_cache: dict[UUID, Mission | None] = {}
    for message in messages:
        mission = None
        if message.mission_id:
            if message.mission_id not in mission_cache:
                mission_cache[message.mission_id] = await db.get_mission(message.mission_id)
            mission = mission_cache[message.mission_id]
        serialized.append(_serialize_chat_message(message, mission))
        mission_meta = mission.extra_metadata if mission else {}
        is_whatsapp_inbound = (
            (message.extra_metadata or {}).get("message_type") == "whatsapp_inbound"
            or (
                mission is not None
                and message.role == "user"
                and bool(mission_meta.get("last_inbound_text"))
                and mission_meta.get("last_inbound_text") == message.content
            )
        )
        if (
            mission
            and is_whatsapp_inbound
        ):
            pending_reply_card = _build_pending_reply_card(message, mission)
            if pending_reply_card:
                serialized.append(pending_reply_card)

    return {"session_id": str(session_id), "messages": serialized}


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
