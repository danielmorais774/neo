"""Desktop routes - endpoints da API para o módulo desktop.

**Autenticação (docs/api-auth-plan.md).** Este router é montado com
``dependencies=[Depends(require_owner)]`` em ``api/main.py``: rota nova aqui nasce
pedindo credencial com dono. O ``user_id`` que chega no corpo/query é **advisório**
— o que vale é ``principal.user_id``, e ``owner_of`` recusa (403) quando o pedido
declara outro dono. Nos degraus ``off``/``shadow`` nada disso barra: o declarado
continua valendo e a divergência só aparece no log.
"""

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import Uuid as SAUuid, bindparam
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
from api.auth import Principal, owner_of, require_owner, require_principal
from api.routes.missions import _execute_mission_command
from db.async_client import AsyncPostgresClient
from db.models import ConversationMessage, Mission
from modules.desktop import AIService, AIServiceResponse, DesktopService
from modules.desktop.ai_service import CYCLE_PROGRESS_MARKER
from core.claude_code.balao import tarefas_da_mensagem
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
    # O botão do processo do Claude no balão: as tarefas que esta resposta abriu
    # (do tool_trace salvo — o mesmo que ao vivo vai no AgentResponse.jobs) ou a
    # tarefa cujo resultado ela entrega.
    jobs_payload = tarefas_da_mensagem(metadata)
    if jobs_payload:
        payload["jobs"] = jobs_payload

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


async def _sessao_do_dono(
    db: AsyncPostgresClient,
    session_id: UUID,
    principal: Principal,
    request: Request,
):
    """Confere que a sessão é do dono da credencial. Devolve a sessão (ou None).

    **Sessão com ``user_id`` NULL passa.** A coluna é anulável e existe conversa
    antiga de antes de qualquer auth: barrá-las faria o histórico dele desaparecer
    do seletor de sessões, que é pior do que o risco real (uma conta só com dado).
    O backfill é `scripts/backfill_session_owner.py`; depois dele, isto pode
    endurecer.
    """
    sessao = await db.get_conversation_session(session_id)
    if sessao is None:
        return None
    dono = getattr(sessao, "user_id", None)
    if dono is None or principal.user_id is None or dono == principal.user_id:
        return sessao

    from api.auth.principal import MODE_OFF, MODE_SHADOW, auth_mode

    if auth_mode() in (MODE_OFF, MODE_SHADOW):
        logger.warning(
            "[auth:sombra] %s %s: sessão %s é do usuário %s, não de %s",
            request.method, request.url.path, session_id, dono, principal.user_id,
        )
        return sessao
    raise HTTPException(status_code=403, detail="esta sessão não é sua")


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
    http_request: Request,
    ai_service: AIService = Depends(get_ai_service),
    principal: Principal = Depends(require_owner),
) -> dict[str, Any]:
    """Handle structured desktop chat messages.

    Mission commands are explicit UI events, so they are dispatched directly
    without calling IntentRouter. Natural user input stays on the normal
    execution path.
    """
    # O dono sai da CREDENCIAL. O `user_id` do corpo é advisório (e em enforce,
    # declarar outro é 403) — era por ele que um pedido se dizia dono da memória
    # de qualquer um. Ver docs/api-auth-plan.md §3.1.
    dono = owner_of(principal, request.user_id, request=http_request)

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
                user_id=dono,
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
    if request.pasted_chars:
        metadata["pasted_chars"] = str(request.pasted_chars)

    async for resp in ai_service.process_user_input(
        user_message=request.user_message.strip(),
        session_id=request.session_id,
        user_id=dono,
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
            ai_service.db, dono, getattr(ai_service, "settings", None)
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
    http_request: Request,
    ai_service: AIService = Depends(get_ai_service),
    principal: Principal = Depends(require_owner),
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
    if request.pasted_chars:
        metadata["pasted_chars"] = str(request.pasted_chars)

    resp_iter = ai_service.process_user_input(
        user_message=request.user_message.strip(),
        session_id=request.session_id,
        user_id=owner_of(principal, request.user_id, request=http_request),
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
    http_request: Request,
    ai_service: AIService = Depends(get_ai_service),
    principal: Principal = Depends(require_owner),
    db: AsyncPostgresClient = Depends(get_db),
) -> StreamingResponse:
    """Continuação de um turno pausado por `command_request`. Aprovar não executa
    nada pelo painel (sem acesso a shell local) — o turno fecha informando isso;
    rejeitar fecha o turno normalmente. Ver módulo `_stream_ai_events` acima.

    **Amarrado ao dono.** Esta rota aprovava pendência de QUALQUER sessão: era o
    caminho mais curto para alguém que alcançasse a :8000 transformar um comando
    pendente em execução na máquina dele. Agora a sessão tem que ser sua.
    """
    await _sessao_do_dono(db, request.session_id, principal, http_request)

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


# O inventário de ferramentas é mapa para quem ataca ("o que essa máquina sabe
# fazer"). Herda o `require_owner` do router: não é dado pessoal, mas também não
# é cartaz de rua, e uma dependência mais frouxa por rota não afrouxaria o router.
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


@router.get("/model-catalog", response_model=dict)
async def get_model_catalog() -> dict:
    """Vitrine do seletor de provider/modelo do chat (desktop e painel).

    Antes cada cliente tinha o seu enum copiado à mão e eles já divergiam do core.
    Agora a lista vem do catálogo (core/llm/catalog.py, editado no painel em
    Configurações → Providers): só provider ativo, COM chave e com modelo marcado
    para o seletor. ``default`` é o par que o cliente usa quando não tem escolha
    salva — o cérebro da aba Modelos (slot ``cognitive``), que é o que o core
    usaria se o cliente não mandasse nada.
    """
    from core.llm.catalog import selector_payload
    from modules.model_config.store import get_model_config

    if _ai_service is None:
        raise HTTPException(status_code=503, detail="AI service not initialized")
    llm = _ai_service.llm
    brain = {"provider": llm.settings.default_provider, "model": None}
    try:
        brain = await get_model_config(_db, "cognitive")
    except Exception as e:  # pragma: no cover - defensivo: a vitrine não depende disso
        logger.warning("[model-catalog] slot cognitive ilegível: %s", e)
    return selector_payload(llm.list_available_providers(), brain["provider"], brain["model"])


@router.get("/sessions", response_model=dict)
async def list_sessions(
    service_name: str = Query(default="neo-desktop-tauri", alias="service"),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncPostgresClient = Depends(get_db),
    principal: Principal = Depends(require_owner),
) -> dict[str, Any]:
    """Lista sessões anteriores (mais recentes primeiro) para o seletor de sessões.

    `topic_summary` é quase sempre NULL na prática, então o título exibido vem da
    primeira mensagem do usuário da sessão. Sessões sem mensagem nenhuma são
    omitidas: o cliente cria a sessão ao abrir o app, então listá-las encheria a
    lista de conversas vazias.

    **Escopo por dono** (docs/api-auth-plan.md F4). Antes o filtro era só
    `service` — qualquer um que alcançasse a rota listava as conversas de todos.
    Sessão com `user_id` NULL continua aparecendo: a coluna é anulável e há
    conversa antiga de antes da auth; esconder o histórico dele seria pior do que
    o risco de uma conta só. `scripts/backfill_session_owner.py` resolve a NULL,
    e depois o `OR ... IS NULL` pode cair.
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
           AND (:owner IS NULL OR s.user_id = :owner OR s.user_id IS NULL)
         ORDER BY s.last_activity_at DESC
         LIMIT :limit
        """
    ).bindparams(bindparam("owner", type_=SAUuid))

    async with db.get_session() as session:
        rows = (
            await session.execute(
                stmt,
                {"service": service_name, "limit": limit, "owner": principal.user_id},
            )
        ).all()

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
    http_request: Request,
    service: DesktopService = Depends(get_desktop_service),
    db: AsyncPostgresClient = Depends(get_db),
    principal: Principal = Depends(require_owner),
) -> SessionHistoryResponse:
    """Retorna histórico de uma sessão (cópia em memória do DesktopService)."""
    await _sessao_do_dono(db, session_id, principal, http_request)
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
    http_request: Request,
    limit: int = Query(default=100, ge=1, le=300),
    db: AsyncPostgresClient = Depends(get_db),
    principal: Principal = Depends(require_owner),
) -> dict[str, Any]:
    """Retorna histórico persistente da sessão, incluindo cards de missão.

    Confere o dono antes de ler: era a rota que entregava a conversa inteira de
    qualquer sessão a quem soubesse (ou sorteasse) um UUID.
    """
    await _sessao_do_dono(db, session_id, principal, http_request)
    messages = await db.fetch_session_messages(
        session_id=session_id,
        limit=limit,
        roles=["user", "assistant"],
        exclude_tool_calls=True,
    )

    serialized: list[dict[str, Any]] = []
    mission_cache: dict[UUID, Mission | None] = {}
    for message in messages:
        # O texto que a agenda ou a continuação de um job manda ao turno é do
        # sistema, não do Daniel: ao vivo o desktop nunca o mostrou, e reabrir a
        # conversa o desenhava como bolha dele — 6.000 caracteres de
        # "[Continuação automática…]" (session-bd44cc03). O cérebro continua
        # vendo-o: o histórico do turno não passa por aqui.
        if message.role == "user" and (message.extra_metadata or {}).get("source") == "schedule":
            continue
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
    http_request: Request,
    service: DesktopService = Depends(get_desktop_service),
    db: AsyncPostgresClient = Depends(get_db),
    principal: Principal = Depends(require_owner),
) -> dict[str, str]:
    """Limpa uma sessão (apagar é irreversível — o dono é conferido antes)."""
    await _sessao_do_dono(db, session_id, principal, http_request)
    deleted = service.clear_session(session_id)

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    return {"message": "Session cleared successfully"}


# Havia aqui um SEGUNDO `@router.get("/sessions")` (sessões ativas em memória).
# O FastAPI casa a primeira rota registrada, então ele nunca respondeu nada desde
# que o seletor de sessões chegou — código morto removido em 2026-09-25.


# ============ MEMÓRIA DE TRABALHO (leitura) ============
#
# O caderno do Neo (docs/working-memory-plan.md) nasceu SEM janela: até aqui a
# única forma de ver o que ele anota era `psql` na VPS ou pedir `wm_read` no
# chat — e pedir ao modelo o que ele escreveu é a pior fonte possível, porque a
# resposta passa pelo próprio componente sob suspeita. Estes dois endpoints são
# o visor read-only do desktop.
#
# Por que consultar a TABELA e não `WorkingMemoryStore.index()`:
#
# * `index()` devolve o que cabe no PROMPT — com a quota de notas automáticas
#   (§ docstring de `index`). Quem abre o visor quer a verdade do banco,
#   inclusive as notas que a quota deixou de fora. Foi olhando justamente essa
#   diferença que se mediu o desequilíbrio 36 auto × 3 model;
# * o store só existe quando `WORKING_MEMORY_ENABLED=true`. Nota escrita antes
#   de a flag cair continua no banco, e um visor que some com a flag esconde
#   exatamente o histórico que se quer auditar.
#
# Read-only de propósito: nada aqui escreve. `pinned`/`status` mudam o que entra
# no contexto do Neo, então mexer neles é decisão de outro turno, não efeito
# colateral de abrir uma tela.


class WorkingNoteSummary(BaseModel):
    """Uma linha da lista. Sem `body` — o corpo é caro e vem sob demanda."""

    slug: str
    title: str
    summary: str
    status: str
    origin: str
    pinned: bool
    entry_count: int
    links: list[str]
    #: ISO-8601 SEM timezone — o core grava `datetime.utcnow()`. O front humaniza
    #: tratando como UTC, igual ao seletor de sessões (ver SessionInfo no Rust).
    created_at: str | None
    updated_at: str | None
    expires_at: str | None
    session_id: str | None


class WorkingNoteDetail(WorkingNoteSummary):
    """A nota inteira. `body` é markdown pronto para render."""

    body: str


def _serialize_working_note(note: Any) -> dict[str, Any]:
    def stamp(value: Any) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "slug": note.slug,
        "title": note.title,
        "summary": note.summary or "",
        "status": note.status,
        "origin": note.origin,
        "pinned": bool(note.pinned),
        "entry_count": note.entry_count,
        # `links` é JSONB: já volta como lista, mas nota antiga pode ter NULL.
        "links": [str(item) for item in (note.links or [])],
        "created_at": stamp(note.created_at),
        "updated_at": stamp(note.updated_at),
        "expires_at": stamp(note.expires_at),
        "session_id": str(note.session_id) if note.session_id else None,
        "body": note.body or "",
    }


async def _resolve_working_memory_user(
    db: AsyncPostgresClient,
    user_id: str | None,
    principal: Principal | None = None,
    request: Request | None = None,
) -> UUID | None:
    """A CREDENCIAL ganha; sem ela, o dono do CADERNO — não "o único usuário".

    A ordem mudou em 2026-09-25 (docs/api-auth-plan.md F4): antes o `user_id` do
    query param vinha primeiro, então bastava declarar o UUID de alguém para ler
    o caderno dele. Agora, autenticado, quem manda é o dono do token; o param só
    sobrevive para os degraus `off`/`shadow`, onde `owner_of` o deixa passar.

    A política de `events.py`/`datasets.py` ("se houver exatamente um usuário,
    é ele") não serve aqui: a base de produção tem 5 linhas em `user`, das
    quais 4 são resíduo de seed/teste (nenhuma sessão, nenhuma nota). Aquela
    regra devolvia 400 e o visor nascia inútil no único ambiente que importa.

    Aqui a pergunta é mais simples e não depende de faxina no cadastro: de quem
    é o caderno? Quem tem nota é o dono; havendo empate improvável, ganha quem
    escreveu por último. Sem nota nenhuma, devolve ``None`` e a tela diz
    "caderno vazio" — que é a verdade — em vez de um erro que o usuário leria
    como desktop quebrado.
    """
    if principal is not None:
        dono = owner_of(principal, user_id, request=request)
        if dono is not None:
            return dono
    elif user_id:
        try:
            return UUID(user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="user_id inválido")

    from sqlalchemy import func
    from db.models.working_note import WorkingNote
    from sqlmodel import select

    statement = (
        select(WorkingNote.user_id)
        .where(WorkingNote.user_id.is_not(None))
        .group_by(WorkingNote.user_id)
        .order_by(func.count().desc(), func.max(WorkingNote.updated_at).desc())
        .limit(1)
    )
    async with db.get_session() as session:
        owner = (await session.execute(statement)).scalars().first()
    if owner is not None:
        return owner

    # Caderno vazio: cai para o cadastro só quando ele é inequívoco.
    user_ids = await db.list_user_ids()
    return user_ids[0] if len(user_ids) == 1 else None


@router.get("/working-memory/notes")
async def list_working_notes(
    http_request: Request,
    user_id: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    db: AsyncPostgresClient = Depends(get_db),
    principal: Principal = Depends(require_owner),
) -> dict[str, Any]:
    """Todas as notas do caderno, ativas primeiro e mais recentes no topo.

    Arquivadas (`status='done'`) vêm no mesmo pacote, marcadas — sumir com elas
    esconderia o caderno de ontem, que é metade do valor de ter um visor.
    """
    from db.models.working_note import WorkingNote
    from sqlmodel import select

    user = await _resolve_working_memory_user(db, user_id, principal, http_request)
    if user is None:
        return {"notes": [], "count": 0, "counts": {}, "user_id": None}

    statement = (
        select(WorkingNote)
        .where(WorkingNote.user_id == user)
        # Ativas antes das arquivadas; fixadas no topo de cada grupo; depois a
        # mais recentemente TOCADA (`updated_at`), que é a ordem em que o
        # trabalho de fato aconteceu.
        .order_by(
            (WorkingNote.status != "active"),
            WorkingNote.pinned.desc(),
            WorkingNote.updated_at.desc(),
        )
        .limit(limit)
    )

    async with db.get_session() as session:
        notes = list((await session.execute(statement)).scalars().all())

    serialized = []
    counts: dict[str, int] = {}
    for note in notes:
        payload = _serialize_working_note(note)
        # A lista não carrega corpo: com 36 notas isso seria centenas de KB por
        # abertura de tela, e o corpo só é lido quando se clica em uma.
        payload.pop("body", None)
        serialized.append(payload)
        key = f"{note.origin}/{note.status}"
        counts[key] = counts.get(key, 0) + 1

    return {
        "notes": serialized,
        "count": len(serialized),
        # `origin/status` agregado: é o número que revela o backstop escrevendo
        # no lugar do modelo (auto × model) sem precisar contar na mão.
        "counts": counts,
        "user_id": str(user),
    }


@router.get("/working-memory/notes/{slug}")
async def read_working_note(
    http_request: Request,
    slug: str,
    user_id: str | None = Query(default=None),
    db: AsyncPostgresClient = Depends(get_db),
    principal: Principal = Depends(require_owner),
) -> dict[str, Any]:
    """O markdown inteiro de uma nota.

    Sem a truncagem do `wm_read` (`render_tail` corta as entradas antigas para
    caber no contexto do modelo): aqui quem lê é uma pessoa numa tela com
    scroll, e ver só a cauda é justamente o que impede de auditar o caderno.
    """
    from db.models.working_note import WorkingNote
    from sqlmodel import select

    user = await _resolve_working_memory_user(db, user_id, principal, http_request)
    if user is None:
        raise HTTPException(status_code=404, detail=f"Nota '{slug}' não encontrada")

    statement = select(WorkingNote).where(
        WorkingNote.user_id == user, WorkingNote.slug == slug
    )
    async with db.get_session() as session:
        note = (await session.execute(statement)).scalars().first()

    if note is None:
        raise HTTPException(status_code=404, detail=f"Nota '{slug}' não encontrada")

    return _serialize_working_note(note)


# ============ TAREFAS EM BACKGROUND (visor + cancelar) ============
#
# O caderno ganhou janela; os jobs não tinham nenhuma. Agora um comando longo vira
# job sozinho (WorkbenchExecutor._bash) — e trabalho que roda longe da conversa sem
# um lugar para olhar é trabalho que o usuário acha que travou. Estas rotas são a
# fonte da janela "Tarefas" do desktop: mesmo router e mesma resolução de dono do
# caderno (sem JWT, `get_db` já ligado, zero mudança no `main.py`).
#
# Um único gesto de escrita, e deliberado: cancelar. Mesma regra da tool
# `job_cancel` (`core.jobs.executor.cancel_job`) — num sandbox_task, cancelar é o
# que mata o processo no container.

_JOB_ACTIVE_STATUSES = ("queued", "running")


def _job_detail_text(spec: dict[str, Any]) -> str:
    """O que foi PEDIDO ao job, qualquer que seja o tipo."""
    acao = spec.get("action") if isinstance(spec.get("action"), dict) else {}
    return str(
        spec.get("command") or spec.get("task") or spec.get("goal")
        or spec.get("topic") or acao.get("texto") or ""
    )


def _serialize_job(job: Any, *, full: bool = False) -> dict[str, Any]:
    from datetime import datetime as _datetime

    def stamp(value: Any) -> str | None:
        return value.isoformat() if value is not None else None

    spec = job.spec_json or {}
    duracao = None
    if job.started_at is not None:
        fim = job.finished_at or _datetime.utcnow()
        duracao = max(0, int((fim - job.started_at).total_seconds()))
    detalhe = _job_detail_text(spec)
    payload: dict[str, Any] = {
        "id": str(job.id),
        "kind": job.kind,
        "title": job.title,
        "status": job.status,
        "notify_policy": job.notify_policy,
        "session_id": str(job.session_id) if job.session_id else None,
        # ISO-8601 SEM timezone (o core grava `utcnow()`), igual ao caderno.
        "created_at": stamp(job.created_at),
        "started_at": stamp(job.started_at),
        "finished_at": stamp(job.finished_at),
        "heartbeat_at": stamp(job.heartbeat_at),
        "duration_s": duracao,
        "exit_code": job.exit_code,
        "detail": detalhe if full else detalhe[:300],
        "preview": (job.result_summary or job.progress_note or "")[:280],
        "adopted": bool(spec.get("adopted")),
        "has_then": bool(spec.get("then")),
        "continuation_of": spec.get("continuation_of"),
        "has_log": bool(job.log_path),
    }
    retomar = _claude_retomar(job)
    if retomar:
        payload["claude"] = retomar
    if full:
        payload.update({
            "result_summary": job.result_summary or "",
            "progress_note": job.progress_note or "",
            "then": str(spec.get("then") or ""),
            "log_path": job.log_path,
            "spec": spec,
        })
    return payload


def _claude_retomar(job: Any) -> dict[str, Any] | None:
    """Pasta e sessão de uma tarefa do Claude Code — o botão "Terminal" do desktop.

    Com isso o desktop abre o Terminal do Mac em `cd <pasta> && claude --resume
    <sessão>`. `rodando`: a tarefa ainda está com o Neo (numa rodada ou entre
    duas). Duas cópias do Claude escrevendo na mesma sessão se atropelam, então
    o botão espera ela parar.
    O desktop confere tudo de novo antes de abrir (claude_cli.rs).
    """
    if job.kind != "claude_code":
        return None
    estado = (job.spec_json or {}).get("claude") or {}
    final = (job.extra_metadata or {}).get("claude") or {}
    sessao = str(final.get("session_id") or estado.get("session_id") or "")
    pasta = str(final.get("cwd") or estado.get("cwd") or "")
    if not sessao or not pasta or not (final or estado.get("sessao_iniciada")):
        return None
    return {
        "session_id": sessao,
        "cwd": pasta,
        "rodando": job.status in ("queued", "running"),
    }


async def _owned_job_or_404(
    db: AsyncPostgresClient,
    job_id: str,
    user_id: str | None,
    principal: Principal | None = None,
    request: Request | None = None,
) -> Any:
    try:
        uid = UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    job = await db.get_agent_job(uid)
    owner = await _resolve_working_memory_user(db, user_id, principal, request)
    # Job sem dono (agenda antiga, teste) é global, como na tool `job_status`.
    if job is None or (owner is not None and job.user_id is not None and job.user_id != owner):
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    return job


@router.get("/jobs")
async def list_jobs(
    http_request: Request,
    status_filter: str = Query(default="all", alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    user_id: str | None = Query(default=None),
    db: AsyncPostgresClient = Depends(get_db),
    principal: Principal = Depends(require_owner),
) -> dict[str, Any]:
    """Tarefas do dono do caderno, ativas primeiro e mais recentes no topo."""
    from sqlalchemy import case, func, or_
    from sqlmodel import select

    from db.models.agent_job import AgentJob

    owner = await _resolve_working_memory_user(db, user_id, principal, http_request)
    escopo = [] if owner is None else [or_(AgentJob.user_id == owner, AgentJob.user_id.is_(None))]
    statement = select(AgentJob).where(*escopo)
    if status_filter == "active":
        statement = statement.where(AgentJob.status.in_(_JOB_ACTIVE_STATUSES))
    statement = statement.order_by(
        case((AgentJob.status.in_(_JOB_ACTIVE_STATUSES), 0), else_=1),
        AgentJob.created_at.desc(),
    ).limit(limit)
    contagem = select(AgentJob.status, func.count()).where(*escopo).group_by(AgentJob.status)

    async with db.get_session() as session:
        jobs = list((await session.execute(statement)).scalars().all())
        counts = {str(s): int(n) for s, n in (await session.execute(contagem)).all()}

    return {
        "jobs": [_serialize_job(job) for job in jobs],
        "count": len(jobs),
        "counts": counts,
        "active": sum(counts.get(s, 0) for s in _JOB_ACTIVE_STATUSES),
        "user_id": str(owner) if owner else None,
    }


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    http_request: Request,
    user_id: str | None = Query(default=None),
    db: AsyncPostgresClient = Depends(get_db),
    principal: Principal = Depends(require_owner),
) -> dict[str, Any]:
    job = await _owned_job_or_404(db, job_id, user_id, principal, http_request)
    return _serialize_job(job, full=True)


@router.get("/jobs/{job_id}/logs")
async def get_job_logs(
    job_id: str,
    request: Request,
    tail: int = Query(default=16000, ge=500, le=200000),
    user_id: str | None = Query(default=None),
    db: AsyncPostgresClient = Depends(get_db),
    principal: Principal = Depends(require_owner),
) -> dict[str, Any]:
    """O FIM do log — onde quer que ele more (sandbox, diário local ou resumo)."""
    from core.jobs.executor import read_job_logs

    job = await _owned_job_or_404(db, job_id, user_id, principal, request)
    sandbox = getattr(request.app.state, "workbench_sandbox", None)
    try:
        lido = await read_job_logs(job, sandbox, max_bytes=tail)
    except Exception as exc:  # noqa: BLE001 — log é diagnóstico, a tela segue de pé
        logger.warning("[jobs] log do job %s indisponível: %s", job.id, exc)
        lido = {
            "logs": job.result_summary or job.progress_note or "",
            "truncated": False,
            "source": "resumo",
            "error": str(exc),
        }
    return {"job_id": str(job.id), "status": job.status, **lido}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job_route(
    job_id: str,
    request: Request,
    user_id: str | None = Query(default=None),
    db: AsyncPostgresClient = Depends(get_db),
    principal: Principal = Depends(require_owner),
) -> dict[str, Any]:
    from core.jobs.executor import cancel_job

    job = await _owned_job_or_404(db, job_id, user_id, principal, request)
    return await cancel_job(db, getattr(request.app.state, "job_manager", None), job)
