"""Modelos do módulo de finanças pessoais (skill ``financas``).

Ledger relacional dedicado — NÃO mora no PKS de propósito: transação financeira
é dado de alto volume que vive de agregação (SUM/GROUP BY mês/categoria), coisa
que o modelo entidade-campo do PKS não faz. O PKS segue dono do conhecimento
semântico ("prefere pagar tudo no Pix"); estas tabelas são o extrato.

Dinheiro é SEMPRE centavos inteiros (BIGINT) em BRL — nunca float.
``invoice_month`` é o 1º dia do mês da fatura do cartão (DATE), calculado no
insert a partir do ``closing_day`` do cartão. Deleção de transação é soft
(``deleted_at``), igual ao PKS.
"""

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, Column, Date, SmallInteger, String, Text, Uuid as SAUuid
from sqlmodel import Field, SQLModel

# Valores válidos (validados na camada de tools; o banco guarda texto).
ACCOUNT_KINDS = ("corrente", "poupanca", "carteira", "vale", "investimento", "outro")
CARD_BRANDS = ("visa", "mastercard", "elo", "amex", "hipercard", "outro")
CATEGORY_KINDS = ("despesa", "receita")
TRANSACTION_KINDS = (
    "despesa", "receita", "transf_saida", "transf_entrada", "pagamento_fatura"
)
PAYMENT_METHODS = (
    "pix", "debito", "credito", "boleto", "dinheiro", "ted", "transferencia",
    "vale", "outro",
)
RECURRING_FREQUENCIES = ("mensal", "semanal", "anual")


class FinAccount(SQLModel, table=True):
    """Conta: corrente, poupança, carteira (dinheiro vivo), vale (VR/VA), etc."""

    __tablename__ = "fin_account"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        sa_column=Column("user_id", SAUuid, nullable=False, index=True),
    )
    name: str = Field(sa_column=Column("name", String(80), nullable=False))
    kind: str = Field(
        default="corrente",
        sa_column=Column("kind", String(20), nullable=False),
    )
    institution: Optional[str] = Field(
        default=None, sa_column=Column("institution", String(80), nullable=True),
    )
    initial_balance_cents: int = Field(
        default=0,
        sa_column=Column("initial_balance_cents", BigInteger, nullable=False),
    )
    is_default: bool = Field(
        default=False, sa_column=Column("is_default", Boolean, nullable=False),
    )
    archived_at: Optional[datetime] = Field(
        default=None, sa_column=Column("archived_at", nullable=True),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("updated_at", nullable=False),
    )


class FinCard(SQLModel, table=True):
    """Cartão de CRÉDITO (débito é payment_method sobre a conta, não entidade)."""

    __tablename__ = "fin_card"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        sa_column=Column("user_id", SAUuid, nullable=False, index=True),
    )
    name: str = Field(sa_column=Column("name", String(80), nullable=False))
    brand: Optional[str] = Field(
        default=None, sa_column=Column("brand", String(20), nullable=True),
    )
    last4: Optional[str] = Field(
        default=None, sa_column=Column("last4", String(4), nullable=True),
    )
    limit_cents: Optional[int] = Field(
        default=None, sa_column=Column("limit_cents", BigInteger, nullable=True),
    )
    # Dia do fechamento da fatura (compra NO dia do fechamento ou depois → cai
    # na fatura seguinte). NULL = fatura do mês civil da compra.
    closing_day: Optional[int] = Field(
        default=None, sa_column=Column("closing_day", SmallInteger, nullable=True),
    )
    due_day: Optional[int] = Field(
        default=None, sa_column=Column("due_day", SmallInteger, nullable=True),
    )
    # Conta que costuma pagar a fatura (default do fin_transfer).
    account_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("account_id", SAUuid, nullable=True),
    )
    archived_at: Optional[datetime] = Field(
        default=None, sa_column=Column("archived_at", nullable=True),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("updated_at", nullable=False),
    )


class FinCategory(SQLModel, table=True):
    """Categoria em 2 níveis (parent_id) — seed brasileiro em modules/finance."""

    __tablename__ = "fin_category"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        sa_column=Column("user_id", SAUuid, nullable=False, index=True),
    )
    name: str = Field(sa_column=Column("name", String(60), nullable=False))
    parent_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("parent_id", SAUuid, nullable=True),
    )
    kind: str = Field(sa_column=Column("kind", String(10), nullable=False))
    is_system: bool = Field(
        default=False, sa_column=Column("is_system", Boolean, nullable=False),
    )
    archived_at: Optional[datetime] = Field(
        default=None, sa_column=Column("archived_at", nullable=True),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", nullable=False),
    )


class FinTransaction(SQLModel, table=True):
    """Um lançamento do extrato. amount_cents é sempre POSITIVO; kind dá o sinal.

    Compra no crédito: card_id set, account_id NULL (só o pagamento_fatura
    debita a conta — evita contar a despesa duas vezes). Transferência são 2
    linhas (transf_saida/transf_entrada) amarradas por transfer_group.
    """

    __tablename__ = "fin_transaction"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        sa_column=Column("user_id", SAUuid, nullable=False, index=True),
    )
    kind: str = Field(sa_column=Column("kind", String(20), nullable=False, index=True))
    amount_cents: int = Field(
        sa_column=Column("amount_cents", BigInteger, nullable=False),
    )
    description: str = Field(
        sa_column=Column("description", String(200), nullable=False),
    )
    merchant: Optional[str] = Field(
        default=None, sa_column=Column("merchant", String(120), nullable=True),
    )
    category_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("category_id", SAUuid, nullable=True, index=True),
    )
    account_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("account_id", SAUuid, nullable=True, index=True),
    )
    card_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("card_id", SAUuid, nullable=True, index=True),
    )
    payment_method: Optional[str] = Field(
        default=None, sa_column=Column("payment_method", String(20), nullable=True),
    )
    occurred_at: date = Field(
        sa_column=Column("occurred_at", Date, nullable=False, index=True),
    )
    invoice_month: Optional[date] = Field(
        default=None, sa_column=Column("invoice_month", Date, nullable=True, index=True),
    )
    installment_plan_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column("installment_plan_id", SAUuid, nullable=True, index=True),
    )
    installment_no: Optional[int] = Field(
        default=None, sa_column=Column("installment_no", SmallInteger, nullable=True),
    )
    transfer_group: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("transfer_group", SAUuid, nullable=True),
    )
    notes: Optional[str] = Field(
        default=None, sa_column=Column("notes", Text, nullable=True),
    )
    source: str = Field(
        default="chat", sa_column=Column("source", String(20), nullable=False),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("updated_at", nullable=False),
    )
    deleted_at: Optional[datetime] = Field(
        default=None, sa_column=Column("deleted_at", nullable=True),
    )


class FinInstallmentPlan(SQLModel, table=True):
    """Compra parcelada no cartão — as parcelas viram N FinTransaction (uma por
    invoice_month), com o ajuste de centavos na 1ª parcela."""

    __tablename__ = "fin_installment_plan"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        sa_column=Column("user_id", SAUuid, nullable=False, index=True),
    )
    card_id: uuid.UUID = Field(
        sa_column=Column("card_id", SAUuid, nullable=False, index=True),
    )
    description: str = Field(
        sa_column=Column("description", String(200), nullable=False),
    )
    merchant: Optional[str] = Field(
        default=None, sa_column=Column("merchant", String(120), nullable=True),
    )
    category_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("category_id", SAUuid, nullable=True),
    )
    total_cents: int = Field(
        sa_column=Column("total_cents", BigInteger, nullable=False),
    )
    n_installments: int = Field(
        sa_column=Column("n_installments", SmallInteger, nullable=False),
    )
    purchase_date: date = Field(
        sa_column=Column("purchase_date", Date, nullable=False),
    )
    first_invoice_month: date = Field(
        sa_column=Column("first_invoice_month", Date, nullable=False),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", nullable=False),
    )
    canceled_at: Optional[datetime] = Field(
        default=None, sa_column=Column("canceled_at", nullable=True),
    )


class FinRecurring(SQLModel, table=True):
    """Assinatura/conta fixa (aluguel, streaming, salário). amount_cents NULL =
    valor variável (conta de luz). O lançamento em si continua manual — isto é
    o cadastro que alimenta lembretes e o comprometimento nos insights."""

    __tablename__ = "fin_recurring"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        sa_column=Column("user_id", SAUuid, nullable=False, index=True),
    )
    description: str = Field(
        sa_column=Column("description", String(200), nullable=False),
    )
    kind: str = Field(sa_column=Column("kind", String(10), nullable=False))
    amount_cents: Optional[int] = Field(
        default=None, sa_column=Column("amount_cents", BigInteger, nullable=True),
    )
    category_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("category_id", SAUuid, nullable=True),
    )
    payment_method: Optional[str] = Field(
        default=None, sa_column=Column("payment_method", String(20), nullable=True),
    )
    account_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("account_id", SAUuid, nullable=True),
    )
    card_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("card_id", SAUuid, nullable=True),
    )
    due_day: Optional[int] = Field(
        default=None, sa_column=Column("due_day", SmallInteger, nullable=True),
    )
    frequency: str = Field(
        default="mensal", sa_column=Column("frequency", String(10), nullable=False),
    )
    active: bool = Field(
        default=True, sa_column=Column("active", Boolean, nullable=False),
    )
    notes: Optional[str] = Field(
        default=None, sa_column=Column("notes", Text, nullable=True),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("updated_at", nullable=False),
    )


class FinBudget(SQLModel, table=True):
    """Orçamento (teto de gasto) por categoria. month NULL = vale todo mês;
    category_id NULL = teto global de despesas."""

    __tablename__ = "fin_budget"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        sa_column=Column("user_id", SAUuid, nullable=False, index=True),
    )
    category_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("category_id", SAUuid, nullable=True),
    )
    month: Optional[date] = Field(
        default=None, sa_column=Column("month", Date, nullable=True),
    )
    amount_cents: int = Field(
        sa_column=Column("amount_cents", BigInteger, nullable=False),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("updated_at", nullable=False),
    )
