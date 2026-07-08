"""create_agent_job_table

Cria a tabela ``agent_job`` — trabalho assíncrono do próprio Neo (workbench
sandbox, timers/cron, subagentes). Ver docs/workbench-background-jobs-plan.md.

Também FUNDE (merge) as duas heads pré-existentes do histórico — a branch do PKS
(``e2f3a4b5c6d7``) e a branch dos devices (``h1i2j3k4l5m6``) — de volta a uma head
única, para que ``alembic upgrade head`` volte a funcionar. Ambas já estão
aplicadas em produção (estado multi-head), então este merge só acrescenta a nova
tabela por cima das duas.

Revision ID: i2j3k4l5m6n7
Revises: h1i2j3k4l5m6, e2f3a4b5c6d7
Create Date: 2026-07-07 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "i2j3k4l5m6n7"
down_revision: Union[str, Sequence[str], None] = ("h1i2j3k4l5m6", "e2f3a4b5c6d7")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return insp.has_table(table)


def upgrade() -> None:
    if _table_exists("agent_job"):
        return
    op.create_table(
        "agent_job",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("spec_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("progress_note", sa.Text(), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("log_path", sa.String(length=512), nullable=True),
        sa.Column("notify_policy", sa.String(length=16), nullable=False, server_default="chat"),
        sa.Column("schedule_expr", sa.String(length=128), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("timeout_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
        sa.Column("extra_metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_agent_job_user_id", "agent_job", ["user_id"])
    op.create_index("ix_agent_job_session_id", "agent_job", ["session_id"])
    op.create_index("ix_agent_job_kind", "agent_job", ["kind"])
    op.create_index("ix_agent_job_status", "agent_job", ["status"])
    op.create_index("ix_agent_job_next_run_at", "agent_job", ["next_run_at"])
    op.create_index("ix_agent_job_created_at", "agent_job", ["created_at"])


def downgrade() -> None:
    if not _table_exists("agent_job"):
        return
    for idx in (
        "ix_agent_job_created_at",
        "ix_agent_job_next_run_at",
        "ix_agent_job_status",
        "ix_agent_job_kind",
        "ix_agent_job_session_id",
        "ix_agent_job_user_id",
    ):
        try:
            op.drop_index(idx, table_name="agent_job")
        except Exception:
            pass
    op.drop_table("agent_job")
