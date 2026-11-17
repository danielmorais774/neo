"""Guarda determinística: dinheiro não entra no PKS.

Transação financeira é dado de AGREGAÇÃO e mora nas tabelas ``fin_*`` (skill
'financas'). O planner LLM, porém, espelhava cada gasto como entidade da memória
semântica — e com valor e data CHUTADOS: em prod ele criou a entidade "Carteira"
com ``balance=980`` (conta feita por ele, não pelo extrato) e datas de 2025 num
extrato de 2026. Duas fontes de verdade para o mesmo número, e a do PKS não tem
quem a atualize: no lançamento seguinte ela já está velha, e é justamente ela que
o ``search_pks`` serve ao cognitivo.

O gate principal é pular o ``learn()`` quando a skill dona do assunto foi
autocarregada (``skill_owns_turn``, em ai_service). Isto aqui é o backstop
determinístico para a frase que fala de dinheiro sem casar keyword nenhuma.

Fato ≠ valor: "divido a Netflix com minha irmã" e "meu cartão principal é o
Nubank" continuam entrando (não carregam número que envelhece). O que é barrado é
valor, saldo e a transação em si.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

# Chaves de campo que carregam DINHEIRO (o número que envelhece).
_MONEY_KEY = re.compile(
    r"(amount|valor|saldo|balance|preco|price|custo|cost|fatura|invoice"
    r"|parcela|installment|gasto|despesa|receita|salario|orcamento|budget"
    r"|limite_credito|credit_limit)"
)

# Nomes de entidade que SÃO um lançamento ("Gasto de hoje", "Recebimento de R$
# 1.000,00 em 2025-04-10") — inclusive quando só aparecem como alvo de um campo,
# porque upsert_field cria a entidade que não existe.
_LEDGER_NAME = re.compile(
    r"^(gasto|gastos|despesa|despesas|receita|recebimento|pagamento|compra"
    r"|transferencia|fatura|extrato|saldo|salario)\b"
    r"|r\$"
)


def _norm(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


# Vocabulário de uma pergunta sobre dinheiro DO USUÁRIO — usado para redirecionar
# o search_pks (a memória semântica não tem, e não deve ter, essa resposta).
_MONEY_QUERY = re.compile(
    r"\b(gasto|gastos|gastei|despesa|despesas|saldo|extrato|fatura|cartao|credito"
    r"|debito|pix|boleto|parcela|parcelas|orcamento|receita|salario|financ\w*"
    r"|recebi|recebimento|lancamento|lancamentos|transacao|transacoes"
    r"|quanto (gastei|tenho|devo|sobrou))\b"
)

# Onde a resposta REALMENTE mora. O search_pks devolve isto em vez de deixar o
# modelo concluir "não tenho esse dado" (e então chutar) numa pergunta de dinheiro.
LEDGER_REDIRECT = (
    "Finanças NÃO estão na memória semântica (PKS) — valor, saldo, extrato e fatura "
    "vivem nas tabelas da skill `financas`, que é a única fonte de verdade. Para "
    "responder isto: chame `skill_read(\"financas\")` e use as tools `fin_statement` "
    "(extrato), `fin_balances` (saldos), `fin_card_invoice` (fatura) ou `fin_insights`. "
    "Não responda por memória nem por este resultado de busca."
)


def looks_like_money_query(query: str) -> bool:
    """A pergunta é sobre o dinheiro do usuário? (roteia o search_pks p/ a skill)."""
    return bool(_MONEY_QUERY.search(_norm(query)))


def is_ledger_memory(name: str | None, field_keys: Iterable[str]) -> bool:
    """Esta entidade do PKS carrega extrato/dinheiro?

    Usado para NÃO injetar o valor no prompt do cognitivo. A guarda de escrita já
    impede novas, mas o contexto é montado a partir do que existe: a "Carteira"
    com ``balance=980`` chegava ao modelo pelo contexto — sem tool nenhuma — e ele
    respondia o saldo velho com a maior confiança. Este é o terceiro canal (além
    do learn e do search_pks) e o único que não passa por decisão do modelo.
    """
    if name and _LEDGER_NAME.search(_norm(str(name))):
        return True
    return any(k and _MONEY_KEY.search(_norm(str(k))) for k in field_keys)


def ledger_violation(op: Any) -> str | None:
    """Motivo pelo qual esta operação de PKS é dado de extrato — ou None.

    Recebe uma ``PKSOperation`` (``entity``/``field`` são dicts do planner).
    """
    entity = getattr(op, "entity", None) or {}
    field = getattr(op, "field", None) or {}

    target_names = [
        entity.get("name"),
        (field.get("entity") or {}).get("name"),
    ]
    for name in target_names:
        if name and _LEDGER_NAME.search(_norm(str(name))):
            return f"ledger_entity:{name}"

    key = field.get("key")
    if key and _MONEY_KEY.search(_norm(str(key))):
        return f"ledger_field:{key}"

    return None
