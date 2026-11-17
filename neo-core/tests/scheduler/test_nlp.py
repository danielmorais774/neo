"""Frase em português → agendamento (docs/scheduler-plan.md §9).

O que este arquivo protege é a promessa de "me lembra amanhã às 15h" virar o
horário certo — e, o que importa tanto quanto, de uma frase ambígua ser
RECUSADA em vez de chutada.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from core.scheduler.nlp import TimeParseError, parse_quando

TZ = "America/Fortaleza"
AGORA = datetime(2026, 9, 12, 15, 0)  # sábado, 12:00 local


@pytest.mark.parametrize(
    "frase,kind,expr",
    [
        ("todo dia às 7h", "cron", "0 7 * * *"),
        ("todo dia útil as 8h", "cron", "0 8 * * 1,2,3,4,5"),
        ("toda segunda às 9h", "cron", "0 9 * * 1"),
        ("segunda e quarta às 18:30", "cron", "30 18 * * 1,3"),
        ("a cada 15 minutos", "interval", "15m"),
        ("a cada meia hora", "interval", "30m"),
        ("de hora em hora", "interval", "1h"),
        ("todo dia 5 às 9h", "cron", "0 9 5 * *"),
        ("todo fim do mês às 23h", "cron", "0 23 L * *"),
        ("0 7 * * 1-5", "cron", "0 7 * * 1-5"),
        ("todos os dias de manhã", "cron", "0 8 * * *"),
        ("toda sexta à noite", "cron", "0 20 * * 5"),
    ],
)
def test_frases_recorrentes(frase: str, kind: str, expr: str) -> None:
    resultado = parse_quando(frase, tz=TZ, now=AGORA)
    assert (resultado["schedule_kind"], resultado["schedule_expr"]) == (kind, expr)


@pytest.mark.parametrize(
    "frase,esperado",
    [
        ("daqui a 2 horas", "12/09/2026 às 14:00"),
        ("em 30 minutos", "12/09/2026 às 12:30"),
        ("amanhã às 15h", "13/09/2026 às 15:00"),
        ("hoje às 22h", "12/09/2026 às 22:00"),
    ],
)
def test_uma_vez(frase: str, esperado: str) -> None:
    assert parse_quando(frase, tz=TZ, now=AGORA)["proximo_local"] == esperado


def test_hora_que_ja_passou_vira_amanha() -> None:
    """'às 7h' às 12h da tarde é amanhã — é o que a pessoa quis dizer."""
    assert parse_quando("às 7h", tz=TZ, now=AGORA)["proximo_local"] == "13/09/2026 às 07:00"


def test_frase_sem_horario_e_recusada_em_vez_de_chutada() -> None:
    for frase in ("quando der", "sei lá", "qualquer hora dessas"):
        with pytest.raises(TimeParseError):
            parse_quando(frase, tz=TZ, now=AGORA)


def test_resultado_traz_o_horario_para_o_neo_confirmar() -> None:
    """É esse campo que vira 'marquei para segunda, 14/09, às 08:00'."""
    resultado = parse_quando("todo dia útil às 8h", tz=TZ, now=AGORA)
    assert resultado["proximo_local"] == "14/09/2026 às 08:00"
    assert resultado["descricao"] == "de segunda a sexta às 08:00"
