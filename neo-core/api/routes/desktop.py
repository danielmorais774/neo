"""Desktop routes - endpoints da API para o módulo desktop."""

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

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
from modules.desktop import AIService, AIServiceResponse, DesktopService
from modules.desktop.ai_service import CYCLE_PROGRESS_MARKER
from shared import NeoError
from shared.types import MessageType, MissionCommandPayload

logger = logging.getLogger(__name__)

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


def _files_payload(files: Any) -> list[dict[str, Any]] | None:
    """``[{file_id, name, size_bytes}]`` (interno) → ``[{id, name, sizeBytes}]``
    (wire, mesmo shape do `AttachmentRef` do painel) — chip de download na
    mensagem para o arquivo que o Neo salvou no catálogo neste turno."""
    if not files:
        return None
    return [
        {"id": f.get("file_id"), "name": f.get("name"), "sizeBytes": f.get("size_bytes")}
        for f in files
        if f.get("file_id")
    ] or None


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
    files_payload = _files_payload(metadata.get("files"))
    if files_payload:
        payload["files"] = files_payload

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
    final_voice: dict[str, Any] | None = None
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

    # Pattern A: carry the active TTS voice with the reply (so an HTTP renderer can
    # speak it). The gRPC path stamps resp.voice; here we resolve it once for HTTP.
    try:
        from modules.voice import get_active_voice
        final_voice = await get_active_voice(
            ai_service.db, request.user_id, getattr(ai_service, "settings", None)
        )
    except Exception:  # pragma: no cover - defensive
        final_voice = None

    return {
        "status": "completed",
        "final_response": final_response,
        "steps": [],
        "iterations": iterations,
        "session_id": final_session_id,
        "voice": final_voice,
    }


# ============ CHAT STREAMING (NDJSON) ============
#
# O gRPC ExecuteTask (grpc_server.py) é o canal "rico" que o neo-desktop-tauri
# usa: stream bidirecional emitindo thinking/plan_update/command_request/
# complete em tempo real. Não existe grpc-web configurado, então um cliente de
# navegador (neo-panel) não alcança esse canal. Estes dois endpoints replicam a
# MESMA leitura de AIServiceResponse (process_user_input/process_command_approval)
# em NDJSON (uma linha JSON `{"type", "data"}` por evento) via HTTP/fetch — sem
# precisar de proxy grpc-web. Ver neo-panel/src/lib/chatStream.ts no client.
#
# tool_request (execução de mídia/shell no lado do cliente) é OMITIDO de
# propósito: exige acesso ao SO local, que só o neo-desktop-tauri tem. O
# command_request (aprovação de comando de shell) é mostrado no chat, mas só o
# "Rejeitar" fecha o turno de fato — a execução remota fica para depois.


def _sse_line(event_type: str, data: dict[str, Any]) -> str:
    return json.dumps({"type": event_type, "data": data}, ensure_ascii=False) + "\n"


async def _stream_ai_events(
    resp_iter: AsyncIterator[AIServiceResponse],
    session_id_hint: str,
) -> AsyncIterator[str]:
    """AIServiceResponse → NDJSON chat events. Mirrors grpc_server.py's
    `_to_core_message` + `ai_agent.rs`'s `core_message_to_event`, minus the
    client-side tool/media plumbing that requires local OS access."""
    start = time.monotonic()
    active_session_id = session_id_hint
    final_message: str | None = None
    final_iterations = 1
    final_files: list[dict[str, Any]] | None = None
    got_final = False

    try:
        async for resp in resp_iter:
            sid = str(resp.session_id) if resp.session_id else active_session_id
            if sid and sid != active_session_id:
                previous = active_session_id
                active_session_id = sid
                yield _sse_line(
                    "session_changed",
                    {"sessionId": sid, "previousSessionId": previous},
                )

            metadata = resp.metadata or {}
            plan_update = metadata.get("plan_update")
            if plan_update:
                yield _sse_line(
                    "plan_update",
                    {
                        "sessionId": active_session_id,
                        "planId": plan_update.get("plan_id", ""),
                        "goal": plan_update.get("goal", ""),
                        "currentStep": plan_update.get("current_step", 0),
                        "status": plan_update.get("status", "active"),
                        "parentPlanId": plan_update.get("parent_plan_id", ""),
                        "steps": [
                            {
                                "id": s.get("id", ""),
                                "index": s.get("index", 0),
                                "description": s.get("description", ""),
                                "toolName": s.get("tool_name", ""),
                                "status": s.get("status", "pending"),
                                "resultSummary": s.get("result_summary", ""),
                                "error": s.get("error", ""),
                            }
                            for s in plan_update.get("steps", [])
                        ],
                    },
                )
                continue

            if resp.command_request:
                cr = resp.command_request
                yield _sse_line(
                    "command_request",
                    {
                        "sessionId": active_session_id,
                        "id": cr.get("command_id", ""),
                        "cmd": cr.get("command", ""),
                        "command": cr.get("command", ""),
                        "explanation": cr.get("explanation", ""),
                        "isBackground": cr.get("is_background", False),
                        "status": "pending",
                    },
                )
                continue

            if resp.tool_request:
                continue

            if resp.is_final and resp.message:
                got_final = True
                final_message = resp.message
                final_iterations = resp.iterations
                final_files = _files_payload(metadata.get("files"))
                continue

            if resp.message and not resp.is_final and metadata.get("persist_reply"):
                assistant_payload: dict[str, Any] = {"sessionId": active_session_id, "content": resp.message}
                files_payload = _files_payload(metadata.get("files"))
                if files_payload:
                    assistant_payload["files"] = files_payload
                yield _sse_line("assistant_message", assistant_payload)
                continue

            if resp.message and not resp.is_final:
                # Um passo de autocontinuação se anuncia com CYCLE_PROGRESS_MARKER.
                # O cliente Rust já tira o prefixo e troca o rótulo pra "Executando";
                # aqui não tirava, e o painel mostrava "[[CYCLE]]Continuando (4/8): …"
                # cru na tela — visto no primeiro ciclo que rodou de ponta a ponta.
                is_cycle = resp.message.startswith(CYCLE_PROGRESS_MARKER)
                yield _sse_line(
                    "thinking",
                    {
                        "sessionId": active_session_id,
                        "type": "executing" if is_cycle else "processing",
                        "content": resp.message[len(CYCLE_PROGRESS_MARKER):] if is_cycle else resp.message,
                        "iteration": resp.iterations,
                        "cycle": is_cycle,
                    },
                )
    except Exception:
        logger.error("[desktop-chat-stream] error", exc_info=True)
        yield _sse_line(
            "error",
            {
                "message": "Não foi possível concluir a execução: ocorreu um erro interno. "
                "Pode tentar novamente?"
            },
        )
        return

    if got_final:
        complete_payload: dict[str, Any] = {
            "success": True,
            "message": final_message or "Tarefa concluída!",
            "iterations": final_iterations,
            "durationMs": int((time.monotonic() - start) * 1000),
            "sessionId": active_session_id,
        }
        if final_files:
            complete_payload["files"] = final_files
        yield _sse_line("complete", complete_payload)


@router.post("/chat/stream")
async def handle_desktop_chat_stream(
    request: DesktopChatMessage,
    ai_service: AIService = Depends(get_ai_service),
) -> StreamingResponse:
    """Mesma entrada do `/chat`, mas em streaming NDJSON (thinking/plan/command/
    complete em tempo real) — ver comentário de módulo acima."""
    if not (request.user_message or "").strip():
        raise HTTPException(status_code=400, detail="user_message é obrigatório")

    metadata: dict[str, Any] = {"client": "neo-panel"}
    if request.provider:
        metadata["provider"] = request.provider
    if request.model:
        metadata["model"] = request.model
    if request.attachments:
        metadata["attachments"] = request.attachments

    resp_iter = ai_service.process_user_input(
        user_message=request.user_message.strip(),
        session_id=request.session_id,
        user_id=request.user_id,
        service="neo-panel",
        metadata=metadata,
    )
    session_id_hint = str(request.session_id) if request.session_id else ""
    return StreamingResponse(
        _stream_ai_events(resp_iter, session_id_hint),
        media_type="application/x-ndjson",
    )


class DesktopCommandResponseMessage(BaseModel):
    """Aprovação/rejeição de um `command_request` recebido no `/chat/stream`."""

    session_id: UUID
    command_id: str
    command: str
    approved: bool
    user_message: str = ""


@router.post("/chat/command-response")
async def handle_desktop_command_response(
    request: DesktopCommandResponseMessage,
    ai_service: AIService = Depends(get_ai_service),
) -> StreamingResponse:
    """Continuação de um turno pausado por `command_request`. Aprovar não executa
    nada pelo painel (sem acesso a shell local) — o turno fecha informando isso;
    rejeitar fecha o turno normalmente. Ver módulo `_stream_ai_events` acima."""
    resp_iter = ai_service.process_command_approval(
        session_id=request.session_id,
        command_id=request.command_id,
        approved=request.approved,
        command=request.command,
        user_message=request.user_message,
    )
    return StreamingResponse(
        _stream_ai_events(resp_iter, str(request.session_id)),
        media_type="application/x-ndjson",
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


@router.get("/sessions", response_model=dict)
async def list_sessions(
    service_name: str = Query(default="neo-desktop-tauri", alias="service"),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncPostgresClient = Depends(get_db),
) -> dict[str, Any]:
    """Lista sessões anteriores (mais recentes primeiro) para o seletor de sessões.

    `topic_summary` é quase sempre NULL na prática, então o título exibido vem da
    primeira mensagem do usuário da sessão. Sessões sem mensagem nenhuma são
    omitidas: o cliente cria a sessão ao abrir o app, então listá-las encheria a
    lista de conversas vazias.
    """
    from sqlalchemy import text

    stmt = text(
        """
        SELECT s.id,
               s.message_count,
               s.last_activity_at,
               COALESCE(
                   s.topic_summary,
                   (SELECT m.content
                      FROM conversation_message m
                     WHERE m.session_id = s.id
                       AND m.role = 'user'
                       AND m.content <> ''
                     ORDER BY m.created_at ASC
                     LIMIT 1)
               ) AS title
          FROM conversation_session s
         WHERE s.service = :service
           AND s.message_count > 0
         ORDER BY s.last_activity_at DESC
         LIMIT :limit
        """
    )

    async with db.get_session() as session:
        rows = (await session.execute(stmt, {"service": service_name, "limit": limit})).all()

    sessions = [
        {
            "session_id": str(row.id),
            "title": (row.title or "").strip()[:80] or None,
            "message_count": row.message_count,
            "last_activity_at": row.last_activity_at.isoformat() if row.last_activity_at else None,
        }
        for row in rows
    ]

    return {"sessions": sessions, "count": len(sessions)}


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
