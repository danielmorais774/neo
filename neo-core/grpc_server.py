"""
gRPC Server — thin controller for Neo Desktop communication.

This is ONLY a transport layer. All AI logic lives in AIService.
Responsibilities:
  - Receive protobuf messages from neo-desktop
  - Delegate to AIService
  - Convert AIServiceResponse → CoreMessage protobuf
  - Send back to desktop
"""

import json
import logging
from collections.abc import AsyncIterator
from uuid import UUID

import grpc
from grpc import aio

import neo_desktop_pb2
import neo_desktop_pb2_grpc
from modules.desktop.ai_service import AIService, AIServiceResponse
from shared import IntentType

logger = logging.getLogger(__name__)


class NeoDesktopServicer:
    """gRPC Servicer — thin controller that delegates to AIService."""

    def __init__(self, ai_service: AIService) -> None:
        self.ai_service = ai_service
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

                if desktop_msg.HasField("user_input"):
                    msg = desktop_msg.user_input.message
                    metadata = dict(desktop_msg.user_input.metadata) if desktop_msg.user_input.metadata else {}
                    logger.info(f"[gRPC] USER_INPUT session={session_id} user={uid_uuid}: {msg[:80]}")

                    async for resp in self.ai_service.process_user_input(
                        user_message=msg,
                        session_id=sid_uuid,
                        user_id=uid_uuid,
                        service=metadata.get("client", "neo-desktop"),
                        metadata=metadata,
                    ):
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
                        core_msg = self._to_core_message(session_id, resp)
                        if core_msg:
                            yield core_msg
                        # Track tool_call_id for subsequent tool_result
                        if resp.metadata and resp.tool_request:
                            self._pending[session_id] = {**pending, **resp.metadata}

                elif desktop_msg.HasField("keep_alive"):
                    logger.debug(f"[gRPC] keep_alive session={session_id}")

        except Exception as e:
            logger.error(f"[gRPC] Error in ExecuteTask: {e}", exc_info=True)
            yield neo_desktop_pb2.CoreMessage(
                session_id=session_id or "",
                error=neo_desktop_pb2.ErrorMessage(error=str(e), details="Stream error"),
            )
        finally:
            if session_id:
                self._pending.pop(session_id, None)

    # ---------------------------------------------------------------
    # AIServiceResponse → CoreMessage conversion
    # ---------------------------------------------------------------

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


async def serve(ai_service: AIService, port: int = 50051) -> grpc.aio.Server:
    """Start gRPC server with injected AIService.

    Args:
        ai_service: The AIService instance (created by api/main.py lifespan).
        port: gRPC listen port.

    Returns:
        Running grpc.aio.Server instance.
    """
    server = grpc.aio.server()
    neo_desktop_pb2_grpc.add_NeoDesktopServiceServicer_to_server(
        NeoDesktopServicer(ai_service), server
    )
    server.add_insecure_port(f"[::]:{port}")
    logger.info(f"Starting gRPC server on port {port}")
    await server.start()
    return server
