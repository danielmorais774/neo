"""neo-panel → Configurações → Claude Code: liga e desliga o Claude Code integrado.

GET devolve o estado; PUT grava e vale na hora, sem reiniciar
(``core/claude_code/chave.py``). Desligado, o Neo não oferece nem executa
``claude_task``/``claude_reply``; tarefa que já está rodando termina sozinha.

Protegido pelo JWT do painel (ligado em ``api/main.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.claude_code import chave
from db.async_client import AsyncPostgresClient

router = APIRouter(prefix="/claude-code-config", tags=["claude-code-config"])

_db: AsyncPostgresClient | None = None
_runner_montado = False


def set_dependencies(db: AsyncPostgresClient, *, runner_montado: bool) -> None:
    """Chamado no lifespan do app. Sem o runner (jobs desligados), ligar não adianta."""
    global _db, _runner_montado
    _db = db
    _runner_montado = runner_montado


class ClaudeCodeConfigUpdate(BaseModel):
    enabled: bool


async def _payload() -> dict:
    ativas = 0
    if _db is not None:
        try:
            jobs = await _db.list_agent_jobs(None, statuses=["queued", "running", "waiting"], limit=50)
            ativas = sum(1 for j in jobs if j.kind == "claude_code")
        except Exception:  # noqa: BLE001 — a contagem é informativa
            pass
    return {
        "enabled": chave.ligado(),
        # "env" = o painel nunca mexeu e vale o CLAUDE_CODE_ENABLED do .env
        "source": chave.origem(),
        "env_default": chave.padrao(),
        "available": _runner_montado,
        "active_tasks": ativas,
    }


@router.get("")
async def get_config() -> dict:
    if _db is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    return await _payload()


@router.put("")
async def update_config(payload: ClaudeCodeConfigUpdate) -> dict:
    if _db is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    if payload.enabled and not _runner_montado:
        raise HTTPException(
            status_code=409,
            detail="o runner do Claude não está montado (JOBS_ENABLED desligado no .env)",
        )
    await chave.gravar(_db, payload.enabled)
    return await _payload()
