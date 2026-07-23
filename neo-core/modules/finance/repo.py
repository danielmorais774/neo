"""Camada de dados do módulo de finanças — CRUD + agregações sobre fin_*.

Volumes são pessoais (milhares de linhas, não milhões): resolução fuzzy de
nomes é feita em Python sobre a lista do usuário, e as agregações em SQL puro.
Toda função recebe ``user_id`` explícito — nada aqui é global.
"""

from __future__ import annotations

import logging
import unicodedata
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, text
from sqlmodel import select

from db.models.finance import (
    FinAccount,
    FinBudget,
    FinCard,
    FinCategory,
    FinInstallmentPlan,
    FinRecurring,
    FinTransaction,
)
from modules.finance.categories import (
    DEFAULT_EXPENSE_CATEGORIES,
    DEFAULT_INCOME_CATEGORIES,
)
from modules.finance.money import add_months, invoice_month_for, split_installments

logger = logging.getLogger(__name__)


def _fold(text_value: str) -> str:
    """lower + sem acentos, para casar 'Cartao nubank' com 'Cartão Nubank'."""
    normalized = unicodedata.normalize("NFKD", text_value or "")
    return "".join(c for c in normalized if not unicodedata.combining(c)).lower().strip()


class AmbiguousNameError(ValueError):
    """Mais de um item casa com o nome — a mensagem lista as opções."""


class FinanceRepo:
    def __init__(self, db: Any) -> None:
        self._db = db

    # ── Seed ─────────────────────────────────────────────────────────────

    async def ensure_defaults(self, user_id: UUID) -> str | None:
        """Primeira vez do usuário: semeia categorias BR + conta Carteira.

        Retorna uma nota humana quando semeou (para o modelo avisar), ou None.
        """
        async with self._db.get_session() as db:
            has_cat = (
                await db.execute(
                    select(FinCategory.id).where(FinCategory.user_id == user_id).limit(1)
                )
            ).first()
            if has_cat:
                return None
            for kind, groups in (
                ("despesa", DEFAULT_EXPENSE_CATEGORIES),
                ("receita", DEFAULT_INCOME_CATEGORIES),
            ):
                for parent_name, children in groups:
                    parent = FinCategory(
                        user_id=user_id, name=parent_name, kind=kind, is_system=True
                    )
                    db.add(parent)
                    for child_name in children:
                        db.add(
                            FinCategory(
                                user_id=user_id, name=child_name, kind=kind,
                                parent_id=parent.id, is_system=True,
                            )
                        )
            has_account = (
                await db.execute(
                    select(FinAccount.id).where(FinAccount.user_id == user_id).limit(1)
                )
            ).first()
            if not has_account:
                db.add(
                    FinAccount(
                        user_id=user_id, name="Carteira", kind="carteira",
                        is_default=True,
                    )
                )
            await db.commit()
        return (
            "Primeiro uso: criei as categorias padrão (Alimentação, Transporte, "
            "Moradia, ...) e a conta 'Carteira' (dinheiro vivo)."
        )

    # ── Resolução fuzzy por nome ─────────────────────────────────────────

    @staticmethod
    def _match_by_name(items: list[Any], query: str, *, label: str) -> Any:
        """exact → startswith → contains, sem acento/caixa. Ambíguo → erro com opções."""
        q = _fold(query)
        if not q:
            raise ValueError(f"Nome de {label} vazio.")
        names = [(item, _fold(item.name)) for item in items]
        exact = [item for item, name in names if name == q]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            # Dois cadastros com o MESMO nome: escolher em silêncio manda o pagamento
            # para o cartão errado (havia dois "Nubank" em prod, um deles sem fatura).
            ids = ", ".join(str(i.id)[:8] for i in exact)
            raise AmbiguousNameError(
                f"Existem {len(exact)} {label}s com o nome exato '{query}' (ids {ids}) — "
                f"cadastro duplicado. Peça ao usuário para apagar ou renomear um deles "
                f"(fin_manage) antes de continuar."
            )
        tiers = (
            [item for item, name in names if name.startswith(q)],
            [item for item, name in names if q in name],
            # Reverso: "cartão nubank" deve casar com o cartão chamado "Nubank".
            [item for item, name in names if len(name) >= 3 and name in q],
        )
        for tier in tiers:
            if len(tier) == 1:
                return tier[0]
            if len(tier) > 1:
                options = ", ".join(sorted(i.name for i in tier))
                raise AmbiguousNameError(
                    f"Mais de um(a) {label} casa com '{query}': {options}. Qual deles?"
                )
        existing = ", ".join(sorted(i.name for i in items)) or "(nenhum cadastrado)"
        raise ValueError(
            f"Não achei {label} '{query}'. Cadastrados: {existing}. "
            f"Cadastre com fin_manage se for novo."
        )

    async def list_accounts(self, user_id: UUID, include_archived: bool = False) -> list[FinAccount]:
        async with self._db.get_session() as db:
            stmt = select(FinAccount).where(FinAccount.user_id == user_id)
            if not include_archived:
                stmt = stmt.where(FinAccount.archived_at.is_(None))
            return list((await db.execute(stmt.order_by(FinAccount.name))).scalars().all())

    async def list_cards(self, user_id: UUID, include_archived: bool = False) -> list[FinCard]:
        async with self._db.get_session() as db:
            stmt = select(FinCard).where(FinCard.user_id == user_id)
            if not include_archived:
                stmt = stmt.where(FinCard.archived_at.is_(None))
            return list((await db.execute(stmt.order_by(FinCard.name))).scalars().all())

    async def list_categories(self, user_id: UUID) -> list[FinCategory]:
        async with self._db.get_session() as db:
            stmt = (
                select(FinCategory)
                .where(FinCategory.user_id == user_id)
                .where(FinCategory.archived_at.is_(None))
            )
            return list((await db.execute(stmt.order_by(FinCategory.name))).scalars().all())

    @staticmethod
    def _match_by_id(items: list[Any], query: str) -> Any | None:
        """Prefixo de id (≥6 hex). Saída do impasse quando dois cadastros têm o MESMO
        nome: sem isto, o usuário não conseguiria nem apagar o duplicado."""
        clean = (query or "").strip().lower().replace("-", "")
        if len(clean) < 6 or not all(c in "0123456789abcdef" for c in clean):
            return None
        hits = [i for i in items if str(i.id).replace("-", "").startswith(clean)]
        return hits[0] if len(hits) == 1 else None

    async def resolve_account(self, user_id: UUID, name: str) -> FinAccount:
        accounts = await self.list_accounts(user_id)
        return self._match_by_id(accounts, name) or self._match_by_name(
            accounts, name, label="conta"
        )

    async def resolve_card(self, user_id: UUID, name: str) -> FinCard:
        cards = await self.list_cards(user_id)
        return self._match_by_id(cards, name) or self._match_by_name(
            cards, name, label="cartão"
        )

    async def resolve_category(
        self, user_id: UUID, name: str, *, kind: str | None = None
    ) -> FinCategory:
        categories = await self.list_categories(user_id)
        if kind:
            scoped = [c for c in categories if c.kind == kind]
            try:
                return self._match_by_name(scoped, name, label="categoria")
            except AmbiguousNameError:
                raise
            except ValueError:
                pass  # tenta sem o filtro de kind (ex.: "Reembolso" numa despesa estornada)
        return self._match_by_name(categories, name, label="categoria")

    async def default_account(self, user_id: UUID) -> FinAccount | None:
        accounts = await self.list_accounts(user_id)
        for account in accounts:
            if account.is_default:
                return account
        return accounts[0] if len(accounts) == 1 else None

    # ── CRUD de cadastro ─────────────────────────────────────────────────

    async def create(self, obj: Any) -> Any:
        async with self._db.get_session() as db:
            db.add(obj)
            await db.commit()
            await db.refresh(obj)
        return obj

    async def apply_update(self, model_cls: type, obj_id: UUID, changes: dict[str, Any]) -> Any:
        """Carrega fresco DENTRO da sessão e aplica — nunca re-add de objeto
        detached (viraria INSERT duplicado)."""
        async with self._db.get_session() as db:
            obj = await db.get(model_cls, obj_id)
            if obj is None:
                raise ValueError("Registro não existe mais.")
            for key, value in changes.items():
                setattr(obj, key, value)
            if hasattr(obj, "updated_at"):
                obj.updated_at = datetime.utcnow()
            db.add(obj)
            await db.commit()
            await db.refresh(obj)
            return obj

    async def set_default_account(self, user_id: UUID, account_id: UUID) -> None:
        async with self._db.get_session() as db:
            await db.execute(
                text("UPDATE fin_account SET is_default = (id = :aid) WHERE user_id = :uid"),
                {"aid": str(account_id), "uid": str(user_id)},
            )
            await db.commit()

    async def upsert_budget(
        self, user_id: UUID, *, category_id: UUID | None, month: date | None,
        amount_cents: int,
    ) -> tuple[FinBudget, bool]:
        """(budget, created?) — chave lógica user+categoria+mês (ambos anuláveis)."""
        async with self._db.get_session() as db:
            stmt = select(FinBudget).where(FinBudget.user_id == user_id)
            stmt = (
                stmt.where(FinBudget.category_id == category_id)
                if category_id else stmt.where(FinBudget.category_id.is_(None))
            )
            stmt = (
                stmt.where(FinBudget.month == month)
                if month else stmt.where(FinBudget.month.is_(None))
            )
            existing = (await db.execute(stmt.limit(1))).scalars().first()
            if existing:
                existing.amount_cents = amount_cents
                existing.updated_at = datetime.utcnow()
                db.add(existing)
                await db.commit()
                await db.refresh(existing)
                return existing, False
            budget = FinBudget(
                user_id=user_id, category_id=category_id, month=month,
                amount_cents=amount_cents,
            )
            db.add(budget)
            await db.commit()
            await db.refresh(budget)
            return budget, True

    async def delete_budget(self, budget_id: UUID) -> None:
        async with self._db.get_session() as db:
            obj = await db.get(FinBudget, budget_id)
            if obj is not None:
                await db.delete(obj)
                await db.commit()

    async def oldest_open_invoice(self, user_id: UUID, card_id: UUID) -> date | None:
        """Mês de fatura mais antigo com saldo em aberto (compras > pagamentos)."""
        async with self._db.get_session() as db:
            value = (
                await db.execute(
                    text(
                        """
                        SELECT invoice_month FROM fin_transaction
                        WHERE user_id = :uid AND card_id = :card
                          AND deleted_at IS NULL AND invoice_month IS NOT NULL
                        GROUP BY invoice_month
                        HAVING SUM(CASE kind WHEN 'despesa' THEN amount_cents
                                             WHEN 'pagamento_fatura' THEN -amount_cents
                                             ELSE 0 END) > 0
                        ORDER BY invoice_month LIMIT 1
                        """
                    ),
                    {"uid": str(user_id), "card": str(card_id)},
                )
            ).scalar()
        return value

    async def open_invoices(self, user_id: UUID, card_id: UUID) -> list[tuple[date, int]]:
        """Todos os meses com fatura em aberto (compras > pagamentos), do mais antigo."""
        async with self._db.get_session() as db:
            rows = (
                await db.execute(
                    text(
                        """
                        SELECT invoice_month,
                               SUM(CASE kind WHEN 'despesa' THEN amount_cents
                                             WHEN 'pagamento_fatura' THEN -amount_cents
                                             ELSE 0 END) AS aberto
                        FROM fin_transaction
                        WHERE user_id = :uid AND card_id = :card
                          AND deleted_at IS NULL AND invoice_month IS NOT NULL
                        GROUP BY invoice_month
                        HAVING SUM(CASE kind WHEN 'despesa' THEN amount_cents
                                             WHEN 'pagamento_fatura' THEN -amount_cents
                                             ELSE 0 END) > 0
                        ORDER BY invoice_month
                        """
                    ),
                    {"uid": str(user_id), "card": str(card_id)},
                )
            ).all()
        return [(r.invoice_month, int(r.aberto)) for r in rows]

    async def invoice_balance(self, user_id: UUID, card_id: UUID, month: date) -> dict[str, int]:
        """Fatura de um mês: total de compras, quanto já foi pago, o que falta.

        A "baixa" da fatura é DERIVADA daqui (compras − pagamentos), nunca um flag
        gravado na parcela: flag seria uma segunda fonte de verdade, e é ela que
        envelhece e mente quando um pagamento é estornado/corrigido.
        """
        async with self._db.get_session() as db:
            row = (
                await db.execute(
                    text(
                        """
                        SELECT
                          COALESCE(SUM(CASE WHEN kind = 'despesa'
                                            THEN amount_cents ELSE 0 END), 0) AS total,
                          COALESCE(SUM(CASE WHEN kind = 'pagamento_fatura'
                                            THEN amount_cents ELSE 0 END), 0) AS pago
                        FROM fin_transaction
                        WHERE user_id = :uid AND card_id = :card
                          AND invoice_month = :month AND deleted_at IS NULL
                        """
                    ),
                    {"uid": str(user_id), "card": str(card_id), "month": month},
                )
            ).one()
        total, pago = int(row.total), int(row.pago)
        return {"total": total, "pago": pago, "aberto": total - pago}

    async def recent_duplicate(
        self,
        user_id: UUID,
        *,
        kind: str,
        amount_cents: int,
        when: date,
        card_id: UUID | None = None,
        account_id: UUID | None = None,
        window_minutes: int = 10,
    ) -> FinTransaction | None:
        """Lançamento idêntico gravado há poucos minutos (mesmo valor/tipo/destino).

        Rede contra a escrita repetida: o cognitivo pagou a MESMA fatura duas vezes
        em 50s (sessão b7fd9269) porque o histórico do turno seguinte não guarda o
        resultado da tool — ele não tinha como saber que já havia executado.
        """
        async with self._db.get_session() as db:
            stmt = (
                select(FinTransaction)
                .where(
                    FinTransaction.user_id == user_id,
                    FinTransaction.kind == kind,
                    FinTransaction.amount_cents == amount_cents,
                    FinTransaction.occurred_at == when,
                    FinTransaction.deleted_at.is_(None),
                    FinTransaction.created_at
                    >= datetime.utcnow() - timedelta(minutes=window_minutes),
                )
                .order_by(FinTransaction.created_at.desc())
                .limit(1)
            )
            if card_id is not None:
                stmt = stmt.where(FinTransaction.card_id == card_id)
            if account_id is not None:
                stmt = stmt.where(FinTransaction.account_id == account_id)
            return (await db.execute(stmt)).scalars().first()

    async def list_budgets(self, user_id: UUID) -> list[FinBudget]:
        async with self._db.get_session() as db:
            stmt = select(FinBudget).where(FinBudget.user_id == user_id)
            return list((await db.execute(stmt)).scalars().all())

    async def list_recurrings(self, user_id: UUID, only_active: bool = True) -> list[FinRecurring]:
        async with self._db.get_session() as db:
            stmt = select(FinRecurring).where(FinRecurring.user_id == user_id)
            if only_active:
                stmt = stmt.where(FinRecurring.active.is_(True))
            return list((await db.execute(stmt.order_by(FinRecurring.description))).scalars().all())

    # ── Lançamentos ──────────────────────────────────────────────────────

    async def add_simple_transaction(self, tx: FinTransaction) -> FinTransaction:
        return await self.create(tx)

    async def add_installment_purchase(
        self,
        *,
        user_id: UUID,
        card: FinCard,
        total_cents: int,
        n: int,
        description: str,
        merchant: str | None,
        category_id: UUID | None,
        purchase_date: date,
        notes: str | None,
    ) -> tuple[FinInstallmentPlan, list[FinTransaction]]:
        """Cria o plano + N parcelas. Cada parcela vive no mês da sua fatura
        (occurred_at deslocado mês a mês), com ajuste de centavos na 1ª."""
        first_invoice = invoice_month_for(purchase_date, card.closing_day)
        amounts = split_installments(total_cents, n)
        plan = FinInstallmentPlan(
            user_id=user_id, card_id=card.id, description=description,
            merchant=merchant, category_id=category_id, total_cents=total_cents,
            n_installments=n, purchase_date=purchase_date,
            first_invoice_month=first_invoice,
        )
        transactions: list[FinTransaction] = []
        for i, amount in enumerate(amounts):
            invoice = add_months(first_invoice, i)
            occurred = purchase_date if i == 0 else invoice.replace(
                day=min(purchase_date.day, 28)
            )
            # Description limpa — quem mostra "parc i/n" são os formatters (extrato/
            # fatura), a partir de installment_no; sufixo aqui duplicaria na saída.
            transactions.append(
                FinTransaction(
                    user_id=user_id, kind="despesa", amount_cents=amount,
                    description=description, merchant=merchant,
                    category_id=category_id, card_id=card.id,
                    payment_method="credito", occurred_at=occurred,
                    invoice_month=invoice, installment_plan_id=plan.id,
                    installment_no=i + 1, notes=notes if i == 0 else None,
                )
            )
        async with self._db.get_session() as db:
            db.add(plan)
            for tx in transactions:
                db.add(tx)
            await db.commit()
        return plan, transactions

    async def add_transfer_pair(
        self,
        *,
        user_id: UUID,
        amount_cents: int,
        from_account: FinAccount,
        to_account: FinAccount,
        when: date,
        description: str,
    ) -> UUID:
        group = uuid.uuid4()
        async with self._db.get_session() as db:
            db.add(FinTransaction(
                user_id=user_id, kind="transf_saida", amount_cents=amount_cents,
                description=description, account_id=from_account.id,
                payment_method="transferencia", occurred_at=when, transfer_group=group,
            ))
            db.add(FinTransaction(
                user_id=user_id, kind="transf_entrada", amount_cents=amount_cents,
                description=description, account_id=to_account.id,
                payment_method="transferencia", occurred_at=when, transfer_group=group,
            ))
            await db.commit()
        return group

    async def find_transaction(self, user_id: UUID, ref: str) -> FinTransaction:
        """Acha por id (completo ou prefixo ≥6 chars) ou 'ultimo'."""
        ref = (ref or "").strip().lower()
        async with self._db.get_session() as db:
            if ref in ("ultimo", "último", "last", ""):
                stmt = (
                    select(FinTransaction)
                    .where(FinTransaction.user_id == user_id)
                    .where(FinTransaction.deleted_at.is_(None))
                    .order_by(FinTransaction.created_at.desc())
                    .limit(1)
                )
                tx = (await db.execute(stmt)).scalars().first()
                if tx is None:
                    raise ValueError("Nenhum lançamento registrado ainda.")
                return tx
            clean = ref.replace("-", "")
            if len(clean) < 6:
                raise ValueError("Id muito curto — use pelo menos 6 caracteres do id.")
            rows = (
                await db.execute(
                    text(
                        """
                        SELECT id FROM fin_transaction
                        WHERE user_id = :uid AND deleted_at IS NULL
                          AND replace(id::text, '-', '') LIKE :prefix
                        LIMIT 2
                        """
                    ),
                    {"uid": str(user_id), "prefix": f"{clean}%"},
                )
            ).fetchall()
            if not rows:
                raise ValueError(f"Não achei lançamento com id '{ref}'.")
            if len(rows) > 1:
                raise ValueError(f"Id '{ref}' é ambíguo — use mais caracteres.")
            tx = await db.get(FinTransaction, rows[0][0])
            assert tx is not None
            return tx

    async def select_transactions(
        self, user_id: UUID, target: dict[str, Any], *, limit: int = 25
    ) -> list[FinTransaction]:
        """Lançamentos que casam com um alvo DESCRITIVO (kind/card/conta/mês/texto).

        Existe porque "desfaz o pagamento da fatura" não tinha como virar um id: o
        modelo precisava adivinhar, e adivinhou errado — criou um pagamento novo só
        para poder apagá-lo (sessão 4b2d2685). Aqui a descrição vira SQL.
        """
        stmt = select(FinTransaction).where(
            FinTransaction.user_id == user_id, FinTransaction.deleted_at.is_(None)
        )
        if target.get("kind"):
            stmt = stmt.where(FinTransaction.kind == str(target["kind"]))
        if target.get("card_id"):
            stmt = stmt.where(FinTransaction.card_id == target["card_id"])
        if target.get("account_id"):
            stmt = stmt.where(FinTransaction.account_id == target["account_id"])
        if target.get("invoice_month"):
            stmt = stmt.where(FinTransaction.invoice_month == target["invoice_month"])
        if target.get("amount_cents"):
            stmt = stmt.where(FinTransaction.amount_cents == int(target["amount_cents"]))
        if target.get("date"):
            stmt = stmt.where(FinTransaction.occurred_at == target["date"])
        if target.get("description"):
            like = f"%{_fold(str(target['description']))}%"
            stmt = stmt.where(func.lower(FinTransaction.description).like(like))
        async with self._db.get_session() as db:
            stmt = stmt.order_by(FinTransaction.created_at.desc()).limit(limit)
            return list((await db.execute(stmt)).scalars().all())

    async def ledger_snapshot(
        self, user_id: UUID, *, today: date, invoices: list[tuple[UUID, date]]
    ) -> dict[str, int]:
        """Retrato dos números que uma operação pode mexer: saldo por conta + faturas.

        É o que permite CONFERIR o resultado: tira-se um retrato antes, executa, tira
        outro e compara com o efeito previsto. Sem isso o Neo dizia "desfeito" sem
        ninguém nunca ter olhado o banco.
        """
        snap: dict[str, int] = {}
        data = await self.balances(user_id, today=today)
        for acc in data["accounts"]:
            snap[f"conta:{acc['name']}"] = int(acc["balance_cents"])
        for card_id, month in invoices:
            bal = await self.invoice_balance(user_id, card_id, month)
            snap[f"fatura:{card_id}:{month:%Y-%m}"] = bal["aberto"]
        return snap

    async def plan_delete_targets(
        self, user_id: UUID, tx: FinTransaction, *, whole_plan: bool
    ) -> list[FinTransaction]:
        """TODAS as linhas que um delete vai levar junto — o alcance real da operação.

        Apagar uma perna de transferência apaga as duas; ``scope=plano`` apaga as N
        parcelas. O plano precisa dizer isso ANTES, não o usuário descobrir depois.
        """
        async with self._db.get_session() as db:
            if whole_plan and tx.installment_plan_id:
                stmt = select(FinTransaction).where(
                    FinTransaction.user_id == user_id,
                    FinTransaction.installment_plan_id == tx.installment_plan_id,
                    FinTransaction.deleted_at.is_(None),
                )
            elif tx.transfer_group:
                stmt = select(FinTransaction).where(
                    FinTransaction.user_id == user_id,
                    FinTransaction.transfer_group == tx.transfer_group,
                    FinTransaction.deleted_at.is_(None),
                )
            else:
                return [tx]
            rows = list((await db.execute(stmt.order_by(FinTransaction.occurred_at))).scalars().all())
        return rows or [tx]

    async def soft_delete_transaction(
        self, user_id: UUID, tx: FinTransaction, *, whole_plan: bool
    ) -> int:
        now = datetime.utcnow()
        async with self._db.get_session() as db:
            if whole_plan and tx.installment_plan_id:
                result = await db.execute(
                    text(
                        """
                        UPDATE fin_transaction SET deleted_at = :now
                        WHERE user_id = :uid AND installment_plan_id = :plan
                          AND deleted_at IS NULL
                        """
                    ),
                    {"now": now, "uid": str(user_id), "plan": str(tx.installment_plan_id)},
                )
                await db.execute(
                    text("UPDATE fin_installment_plan SET canceled_at = :now WHERE id = :plan"),
                    {"now": now, "plan": str(tx.installment_plan_id)},
                )
                await db.commit()
                return int(result.rowcount or 0)
            if tx.transfer_group:
                result = await db.execute(
                    text(
                        """
                        UPDATE fin_transaction SET deleted_at = :now
                        WHERE user_id = :uid AND transfer_group = :grp AND deleted_at IS NULL
                        """
                    ),
                    {"now": now, "uid": str(user_id), "grp": str(tx.transfer_group)},
                )
                await db.commit()
                return int(result.rowcount or 0)
            fresh = await db.get(FinTransaction, tx.id)
            if fresh is None or fresh.deleted_at is not None:
                return 0
            fresh.deleted_at = now
            db.add(fresh)
            await db.commit()
            return 1

    # ── Consultas (extrato, fatura, saldos, insights) ────────────────────

    async def _category_scope_ids(self, user_id: UUID, category: FinCategory) -> list[str]:
        """A categoria + as filhas (filtro de extrato/insights inclui a árvore)."""
        categories = await self.list_categories(user_id)
        ids = [str(category.id)]
        ids += [str(c.id) for c in categories if c.parent_id == category.id]
        return ids

    async def statement(
        self,
        user_id: UUID,
        *,
        start: date,
        end: date,
        account_id: UUID | None = None,
        card_id: UUID | None = None,
        category_ids: list[str] | None = None,
        kind: str | None = None,
        payment_method: str | None = None,
        search: str | None = None,
        limit: int = 30,
    ) -> tuple[list[dict], dict]:
        """Linhas do extrato + totais do recorte COMPLETO (não só das linhas)."""
        where = [
            "t.user_id = :uid", "t.deleted_at IS NULL",
            "t.occurred_at BETWEEN :start AND :end",
        ]
        params: dict[str, Any] = {
            "uid": str(user_id), "start": start, "end": end, "limit": limit,
        }
        if account_id:
            where.append("t.account_id = :account_id")
            params["account_id"] = str(account_id)
        if card_id:
            where.append("t.card_id = :card_id")
            params["card_id"] = str(card_id)
        if category_ids:
            where.append("t.category_id = ANY(CAST(:category_ids AS uuid[]))")
            params["category_ids"] = category_ids
        if kind:
            where.append("t.kind = :kind")
            params["kind"] = kind
        if payment_method:
            where.append("t.payment_method = :pm")
            params["pm"] = payment_method
        if search:
            where.append("(t.description ILIKE :q OR t.merchant ILIKE :q)")
            params["q"] = f"%{search}%"
        where_sql = " AND ".join(where)

        async with self._db.get_session() as db:
            rows = (
                await db.execute(
                    text(
                        f"""
                        SELECT t.id, t.kind, t.amount_cents, t.description, t.merchant,
                               t.payment_method, t.occurred_at, t.invoice_month,
                               t.installment_no, p.n_installments,
                               c.name AS category, a.name AS account, k.name AS card
                        FROM fin_transaction t
                        LEFT JOIN fin_category c ON c.id = t.category_id
                        LEFT JOIN fin_account a ON a.id = t.account_id
                        LEFT JOIN fin_card k ON k.id = t.card_id
                        LEFT JOIN fin_installment_plan p ON p.id = t.installment_plan_id
                        WHERE {where_sql}
                        ORDER BY t.occurred_at DESC, t.created_at DESC
                        LIMIT :limit
                        """
                    ),
                    params,
                )
            ).mappings().all()
            totals_row = (
                await db.execute(
                    text(
                        f"""
                        SELECT
                          COUNT(*) AS n,
                          COALESCE(SUM(t.amount_cents) FILTER (WHERE t.kind = 'despesa'), 0) AS despesas,
                          COALESCE(SUM(t.amount_cents) FILTER (WHERE t.kind = 'receita'), 0) AS receitas,
                          -- Movimento de CAIXA (o que realmente entrou/saiu das contas):
                          -- pagamento de fatura e transferências contam; compra no crédito
                          -- (card_id, sem account_id) NÃO conta — ela vive na fatura.
                          COALESCE(SUM(t.amount_cents) FILTER (
                            WHERE t.account_id IS NOT NULL
                              AND t.kind IN ('receita', 'transf_entrada')), 0) AS entrada_conta,
                          COALESCE(SUM(t.amount_cents) FILTER (
                            WHERE t.account_id IS NOT NULL
                              AND t.kind IN ('despesa', 'transf_saida', 'pagamento_fatura')), 0) AS saida_conta,
                          COALESCE(SUM(t.amount_cents) FILTER (
                            WHERE t.card_id IS NOT NULL AND t.kind = 'despesa'), 0) AS credito
                        FROM fin_transaction t
                        WHERE {where_sql}
                        """
                    ),
                    params,
                )
            ).mappings().first()
        return [dict(r) for r in rows], dict(totals_row or {})

    async def card_invoice(
        self, user_id: UUID, card: FinCard, invoice_month: date
    ) -> dict:
        async with self._db.get_session() as db:
            items = (
                await db.execute(
                    text(
                        """
                        SELECT t.id, t.amount_cents, t.description, t.merchant,
                               t.occurred_at, t.installment_no, p.n_installments,
                               c.name AS category
                        FROM fin_transaction t
                        LEFT JOIN fin_category c ON c.id = t.category_id
                        LEFT JOIN fin_installment_plan p ON p.id = t.installment_plan_id
                        WHERE t.user_id = :uid AND t.card_id = :card
                          AND t.kind = 'despesa' AND t.invoice_month = :month
                          AND t.deleted_at IS NULL
                        ORDER BY t.occurred_at, t.created_at
                        """
                    ),
                    {"uid": str(user_id), "card": str(card.id), "month": invoice_month},
                )
            ).mappings().all()
            paid = (
                await db.execute(
                    text(
                        """
                        SELECT COALESCE(SUM(amount_cents), 0) AS paid
                        FROM fin_transaction
                        WHERE user_id = :uid AND card_id = :card
                          AND kind = 'pagamento_fatura' AND invoice_month = :month
                          AND deleted_at IS NULL
                        """
                    ),
                    {"uid": str(user_id), "card": str(card.id), "month": invoice_month},
                )
            ).scalar() or 0
            debt = await self._card_debt(db, user_id, card.id)
            future = (
                await db.execute(
                    text(
                        """
                        SELECT t.invoice_month, SUM(t.amount_cents) AS total, COUNT(*) AS n
                        FROM fin_transaction t
                        WHERE t.user_id = :uid AND t.card_id = :card AND t.kind = 'despesa'
                          AND t.invoice_month > :month AND t.deleted_at IS NULL
                        GROUP BY t.invoice_month ORDER BY t.invoice_month LIMIT 12
                        """
                    ),
                    {"uid": str(user_id), "card": str(card.id), "month": invoice_month},
                )
            ).mappings().all()
        total = sum(i["amount_cents"] for i in items)
        return {
            "items": [dict(i) for i in items],
            "total_cents": total,
            "paid_cents": int(paid),
            "debt_cents": int(debt),
            "future": [dict(f) for f in future],
        }

    @staticmethod
    async def _card_debt(db: Any, user_id: UUID, card_id: UUID) -> int:
        """Saldo devedor total do cartão (compras − pagamentos, inclui parcelas futuras)."""
        value = (
            await db.execute(
                text(
                    """
                    SELECT COALESCE(SUM(
                      CASE kind WHEN 'despesa' THEN amount_cents
                                WHEN 'pagamento_fatura' THEN -amount_cents
                                ELSE 0 END), 0)
                    FROM fin_transaction
                    WHERE user_id = :uid AND card_id = :card AND deleted_at IS NULL
                    """
                ),
                {"uid": str(user_id), "card": str(card_id)},
            )
        ).scalar()
        return int(value or 0)

    async def balances(self, user_id: UUID, *, today: date) -> dict:
        month_start = today.replace(day=1)
        async with self._db.get_session() as db:
            accounts = (
                await db.execute(
                    text(
                        """
                        SELECT a.id, a.name, a.kind, a.is_default,
                               a.initial_balance_cents + COALESCE(SUM(
                                 CASE t.kind
                                   WHEN 'receita' THEN t.amount_cents
                                   WHEN 'transf_entrada' THEN t.amount_cents
                                   WHEN 'despesa' THEN -t.amount_cents
                                   WHEN 'transf_saida' THEN -t.amount_cents
                                   WHEN 'pagamento_fatura' THEN -t.amount_cents
                                   ELSE 0 END
                               ) FILTER (WHERE t.deleted_at IS NULL AND t.occurred_at <= :today), 0)
                               AS balance_cents
                        FROM fin_account a
                        LEFT JOIN fin_transaction t ON t.account_id = a.id AND t.user_id = a.user_id
                        WHERE a.user_id = :uid AND a.archived_at IS NULL
                        GROUP BY a.id ORDER BY a.name
                        """
                    ),
                    {"uid": str(user_id), "today": today},
                )
            ).mappings().all()
            cards = []
            for card in await self.list_cards(user_id):
                debt = await self._card_debt(db, user_id, card.id)
                current_invoice = invoice_month_for(today, card.closing_day)
                open_total = (
                    await db.execute(
                        text(
                            """
                            SELECT COALESCE(SUM(
                              CASE kind WHEN 'despesa' THEN amount_cents
                                        WHEN 'pagamento_fatura' THEN -amount_cents
                                        ELSE 0 END), 0)
                            FROM fin_transaction
                            WHERE user_id = :uid AND card_id = :card
                              AND invoice_month = :month AND deleted_at IS NULL
                            """
                        ),
                        {"uid": str(user_id), "card": str(card.id), "month": current_invoice},
                    )
                ).scalar() or 0
                cards.append({
                    "card": card, "debt_cents": debt,
                    "current_invoice_month": current_invoice,
                    "current_invoice_cents": int(open_total),
                })
            month_totals = (
                await db.execute(
                    text(
                        """
                        SELECT
                          COALESCE(SUM(amount_cents) FILTER (WHERE kind = 'despesa'), 0) AS despesas,
                          COALESCE(SUM(amount_cents) FILTER (WHERE kind = 'receita'), 0) AS receitas
                        FROM fin_transaction
                        WHERE user_id = :uid AND deleted_at IS NULL
                          AND occurred_at BETWEEN :start AND :today
                        """
                    ),
                    {"uid": str(user_id), "start": month_start, "today": today},
                )
            ).mappings().first()
        return {
            "accounts": [dict(a) for a in accounts],
            "cards": cards,
            "month": dict(month_totals or {}),
        }

    async def insights(
        self, user_id: UUID, *, start: date, end: date, compare: bool, today: date
    ) -> dict:
        span_days = (end - start).days + 1
        prev_start = date.fromordinal(start.toordinal() - span_days)
        prev_end = date.fromordinal(start.toordinal() - 1)
        params = {"uid": str(user_id), "start": start, "end": end}
        prev_params = {"uid": str(user_id), "start": prev_start, "end": prev_end}

        base_where = (
            "user_id = :uid AND deleted_at IS NULL AND kind = 'despesa' "
            "AND occurred_at BETWEEN :start AND :end"
        )
        by_group_sql = text(
            """
            SELECT COALESCE(pc.name, c.name, 'Sem categoria') AS grupo,
                   COALESCE(c.name, 'Sem categoria') AS categoria,
                   SUM(t.amount_cents) AS total
            FROM fin_transaction t
            LEFT JOIN fin_category c ON c.id = t.category_id
            LEFT JOIN fin_category pc ON pc.id = c.parent_id
            WHERE t.user_id = :uid AND t.deleted_at IS NULL AND t.kind = 'despesa'
              AND t.occurred_at BETWEEN :start AND :end
            GROUP BY 1, 2 ORDER BY 3 DESC
            """
        )
        async with self._db.get_session() as db:
            totals = (
                await db.execute(
                    text(
                        """
                        SELECT
                          COALESCE(SUM(amount_cents) FILTER (WHERE kind = 'despesa'), 0) AS despesas,
                          COALESCE(SUM(amount_cents) FILTER (WHERE kind = 'receita'), 0) AS receitas
                        FROM fin_transaction
                        WHERE user_id = :uid AND deleted_at IS NULL
                          AND occurred_at BETWEEN :start AND :end
                        """
                    ),
                    params,
                )
            ).mappings().first()
            by_group = (await db.execute(by_group_sql, params)).mappings().all()
            prev_by_group = (
                (await db.execute(by_group_sql, prev_params)).mappings().all()
                if compare else []
            )
            merchants = (
                await db.execute(
                    text(
                        f"""
                        SELECT merchant, SUM(amount_cents) AS total, COUNT(*) AS n
                        FROM fin_transaction WHERE {base_where} AND merchant IS NOT NULL
                        GROUP BY merchant ORDER BY total DESC LIMIT 5
                        """
                    ),
                    params,
                )
            ).mappings().all()
            methods = (
                await db.execute(
                    text(
                        f"""
                        SELECT COALESCE(payment_method, '(sem método)') AS metodo,
                               SUM(amount_cents) AS total
                        FROM fin_transaction WHERE {base_where}
                        GROUP BY 1 ORDER BY total DESC
                        """
                    ),
                    params,
                )
            ).mappings().all()
            trend = (
                await db.execute(
                    text(
                        """
                        SELECT date_trunc('month', occurred_at)::date AS mes,
                          COALESCE(SUM(amount_cents) FILTER (WHERE kind = 'despesa'), 0) AS despesas,
                          COALESCE(SUM(amount_cents) FILTER (WHERE kind = 'receita'), 0) AS receitas
                        FROM fin_transaction
                        WHERE user_id = :uid AND deleted_at IS NULL
                          AND occurred_at >= (CAST(:end AS date) - INTERVAL '5 months')
                          AND occurred_at <= :end
                        GROUP BY 1 ORDER BY 1
                        """
                    ),
                    params,
                )
            ).mappings().all()
            future_installments = (
                await db.execute(
                    text(
                        """
                        SELECT invoice_month, SUM(amount_cents) AS total, COUNT(*) AS n
                        FROM fin_transaction
                        WHERE user_id = :uid AND deleted_at IS NULL AND kind = 'despesa'
                          AND installment_plan_id IS NOT NULL AND occurred_at > :end
                        GROUP BY 1 ORDER BY 1 LIMIT 6
                        """
                    ),
                    params,
                )
            ).mappings().all()
            budget_rows = (
                await db.execute(
                    text(
                        """
                        SELECT b.id, b.amount_cents, b.month, b.category_id,
                               c.name AS category, c.parent_id
                        FROM fin_budget b
                        LEFT JOIN fin_category c ON c.id = b.category_id
                        WHERE b.user_id = :uid
                          AND (b.month IS NULL OR b.month = :month)
                        """
                    ),
                    {"uid": str(user_id), "month": end.replace(day=1)},
                )
            ).mappings().all()
        recurrings = await self.list_recurrings(user_id)
        return {
            "totals": dict(totals or {}),
            "by_group": [dict(r) for r in by_group],
            "prev_by_group": [dict(r) for r in prev_by_group],
            "merchants": [dict(r) for r in merchants],
            "methods": [dict(r) for r in methods],
            "trend": [dict(r) for r in trend],
            "future_installments": [dict(r) for r in future_installments],
            "budgets": [dict(r) for r in budget_rows],
            "recurrings": recurrings,
            "prev_range": (prev_start, prev_end),
        }

    async def spent_by_category_tree(
        self, user_id: UUID, *, start: date, end: date
    ) -> dict[str, int]:
        """Gasto por categoria_id (string) no período, para checar orçamentos."""
        async with self._db.get_session() as db:
            rows = (
                await db.execute(
                    text(
                        """
                        SELECT COALESCE(c.parent_id, t.category_id) AS root_id,
                               t.category_id, SUM(t.amount_cents) AS total
                        FROM fin_transaction t
                        LEFT JOIN fin_category c ON c.id = t.category_id
                        WHERE t.user_id = :uid AND t.deleted_at IS NULL
                          AND t.kind = 'despesa' AND t.occurred_at BETWEEN :start AND :end
                        GROUP BY 1, 2
                        """
                    ),
                    {"uid": str(user_id), "start": start, "end": end},
                )
            ).mappings().all()
        spent: dict[str, int] = {}
        for row in rows:
            for key in {str(row["root_id"]), str(row["category_id"])}:
                if key and key != "None":
                    spent[key] = spent.get(key, 0) + int(row["total"])
        return spent
