"""Módulo finance (skill ``financas``) — helpers puros, fuzzy match e contratos.

O executor contra Postgres real não é testado aqui (mesma linha do sqlro: as
agregações SQL são exercitadas em prod); o que quebra silencioso — parsing de
dinheiro BR, calendário de fatura, divisão de parcelas, resolução por nome e o
contrato tools ⟷ skill — é coberto.
"""

from datetime import date
from types import SimpleNamespace
from pathlib import Path

import pytest

from modules.finance import FINANCE_TOOL_NAMES, FINANCE_TOOLS
from modules.finance.executor import FinanceExecutor, _parse_fields, _validate_day
from modules.finance.money import (
    add_months,
    format_brl,
    invoice_due_date,
    invoice_month_for,
    parse_amount_cents,
    parse_date_br,
    parse_month,
    split_installments,
)
from modules.finance.repo import AmbiguousNameError, FinanceRepo


# ── Dinheiro (formato BR) ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, cents",
    [
        (45.9, 4590),
        (45, 4500),
        ("45,90", 4590),
        ("R$ 45,90", 4590),
        ("r$1.234,56", 123456),
        ("1234.56", 123456),
        ("1.234", 123400),      # só ponto com 3 dígitos = milhar (pt-BR)
        ("12.5", 1250),          # só ponto com 2 dígitos = decimal
        ("0,05", 5),
        ("10", 1000),
    ],
)
def test_parse_amount_br(raw, cents):
    assert parse_amount_cents(raw) == cents


@pytest.mark.parametrize("raw", ["", None, "abc", "-10", 0, -5, True])
def test_parse_amount_rejects(raw):
    with pytest.raises(ValueError):
        parse_amount_cents(raw)


def test_format_brl():
    assert format_brl(123456) == "R$ 1.234,56"
    assert format_brl(5) == "R$ 0,05"
    assert format_brl(-9900) == "-R$ 99,00"


# ── Datas ────────────────────────────────────────────────────────────────


def test_parse_date_both_formats():
    assert parse_date_br("2026-07-13") == date(2026, 7, 13)
    assert parse_date_br("13/07/2026") == date(2026, 7, 13)
    assert parse_date_br("", default=date(2026, 1, 2)) == date(2026, 1, 2)
    with pytest.raises(ValueError):
        parse_date_br("ontem")


def test_parse_month():
    assert parse_month("2026-07") == date(2026, 7, 1)
    assert parse_month("15/07/2026") == date(2026, 7, 1)
    assert parse_month(None, default=date(2026, 3, 20)) == date(2026, 3, 1)


def test_add_months_wraps_year():
    assert add_months(date(2026, 11, 1), 3) == date(2027, 2, 1)
    assert add_months(date(2026, 1, 1), -1) == date(2025, 12, 1)


# ── Calendário de fatura (convenção BR) ──────────────────────────────────


def test_invoice_month_before_closing_day_stays():
    assert invoice_month_for(date(2026, 7, 10), closing_day=28) == date(2026, 7, 1)


def test_invoice_month_on_or_after_closing_rolls():
    # No dia do fechamento (ou depois) já cai na fatura seguinte.
    assert invoice_month_for(date(2026, 7, 28), closing_day=28) == date(2026, 8, 1)
    assert invoice_month_for(date(2026, 12, 30), closing_day=28) == date(2027, 1, 1)


def test_invoice_month_without_closing_day_is_civil_month():
    assert invoice_month_for(date(2026, 7, 31), closing_day=None) == date(2026, 7, 1)


def test_invoice_due_date_same_or_next_month():
    # vence dia 7, fecha dia 28 → vencimento no mês seguinte ao da fatura
    assert invoice_due_date(date(2026, 7, 1), closing_day=28, due_day=7) == date(2026, 8, 7)
    # vence dia 15, fecha dia 5 → vencimento no próprio mês da fatura
    assert invoice_due_date(date(2026, 7, 1), closing_day=5, due_day=15) == date(2026, 7, 15)
    assert invoice_due_date(date(2026, 7, 1), closing_day=28, due_day=None) is None


# ── Parcelas ─────────────────────────────────────────────────────────────


def test_split_installments_sums_and_first_takes_remainder():
    parts = split_installments(100_00, 3)
    assert sum(parts) == 100_00
    assert parts == [3334, 3333, 3333]
    assert split_installments(1200_00, 12) == [100_00] * 12
    with pytest.raises(ValueError):
        split_installments(1000, 0)


# ── Resolução fuzzy por nome ─────────────────────────────────────────────


def _items(*names):
    return [SimpleNamespace(name=n) for n in names]


def test_match_exact_ignores_case_and_accents():
    items = _items("Cartão Nubank", "Inter")
    assert FinanceRepo._match_by_name(items, "cartao nubank", label="cartão").name == "Cartão Nubank"


def test_match_prefix_and_contains():
    items = _items("Nubank Ultravioleta", "Banco do Brasil")
    assert FinanceRepo._match_by_name(items, "nubank", label="cartão").name == "Nubank Ultravioleta"
    assert FinanceRepo._match_by_name(items, "brasil", label="conta").name == "Banco do Brasil"


def test_match_reverse_containment():
    # "cartão nubank" deve achar o cartão cadastrado só como "Nubank".
    items = _items("Nubank", "Inter")
    assert FinanceRepo._match_by_name(items, "cartão nubank", label="cartão").name == "Nubank"


def test_match_ambiguous_lists_options():
    items = _items("Nubank PJ", "Nubank PF")
    with pytest.raises(AmbiguousNameError, match="Nubank PF"):
        FinanceRepo._match_by_name(items, "nubank", label="conta")


def test_match_not_found_lists_existing():
    with pytest.raises(ValueError, match="Cadastrados"):
        FinanceRepo._match_by_name(_items("Carteira"), "itau", label="conta")


# ── Contrato tools ⟷ skill ───────────────────────────────────────────────


def test_tools_are_server_side_finance_module():
    for tool in FINANCE_TOOLS:
        assert tool.module == "finance"
        assert tool.execution == "server-side"
        assert tool.name.startswith("fin_")


def test_skill_frontmatter_tools_match_module():
    skill = Path(__file__).resolve().parents[2] / "skills" / "financas" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    tools_line = next(l for l in text.splitlines() if l.startswith("tools:"))
    declared = {t.strip() for t in tools_line.split("[", 1)[1].rstrip("]").split(",")}
    assert declared == set(FINANCE_TOOL_NAMES)


# ── Executor: validações que não precisam de banco ───────────────────────


@pytest.mark.asyncio
async def test_executor_refuses_without_user():
    executor = FinanceExecutor(db=None)
    with pytest.raises(ValueError, match="usuário"):
        await executor.execute("fin_balances", {})


def test_parse_fields_json_and_errors():
    assert _parse_fields('{"name": "Nubank"}') == {"name": "Nubank"}
    assert _parse_fields({"a": 1}) == {"a": 1}
    assert _parse_fields(None) == {}
    with pytest.raises(ValueError, match="JSON"):
        _parse_fields("{name: nubank}")
    with pytest.raises(ValueError):
        _parse_fields("[1, 2]")


def test_validate_day_bounds():
    assert _validate_day(28, "closing_day") == 28
    assert _validate_day(None, "closing_day") is None
    with pytest.raises(ValueError):
        _validate_day(31, "closing_day")
