"""API da agenda (docs/scheduler-plan.md §10).

Painel e tools do cérebro entram pelo MESMO :class:`ScheduleService` — se a tela
validasse de um jeito e a tool de outro, o Neo criaria por conversa tarefas que
a tela não consegue editar.

Duas rotas existem só para tirar o usuário do escuro, e são as que mais evitam
suporte: ``/preview`` (o que "toda segunda às 7h" vira, e quando dispara) e
``/simular`` (roda de verdade, mas sem entregar nada). Descobrir que o prompt
estava ruim depois de três dias de mensagem errada é o modo de falha clássico
de agenda.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from api.security import require_panel_jwt
from core.scheduler.service import ScheduleValidationError
from core.scheduler.timing import ScheduleError, describe, to_local, upcoming, utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agenda", tags=["agenda"], dependencies=[Depends(require_panel_jwt)])

_service: Any = None
_tick: Any = None
_db: Any = None
_registry: Any = None


def set_dependencies(service: Any, tick: Any = None, db: Any = None, registry: Any = None) -> None:
    global _service, _tick, _db, _registry
    _service = service
    _tick = tick
    _db = db
    _registry = registry


def _require_service() -> Any:
    if _service is None:
        raise HTTPException(
            status_code=503, detail="Agenda desligada (NEO_SCHEDULER_ENABLED=false)."
        )
    return _service


async def _resolve_user(user_id: str | None) -> UUID:
    """Mesma política do datasets.py/events.py: explícito ganha; senão, o único."""
    if user_id:
        try:
            return UUID(user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="user_id inválido")
    if _db is None:
        raise HTTPException(status_code=503, detail="Banco indisponível")
    user_ids = await _db.list_user_ids()
    if len(user_ids) != 1:
        raise HTTPException(
            status_code=400,
            detail=f"{len(user_ids)} usuários cadastrados — informe user_id explicitamente.",
        )
    return user_ids[0]


async def _get_or_404(task_id: str) -> Any:
    service = _require_service()
    try:
        task = await service.get(UUID(task_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="id inválido")
    if task is None:
        raise HTTPException(status_code=404, detail="tarefa não encontrada")
    return task


# ---------------------------------------------------------------------------
# Construtor de horário (a tela não deve exigir que ninguém saiba cron)
# ---------------------------------------------------------------------------

@router.get("/preview")
async def preview(
    quando: str | None = Query(default=None),
    schedule_kind: str | None = Query(default=None),
    schedule_expr: str | None = Query(default=None),
    timezone: str | None = Query(default=None),
) -> dict[str, Any]:
    """Frase (ou cron) → descrição humana + os próximos 5 disparos."""
    _require_service()
    tz = timezone or "America/Fortaleza"
    try:
        if quando and not schedule_kind:
            from core.scheduler.nlp import parse_quando

            parsed = parse_quando(quando, tz=tz)
            kind, expr = parsed["schedule_kind"], parsed["schedule_expr"]
        else:
            kind, expr = (schedule_kind or ""), (schedule_expr or "")
            if not kind or not expr:
                raise HTTPException(status_code=400, detail="informe 'quando' ou kind+expr")
        proximos = upcoming(schedule_kind=kind, schedule_expr=expr, tz=tz, count=5, after=utcnow())
    except ScheduleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "schedule_kind": kind,
        "schedule_expr": expr,
        "timezone": tz,
        "descricao": describe(kind, expr, tz),
        "proximos": [to_local(m, tz).strftime("%d/%m/%Y %H:%M") for m in proximos],
    }


@router.get("/ferramentas")
async def ferramentas() -> dict[str, Any]:
    """Tools server-side que uma tarefa pode disparar (para o form de ação)."""
    _require_service()
    if _registry is None:
        return {"ferramentas": []}
    saida = []
    for tool in _registry.get_all():
        if getattr(tool, "execution", "") != "server-side":
            continue
        # A própria agenda fora da lista: uma tarefa que agenda tarefas é um
        # laço esperando acontecer, e o executor global nem sabe rodá-las.
        if tool.name.startswith("schedule_"):
            continue
        saida.append({
            "name": tool.name,
            "description": (tool.description or "")[:280],
            "module": tool.module,
            "parameters": {
                nome: {
                    "type": param.type,
                    "description": param.description,
                    "required": param.required,
                    "enum": param.enum,
                }
                for nome, param in (tool.parameters or {}).items()
            },
        })
    return {"ferramentas": sorted(saida, key=lambda t: t["name"])}


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("/tarefas")
async def listar(
    user_id: str | None = Query(default=None),
    ativas_apenas: bool = Query(default=False),
) -> dict[str, Any]:
    service = _require_service()
    uid = await _resolve_user(user_id)
    tarefas = await service.list(uid, enabled_only=ativas_apenas)
    return {
        "total": len(tarefas),
        "pausado_global": bool(getattr(_tick, "paused", False)),
        "tarefas": [service.to_dict(t) for t in tarefas],
    }


@router.post("/tarefas", status_code=201)
async def criar(
    payload: dict[str, Any] = Body(...),
    user_id: str | None = Query(default=None),
) -> dict[str, Any]:
    service = _require_service()
    uid = await _resolve_user(user_id or payload.get("user_id"))
    try:
        tarefa = await service.create(payload, user_id=uid)
    except (ScheduleValidationError, ScheduleError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if _tick is not None:
        _tick.poke()
    return service.to_dict(tarefa)


@router.get("/tarefas/{task_id}")
async def detalhe(task_id: str) -> dict[str, Any]:
    service = _require_service()
    tarefa = await _get_or_404(task_id)
    return service.to_dict(tarefa)


@router.patch("/tarefas/{task_id}")
async def editar(task_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    service = _require_service()
    tarefa = await _get_or_404(task_id)
    # Ligar/desligar tem caminho próprio: religar recalcula o horário.
    if set(payload.keys()) == {"enabled"}:
        atualizada = await service.set_enabled(tarefa.id, bool(payload["enabled"]))
        return service.to_dict(atualizada)
    try:
        atualizada = await service.update(tarefa.id, payload)
    except (ScheduleValidationError, ScheduleError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if _tick is not None:
        _tick.poke()
    return service.to_dict(atualizada)


@router.delete("/tarefas/{task_id}")
async def apagar(task_id: str) -> dict[str, Any]:
    service = _require_service()
    tarefa = await _get_or_404(task_id)
    apagou = await service.delete(tarefa.id)
    return {"ok": apagou, "id": str(tarefa.id)}


# ---------------------------------------------------------------------------
# Disparo manual, simulação e histórico
# ---------------------------------------------------------------------------

@router.post("/tarefas/{task_id}/rodar")
async def rodar_agora(task_id: str) -> dict[str, Any]:
    service = _require_service()
    tarefa = await _get_or_404(task_id)
    job = await service.materialize(tarefa, reason="run_now")
    return {"ok": True, "job_id": str(job.id), "entrega": tarefa.notify_policy}


@router.post("/tarefas/{task_id}/simular")
async def simular(task_id: str) -> dict[str, Any]:
    """Roda de verdade, mas **sem entregar**: o resultado volta só nesta resposta.

    Não é dry-run de mentira: a tool/o turno rodam mesmo (é a única forma de
    saber se o prompt presta). O que muda é o canal — o disparo nasce `silent`.
    """
    service = _require_service()
    tarefa = await _get_or_404(task_id)
    job = await service.materialize(tarefa, reason="simulacao", deliver=False)
    return {
        "ok": True,
        "job_id": str(job.id),
        "nota": (
            "Disparo de teste enfileirado sem entrega. Acompanhe em /disparos: o "
            "resultado aparece lá quando terminar."
        ),
    }


@router.get("/tarefas/{task_id}/disparos")
async def disparos(task_id: str, limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    _require_service()
    tarefa = await _get_or_404(task_id)
    if _db is None:
        return {"disparos": []}
    jobs = await _db.list_jobs_of_scheduled_task(tarefa.id, limit=limit)
    return {
        "disparos": [
            {
                "job_id": str(j.id),
                "status": j.status,
                "titulo": j.title,
                "resultado": (j.result_summary or "")[:4000],
                "texto_entregue": (j.extra_metadata or {}).get("texto_literal"),
                "motivo": (j.extra_metadata or {}).get("reason"),
                "criado_em": j.created_at.isoformat() if j.created_at else None,
                "terminou_em": j.finished_at.isoformat() if j.finished_at else None,
                "entregue": bool(j.notified_at),
                "duracao_s": (
                    round((j.finished_at - j.started_at).total_seconds(), 1)
                    if j.started_at and j.finished_at else None
                ),
            }
            for j in jobs
        ]
    }


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------

@router.post("/pausar-tudo")
async def pausar_tudo(pausar: bool = Query(default=True)) -> dict[str, Any]:
    """Freio de emergência. Em memória de propósito: o restart solta o freio."""
    _require_service()
    if _tick is None:
        raise HTTPException(status_code=503, detail="Loop da agenda não está rodando.")
    _tick.paused = bool(pausar)
    logger.warning("[agenda] kill switch: paused=%s", _tick.paused)
    return {"pausado": _tick.paused}
