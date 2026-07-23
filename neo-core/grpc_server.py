"""
gRPC Server — thin controller for Neo Desktop communication.

This is ONLY a transport layer. All AI logic lives in AIService.
Responsibilities:
  - Receive protobuf messages from neo-desktop
  - Delegate to AIService
  - Convert AIServiceResponse → CoreMessage protobuf
  - Send back to desktop

NOTE: After editing proto/neo_desktop.proto, regenerate with:
  protoc --python_out=. --grpc_python_out=. proto/neo_desktop.proto
"""

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator
from uuid import UUID

import grpc
from grpc import aio

import neo_desktop_pb2
import neo_desktop_pb2_grpc
from modules.desktop.ai_service import AIService, AIServiceResponse
from shared import IntentType

logger = logging.getLogger(__name__)


class ConnectedDevicesRegistry:
    """Thread-safe registry of devices connected via gRPC."""

    def __init__(self):
        self._devices: dict[str, dict] = {}
        self._lock = threading.Lock()

    def register(self, device_id: str, info: dict) -> None:
        with self._lock:
            info["connected_at_unix"] = int(time.time())
            info["last_heartbeat_unix"] = int(time.time())
            info["is_online"] = True
            self._devices[device_id] = info
            logger.info(f"[Devices] Registered: {device_id} ({info.get('device_type', 'unknown')})")

    def unregister(self, device_id: str) -> None:
        with self._lock:
            if device_id in self._devices:
                self._devices[device_id]["is_online"] = False
                logger.info(f"[Devices] Unregistered: {device_id}")

    def update(self, device_id: str, updates: dict) -> None:
        with self._lock:
            if device_id in self._devices:
                self._devices[device_id].update(updates)

    def list_all(self) -> list[dict]:
        with self._lock:
            return list(self._devices.values())

    def get(self, device_id: str) -> dict | None:
        with self._lock:
            return self._devices.get(device_id)


class DevicesWSRegistry:
    """Async registry of devices connected via WebSocket (online/offline signal)."""

    def __init__(self):
        self._online: set[str] = set()
        self._connection_ids: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._db = None

    def set_db(self, db) -> None:
        self._db = db

    async def register(self, device_id: str, ws_connection_id: str | None = None) -> None:
        conn_id = ws_connection_id or str(id(self))  # fallback to instance id if no ws id
        async with self._lock:
            self._online.add(device_id)
            self._connection_ids[device_id] = conn_id
            logger.info(f"[WS] Device online: {device_id} conn={conn_id}")
        # Sync to DB (async, non-blocking)
        if self._db:
            asyncio.create_task(self._db.set_device_ws_connection(device_id, conn_id))

    async def unregister(self, device_id: str) -> None:
        async with self._lock:
            self._online.discard(device_id)
            self._connection_ids.pop(device_id, None)
            logger.info(f"[WS] Device offline: {device_id}")
        # Sync to DB (async, non-blocking)
        if self._db:
            asyncio.create_task(self._db.clear_device_ws_connection(device_id))

    async def is_online(self, device_id: str) -> bool:
        return device_id in self._online

    async def list_online(self) -> list[str]:
        async with self._lock:
            return list(self._online)


# Global registry instances
_connected_devices = ConnectedDevicesRegistry()
_devices_ws_registry = DevicesWSRegistry()


class NeoDesktopServicer(neo_desktop_pb2_grpc.NeoDesktopServiceServicer):
    """gRPC Servicer — thin controller that delegates to AIService."""

    def __init__(
        self,
        ai_service: AIService,
        notification_broker=None,
        skill_executor=None,
    ) -> None:
        self.ai_service = ai_service
        self.notification_broker = notification_broker
        self.skill_executor = skill_executor
        # Track pending client-tool flows per session for approval/result routing
        self._pending: dict[str, dict] = {}

    async def ExecuteTask(
        self,
        request_iterator: AsyncIterator,
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator:
        """Bidirectional streaming — routes desktop messages to AIService."""
        logger.info("[gRPC] ExecuteTask stream started")
        session_id: str | None = None
        device_id: str | None = None
        device_info: dict = {}
        stream_user_id: UUID | None = None  # last known user on this stream (for voice stamping)
        _connected_devices_local = _connected_devices  # local ref for finally block

        try:
            async for desktop_msg in request_iterator:
                session_id = desktop_msg.session_id
                sid_uuid = UUID(session_id) if session_id else None

                # Extract user_id from DesktopMessage (optional)
                uid_uuid: UUID | None = None
                if desktop_msg.user_id:
                    try:
                        uid_uuid = UUID(desktop_msg.user_id)
                    except ValueError:
                        logger.warning(f"[gRPC] Invalid user_id: {desktop_msg.user_id}")
                if uid_uuid:
                    stream_user_id = uid_uuid  # continuations (tool_result/approval) reuse it

                if desktop_msg.HasField("user_input"):
                    msg = desktop_msg.user_input.message
                    metadata = dict(desktop_msg.user_input.metadata) if desktop_msg.user_input.metadata else {}
                    logger.info(f"[gRPC] USER_INPUT session={session_id} user={uid_uuid}: {msg[:80]}")

                    # Register device on first user_input — use device_id from metadata if present,
                    # otherwise fall back to session_id (for backwards compat)
                    if device_id is None:
                        device_id = metadata.get("device_id") or session_id or (str(sid_uuid) if sid_uuid else None)
                        device_info = {
                            "device_id": device_id,
                            "device_type": metadata.get("client", "neo-desktop"),
                            "model": metadata.get("model", "unknown"),
                            "hardware_id": metadata.get("hardware_id", ""),
                            "os_platform": metadata.get("os_platform", "unknown"),
                            "os_version": metadata.get("os_release", "unknown"),
                            "client_version": metadata.get("client_version", "unknown"),
                            "ip_address": metadata.get("ip_address", ""),
                            "user_id": desktop_msg.user_id or "",
                        }
                        if device_id:
                            _connected_devices.register(device_id, device_info)
                            # Sync to DB (async, non-blocking)
                            if self.ai_service and self.ai_service.db and uid_uuid:
                                asyncio.create_task(
                                    self.ai_service.db.upsert_connected_device(
                                        user_id=uid_uuid,
                                        device_id=device_id,
                                        device_type=metadata.get("client", "neo-desktop") or "neo-desktop",
                                        model=metadata.get("model", "unknown"),
                                        hardware_id=metadata.get("hardware_id", ""),
                                        os_platform=metadata.get("os_platform", "unknown"),
                                        os_version=metadata.get("os_release", "unknown"),
                                        client_version=metadata.get("client_version", "unknown"),
                                        ip_address=metadata.get("ip_address", ""),
                                        last_heartbeat_unix=int(time.time()),
                                        is_online=True,
                                    )
                                )

                    async for resp in self.ai_service.process_user_input(
                        user_message=msg,
                        session_id=sid_uuid,
                        user_id=uid_uuid,
                        service=metadata.get("client", "neo-desktop"),
                        metadata=metadata,
                    ):
                        await self._stamp_voice(resp, stream_user_id)
                        core_msg = self._to_core_message(session_id, resp)
                        if core_msg:
                            yield core_msg
                        # Track pending flows
                        if resp.metadata and not resp.is_final:
                            self._pending[session_id] = {**resp.metadata, "session_id": session_id}

                elif desktop_msg.HasField("tool_result"):
                    tr = desktop_msg.tool_result
                    logger.info(f"[gRPC] TOOL_RESULT session={session_id}: {tr.tool_name} success={tr.success}")
                    pending = self._pending.pop(session_id, {})

                    async for resp in self.ai_service.process_tool_result(
                        session_id=sid_uuid,
                        tool_name=tr.tool_name,
                        tool_call_id=tr.tool_call_id,
                        success=tr.success,
                        result=tr.result,
                        error=tr.error,
                        user_message=pending.get("user_message", ""),
                        command=pending.get("command", tr.tool_name),
                        tool_arguments=pending.get("tool_arguments"),
                        _retry_count=pending.get("_retry_count", 0),
                        _is_diagnostic=pending.get("_is_diagnostic", False),
                        _followup_count=pending.get("_followup_count", 0),
                        _has_plan=pending.get("_has_plan", False),
                    ):
                        await self._stamp_voice(resp, stream_user_id)
                        core_msg = self._to_core_message(session_id, resp)
                        if core_msg:
                            yield core_msg
                        # Re-track pending metadata for retry flows (new tool_request/command_request)
                        if resp.metadata and not resp.is_final:
                            self._pending[session_id] = {**resp.metadata, "session_id": session_id}

                elif desktop_msg.HasField("command_approval"):
                    ca = desktop_msg.command_approval
                    logger.info(f"[gRPC] COMMAND_APPROVAL session={session_id}: approved={ca.approved}")
                    pending = self._pending.get(session_id, {})

                    async for resp in self.ai_service.process_command_approval(
                        session_id=sid_uuid,
                        command_id=ca.command_id,
                        approved=ca.approved,
                        command=pending.get("command", ""),
                        user_message=pending.get("user_message", ""),
                    ):
                        await self._stamp_voice(resp, stream_user_id)
                        core_msg = self._to_core_message(session_id, resp)
                        if core_msg:
                            yield core_msg
                        # Track tool_call_id for subsequent tool_result
                        if resp.metadata and resp.tool_request:
                            self._pending[session_id] = {**pending, **resp.metadata}

                
        except asyncio.CancelledError:
            # Client pressed Stop → it dropped the stream, which grpc.aio surfaces
            # as cancellation of this handler. The in-flight work is already being
            # torn down; just log and let it propagate (cleanup runs in `finally`).
            logger.info(f"[gRPC] ExecuteTask cancelled by client (session={session_id})")
            raise
        except Exception as e:
            logger.error(f"[gRPC] Error in ExecuteTask: {e}", exc_info=True)
            # Always close the turn with a readable final message so the UI exits
            # the "Executando" state instead of hanging. The raw error stays in logs.
            yield neo_desktop_pb2.CoreMessage(
                session_id=session_id or "",
                response=neo_desktop_pb2.AgentResponse(
                    message=(
                        "Não foi possível concluir a execução: ocorreu um erro interno. "
                        "Pode tentar novamente?"
                    ),
                    is_final=True,
                    total_iterations=1,
                ),
            )
        finally:
            # If a client-tool flow was still awaiting the desktop when the stream
            # ended (disconnect/crash), close the turn gracefully so it does not
            # dangle as "Executando" — event-driven, no time limit.
            pending = self._pending.pop(session_id, None) if session_id else None
            if pending and pending.get("flow") in ("client_tool", "client_command"):
                self._close_incomplete_tool_turn(pending)
            # Mark device offline when stream ends — this fires on graceful disconnect
            # AND when the connection drops (gRPC detects broken pipe/stream complete)
            if device_id:
                _connected_devices.update(device_id, {"is_online": False})
                if self.ai_service and self.ai_service.db:
                    asyncio.create_task(
                        self.ai_service.db.mark_device_offline(device_id)
                    )
                logger.info(f"[Devices] Stream ended, marked offline: {device_id}")

    def _close_incomplete_tool_turn(self, pending: dict) -> None:
        """Persist a failure message when a client-tool turn ends without a result.

        Fires when the desktop stream drops while a tool/command request was still
        awaiting its result. Saves a final assistant message so the conversation is
        resolved (not left dangling) the next time the session is loaded.
        """
        if not (self.ai_service and getattr(self.ai_service, "sessions", None)):
            return
        raw_sid = pending.get("session_id")
        if not raw_sid:
            return
        try:
            sid_uuid = UUID(str(raw_sid))
        except (ValueError, TypeError):
            return

        tool_label = pending.get("tool_name") or pending.get("command") or "a ação solicitada"
        message = (
            f"Não foi possível concluir {tool_label}: a conexão com o dispositivo foi "
            "encerrada antes de a execução retornar um resultado."
        )

        async def _persist() -> None:
            try:
                await self.ai_service.sessions.add_message(
                    session_id=sid_uuid,
                    role="assistant",
                    content=message,
                    generate_embedding=False,
                    user_id=None,
                )
                logger.info(f"[gRPC] Closed incomplete tool turn for session {sid_uuid}")
            except Exception as exc:  # pragma: no cover - defensive, fire-and-forget
                logger.warning(f"[gRPC] Failed to close incomplete tool turn: {exc}")

        asyncio.create_task(_persist())

    # ---------------------------------------------------------------
    # AIServiceResponse → CoreMessage conversion
    # ---------------------------------------------------------------

    async def _stamp_voice(self, resp: AIServiceResponse, user_id: UUID | None) -> None:
        """Pattern A: attach the active TTS voice to a FINAL reply so the desktop
        speaks with the voice persisted in the DB. Never blocks a reply on a read miss."""
        if not (resp.is_final and resp.message) or resp.voice is not None or not self.ai_service:
            return
        try:
            from modules.voice import get_active_voice
            resp.voice = await get_active_voice(
                self.ai_service.db, user_id, getattr(self.ai_service, "settings", None)
            )
        except Exception:  # pragma: no cover - defensive
            logger.debug("[gRPC] voice stamp skipped", exc_info=True)

    @staticmethod
    def _voice_pb(voice: dict | None) -> "neo_desktop_pb2.VoiceSettings | None":
        """Build a VoiceSettings proto from the active-voice dict (or None)."""
        if not voice:
            return None
        return neo_desktop_pb2.VoiceSettings(
            engine=voice.get("engine") or "",
            voice=voice.get("voice") or "",
            language=voice.get("language") or "",
            label=voice.get("label") or "",
        )

    @staticmethod
    def _segments_pb(segments: "list[dict] | None") -> "list[neo_desktop_pb2.SpeechSegment]":
        """Build repeated SpeechSegment protos from resolved segment dicts (voice
        imitation). A segment without a voice leaves it unset → renderer speaks it
        with the reply-level default voice."""
        if not segments:
            return []
        return [
            neo_desktop_pb2.SpeechSegment(
                text=seg.get("text") or "",
                voice=NeoDesktopServicer._voice_pb(seg.get("voice")),
            )
            for seg in segments
        ]

    @staticmethod
    def _to_core_message(session_id: str, resp: AIServiceResponse) -> neo_desktop_pb2.CoreMessage | None:
        """Convert an AIServiceResponse to a protobuf CoreMessage."""
        # Prefer the DB-assigned session_id from the response over the raw desktop string.
        # This ensures the desktop always echoes back a valid DB UUID on subsequent messages.
        sid = str(resp.session_id) if resp.session_id else session_id

        # Plan update (from metadata)
        plan_update_data = resp.metadata.get("plan_update") if resp.metadata else None
        if plan_update_data:
            steps = [
                neo_desktop_pb2.PlanStepInfo(
                    id=s.get("id", ""),
                    index=s.get("index", 0),
                    description=s.get("description", ""),
                    tool_name=s.get("tool_name", ""),
                    status=s.get("status", "pending"),
                    result_summary=s.get("result_summary", ""),
                    error=s.get("error", ""),
                )
                for s in plan_update_data.get("steps", [])
            ]
            plan_msg = neo_desktop_pb2.CoreMessage(
                session_id=sid,
                plan_update=neo_desktop_pb2.PlanUpdate(
                    plan_id=plan_update_data.get("plan_id", ""),
                    goal=plan_update_data.get("goal", ""),
                    steps=steps,
                    current_step=plan_update_data.get("current_step", 0),
                    status=plan_update_data.get("status", "active"),
                    parent_plan_id=plan_update_data.get("parent_plan_id", ""),
                ),
            )
            # If there's also a message, we need to send plan_update first, then the message
            # Since proto oneof only allows one, handle the plan_update priority
            if not resp.message:
                return plan_msg
            # If there's also a message, send plan_update (message will be sent as thinking in next yield)
            return plan_msg

        # Command request
        if resp.command_request:
            cr = resp.command_request
            return neo_desktop_pb2.CoreMessage(
                session_id=sid,
                command_request=neo_desktop_pb2.CommandRequest(
                    command_id=cr["command_id"],
                    command=cr["command"],
                    explanation=cr.get("explanation", ""),
                    is_background=cr.get("is_background", False),
                ),
            )

        # Tool request
        if resp.tool_request:
            tr = resp.tool_request
            return neo_desktop_pb2.CoreMessage(
                session_id=sid,
                tool_request=neo_desktop_pb2.ToolRequest(
                    tool_call_id=tr["tool_call_id"],
                    tool_name=tr["tool_name"],
                    arguments=tr["arguments"],
                    requires_approval=tr.get("requires_approval", False),
                ),
            )

        # Final response
        if resp.is_final and resp.message:
            return neo_desktop_pb2.CoreMessage(
                session_id=sid,
                response=neo_desktop_pb2.AgentResponse(
                    message=resp.message,
                    is_final=True,
                    total_iterations=resp.iterations,
                    voice=NeoDesktopServicer._voice_pb(resp.voice),
                    segments=NeoDesktopServicer._segments_pb(getattr(resp, "segments", None)),
                ),
            )

        # Persistent intermediate reply: the cognitive's user-facing answer that
        # precedes a client-actuating handoff (music). Sent as a NON-final
        # AgentResponse so the desktop renders it as its own chat message instead of
        # a transient "thinking" — the real final ("🎵 …") follows after the play.
        if (
            resp.message
            and not resp.is_final
            and resp.metadata
            and resp.metadata.get("persist_reply")
        ):
            return neo_desktop_pb2.CoreMessage(
                session_id=sid,
                response=neo_desktop_pb2.AgentResponse(
                    message=resp.message,
                    is_final=False,
                    total_iterations=resp.iterations,
                ),
            )

        # Thinking / intermediate
        if resp.message and not resp.is_final:
            return neo_desktop_pb2.CoreMessage(
                session_id=sid,
                thinking=neo_desktop_pb2.AgentThinking(
                    message=resp.message,
                    iteration=resp.iterations,
                ),
            )

        return None

    # ---------------------------------------------------------------
    # Health check
    # ---------------------------------------------------------------

    async def HealthCheck(self, request, context):
        """Health check endpoint."""
        return neo_desktop_pb2.HealthCheckResponse(
            healthy=True,
            version="2.0.0",
            status={"core": "ok", "ai": "ok"},
        )

    # ---------------------------------------------------------------
    # Notifications — server streaming
    # ---------------------------------------------------------------

    async def SubscribeNotifications(self, request, context):
        """Server-streaming RPC: pushes notifications to desktop as they arrive.

        On connect, replays any undelivered notifications from the DB so the
        desktop catches up on anything it missed while offline. Then keeps the
        stream open and yields each new broker publication.
        """
        user_id_str = request.user_id
        try:
            user_id = UUID(user_id_str)
        except ValueError:
            logger.warning(f"[gRPC] Invalid user_id for notifications: {user_id_str}")
            return

        logger.info(f"[gRPC] SubscribeNotifications user={user_id}")

        # Replay undelivered notifications from DB
        if self.ai_service and self.ai_service.db:
            try:
                pending = await self.ai_service.db.get_pending_notifications(user_id)
                logger.info(f"[gRPC] Replaying {len(pending)} pending notifications")
                for notif in pending:
                    yield neo_desktop_pb2.Notification(
                        id=str(notif.id),
                        user_id=user_id_str,
                        mission_id=str(notif.mission_id) if notif.mission_id else "",
                        kind=notif.kind or "",
                        title=notif.title or "",
                        body=notif.body or "",
                        payload_json=json.dumps(notif.payload or {}),
                        created_at_unix=int(notif.created_at.timestamp()) if notif.created_at else 0,
                    )
            except Exception as e:
                logger.error(f"[gRPC] Pending replay failed: {e}")

        if self.notification_broker:
            async for notif in self.notification_broker.subscribe(user_id):
                pb_notif = neo_desktop_pb2.Notification(
                    id=notif.get("id", ""),
                    user_id=notif.get("user_id", user_id_str),
                    mission_id=notif.get("mission_id", ""),
                    kind=notif.get("kind", ""),
                    title=notif.get("title", ""),
                    body=notif.get("body", ""),
                    payload_json=json.dumps(notif.get("payload", {})),
                    created_at_unix=notif.get("created_at_unix", 0),
                )
                yield pb_notif

    async def AckNotification(self, request, context):
        """Mark a notification as acked."""
        if self.ai_service and self.ai_service.db:
            from uuid import UUID as _UUID
            try:
                notif_id = _UUID(request.notification_id)
                await self.ai_service.db.mark_notification_acked(notif_id)
                logger.info(f"[gRPC] AckNotification {notif_id}")
            except Exception as e:
                logger.error(f"[gRPC] AckNotification failed: {e}")
        return neo_desktop_pb2.AckResponse()

    async def RespondToMissionPrompt(self, request, context):
        """Handle Daniel's approval/denial of a pending mission draft."""
        if not self.skill_executor:
            return neo_desktop_pb2.MissionPromptAck(ok=False, message="SkillExecutor not available")

        try:
            mission_id = UUID(request.mission_id)
        except ValueError:
            return neo_desktop_pb2.MissionPromptAck(ok=False, message="Invalid mission_id")

        logger.info(f"[gRPC] RespondToMissionPrompt mission={mission_id} approved={request.approved}")

        try:
            from core.skills.whatsapp_messaging.pipeline import WhatsAppMessagingPipeline
            mission = await self.skill_executor.db.get_mission(mission_id)
            if not mission:
                return neo_desktop_pb2.MissionPromptAck(ok=False, message="Mission not found")
            pipeline = WhatsAppMessagingPipeline(
                agent_executor=self.skill_executor.agent_executor,
                db=self.skill_executor.db,
                broker=self.skill_executor.broker,
            )
            result = await pipeline.run(
                session_id=mission.session_id,
                user_message="",
                history=[],
                mission_id=mission_id,
                extra={
                    "approved": request.approved,
                    "edited_text": request.edited_text or None,
                },
                user_id=mission.user_id,
            )
            return neo_desktop_pb2.MissionPromptAck(
                ok=result.ok,
                message=result.message,
            )
        except Exception as e:
            logger.exception(f"[gRPC] RespondToMissionPrompt failed: {e}")
            return neo_desktop_pb2.MissionPromptAck(ok=False, message=str(e))

    async def ListConnectedDevices(self, request, context):
        """Return list of devices currently connected via gRPC."""
        user_id = request.user_id
        logger.info(f"[gRPC] ListConnectedDevices user={user_id}")

        all_devices = _connected_devices.list_all()
        # Filter by user_id if provided
        if user_id:
            all_devices = [d for d in all_devices if d.get("user_id") == user_id]

        proto_devices = [
            neo_desktop_pb2.DeviceInfo(
                device_id=d.get("device_id", ""),
                device_type=d.get("device_type", "unknown"),
                model=d.get("model", "unknown"),
                os_platform=d.get("os_platform", "unknown"),
                os_version=d.get("os_version", "unknown"),
                client_version=d.get("client_version", ""),
                ip_address=d.get("ip_address", ""),
                connected_at_unix=d.get("connected_at_unix", 0),
                is_current_device=d.get("is_current_device", False),
                is_online=d.get("is_online", False),
                last_heartbeat_unix=d.get("last_heartbeat_unix", 0),
            )
            for d in all_devices
        ]

        return neo_desktop_pb2.ListConnectedDevicesResponse(devices=proto_devices)

    async def RegisterDevice(self, request, context):
        """Register a device proactively (called by desktop on startup)."""
        device_id = request.device_id or f"device-{id(request)}"
        user_id_str = request.user_id or ""
        try:
            uid_uuid = UUID(user_id_str) if user_id_str else None
        except ValueError:
            uid_uuid = None

        device_info = {
            "device_id": device_id,
            "device_type": request.device_type or "neo-desktop",
            "model": request.model or "unknown",
            "hardware_id": getattr(request, "hardware_id", ""),
            "os_platform": request.os_platform or "unknown",
            "os_version": request.os_version or "unknown",
            "client_version": request.client_version or "",
            "ip_address": request.ip_address or "",
            "user_id": user_id_str,
        }
        _connected_devices.register(device_id, device_info)

        # Sync to DB (async, non-blocking)
        if self.ai_service and self.ai_service.db and uid_uuid:
            asyncio.create_task(
                self.ai_service.db.upsert_connected_device(
                    user_id=uid_uuid,
                    device_id=device_id,
                    device_type=request.device_type or "neo-desktop",
                    model=request.model or "unknown",
                    hardware_id=getattr(request, "hardware_id", ""),
                    os_platform=request.os_platform or "unknown",
                    os_version=request.os_version or "unknown",
                    client_version=request.client_version or "",
                    ip_address=request.ip_address or "",
                    last_heartbeat_unix=int(time.time()),
                    is_online=True,
                )
            )

        logger.info(f"[gRPC] RegisterDevice: {device_id}")
        return neo_desktop_pb2.RegisterDeviceResponse(ok=True)

    async def UnregisterDevice(self, request, context):
        """Unregister a device (called by desktop on shutdown)."""
        device_id = request.device_id
        _connected_devices.unregister(device_id)

        # Mark offline in DB (async, non-blocking)
        if self.ai_service and self.ai_service.db:
            asyncio.create_task(
                self.ai_service.db.mark_device_offline(device_id)
            )

        logger.info(f"[gRPC] UnregisterDevice: {device_id}")
        return neo_desktop_pb2.UnregisterDeviceResponse(ok=True)


async def serve(
    ai_service: AIService,
    port: int = 50051,
    notification_broker=None,
    skill_executor=None,
) -> grpc.aio.Server:
    """Start gRPC server with injected AIService.

    Args:
        ai_service: The AIService instance (created by api/main.py lifespan).
        port: gRPC listen port.

    Returns:
        Running grpc.aio.Server instance.
    """
    server = grpc.aio.server()
    neo_desktop_pb2_grpc.add_NeoDesktopServiceServicer_to_server(
        NeoDesktopServicer(ai_service, notification_broker, skill_executor), server
    )
    server.add_insecure_port(f"[::]:{port}")

    # Set DB on WS registry so it can sync online/offline to DB
    _devices_ws_registry.set_db(ai_service.db if ai_service else None)

    logger.info(f"Starting gRPC server on port {port}")
    await server.start()
    return server


async def _offline_checker(db: "AsyncPostgresClient", interval_seconds: int = 30, threshold_seconds: int = 60) -> None:
    """Background task: periodically marks devices with stale heartbeats as offline."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            count = await db.mark_stale_devices_offline(threshold_seconds)
            if count > 0:
                logger.info(f"[Devices] Marked {count} stale device(s) offline")
        except Exception as e:
            logger.warning(f"[Devices] Offline checker error: {e}")
