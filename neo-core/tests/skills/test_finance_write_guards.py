"""Guardas de escrita das tools fin_*: valor da fatura e duplicata.

Regressão da sessão b7fd9269, onde o Neo (a) pagou R$ 3.000,00 numa fatura de
R$ 300,00 — valor que o usuário nunca disse, mas a tool exigia `amount`, então o
modelo inventou — e (b) repetiu o MESMO pagamento 50s depois, num turno em que o
usuário só pediu o saldo. Os dois guards vivem no domínio de propósito: prompt o
modelo ignora, `ValueError` não.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from modules.finance.executor import FinanceExecutor

UID = uuid4()
HOJE = date(2026, 7, 14)


class _FakeRepo:
    """Cartão Nubank com fatura de jul/2026: R$ 300,00 em aberto (parcela 1/12)."""

    def __init__(self, *, aberto: int = 30000, pago: int = 0, duplicata: object | None = None):
        self._bal = {"total": aberto + pago, "pago": pago, "aberto": aberto}
        self._dup = duplicata
        self.gravou: list[object] = []

    async def ensure_defaults(self, uid: UUID) -> str | None:
        return None

    async def resolve_account(self, uid: UUID, name: str):
        return SimpleNamespace(id=uuid4(), name="Carteira")

    async def resolve_card(self, uid: UUID, name: str):
        return SimpleNamespace(id=uuid4(), name="Nubank", closing_day=15)

    async def oldest_open_invoice(self, uid: UUID, card_id: UUID):
        return date(2026, 7, 1)

    async def invoice_balance(self, uid: UUID, card_id: UUID, month: date):
        return self._bal

    async def open_invoices(self, uid: UUID, card_id: UUID):
        return [(date(2026, 7, 1), self._bal["aberto"])] if self._bal["aberto"] > 0 else []

    async def recent_duplicate(self, uid: UUID, **kwargs):
        return self._dup

    async def add_simple_transaction(self, tx):
        self.gravou.append(tx)
        return SimpleNamespace(id=uuid4(), **{"amount_cents": tx.amount_cents})


def _executor(repo: _FakeRepo) -> FinanceExecutor:
    ex = FinanceExecutor(db=None)
    ex.repo = repo  # type: ignore[assignment]
    ex.set_context(UID)
    return ex


@pytest.mark.asyncio
async def test_paguei_a_fatura_sem_valor_paga_o_total_em_aberto() -> None:
    repo = _FakeRepo(aberto=30000)
    out = await _executor(repo).execute(
        "fin_transfer", {"from_account": "Carteira", "card": "Nubank", "date": "14/07/2026"}
    )
    assert repo.gravou and repo.gravou[0].amount_cents == 30000  # R$ 300,00, não um chute
    assert "R$ 300,00" in out and "quitada" in out.lower()


@pytest.mark.asyncio
async def test_valor_muito_acima_da_fatura_e_recusado() -> None:
    # O bug literal: fatura de R$ 300,00, modelo mandou R$ 3.000,00.
    repo = _FakeRepo(aberto=30000)
    with pytest.raises(ValueError, match="MAIOR que a fatura"):
        await _executor(repo).execute(
            "fin_transfer",
            {"amount": "3000", "from_account": "Carteira", "card": "Nubank"},
        )
    assert not repo.gravou


@pytest.mark.asyncio
async def test_pagamento_a_maior_passa_com_confirm_explicito() -> None:
    repo = _FakeRepo(aberto=30000)
    out = await _executor(repo).execute(
        "fin_transfer",
        {"amount": "3000", "from_account": "Carteira", "card": "Nubank", "confirm": True},
    )
    assert repo.gravou[0].amount_cents == 300000
    assert "crédito no cartão" in out


@pytest.mark.asyncio
async def test_fatura_ja_quitada_nao_aceita_novo_pagamento_sem_valor() -> None:
    repo = _FakeRepo(aberto=0, pago=30000)
    with pytest.raises(ValueError, match="não tem saldo em aberto"):
        await _executor(repo).execute(
            "fin_transfer", {"from_account": "Carteira", "card": "Nubank"}
        )
    assert not repo.gravou


@pytest.mark.asyncio
async def test_pagamento_identico_recente_e_recusado() -> None:
    # O segundo fin_transfer, 50s depois, no turno em que o usuário só pediu o saldo.
    dup = SimpleNamespace(id=uuid4(), description="Pagamento fatura Nubank")
    repo = _FakeRepo(aberto=30000, duplicata=dup)
    with pytest.raises(ValueError, match="JÁ EXISTE este pagamento"):
        await _executor(repo).execute(
            "fin_transfer", {"from_account": "Carteira", "card": "Nubank"}
        )
    assert not repo.gravou


# ── invoice_month relativo / nome (sessão 0371a5a8: "próximo mês" pagou julho) ──
from datetime import date as _date  # noqa: E402

from modules.finance.money import parse_month_ref  # noqa: E402


@pytest.mark.parametrize(
    "ref, esperado",
    [
        ("proximo", _date(2026, 8, 1)),
        ("próximo mês", _date(2026, 8, 1)),
        ("adianta o próximo", _date(2026, 8, 1)),
        ("agosto", _date(2026, 8, 1)),
        ("a de agosto", _date(2026, 8, 1)),
        ("atual", _date(2026, 7, 1)),
        ("anterior", _date(2026, 6, 1)),
        ("janeiro", _date(2027, 1, 1)),  # nome antes do mês atual → ano que vem
        ("2026-09", _date(2026, 9, 1)),
        ("", _date(2026, 7, 1)),
    ],
)
def test_parse_month_ref(ref: str, esperado) -> None:
    assert parse_month_ref(ref, current=_date(2026, 7, 1)) == esperado


class _RepoDuasFaturas(_FakeRepo):
    """Julho E agosto abertas (R$ 300 cada). closing_day=15 → corrente = julho."""

    async def resolve_card(self, uid, name):
        return SimpleNamespace(id=uuid4(), name="Nubank", closing_day=15)

    async def oldest_open_invoice(self, uid, card_id):
        return date(2026, 7, 1)

    async def open_invoices(self, uid, card_id):
        return [(date(2026, 7, 1), 30000), (date(2026, 8, 1), 30000)]


@pytest.mark.asyncio
async def test_pagar_proximo_mes_paga_agosto_nao_julho(monkeypatch) -> None:
    # Fixa "hoje" em 14/07 (antes do fechamento dia 15) → fatura corrente = julho,
    # logo "proximo" = agosto. Sem isto o teste vira dependente da data real.
    monkeypatch.setattr("modules.finance.executor.today_br", lambda: date(2026, 7, 14))
    repo = _RepoDuasFaturas(aberto=30000)
    out = await _executor(repo).execute(
        "fin_transfer",
        {"from_account": "Carteira", "card": "Nubank", "invoice_month": "proximo"},
    )
    assert repo.gravou[0].invoice_month == date(2026, 8, 1)  # agosto, não julho
    assert "ago/2026" in out
    assert "Continuam abertas: jul/2026" in out  # denuncia que julho segue aberta
