"""create_service_gateway_tables

Cria as tabelas do Service Gateway (docs/service-gateway-dsl-plan.md §3.2):
``service_connection`` (registro central de conexões — host + auth POR
REFERÊNCIA + política; sem segredo) e ``service_request_log`` (auditoria de toda
requisição: allow/deny/confirm/erro, path/erro redactados, sem header de auth).
Ver db/models/gateway.py para as decisões de modelagem.

Revision ID: m6n7o8p9q0r1
Revises: l5m6n7o8p9q0
Create Date: 2026-07-16 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "m6n7o8p9q0r1"
down_revision: Union[str, Sequence[str], None] = "l5m6n7o8p9q0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return insp.has_table(table)


def upgrade() -> None:
    if not _table_exists("service_connection"):
        op.create_table(
            "service_connection",
            sa.Column("id", sa.String(length=40), primary_key=True),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("provider", sa.String(length=40), nullable=False, server_default="generic"),
            sa.Column("base_url", sa.String(length=500), nullable=False),
            sa.Column("auth_type", sa.String(length=40), nullable=False, server_default="none"),
            sa.Column("auth_config", postgresql.JSONB(), nullable=False, server_default="{}"),
            sa.Column("credential_ref", sa.String(length=200), nullable=True),
            sa.Column("default_headers", postgresql.JSONB(), nullable=False, server_default="{}"),
            sa.Column("timeout_s", sa.Numeric(6, 2), nullable=False, server_default="15"),
            sa.Column("retry_max", sa.Integer(), nullable=False, server_default="2"),
            sa.Column("verify_tls", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("allow_http", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("follow_redirects", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("max_body_bytes", sa.Integer(), nullable=False, server_default="262144"),
            sa.Column("max_response_bytes", sa.Integer(), nullable=False, server_default="65536"),
            sa.Column("default_read", sa.String(length=10), nullable=False, server_default="allow"),
            sa.Column("default_write", sa.String(length=10), nullable=False, server_default="confirm"),
            sa.Column("policies", postgresql.JSONB(), nullable=False, server_default="[]"),
            sa.Column("docs_url", sa.String(length=500), nullable=True),
            sa.Column("openapi_ref", sa.String(length=500), nullable=True),
            sa.Column("capabilities", postgresql.JSONB(), nullable=False, server_default="{}"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("managed_by", sa.String(length=20), nullable=False, server_default="panel"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )

    if not _table_exists("service_request_log"):
        op.create_table(
            "service_request_log",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("ts", sa.DateTime(), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("source", sa.String(length=20), nullable=False, server_default="chat"),
            sa.Column("connection_id", sa.String(length=40), nullable=False),
            sa.Column("method", sa.String(length=10), nullable=False),
            sa.Column("path", sa.String(length=1000), nullable=False),
            sa.Column("policy_decision", sa.String(length=20), nullable=False),
            sa.Column("policy_reason", sa.String(length=300), nullable=True),
            sa.Column("status", sa.Integer(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("req_bytes", sa.Integer(), nullable=True),
            sa.Column("resp_bytes", sa.Integer(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("request_id", sa.String(length=64), nullable=True),
        )
        op.create_index("ix_service_request_log_ts", "service_request_log", ["ts"])
        op.create_index("ix_service_request_log_user_id", "service_request_log", ["user_id"])
        op.create_index(
            "ix_service_request_log_connection_id", "service_request_log", ["connection_id"]
        )


def downgrade() -> None:
    if _table_exists("service_request_log"):
        op.drop_table("service_request_log")
    if _table_exists("service_connection"):
        op.drop_table("service_connection")
