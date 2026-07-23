"""AgentJob model — trabalho assíncrono do próprio Neo (workbench/scheduler).

Generaliza o padrão de ``mission`` (lifecycle persistido no DB) para qualquer
tarefa que o Neo roda em segundo plano: um comando no sandbox (``sandbox_task``),
um timer/cron (``timer``/``cron``) ou um subagente (``subagent``, F5). O JobManager
consome esta tabela; o ProactiveDispatcher entrega o resultado no chat quando
termina. Ver ``docs/workbench-background-jobs-plan.md``.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Integer, String, Text, Uuid as SAUuid, JSON
from sqlmodel import Field, SQLModel

# Estados terminais — o dispatcher só entrega uma vez, ao alcançar um destes.
TERMINAL_STATUSES = frozenset({"completed", "failed", "timeout", "cancelled"})


class AgentJob(SQLModel, table=True):
    __tablename__ = "agent_job"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column("user_id", SAUuid, nullable=True, index=True),
    )
    session_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column("session_id", SAUuid, nullable=True, index=True),
    )
    # sandbox_task | research | browser | timer | cron | subagent
    kind: str = Field(
        sa_column=Column("kind", String(32), nullable=False, index=True),
    )
    title: str = Field(
        sa_column=Column("title", String(256), nullable=False),
    )
    # Payload por kind: {command, cwd, timeout} | {message, goal} | {cron_expr, goal}
    spec_json: dict = Field(
        default_factory=dict,
        sa_column=Column("spec_json", JSON, nullable=False),
    )
    # queued | running | completed | failed | timeout | cancelled
    status: str = Field(
        default="queued",
        sa_column=Column("status", String(32), nullable=False, index=True),
    )
    progress_note: Optional[str] = Field(
        default=None,
        sa_column=Column("progress_note", Text, nullable=True),
    )
    result_summary: Optional[str] = Field(
        default=None,
        sa_column=Column("result_summary", Text, nullable=True),
    )
    exit_code: Optional[int] = Field(
        default=None,
        sa_column=Column("exit_code", Integer, nullable=True),
    )
    # Caminho do log dentro do sandbox (/workspace/.jobs/<id>.log).
    log_path: Optional[str] = Field(
        default=None,
        sa_column=Column("log_path", String(512), nullable=True),
    )
    # chat | notch | both | silent — como o dispatcher entrega o resultado.
    notify_policy: str = Field(
        default="chat",
        sa_column=Column("notify_policy", String(16), nullable=False),
    )
    # Agendamento (timer/cron). next_run_at nulo = rodar assim que houver slot.
    schedule_expr: Optional[str] = Field(
        default=None,
        sa_column=Column("schedule_expr", String(128), nullable=True),
    )
    next_run_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("next_run_at", nullable=True, index=True),
    )
    timeout_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("timeout_at", nullable=True),
    )
    heartbeat_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("heartbeat_at", nullable=True),
    )
    # Marca que o dispatcher já entregou o resultado deste job (evita duplicar).
    notified_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("notified_at", nullable=True),
    )
    extra_metadata: dict = Field(
        default_factory=dict,
        sa_column=Column("extra_metadata", JSON, nullable=False),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", nullable=False, index=True),
    )
    started_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("started_at", nullable=True),
    )
    finished_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("finished_at", nullable=True),
    )
