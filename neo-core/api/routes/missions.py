"""Missions management API."""

import logging
import os
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db.async_client import AsyncPostgresClient
from shared.types import MissionCommandPayload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/missions", tags=["missions"])

_db: AsyncPostgresClient | None = None
_pipeline_factory = None
_LOGS_DIR = Path(os.environ.get("SESSION_LOGS_DIR", "/app/logs"))
if not _LOGS_DIR.exists():
    _LOGS_DIR = Path(__file__).resolve().parent.parent.parent / "logs"


class MissionApprovalRequest(BaseModel):
    edited_text: str | None = None


class MissionCommandRequest(BaseModel):
    command: str
    edited_text: str | None = None
    user_id: UUID | None = None


def set_db(db: AsyncPostgresClient) -> None:
    global _db
    _db = db


def set_pipeline_factory(factory) -> None:
    global _pipeline_factory
    _pipeline_factory = factory


@router.get("", response_model=list[dict])
async def list_missions(user_id: UUID | None = None, status: str | None = None, limit: int = 50):
    """List missions, optionally filtered by user_id and/or status."""
    if not _db:
        raise HTTPException(status_code=500, detail="DB not initialized")

    missions = await _db.list_missions(user_id=user_id, status=status, limit=limit)
    return [
        {
            "id": str(m.id),
            "user_id": str(m.user_id),
            "session_id": str(m.session_id),
            "parent_session_id": str(m.parent_session_id) if m.parent_session_id else None,
            "title": m.title,
            "goal": m.goal,
            "contact_jid": m.contact_jid,
            "status": m.status,
            "channel": m.channel,
            "next_action_type": m.next_action_type,
            "next_action_params": m.next_action_params or {},
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "last_activity_at": m.last_activity_at.isoformat() if m.last_activity_at else None,
            "completed_at": m.completed_at.isoformat() if m.completed_at else None,
            "extra_metadata": m.extra_metadata or {},
        }
        for m in missions
    ]


@router.get("/{mission_id}", response_model=dict)
async def get_mission(mission_id: UUID):
    """Get a single mission by ID."""
    if not _db:
        raise HTTPException(status_code=500, detail="DB not initialized")

    mission = await _db.get_mission(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="Mission not found")

    return {
        "id": str(mission.id),
        "user_id": str(mission.user_id),
        "session_id": str(mission.session_id),
        "parent_session_id": str(mission.parent_session_id) if mission.parent_session_id else None,
        "title": mission.title,
        "goal": mission.goal,
        "contact_jid": mission.contact_jid,
        "status": mission.status,
        "channel": mission.channel,
        "next_action_type": mission.next_action_type,
        "next_action_params": mission.next_action_params or {},
        "created_at": mission.created_at.isoformat() if mission.created_at else None,
        "last_activity_at": mission.last_activity_at.isoformat() if mission.last_activity_at else None,
        "completed_at": mission.completed_at.isoformat() if mission.completed_at else None,
        "extra_metadata": mission.extra_metadata or {},
        "result_summary": mission.result_summary,
    }


@router.post("/{mission_id}/approve", response_model=dict)
async def approve_mission(mission_id: UUID, request: MissionApprovalRequest | None = None):
    """Compatibility wrapper: approve mission via structured command."""
    return await _execute_mission_command(
        MissionCommandPayload(
            command="approve",
            mission_id=mission_id,
            edited_text=request.edited_text if request else None,
            user_id=None,
        ),
        source="legacy-approve",
    )


@router.post("/{mission_id}/command", response_model=dict)
async def command_mission(mission_id: UUID, request: MissionCommandRequest):
    """Execute a structured mission command (approve/cancel/edit/retry)."""
    allowed = {"approve", "cancel", "edit", "retry"}
    if request.command not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid command: {request.command}")
    if request.command == "edit" and not (request.edited_text or "").strip():
        raise HTTPException(status_code=400, detail="edited_text é obrigatório para command=edit")

    return await _execute_mission_command(
        MissionCommandPayload(
            command=request.command,
            mission_id=mission_id,
            edited_text=request.edited_text,
            user_id=request.user_id,
        ),
        source="mission-command",
    )


@router.get("/{mission_id}/messages", response_model=list[dict])
async def get_mission_messages(mission_id: UUID, limit: int = 200):
    """Conversation messages explicitly tied to this mission (chronological)."""
    if not _db:
        raise HTTPException(status_code=500, detail="DB not initialized")

    mission = await _db.get_mission(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="Mission not found")

    messages = await _db.fetch_mission_messages(mission.id, limit=limit)
    return [
        {
            "id": str(m.id),
            "mission_id": str(m.mission_id) if m.mission_id else None,
            "session_id": str(m.session_id),
            "role": m.role,
            "content": m.content,
            "tool_name": m.tool_name,
            "tool_call_id": m.tool_call_id,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in messages
    ]


@router.get("/{mission_id}/notifications", response_model=list[dict])
async def get_mission_notifications(mission_id: UUID):
    """All notifications generated by this mission."""
    if not _db:
        raise HTTPException(status_code=500, detail="DB not initialized")

    mission = await _db.get_mission(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="Mission not found")

    notifications = await _db.list_notifications_for_mission(mission_id)
    return [
        {
            "id": str(n.id),
            "kind": n.kind,
            "title": n.title,
            "body": n.body,
            "payload": n.payload or {},
            "created_at": n.created_at.isoformat() if n.created_at else None,
            "delivered_at": n.delivered_at.isoformat() if n.delivered_at else None,
            "acked_at": n.acked_at.isoformat() if n.acked_at else None,
        }
        for n in notifications
    ]


@router.get("/{mission_id}/timeline", response_model=list[dict])
async def get_mission_timeline(mission_id: UUID):
    """Chronological timeline blending status milestones, messages and
    notifications. The panel uses this to show the full mission process.
    """
    if not _db:
        raise HTTPException(status_code=500, detail="DB not initialized")

    mission = await _db.get_mission(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="Mission not found")

    events: list[dict] = []

    if mission.created_at:
        events.append({
            "type": "milestone",
            "kind": "created",
            "label": "Missão criada",
            "detail": mission.goal,
            "at": mission.created_at.isoformat(),
        })

    messages = await _db.fetch_mission_messages(mission.id, limit=500)
    for m in messages:
        events.append({
            "type": "message",
            "mission_id": str(m.mission_id) if m.mission_id else None,
            "role": m.role,
            "label": "Usuário" if m.role == "user" else ("Assistente" if m.role == "assistant" else m.role),
            "detail": m.content,
            "tool_name": m.tool_name,
            "at": m.created_at.isoformat() if m.created_at else None,
        })

    notifications = await _db.list_notifications_for_mission(mission_id)
    for n in notifications:
        events.append({
            "type": "notification",
            "kind": n.kind,
            "label": n.title,
            "detail": n.body,
            "at": n.created_at.isoformat() if n.created_at else None,
        })

    if mission.completed_at:
        events.append({
            "type": "milestone",
            "kind": "completed",
            "label": "Missão concluída",
            "detail": mission.result_summary,
            "at": mission.completed_at.isoformat(),
        })

    events.append({
        "type": "milestone",
        "kind": "current_status",
        "label": f"Status atual: {mission.status}",
        "detail": mission.result_summary,
        "at": mission.last_activity_at.isoformat() if mission.last_activity_at else None,
    })

    events.sort(key=lambda e: e.get("at") or "")
    return events


@router.get("/{mission_id}/pipeline-log", response_model=dict)
async def get_mission_pipeline_log(mission_id: UUID):
    """Read mission markdown pipeline log file."""
    log_path = _LOGS_DIR / f"mission-{mission_id}.md"
    if not log_path.exists():
        return {"exists": False, "content": "", "path": str(log_path), "updated_at": None}

    try:
        content = log_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.exception("Failed to read mission log file mission_id=%s: %s", mission_id, e)
        raise HTTPException(status_code=500, detail="Failed to read mission log")

    stat = log_path.stat()
    return {
        "exists": True,
        "content": content,
        "path": str(log_path),
        "updated_at": stat.st_mtime,
    }


@router.post("/{mission_id}/cancel", response_model=dict)
async def cancel_mission(mission_id: UUID):
    """Compatibility wrapper: cancel mission via structured command."""
    return await _execute_mission_command(
        MissionCommandPayload(
            command="cancel",
            mission_id=mission_id,
            user_id=None,
        ),
        source="legacy-cancel",
    )


async def _execute_mission_command(payload: MissionCommandPayload, source: str) -> dict:
    if not _db:
        raise HTTPException(status_code=500, detail="DB not initialized")
    if not _pipeline_factory:
        raise HTTPException(status_code=503, detail="Pipeline factory not set")

    mission = await _db.get_mission(payload.mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="Mission not found")

    command = payload.command.value if hasattr(payload.command, "value") else str(payload.command)
    allowed = {"approve", "cancel", "edit", "retry"}
    if command not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid command: {command}")

    if command in {"approve", "edit"} and mission.status != "awaiting_confirmation":
        raise HTTPException(
            status_code=400,
            detail=f"Mission is not awaiting confirmation (status={mission.status})",
        )
    if command == "retry" and mission.status not in {"awaiting_confirmation", "failed"}:
        raise HTTPException(
            status_code=400,
            detail=f"Mission cannot retry in status={mission.status}",
        )

    pipeline = _pipeline_factory()
    result = await pipeline.execute_mission_command(
        mission_id=mission.id,
        command=command,
        edited_text=payload.edited_text,
        user_id=payload.user_id or mission.user_id,
        source=source,
    )

    if not result.ok:
        return {
            "ok": False,
            "message": result.message,
            "mission_id": str(payload.mission_id),
            "command": command,
        }

    return {
        "ok": True,
        "message": result.message,
        "mission_id": str(payload.mission_id),
        "command": command,
    }
