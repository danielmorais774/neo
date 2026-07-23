"""create_finance_tables

Cria as tabelas ``fin_*`` do módulo de finanças pessoais (skill ``financas``):
contas, cartões de crédito, categorias (2 níveis, seed BR feito pela aplicação),
transações (extrato), planos de parcelamento, recorrências e orçamentos.
Ver db/models/finance.py para as decisões de modelagem.

Revision ID: l5m6n7o8p9q0
Revises: k4l5m6n7o8p9
Create Date: 2026-07-13 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "l5m6n7o8p9q0"
down_revision: Union[str, Sequence[str], None] = "k4l5m6n7o8p9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return insp.has_table(table)


def upgrade() -> None:
    if not _table_exists("fin_account"):
        op.create_table(
            "fin_account",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.Column("kind", sa.String(length=20), nullable=False, server_default="corrente"),
            sa.Column("institution", sa.String(length=80), nullable=True),
            sa.Column("initial_balance_cents", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("archived_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_fin_account_user_id", "fin_account", ["user_id"])

    if not _table_exists("fin_card"):
        op.create_table(
            "fin_card",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.Column("brand", sa.String(length=20), nullable=True),
            sa.Column("last4", sa.String(length=4), nullable=True),
            sa.Column("limit_cents", sa.BigInteger(), nullable=True),
            sa.Column("closing_day", sa.SmallInteger(), nullable=True),
            sa.Column("due_day", sa.SmallInteger(), nullable=True),
            sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("archived_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_fin_card_user_id", "fin_card", ["user_id"])

    if not _table_exists("fin_category"):
        op.create_table(
            "fin_category",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("name", sa.String(length=60), nullable=False),
            sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("kind", sa.String(length=10), nullable=False),
            sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("archived_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_fin_category_user_id", "fin_category", ["user_id"])

    if not _table_exists("fin_transaction"):
        op.create_table(
            "fin_transaction",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("kind", sa.String(length=20), nullable=False),
            sa.Column("amount_cents", sa.BigInteger(), nullable=False),
            sa.Column("description", sa.String(length=200), nullable=False),
            sa.Column("merchant", sa.String(length=120), nullable=True),
            sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("card_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("payment_method", sa.String(length=20), nullable=True),
            sa.Column("occurred_at", sa.Date(), nullable=False),
            sa.Column("invoice_month", sa.Date(), nullable=True),
            sa.Column("installment_plan_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("installment_no", sa.SmallInteger(), nullable=True),
            sa.Column("transfer_group", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("source", sa.String(length=20), nullable=False, server_default="chat"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_fin_transaction_user_id", "fin_transaction", ["user_id"])
        op.create_index("ix_fin_transaction_kind", "fin_transaction", ["kind"])
        op.create_index("ix_fin_transaction_category_id", "fin_transaction", ["category_id"])
        op.create_index("ix_fin_transaction_account_id", "fin_transaction", ["account_id"])
        op.create_index("ix_fin_transaction_card_id", "fin_transaction", ["card_id"])
        op.create_index("ix_fin_transaction_occurred_at", "fin_transaction", ["occurred_at"])
        op.create_index("ix_fin_transaction_invoice_month", "fin_transaction", ["invoice_month"])
        op.create_index(
            "ix_fin_transaction_installment_plan_id", "fin_transaction", ["installment_plan_id"]
        )

    if not _table_exists("fin_installment_plan"):
        op.create_table(
            "fin_installment_plan",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("card_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("description", sa.String(length=200), nullable=False),
            sa.Column("merchant", sa.String(length=120), nullable=True),
            sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("total_cents", sa.BigInteger(), nullable=False),
            sa.Column("n_installments", sa.SmallInteger(), nullable=False),
            sa.Column("purchase_date", sa.Date(), nullable=False),
            sa.Column("first_invoice_month", sa.Date(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("canceled_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_fin_installment_plan_user_id", "fin_installment_plan", ["user_id"])
        op.create_index("ix_fin_installment_plan_card_id", "fin_installment_plan", ["card_id"])

    if not _table_exists("fin_recurring"):
        op.create_table(
            "fin_recurring",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("description", sa.String(length=200), nullable=False),
            sa.Column("kind", sa.String(length=10), nullable=False),
            sa.Column("amount_cents", sa.BigInteger(), nullable=True),
            sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("payment_method", sa.String(length=20), nullable=True),
            sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("card_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("due_day", sa.SmallInteger(), nullable=True),
            sa.Column("frequency", sa.String(length=10), nullable=False, server_default="mensal"),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_fin_recurring_user_id", "fin_recurring", ["user_id"])

    if not _table_exists("fin_budget"):
        op.create_table(
            "fin_budget",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("month", sa.Date(), nullable=True),
            sa.Column("amount_cents", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_fin_budget_user_id", "fin_budget", ["user_id"])


def downgrade() -> None:
    for table in (
        "fin_budget",
        "fin_recurring",
        "fin_installment_plan",
        "fin_transaction",
        "fin_category",
        "fin_card",
        "fin_account",
    ):
        if _table_exists(table):
            op.drop_table(table)
