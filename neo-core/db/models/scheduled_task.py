"""ScheduledTask — a REGRA de uma tarefa agendada (docs/scheduler-plan.md §4).

Definição ≠ execução. Esta tabela guarda **quando** e **o quê**; cada disparo
vira uma linha de ``agent_job`` (kind ``scheduled_prompt``/``scheduled_tool``/
``scheduled_notify``) com ``extra_metadata.scheduled_task_id`` apontando para
cá. Foi uma decisão deliberada (§3.1): guardar a recorrência na própria linha do
job quebraria a máquina de estados dele (terminal é terminal, ``notified_at``
existe para entregar UMA vez) e apagaria o histórico a cada disparo.

De brinde, o disparo herda de graça tudo que o JobManager e o ProactiveDispatcher
já sabem fazer: fila com semáforo, timeout, reconcile no boot, redação com
persona, replay offline e a nota no caderno de trabalho.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Column, Integer, String, Text, Uuid as SAUuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

#: Como o horário é calculado (§7.1).
SCHEDULE_KINDS = ("once", "interval", "cron")

#: O que o disparo faz (§5). ``prompt`` é o único que paga um turno cognitivo.
ACTION_KINDS = ("prompt", "tool", "notify", "job")

#: Modo de sessão do turno agendado (§6.1). O padrão NÃO é a sessão ativa.
SESSION_MODES = ("nova", "propria", "ativa")

#: O que fazer com disparo perdido (§7.3). O neo-core reinicia a cada save —
#: esta é a guarda mais importante do sistema neste repo.
MISFIRE_POLICIES = ("skip", "run_once", "run_all")

#: Disparo anterior ainda rodando (§7.4).
OVERLAP_POLICIES = ("skip", "queue", "cancel_previous")

#: Erros seguidos antes de pausar a tarefa. Mesma constante e mesma semântica
#: de ``core/events/router.py`` — uma agenda que falha em silêncio é pior que
#: não ter agenda, então pausar SEMPRE avisa o usuário.
MAX_CONSECUTIVE_ERRORS = 3


class ScheduledTask(SQLModel, table=True):
    """Uma regra de agenda: quando disparar, o que fazer, e com que guardas."""

    __tablename__ = "scheduled_task"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column("user_id", SAUuid, nullable=True, index=True),
    )
    name: str = Field(sa_column=Column("name", String(160), nullable=False))
    description: str = Field(
        default="", sa_column=Column("description", Text, nullable=False, server_default="")
    )
    enabled: bool = Field(
        default=True,
        sa_column=Column("enabled", Boolean, nullable=False, server_default="true", index=True),
    )

    # ── QUANDO ────────────────────────────────────────────────────────
    schedule_kind: str = Field(
        sa_column=Column("schedule_kind", String(16), nullable=False),
    )
    #: "2026-09-20T15:00" | "15m" | "0 7 * * 1-5"
    schedule_expr: str = Field(
        sa_column=Column("schedule_expr", String(160), nullable=False),
    )
    timezone: str = Field(
        default="America/Fortaleza",
        sa_column=Column("timezone", String(64), nullable=False, server_default="America/Fortaleza"),
    )
    #: {dias:[1..7], hora_inicio:"08:00", hora_fim:"22:00", respeitar_sono:bool}
    window_json: dict = Field(
        default_factory=dict,
        sa_column=Column("window_json", JSONB, nullable=False, server_default="{}"),
    )
    jitter_seconds: int = Field(
        default=0, sa_column=Column("jitter_seconds", Integer, nullable=False, server_default="0")
    )
    starts_at: Optional[datetime] = Field(
        default=None, sa_column=Column("starts_at", nullable=True)
    )
    ends_at: Optional[datetime] = Field(default=None, sa_column=Column("ends_at", nullable=True))
    max_runs: Optional[int] = Field(default=None, sa_column=Column("max_runs", Integer, nullable=True))
    misfire_policy: str = Field(
        default="skip",
        sa_column=Column("misfire_policy", String(16), nullable=False, server_default="skip"),
    )
    #: Carência (s) do ``run_once``: perdeu por menos que isso, ainda roda.
    grace_seconds: int = Field(
        default=600, sa_column=Column("grace_seconds", Integer, nullable=False, server_default="600")
    )
    overlap_policy: str = Field(
        default="skip",
        sa_column=Column("overlap_policy", String(16), nullable=False, server_default="skip"),
    )

    # ── O QUÊ ─────────────────────────────────────────────────────────
    action_kind: str = Field(sa_column=Column("action_kind", String(16), nullable=False))
    #: prompt {texto} | tool {tool, arguments} | notify {titulo, corpo} |
    #: job {kind, spec}
    action_json: dict = Field(
        default_factory=dict,
        sa_column=Column("action_json", JSONB, nullable=False, server_default="{}"),
    )
    #: {allow:[], deny:[], client_side:false, max_llm_calls:int, max_seconds:int}
    tool_policy_json: dict = Field(
        default_factory=dict,
        sa_column=Column("tool_policy_json", JSONB, nullable=False, server_default="{}"),
    )
    session_mode: str = Field(
        default="nova",
        sa_column=Column("session_mode", String(16), nullable=False, server_default="nova"),
    )
    session_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("session_id", SAUuid, nullable=True)
    )

    # ── GUARDAS (§8) ──────────────────────────────────────────────────
    #: null | {tipo: tool|device|http|sempre, ...}
    condition_json: dict = Field(
        default_factory=dict,
        sa_column=Column("condition_json", JSONB, nullable=False, server_default="{}"),
    )
    #: Só entrega se o resultado MUDOU desde o disparo anterior. É o antídoto da
    #: fadiga de alerta: sem isto, todo vigia periódico vira ruído em uma semana.
    only_if_changed: bool = Field(
        default=False,
        sa_column=Column("only_if_changed", Boolean, nullable=False, server_default="false"),
    )
    last_fingerprint: Optional[str] = Field(
        default=None, sa_column=Column("last_fingerprint", String(64), nullable=True)
    )

    # ── ENTREGA ───────────────────────────────────────────────────────
    #: chat | notch | both | silent (os mesmos do ProactiveDispatcher).
    notify_policy: str = Field(
        default="chat",
        sa_column=Column("notify_policy", String(16), nullable=False, server_default="chat"),
    )
    #: {inicio:"23:00", fim:"07:00", acao: adiar|silenciar|forcar}
    quiet_hours_json: dict = Field(
        default_factory=dict,
        sa_column=Column("quiet_hours_json", JSONB, nullable=False, server_default="{}"),
    )

    # ── ESTADO ────────────────────────────────────────────────────────
    next_run_at: Optional[datetime] = Field(
        default=None, sa_column=Column("next_run_at", nullable=True, index=True)
    )
    last_run_at: Optional[datetime] = Field(
        default=None, sa_column=Column("last_run_at", nullable=True)
    )
    last_job_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("last_job_id", SAUuid, nullable=True)
    )
    last_status: str = Field(
        default="", sa_column=Column("last_status", String(32), nullable=False, server_default="")
    )
    run_count: int = Field(
        default=0, sa_column=Column("run_count", Integer, nullable=False, server_default="0")
    )
    #: Disparos que a condição barrou. NÃO contam como run nem como erro — mas
    #: contar quantas vezes a guarda salvou um turno é o que diz se ela presta.
    skipped_count: int = Field(
        default=0, sa_column=Column("skipped_count", Integer, nullable=False, server_default="0")
    )
    consecutive_errors: int = Field(
        default=0,
        sa_column=Column("consecutive_errors", Integer, nullable=False, server_default="0"),
    )
    paused_reason: str = Field(
        default="", sa_column=Column("paused_reason", Text, nullable=False, server_default="")
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow, sa_column=Column("created_at", nullable=False)
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow, sa_column=Column("updated_at", nullable=False)
    )
