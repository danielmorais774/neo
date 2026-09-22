"""create_scheduled_task_table

Agenda do Neo (docs/scheduler-plan.md §4): a REGRA de uma tarefa agendada.
Cada disparo continua sendo uma linha de ``agent_job`` — esta tabela só guarda
quando disparar, o que fazer e com que guardas.

Aditiva: nada existente é tocado. Rollback = desligar a flag e dropar a tabela.

Revision ID: y8z9a0b1c2d3
Revises: x7y8z9a0b1c2
Create Date: 2026-09-12 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision: str = "y8z9a0b1c2d3"
down_revision: Union[str, Sequence[str], None] = "x7y8z9a0b1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    return inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if _table_exists("scheduled_task"):
        return
    op.create_table(
        "scheduled_task",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        # quando
        sa.Column("schedule_kind", sa.String(length=16), nullable=False),
        sa.Column("schedule_expr", sa.String(length=160), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="America/Fortaleza"),
        sa.Column("window_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("jitter_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("starts_at", sa.DateTime(), nullable=True),
        sa.Column("ends_at", sa.DateTime(), nullable=True),
        sa.Column("max_runs", sa.Integer(), nullable=True),
        sa.Column("misfire_policy", sa.String(length=16), nullable=False, server_default="skip"),
        sa.Column("grace_seconds", sa.Integer(), nullable=False, server_default="600"),
        sa.Column("overlap_policy", sa.String(length=16), nullable=False, server_default="skip"),
        # o quê
        sa.Column("action_kind", sa.String(length=16), nullable=False),
        sa.Column("action_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("tool_policy_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("session_mode", sa.String(length=16), nullable=False, server_default="nova"),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        # guardas
        sa.Column("condition_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("only_if_changed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("last_fingerprint", sa.String(length=64), nullable=True),
        # entrega
        sa.Column("notify_policy", sa.String(length=16), nullable=False, server_default="chat"),
        sa.Column("quiet_hours_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        # estado
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_status", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consecutive_errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paused_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_scheduled_task_user", "scheduled_task", ["user_id"])
    # O tick pergunta sempre a mesma coisa: "o que está ligado e venceu?".
    op.create_index("ix_scheduled_task_due", "scheduled_task", ["enabled", "next_run_at"])


def downgrade() -> None:
    if not _table_exists("scheduled_task"):
        return
    op.drop_index("ix_scheduled_task_due", table_name="scheduled_task")
    op.drop_index("ix_scheduled_task_user", table_name="scheduled_task")
    op.drop_table("scheduled_task")
