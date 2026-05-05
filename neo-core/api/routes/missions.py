"""Missions management API."""

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException

from db.async_client import AsyncPostgresClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/missions", tags=["missions"])

_db: AsyncPostgresClient | None = None
_pipeline_factory = None


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
            "title": m.title,
            "goal": m.goal,
            "contact_jid": m.contact_jid,
            "status": m.status,
            "channel": m.channel,
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
        "title": mission.title,
        "goal": mission.goal,
        "contact_jid": mission.contact_jid,
        "status": mission.status,
        "channel": mission.channel,
        "created_at": mission.created_at.isoformat() if mission.created_at else None,
        "last_activity_at": mission.last_activity_at.isoformat() if mission.last_activity_at else None,
        "completed_at": mission.completed_at.isoformat() if mission.completed_at else None,
        "extra_metadata": mission.extra_metadata or {},
        "result_summary": mission.result_summary,
    }


@router.post("/{mission_id}/approve", response_model=dict)
async def approve_mission(mission_id: UUID, edited_text: str | None = None):
    """Approve a mission (personal mode approval flow)."""
    if not _db:
        raise HTTPException(status_code=500, detail="DB not initialized")

    if not _pipeline_factory:
        raise HTTPException(status_code=503, detail="Pipeline factory not set")

    mission = await _db.get_mission(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="Mission not found")

    if mission.status != "awaiting_confirmation":
        raise HTTPException(status_code=400, detail=f"Mission is not awaiting confirmation (status={mission.status})")

    pipeline = _pipeline_factory()
    from observability.session_file_logger import SessionFileLogger
    from uuid import UUID as _UUID
    _slog = SessionFileLogger(str(mission.session_id))
    result = await pipeline._handle_approval(mission, {"approved": True, "edited_text": edited_text}, _slog)

    return {"ok": result.ok, "message": result.message, "mission_id": str(mission_id)}


@router.post("/{mission_id}/cancel", response_model=dict)
async def cancel_mission(mission_id: UUID):
    """Cancel a mission."""
    if not _db:
        raise HTTPException(status_code=500, detail="DB not initialized")

    mission = await _db.get_mission(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="Mission not found")

    await _db.update_mission_status(mission_id, status="cancelled")
    return {"ok": True, "message": "Missão cancelada.", "mission_id": str(mission_id)}
