"""Research tasks API — aba "Pesquisas" do neo-panel.

Padrão missions.py: APIRouter + set_db() no bootstrap; registrado em api/main.py
com require_panel_jwt. O list NÃO retorna report_markdown (payload pesado só no
get). Ver docs/web-search-deep-research-plan.md §6.1.
"""

import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException

from db.async_client import AsyncPostgresClient
from db.models.research_task import RESEARCH_TERMINAL_STATUSES

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/research", tags=["research"])

_db: AsyncPostgresClient | None = None
_job_manager = None


def set_db(db: AsyncPostgresClient) -> None:
    global _db
    _db = db


def set_job_manager(job_manager) -> None:
    global _job_manager
    _job_manager = job_manager


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _quality_summary(quality: dict) -> dict:
    """Resumo leve da qualidade para a lista (o detalhe traz quality inteiro)."""
    if not quality:
        return {}
    rubric = quality.get("rubric") or {}
    scores = [v for v in rubric.values() if isinstance(v, (int, float))]
    validation = quality.get("validation") or {}
    out: dict = {}
    if scores:
        out["rubric_avg"] = round(sum(scores) / len(scores), 1)
    if "passed" in validation:
        out["validation_passed"] = bool(validation["passed"])
        out["failures"] = len(validation.get("failures") or [])
    if quality.get("revised"):
        out["revised"] = True
    return out


@router.get("", response_model=list[dict])
async def list_research(status: str | None = None, limit: int = 50):
    """Lista compacta (sem report_markdown)."""
    if not _db:
        raise HTTPException(status_code=500, detail="DB not initialized")
    tasks = await _db.list_research_tasks(status=status, limit=limit)
    return [
        {
            "id": str(t["id"]),
            "job_id": str(t["job_id"]),
            "user_id": str(t["user_id"]) if t["user_id"] else None,
            "topic": t["topic"],
            "depth": t["depth"],
            "status": t["status"],
            "model": t["model"],
            "backend": t["backend"],
            "stats": t["stats_json"] or {},
            "quality": _quality_summary(t.get("quality_json") or {}),
            "report_path": t["report_path"],
            "error": t["error"],
            "created_at": _iso(t["created_at"]),
            "started_at": _iso(t["started_at"]),
            "finished_at": _iso(t["finished_at"]),
            "has_report": t["has_report"],
        }
        for t in tasks
    ]


@router.get("/{task_id}", response_model=dict)
async def get_research(task_id: UUID):
    """Detalhe completo (com report_markdown e fontes)."""
    if not _db:
        raise HTTPException(status_code=500, detail="DB not initialized")
    task = await _db.get_research_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Research task not found")
    return {
        "id": str(task.id),
        "job_id": str(task.job_id),
        "user_id": str(task.user_id) if task.user_id else None,
        "session_id": str(task.session_id) if task.session_id else None,
        "topic": task.topic,
        "instructions": task.instructions,
        "depth": task.depth,
        "status": task.status,
        "model": task.model,
        "backend": task.backend,
        "stats": task.stats_json or {},
        "quality": task.quality_json or {},
        "requirements": task.requirements_json or {},
        "sources": task.sources_json or [],
        "report_markdown": task.report_markdown,
        "report_path": task.report_path,
        "error": task.error,
        "created_at": _iso(task.created_at),
        "started_at": _iso(task.started_at),
        "finished_at": _iso(task.finished_at),
    }


@router.post("/{task_id}/cancel", response_model=dict)
async def cancel_research(task_id: UUID):
    """Cancela a pesquisa via job (409 se já terminal)."""
    if not _db:
        raise HTTPException(status_code=500, detail="DB not initialized")
    task = await _db.get_research_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Research task not found")
    if task.status in RESEARCH_TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"Research already {task.status}")

    now = datetime.utcnow()
    await _db.update_agent_job(
        task.job_id,
        status="cancelled",
        finished_at=now,
        result_summary="Pesquisa cancelada pelo painel.",
    )
    await _db.update_research_task(task_id, status="cancelled", finished_at=now)
    # Aborta o worker em execução, se houver (o runner marca a fase; aqui já marcamos).
    if _job_manager is not None:
        try:
            worker = _job_manager._active.get(str(task.job_id))
            if worker is not None:
                worker.cancel()
        except Exception as e:  # noqa: BLE001
            logger.warning("[Research] cancel worker falhou: %s", e)
    return {"ok": True, "id": str(task_id), "status": "cancelled"}
