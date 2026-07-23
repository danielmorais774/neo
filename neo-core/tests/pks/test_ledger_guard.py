"""O extrato não pode virar memória semântica.

As fixtures de bloqueio são as 7 entidades que o planner REALMENTE criou em prod
em 2026-07-13 (com datas de 2025 e um saldo `balance=980` calculado por ele) —
se a guarda deixar qualquer uma passar de novo, o bug voltou.
"""

from __future__ import annotations

import pytest

from core.pks.ledger_guard import (
    is_ledger_memory,
    ledger_violation,
    looks_like_money_query,
)
from core.pks.schemas import PKSOperation


def _entity_op(name: str, entity_type: str = "document") -> PKSOperation:
    return PKSOperation(op="upsert_entity", entity={"name": name, "type": entity_type})


def _field_op(entity_name: str, key: str, entity_type: str = "document") -> PKSOperation:
    return PKSOperation(
        op="upsert_field",
        field={"entity": {"name": entity_name, "type": entity_type}, "key": key, "value": 1},
    )


# Nomes de entidade vistos em prod (pks_entity, source=conversation).
@pytest.mark.parametrize(
    "name",
    [
        "Gasto Pix teste 2025-04-14",
        "Gasto de hoje",
        "Gasto Pix teste",
        "Recebimento de R$ 1.000,00 em 2025-04-10",
        "Salário",
        "Pagamento da fatura do Nubank",
        "Compra parcelada em 10x",
    ],
)
def test_bloqueia_entidade_de_lancamento(name: str) -> None:
    assert ledger_violation(_entity_op(name)) is not None


# Campos monetários vistos em prod — inclusive o `balance` da "Carteira", que era
# uma conta feita pelo LLM (980) competindo com o extrato real.
@pytest.mark.parametrize(
    "key",
    ["amount", "amount_brl", "balance", "saldo", "valor", "fatura", "orcamento", "salario"],
)
def test_bloqueia_campo_monetario(key: str) -> None:
    assert ledger_violation(_field_op("Carteira", key, entity_type="account")) is not None


def test_bloqueia_campo_de_entidade_lancamento_mesmo_sem_dinheiro_na_chave() -> None:
    # upsert_field CRIA a entidade ausente: barrar só a chave deixaria "Gasto de
    # hoje" nascer pela porta dos fundos com category/payment_method.
    assert ledger_violation(_field_op("Gasto de hoje", "category")) is not None


@pytest.mark.parametrize(
    "op",
    [
        _entity_op("Netflix", "account"),
        _field_op("Netflix", "compartilhada_com", entity_type="account"),
        _entity_op("Nubank", "organization"),
        _field_op("Daniel", "full_name", entity_type="person"),
        _field_op("Daniel", "cidade", entity_type="person"),
        _entity_op("Mercado Assaí", "place"),
    ],
)
def test_deixa_passar_fato_sem_dinheiro(op: PKSOperation) -> None:
    # PKS continua guardando FATO (identidade, relação, preferência) — o que não
    # entra é VALOR, que envelhece e já tem dono nas tabelas fin_*.
    assert ledger_violation(op) is None


# ── Canal 2: contexto injetado no prompt (não passa por tool nenhuma) ──────────
def test_carteira_com_saldo_nao_vai_para_o_contexto() -> None:
    # O caso real: entidade "Carteira" com balance=980 (conta feita pelo LLM) chegou
    # ao prompt sem tool e o Neo respondeu o saldo velho com data de 2025.
    assert is_ledger_memory("Carteira", ["balance", "last_transaction_date"]) is True


def test_pessoa_e_lugar_continuam_no_contexto() -> None:
    assert is_ledger_memory("Daniel", ["full_name", "cidade"]) is False
    assert is_ledger_memory("Netflix", ["compartilhada_com"]) is False


# ── Canal 3: search_pks (o modelo perguntando à memória o que é do extrato) ────
@pytest.mark.parametrize(
    "query", ["carteira saldo", "quanto gastei esse mes", "fatura do nubank", "meus gastos"]
)
def test_pergunta_de_dinheiro_redireciona(query: str) -> None:
    assert looks_like_money_query(query) is True


@pytest.mark.parametrize("query", ["nome da minha irma", "qual meu peso", "onde eu moro"])
def test_pergunta_normal_nao_redireciona(query: str) -> None:
    assert looks_like_money_query(query) is False
