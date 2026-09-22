"""A matemática de horário da agenda (docs/scheduler-plan.md §7).

Todos os casos usam um instante fixo em UTC e conferem o resultado no fuso do
usuário — porque o modo de falha aqui não é exceção, é uma tarefa que dispara
3h fora da hora e ninguém percebe até o despertador tocar errado.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from core.scheduler.timing import (
    CronExpr,
    ScheduleError,
    describe,
    in_quiet_hours,
    in_window,
    next_fire,
    parse_interval,
    to_local,
    upcoming,
)

TZ = "America/Fortaleza"  # UTC-3, sem horário de verão
#: Sábado, 12/09/2026, 12:00 em Fortaleza.
AGORA = datetime(2026, 9, 12, 15, 0)


def local(moment: datetime) -> str:
    return to_local(moment, TZ).strftime("%a %d/%m %H:%M")


@pytest.mark.parametrize(
    "expr,esperado",
    [
        ("0 7 * * *", "Sun 13/09 07:00"),
        ("0 7 * * 1-5", "Mon 14/09 07:00"),
        ("*/15 * * * *", "Sat 12/09 12:15"),
        ("0 3 L * *", "Wed 30/09 03:00"),
        ("30 8 5 * *", "Mon 05/10 08:30"),
        ("0 8,12,18 * * *", "Sat 12/09 18:00"),
    ],
)
def test_proximo_disparo_no_fuso_do_usuario(expr: str, esperado: str) -> None:
    assert local(next_fire(schedule_kind="cron", schedule_expr=expr, tz=TZ, after=AGORA)) == esperado


def test_grava_utc_mostra_local() -> None:
    """O banco guarda naive-UTC; a diferença tem que ser exatamente o offset."""
    momento = next_fire(schedule_kind="cron", schedule_expr="0 7 * * *", tz=TZ, after=AGORA)
    assert momento == datetime(2026, 9, 13, 10, 0)  # 07:00 -03:00
    assert to_local(momento, TZ).hour == 7


def test_dia_do_mes_e_dia_da_semana_sao_OU() -> None:
    """Regra clássica do cron: '0 0 1 * 1' = todo dia 1º E toda segunda."""
    cron = CronExpr("0 0 1 * 1")
    assert cron.matches(datetime(2026, 9, 14, 0, 0))  # segunda
    assert cron.matches(datetime(2026, 10, 1, 0, 0))  # dia 1º (quinta)
    assert not cron.matches(datetime(2026, 9, 16, 0, 0))  # quarta, dia 16


def test_intervalo_tem_piso() -> None:
    assert parse_interval("15m").total_seconds() == 900
    with pytest.raises(ScheduleError):
        parse_interval("30s")


def test_janela_empurra_para_frente_em_vez_de_perder() -> None:
    """Sábado 12h + janela seg-sex 08:00-18:00 → segunda 08:00, não 'nunca'."""
    janela = {"dias": [1, 2, 3, 4, 5], "hora_inicio": "08:00", "hora_fim": "18:00"}
    momento = next_fire(
        schedule_kind="interval", schedule_expr="2h", tz=TZ, after=AGORA, window=janela
    )
    assert local(momento) == "Mon 14/09 08:00"


def test_janela_atravessando_meia_noite() -> None:
    assert in_window(datetime(2026, 9, 12, 23, 30), {"hora_inicio": "22:00", "hora_fim": "06:00"})
    assert in_window(datetime(2026, 9, 12, 2, 0), {"hora_inicio": "22:00", "hora_fim": "06:00"})
    assert not in_window(datetime(2026, 9, 12, 12, 0), {"hora_inicio": "22:00", "hora_fim": "06:00"})


def test_uma_vez_no_passado_nao_dispara() -> None:
    assert next_fire(schedule_kind="once", schedule_expr="2020-01-01T10:00", tz=TZ, after=AGORA) is None


def test_cron_insatisfazivel_devolve_none_em_vez_de_girar() -> None:
    assert next_fire(schedule_kind="cron", schedule_expr="0 0 30 2 *", tz=TZ, after=AGORA) is None


def test_upcoming_nao_repete_o_mesmo_instante() -> None:
    proximos = upcoming(schedule_kind="cron", schedule_expr="0 7 * * 1-5", tz=TZ, count=3, after=AGORA)
    assert len(proximos) == len(set(proximos)) == 3
    assert proximos == sorted(proximos)


@pytest.mark.parametrize(
    "expr,texto",
    [
        ("0 7 * * *", "todo dia às 07:00"),
        ("0 7 * * 1-5", "de segunda a sexta às 07:00"),
        ("*/15 * * * *", "a cada 15 minutos"),
        ("0 3 L * *", "no último dia do mês às 03:00"),
    ],
)
def test_descricao_humana(expr: str, texto: str) -> None:
    assert describe("cron", expr, TZ) == texto


def test_cron_invalido_explica_o_erro() -> None:
    for ruim in ("0 7 * *", "99 7 * * *", "0 7 * * 9", "a b c d e"):
        with pytest.raises(ScheduleError):
            CronExpr(ruim)


def test_quiet_hours_atravessa_a_meia_noite() -> None:
    silencio = {"inicio": "23:00", "fim": "07:00"}
    assert in_quiet_hours(datetime(2026, 9, 13, 2, 30), silencio)
    assert not in_quiet_hours(datetime(2026, 9, 13, 12, 0), silencio)
