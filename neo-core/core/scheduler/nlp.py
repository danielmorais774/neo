"""Tempo em português → agendamento concreto (docs/scheduler-plan.md §9).

O modelo passa a FRASE ("todo dia útil às 8h") e quem resolve é o servidor,
deterministicamente. Dois motivos, os dois medidos neste repo:

1. LLM erra data em silêncio. Um cron fantasma às 3h da manhã não tem como ser
   percebido antes de acordar o usuário.
2. O resultado tem que voltar em texto para o Neo **confirmar em voz alta**
   ("marquei para segunda, 15/09, às 08:00"). Confirmar o horário RESOLVIDO é o
   que transforma um erro de interpretação em uma correção de meio segundo.

O que não casar aqui não é chutado: devolve erro pedindo a frase de outro jeito.
Chute silencioso é pior que pergunta.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta

from core.scheduler.timing import (
    DEFAULT_TZ,
    CronExpr,
    ScheduleError,
    describe,
    get_tz,
    to_local,
    to_utc_naive,
    utcnow,
)

#: nome → número do cron (0=domingo).
_DIAS = {
    "domingo": 0, "segunda": 1, "segunda-feira": 1, "terca": 2, "terca-feira": 2,
    "quarta": 3, "quarta-feira": 3, "quinta": 4, "quinta-feira": 4,
    "sexta": 5, "sexta-feira": 5, "sabado": 6,
}

_UNIDADES = {
    "segundo": "s", "segundos": "s",
    "minuto": "m", "minutos": "m", "min": "m", "mins": "m",
    "hora": "h", "horas": "h",
    "dia": "d", "dias": "d",
}

_NUM_PT = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5,
    "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10, "onze": 11, "doze": 12,
    "quinze": 15, "vinte": 20, "trinta": 30, "meia": 30,
}

#: Âncoras de hora do dia quando a frase não dá número ("de manhã", "à noite").
_PERIODOS = {"manha": (8, 0), "tarde": (14, 0), "noite": (20, 0), "madrugada": (3, 0)}


class TimeParseError(ScheduleError):
    """A frase não foi entendida — com uma dica de como reescrever."""


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFD", (text or "").lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text).strip()


def _hora(texto: str) -> tuple[int, int] | None:
    """Extrai hora:minuto da frase. Devolve ``None`` se não houver nenhuma."""
    # "às 7h30", "7:30", "07h", "as 19 horas"
    match = re.search(r"(?:as|à|a|ás)?\s*(\d{1,2})\s*(?::|h|hs|horas?)\s*(\d{2})?", texto)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if hour <= 23 and minute <= 59:
            if "da tarde" in texto or "da noite" in texto:
                if hour < 12:
                    hour += 12
            return hour, minute
    # "meio-dia" / "meia-noite"
    if "meio dia" in texto or "meio-dia" in texto:
        return 12, 0
    if "meia noite" in texto or "meia-noite" in texto:
        return 0, 0
    for nome, (hour, minute) in _PERIODOS.items():
        if f"de {nome}" in texto or f"da {nome}" in texto or f"a {nome}" in texto:
            return hour, minute
    return None


def _quantidade(texto: str) -> tuple[int, str] | None:
    """"a cada 15 minutos" / "de 2 em 2 horas" / "a cada meia hora"."""
    match = re.search(
        r"(?:a cada|de)\s+(\d+|\w+)\s*(?:em\s+\d+\s*)?"
        r"(segundos?|minutos?|mins?|horas?|dias?)",
        texto,
    )
    if not match:
        return None
    raw, unidade = match.group(1), match.group(2)
    if raw.isdigit():
        qtd = int(raw)
    elif raw in _NUM_PT:
        qtd = _NUM_PT[raw]
    else:
        return None
    unit = _UNIDADES.get(unidade)
    if not unit:
        return None
    # "a cada meia hora" → 30 minutos, não 30 horas.
    if raw == "meia" and unit == "h":
        return 30, "m"
    return qtd, unit


def _dias_da_semana(texto: str) -> list[int]:
    achados = []
    for nome, num in _DIAS.items():
        if re.search(rf"\b{nome}s?\b", texto):
            achados.append(num)
    return sorted(set(achados))


def parse_quando(frase: str, tz: str | None = DEFAULT_TZ, now: datetime | None = None) -> dict:
    """Frase em português → ``{schedule_kind, schedule_expr, descricao, proximo}``.

    ``now`` é naive-UTC (injetável para teste). Levanta :class:`TimeParseError`
    quando não entende — de propósito.
    """
    texto = _norm(frase)
    if not texto:
        raise TimeParseError("Diga quando a tarefa deve rodar (ex.: 'todo dia às 7h').")
    agora_utc = now or utcnow()
    local = to_local(agora_utc, tz)

    # 0. Passagem direta: já veio cron ("0 7 * * 1-5") ou ISO.
    if re.fullmatch(r"[\d\*/,\-lL ]{5,}", frase.strip()) and len(frase.split()) == 5:
        CronExpr(frase.strip())  # valida
        return _resultado("cron", frase.strip(), tz, agora_utc)
    iso = re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?", frase.strip())
    if iso:
        return _resultado("once", frase.strip().replace(" ", "T"), tz, agora_utc)

    hora = _hora(texto)

    # 1. Relativo puro: "daqui a 2 horas", "em 30 minutos" → once.
    rel = re.search(r"(?:daqui a|daqui|em|dentro de)\s+(\d+|\w+)\s*(segundos?|minutos?|mins?|horas?|dias?)", texto)
    if rel and "a cada" not in texto and "todo" not in texto and "toda" not in texto:
        raw, unidade = rel.group(1), rel.group(2)
        qtd = int(raw) if raw.isdigit() else _NUM_PT.get(raw, 0)
        unit = _UNIDADES.get(unidade)
        if qtd and unit:
            delta = timedelta(**{
                {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[unit]: qtd
            })
            alvo = (local + delta).replace(second=0, microsecond=0)
            return _resultado("once", alvo.strftime("%Y-%m-%dT%H:%M"), tz, agora_utc)

    # 2. Recorrente por intervalo: "a cada 15 minutos", "de hora em hora".
    if "a cada" in texto or re.search(r"de \d+ em \d+", texto) or "de hora em hora" in texto:
        if "de hora em hora" in texto:
            return _resultado("interval", "1h", tz, agora_utc)
        qt = _quantidade(texto)
        if qt:
            qtd, unit = qt
            # "a cada dia às 7h" é cron, não intervalo — senão o horário se perde.
            if unit == "d" and hora:
                return _resultado("cron", f"{hora[1]} {hora[0]} */{qtd} * *", tz, agora_utc)
            return _resultado("interval", f"{qtd}{unit}", tz, agora_utc)

    # 3. Dia da semana: "toda segunda às 9h", "segundas e quartas".
    dows = _dias_da_semana(texto)
    util = "dia util" in texto or "dias uteis" in texto or "durante a semana" in texto
    if util:
        dows = [1, 2, 3, 4, 5]
    if dows:
        h, m = hora or (9, 0)
        return _resultado("cron", f"{m} {h} * * {','.join(str(d) for d in dows)}", tz, agora_utc)

    # 4. Dia do mês: "todo dia 5 às 9h", "dia 10 de todo mês".
    dia_mes = re.search(r"\b(?:todo )?dia (\d{1,2})\b", texto)
    if dia_mes and int(dia_mes.group(1)) <= 31 and not re.search(r"dia \d{1,2}\s*[:h]", texto):
        h, m = hora or (9, 0)
        return _resultado("cron", f"{m} {h} {int(dia_mes.group(1))} * *", tz, agora_utc)

    # 5. Fim do mês.
    if "fim do mes" in texto or "ultimo dia do mes" in texto or "final do mes" in texto:
        h, m = hora or (9, 0)
        return _resultado("cron", f"{m} {h} L * *", tz, agora_utc)

    # 6. Todo dia / diariamente.
    if any(p in texto for p in ("todo dia", "todos os dias", "diariamente", "diario", "por dia")):
        h, m = hora or (9, 0)
        return _resultado("cron", f"{m} {h} * * *", tz, agora_utc)

    # 7. Toda semana / todo mês (sem dia dito: usa hoje como âncora).
    if "toda semana" in texto or "semanalmente" in texto:
        h, m = hora or (9, 0)
        return _resultado("cron", f"{m} {h} * * {(local.weekday() + 1) % 7}", tz, agora_utc)
    if "todo mes" in texto or "mensalmente" in texto:
        h, m = hora or (9, 0)
        return _resultado("cron", f"{m} {h} {local.day} * *", tz, agora_utc)

    # 8. Uma vez: "amanhã às 15h", "hoje às 22h", "depois de amanhã".
    if hora or "amanha" in texto or "hoje" in texto:
        h, m = hora or (9, 0)
        alvo = local.replace(hour=h, minute=m, second=0, microsecond=0)
        if "depois de amanha" in texto:
            alvo += timedelta(days=2)
        elif "amanha" in texto:
            alvo += timedelta(days=1)
        elif alvo <= local:
            # "às 7h" quando já passou das 7 é amanhã — é o que a pessoa quis dizer.
            alvo += timedelta(days=1)
        return _resultado("once", alvo.strftime("%Y-%m-%dT%H:%M"), tz, agora_utc)

    raise TimeParseError(
        f"Não entendi quando é {frase!r}. Tente algo como 'todo dia às 7h', "
        "'toda segunda às 9h', 'a cada 30 minutos', 'amanhã às 15h' ou um cron "
        "de 5 campos ('0 7 * * 1-5')."
    )


def _resultado(kind: str, expr: str, tz: str | None, agora: datetime) -> dict:
    from core.scheduler.timing import next_fire

    proximo = next_fire(schedule_kind=kind, schedule_expr=expr, tz=tz, after=agora)
    return {
        "schedule_kind": kind,
        "schedule_expr": expr,
        "timezone": tz or DEFAULT_TZ,
        "descricao": describe(kind, expr, tz),
        "proximo_utc": proximo,
        "proximo_local": (
            to_local(proximo, tz).strftime("%d/%m/%Y às %H:%M") if proximo else None
        ),
    }
