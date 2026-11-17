"""create_research_task_table

Cria a tabela ``research_task`` — pesquisa profunda em background (deep_research),
1:1 com um agent_job de kind="research". Ver docs/web-search-deep-research-plan.md §4.

Revision ID: j3k4l5m6n7o8
Revises: i2j3k4l5m6n7
Create Date: 2026-07-11 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "j3k4l5m6n7o8"
down_revision: Union[str, Sequence[str], None] = "i2j3k4l5m6n7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return insp.has_table(table)


def upgrade() -> None:
    if _table_exists("research_task"):
        return
    op.create_table(
        "research_task",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("depth", sa.String(length=16), nullable=False, server_default="standard"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("model", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("backend", sa.String(length=32), nullable=True),
        sa.Column("stats_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("sources_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("report_markdown", sa.Text(), nullable=True),
        sa.Column("report_path", sa.String(length=512), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_research_task_job_id", "research_task", ["job_id"], unique=True)
    op.create_index("ix_research_task_user_id", "research_task", ["user_id"])
    op.create_index("ix_research_task_status", "research_task", ["status"])
    op.create_index("ix_research_task_created_at", "research_task", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_research_task_created_at", table_name="research_task")
    op.drop_index("ix_research_task_status", table_name="research_task")
    op.drop_index("ix_research_task_user_id", table_name="research_task")
    op.drop_index("ix_research_task_job_id", table_name="research_task")
    op.drop_table("research_task")
