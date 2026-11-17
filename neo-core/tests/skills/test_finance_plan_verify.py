"""Exclusão com plano e conferência + alvo descritivo + cadastro duplicado.

Regressão da sessão 4b2d2685: pedido "desfaz o pagamento da fatura", o Neo não
tinha como virar isso num id — então CRIOU um pagamento novo (que caiu na fatura
de agosto) só para poder apagá-lo, e garantiu ao usuário que estava desfeito. O
pagamento original nunca foi tocado. Aqui: o alvo vira consulta, o efeito é
previsto antes e CONFERIDO no banco depois.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from modules.finance.executor import (
    FinanceExecutor,
    _check_effects,
    _reject_duplicate_name,
    _render_plan,
)

UID = uuid4()
CARD_ID = uuid4()
ACC_ID = uuid4()
JUL = date(2026, 7, 1)


def _pagamento(amount: int = 30000) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), kind="pagamento_fatura", amount_cents=amount,
        description="Pagamento fatura Nubank jul/2026", occurred_at=date(2026, 7, 14),
        account_id=ACC_ID, card_id=CARD_ID, invoice_month=JUL,
        installment_plan_id=None, transfer_group=None,
    )


class _FakeRepo:
    """Carteira R$ 6.700,00; fatura jul/2026 do Nubank PAGA (R$ 300,00)."""

    def __init__(self, *, achados: list, aplica_delete: bool = True):
        self._achados = achados
        self._aplica = aplica_delete
        self.apagou: list = []
        self._saldo = 670000
        self._fatura_aberta = 0

    async def ensure_defaults(self, uid): return None
    async def find_transaction(self, uid, ref): raise AssertionError("não devia usar id")
    async def resolve_card(self, uid, name): return SimpleNamespace(id=CARD_ID, name="Nubank", closing_day=15)
    async def list_accounts(self, uid): return [SimpleNamespace(id=ACC_ID, name="Carteira")]
    async def list_cards(self, uid): return [SimpleNamespace(id=CARD_ID, name="Nubank")]

    async def select_transactions(self, uid, target, limit=25):
        return list(self._achados)

    async def plan_delete_targets(self, uid, tx, *, whole_plan):
        return [tx]

    async def ledger_snapshot(self, uid, *, today, invoices):
        return {
            "conta:Carteira": self._saldo,
            f"fatura:{CARD_ID}:2026-07": self._fatura_aberta,
        }

    async def soft_delete_transaction(self, uid, tx, *, whole_plan):
        self.apagou.append(tx)
        if self._aplica:  # o banco realmente muda
            self._saldo += tx.amount_cents
            self._fatura_aberta += tx.amount_cents
        return 1


def _executor(repo: _FakeRepo) -> FinanceExecutor:
    ex = FinanceExecutor(db=None)
    ex.repo = repo  # type: ignore[assignment]
    ex.set_context(UID)
    return ex


ALVO = '{"kind":"pagamento_fatura","card":"Nubank","invoice_month":"2026-07"}'


@pytest.mark.asyncio
async def test_preview_mostra_o_plano_e_nao_executa() -> None:
    repo = _FakeRepo(achados=[_pagamento()])
    out = await _executor(repo).execute(
        "fin_edit_transaction", {"target": ALVO, "action": "delete", "preview": True}
    )
    assert not repo.apagou, "preview NÃO pode escrever"
    assert "PLANO" in out
    assert "R$ 6.700,00 → R$ 7.000,00" in out  # devolve o dinheiro à conta
    assert "R$ 0,00 → R$ 300,00" in out  # e reabre a fatura
    assert "NADA FOI EXECUTADO" in out


@pytest.mark.asyncio
async def test_delete_por_alvo_descritivo_apaga_o_certo_e_confere() -> None:
    pg = _pagamento()
    repo = _FakeRepo(achados=[pg])
    out = await _executor(repo).execute(
        "fin_edit_transaction", {"target": ALVO, "action": "delete"}
    )
    assert repo.apagou == [pg]  # apagou o que JÁ existia — não criou nada
    assert "CONFERIDO no banco" in out
    assert "✓" in out and "DIVERGÊNCIA" not in out


@pytest.mark.asyncio
async def test_banco_nao_mudou_vira_divergencia_e_nao_sucesso() -> None:
    # O caso que o Neo mascarava: dizer "desfeito" sobre um banco que não mudou.
    repo = _FakeRepo(achados=[_pagamento()], aplica_delete=False)
    out = await _executor(repo).execute(
        "fin_edit_transaction", {"target": ALVO, "action": "delete"}
    )
    assert "DIVERGÊNCIA" in out
    assert "NÃO diga ao usuário que deu certo" in out


@pytest.mark.asyncio
async def test_alvo_ambiguo_lista_em_vez_de_chutar() -> None:
    repo = _FakeRepo(achados=[_pagamento(30000), _pagamento(15000)])
    with pytest.raises(ValueError, match="2 lançamentos casam"):
        await _executor(repo).execute(
            "fin_edit_transaction", {"target": ALVO, "action": "delete"}
        )
    assert not repo.apagou


@pytest.mark.asyncio
async def test_alvo_sem_match_nao_apaga_nada() -> None:
    repo = _FakeRepo(achados=[])
    with pytest.raises(ValueError, match="Nenhum lançamento casa"):
        await _executor(repo).execute(
            "fin_edit_transaction", {"target": ALVO, "action": "delete"}
        )
    assert not repo.apagou


def test_cadastro_com_nome_repetido_e_recusado() -> None:
    # Os dois cartões "Nubank" que nasceram com 1 min de diferença em prod.
    existentes = [SimpleNamespace(id=CARD_ID, name="Nubank")]
    with pytest.raises(ValueError, match="Já existe cartão com o nome 'Nubank'"):
        _reject_duplicate_name(existentes, " nubank ", "cartão")


def test_nome_diferente_passa() -> None:
    existentes = [SimpleNamespace(id=CARD_ID, name="Nubank")]
    _reject_duplicate_name(existentes, "Itaú", "cartão")  # não levanta


# ── Rodapé do extrato: movimento de conta bate com o saldo (sessão b2a0ecf9) ────
class _RepoStatement:
    """Salário 7000 na conta + 2 pagamentos de fatura (600) + 1 compra no crédito
    (300). Caixa: entrou 7000, saiu 600, líquido 6400 (= saldo real). O crédito
    não entra no 'saiu'."""

    async def ensure_defaults(self, uid): return None

    async def statement(self, uid, **kwargs):
        rows = [
            {"id": uuid4(), "kind": "pagamento_fatura", "amount_cents": 30000,
             "description": "Pagamento fatura set/2026", "merchant": None,
             "payment_method": "pix", "occurred_at": date(2026, 7, 15), "invoice_month": None,
             "installment_no": None, "n_installments": None, "category": None,
             "account": "Carteira", "card": "Nubank"},
            {"id": uuid4(), "kind": "despesa", "amount_cents": 30000,
             "description": "Compra parcelada", "merchant": None, "payment_method": None,
             "occurred_at": date(2026, 7, 14), "invoice_month": date(2026, 7, 1),
             "installment_no": 1, "n_installments": 12, "category": "Compras",
             "account": None, "card": "Nubank"},
        ]
        totals = {"n": 4, "despesas": 30000, "receitas": 700000,
                  "entrada_conta": 700000, "saida_conta": 60000, "credito": 30000}
        return rows, totals


@pytest.mark.asyncio
async def test_extrato_movimento_de_conta_bate_com_saldo() -> None:
    ex = FinanceExecutor(db=None)
    ex.repo = _RepoStatement()  # type: ignore[assignment]
    ex.set_context(uuid4())
    out = await ex.execute("fin_statement", {})
    # Caixa correto: entrou 7000, saiu 600 (os DOIS pagamentos), líquido 6400.
    assert "entrou R$ 7.000,00" in out
    assert "saiu R$ 600,00" in out
    assert "líquido R$ 6.400,00" in out  # == saldo real da Carteira
    # O crédito é mostrado à parte, NÃO somado como saída de conta.
    assert "Compras no CRÉDITO no período: R$ 300,00" in out
    assert "6.700" not in out  # o número fantasma do rodapé antigo sumiu
