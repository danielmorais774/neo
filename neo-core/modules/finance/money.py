"""Helpers puros de dinheiro e calendário de fatura (pt-BR).

Tudo aqui é função pura e testável sem banco: parse de valores no formato
brasileiro ("1.234,56", "R$ 45,90"), formatação BRL, cálculo do mês de fatura
pelo dia de fechamento e divisão de parcelas com ajuste de centavos.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

# Fuso do usuário (Ceará — sem horário de verão). occurred_at é DATE, então o
# único uso do fuso é decidir que dia é "hoje" a partir do relógio UTC da VPS.
TZ_BR = ZoneInfo("America/Fortaleza")

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_BR_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def today_br() -> date:
    return datetime.now(TZ_BR).date()


def parse_amount_cents(value: object) -> int:
    """Valor em reais (número ou texto pt-BR) → centavos inteiros positivos.

    Aceita: 45.9, 45, "45,90", "R$ 1.234,56", "1234.56", "1.234". Regra para
    texto: com ',' e '.' juntos, '.' é milhar; só ',' é decimal; só '.' com
    EXATAMENTE 3 dígitos depois é milhar ("1.234" = 1234 reais), senão decimal.
    """
    if isinstance(value, bool):
        raise ValueError("Valor inválido.")
    if isinstance(value, (int, float)):
        cents = round(float(value) * 100)
    else:
        text = str(value or "").strip()
        text = re.sub(r"(?i)^r\$\s*", "", text).replace(" ", "")
        if not text:
            raise ValueError("Valor vazio.")
        negative = text.startswith("-")
        text = text.lstrip("+-")
        if not re.fullmatch(r"[\d.,]+", text):
            raise ValueError(f"Não entendi o valor '{value}'.")
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        elif "," in text:
            text = text.replace(",", ".")
        elif "." in text:
            head, _, tail = text.rpartition(".")
            if len(tail) == 3 and head.replace(".", ""):
                text = text.replace(".", "")
        cents = round(float(text) * 100)
        if negative:
            cents = -cents
    if cents <= 0:
        raise ValueError("O valor precisa ser maior que zero.")
    return int(cents)


def format_brl(cents: int) -> str:
    """Centavos → "R$ 1.234,56" (sinal na frente quando negativo)."""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    reais, cent = divmod(cents, 100)
    grouped = f"{reais:,}".replace(",", ".")
    return f"{sign}R$ {grouped},{cent:02d}"


def parse_date_br(value: object, *, default: date | None = None) -> date:
    """Data ISO ("2026-07-13") ou BR ("13/07/2026") → date. Vazio → default/hoje."""
    if value is None or str(value).strip() == "":
        return default or today_br()
    text = str(value).strip()
    m = _ISO_DATE_RE.match(text)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _BR_DATE_RE.match(text)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    raise ValueError(f"Data inválida: '{value}'. Use AAAA-MM-DD ou DD/MM/AAAA.")


def parse_month(value: object, *, default: date | None = None) -> date:
    """"2026-07" (ou data completa) → 1º dia do mês. Vazio → default/mês atual."""
    if value is None or str(value).strip() == "":
        base = default or today_br()
        return base.replace(day=1)
    text = str(value).strip()
    m = _MONTH_RE.match(text)
    if m:
        return date(int(m.group(1)), int(m.group(2)), 1)
    return parse_date_br(text).replace(day=1)


def add_months(month: date, n: int) -> date:
    """Soma n meses a um 1º-dia-do-mês."""
    total = month.year * 12 + (month.month - 1) + n
    return date(total // 12, total % 12 + 1, 1)


# "próximo mês" / "mês que vem" etc. → deslocamento em relação à fatura corrente.
_RELATIVE_MONTH = {
    "proximo": 1, "próximo": 1, "proxima": 1, "próxima": 1, "seguinte": 1,
    "que vem": 1, "next": 1, "adiantar": 1, "adiantado": 1,
    "atual": 0, "corrente": 0, "vigente": 0, "esse": 0, "este": 0, "current": 0,
    "anterior": -1, "passado": -1, "passada": -1, "previous": -1,
}

_MONTH_NAMES = {
    name: n
    for n, names in enumerate(
        (("janeiro", "jan"), ("fevereiro", "fev"), ("marco", "março", "mar"),
         ("abril", "abr"), ("maio", "mai"), ("junho", "jun"), ("julho", "jul"),
         ("agosto", "ago"), ("setembro", "set"), ("outubro", "out"),
         ("novembro", "nov"), ("dezembro", "dez")), start=1)
    for name in names
}


def parse_month_ref(value: object, *, current: date) -> date:
    """AAAA-MM/data, relativo ("próximo"/"atual"/"anterior") ou nome do mês → 1º do mês.

    Relativo e nome são resolvidos contra ``current`` (a fatura corrente do cartão).
    Existe porque "paga a fatura do próximo mês"/"a de agosto" fazia o modelo chutar
    o AAAA-MM ou cair no default (fatura aberta mais antiga) e pagar o mês ERRADO
    (sessão 0371a5a8: pediram agosto, pagou julho). Nome de mês vira a PRÓXIMA
    ocorrência a partir da corrente (em jul/2026, "agosto" = ago/2026; "janeiro" =
    jan/2027) — a leitura natural de "a fatura de <mês>".
    """
    text = str(value or "").strip().lower()
    if not text:
        return current
    for token, delta in _RELATIVE_MONTH.items():
        if token in text:
            return add_months(current, delta)
    for token, month_n in _MONTH_NAMES.items():
        if token in text.split() or text == token:
            year = current.year + (1 if month_n < current.month else 0)
            return date(year, month_n, 1)
    return parse_month(value, default=current)


def month_label(month: date) -> str:
    names = (
        "jan", "fev", "mar", "abr", "mai", "jun",
        "jul", "ago", "set", "out", "nov", "dez",
    )
    return f"{names[month.month - 1]}/{month.year}"


def invoice_month_for(purchase: date, closing_day: int | None) -> date:
    """Mês da fatura de uma compra no crédito.

    Compra NO dia do fechamento ou depois cai na fatura seguinte (convenção
    documentada na skill). Sem closing_day cadastrado, fatura = mês civil.
    """
    first = purchase.replace(day=1)
    if closing_day is None:
        return first
    if purchase.day >= closing_day:
        return add_months(first, 1)
    return first


def invoice_due_date(invoice_month: date, closing_day: int | None, due_day: int | None) -> date | None:
    """Vencimento da fatura: due_day no próprio mês se cai depois do fechamento,
    senão no mês seguinte (padrão dos cartões brasileiros)."""
    if due_day is None:
        return None
    if closing_day is not None and due_day > closing_day:
        base = invoice_month
    else:
        base = add_months(invoice_month, 1)
    return base.replace(day=min(due_day, 28))


def split_installments(total_cents: int, n: int) -> list[int]:
    """Divide o total em n parcelas; o resto de centavos vai na 1ª parcela."""
    if n < 1:
        raise ValueError("Número de parcelas inválido.")
    base = total_cents // n
    remainder = total_cents - base * n
    return [base + remainder] + [base] * (n - 1)
