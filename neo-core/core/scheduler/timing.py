"""Matemática de horário da agenda (docs/scheduler-plan.md §7).

Três tipos de horário — ``once``, ``interval``, ``cron`` — e um parser de cron
escrito à mão, de propósito: `croniter` obrigaria a entrar na IMAGEM do
container (dependência em runtime já queimou uma vez neste repo), e o que a
agenda precisa de cron cabe em 150 linhas.

**A regra do fuso, que é onde isto quebra se alguém distrair:** o banco guarda
``datetime`` naive em UTC (igual ao resto do core). Todo cálculo aqui converte
para o fuso da tarefa, calcula no calendário de lá, e volta para naive-UTC. Um
``astimezone`` esquecido é uma tarefa 3h fora do horário — e o usuário só
descobre quando o despertador toca às 4h da manhã.
"""

from __future__ import annotations

import calendar
import random
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Fuso padrão quando a tarefa não diz outro.
DEFAULT_TZ = "America/Fortaleza"

#: Piso do intervalo. Menos que isto é loop, não agenda.
MIN_INTERVAL_SECONDS = 60

#: Teto da busca do próximo disparo (um cron pode ser insatisfazível: "31 de
#: fevereiro"). Sem este teto a busca gira para sempre e leva o tick junto.
_MAX_SEARCH_DAYS = 400

_WEEKDAY_PT = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
_WEEKDAY_PT_SHORT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]

_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class ScheduleError(ValueError):
    """Expressão de agendamento inválida (mensagem já em português, p/ o usuário)."""


# ──────────────────────────────────────────────────────────────────────
# Fuso
# ──────────────────────────────────────────────────────────────────────

def get_tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo(DEFAULT_TZ)


def utcnow() -> datetime:
    """Agora em UTC naive — a moeda de tempo do banco."""
    return datetime.utcnow().replace(microsecond=0)


def to_local(dt_utc_naive: datetime, tz: str | None) -> datetime:
    """naive-UTC → aware no fuso da tarefa."""
    base = dt_utc_naive if dt_utc_naive.tzinfo else dt_utc_naive.replace(tzinfo=timezone.utc)
    return base.astimezone(get_tz(tz))


def to_utc_naive(dt_local: datetime, tz: str | None = None) -> datetime:
    """aware (ou naive no fuso da tarefa) → naive-UTC."""
    aware = dt_local if dt_local.tzinfo else dt_local.replace(tzinfo=get_tz(tz))
    return aware.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


# ──────────────────────────────────────────────────────────────────────
# once / interval
# ──────────────────────────────────────────────────────────────────────

def parse_once(expr: str, tz: str | None) -> datetime:
    """ISO local ("2026-09-20T15:00") → instante naive-UTC."""
    raw = (expr or "").strip().replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ScheduleError(
            f"Data/hora inválida: {expr!r}. Use ISO, ex.: 2026-09-20T15:00"
        ) from exc
    return to_utc_naive(parsed, tz)


def parse_interval(expr: str) -> timedelta:
    """"15m" / "2h" / "30s" / "1d" → timedelta, com piso de 1 minuto."""
    match = _INTERVAL_RE.match(expr or "")
    if not match:
        raise ScheduleError(
            f"Intervalo inválido: {expr!r}. Use um número + s/m/h/d, ex.: 15m, 2h, 1d"
        )
    seconds = int(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]
    if seconds < MIN_INTERVAL_SECONDS:
        raise ScheduleError(
            f"Intervalo curto demais ({expr}). O mínimo é {MIN_INTERVAL_SECONDS}s — "
            "abaixo disso não é agenda, é laço."
        )
    return timedelta(seconds=seconds)


# ──────────────────────────────────────────────────────────────────────
# cron
# ──────────────────────────────────────────────────────────────────────

class CronExpr:
    """Cron de 5 campos: minuto hora dia-do-mês mês dia-da-semana.

    Suporta ``*``, listas (``1,15``), faixas (``1-5``), passos (``*/15``,
    ``8-18/2``) e ``L`` no dia-do-mês (último dia — "todo fim de mês", que é
    pedido com uma frequência que não justifica não ter).

    Domingo é 0 **e** 7, como no cron de sempre. E vale a regra clássica: com
    dia-do-mês E dia-da-semana restritos, casa quem satisfizer QUALQUER um dos
    dois (é assim que ``0 0 1 * 1`` significa "todo dia 1º e toda segunda").
    """

    __slots__ = ("minutes", "hours", "doms", "months", "dows", "dom_last", "raw",
                 "dom_restricted", "dow_restricted")

    def __init__(self, expr: str) -> None:
        parts = (expr or "").split()
        if len(parts) != 5:
            raise ScheduleError(
                f"Cron precisa de 5 campos (minuto hora dia mês dia-da-semana): {expr!r}"
            )
        self.raw = " ".join(parts)
        minute, hour, dom, month, dow = parts
        self.minutes = self._field(minute, 0, 59, "minuto")
        self.hours = self._field(hour, 0, 23, "hora")
        self.dom_last = "L" in dom.upper()
        dom_clean = dom.upper().replace("L", "*") if self.dom_last else dom
        self.doms = self._field(dom_clean, 1, 31, "dia do mês")
        self.months = self._field(month, 1, 12, "mês")
        dows = self._field(dow, 0, 7, "dia da semana")
        # 7 e 0 são o mesmo domingo.
        self.dows = {0 if d == 7 else d for d in dows}
        self.dom_restricted = dom.strip() != "*" and not self.dom_last
        self.dow_restricted = dow.strip() != "*"

    @staticmethod
    def _field(raw: str, low: int, high: int, label: str) -> set[int]:
        values: set[int] = set()
        for chunk in (raw or "").split(","):
            chunk = chunk.strip()
            if not chunk:
                raise ScheduleError(f"Campo '{label}' vazio no cron.")
            step = 1
            if "/" in chunk:
                chunk, _, step_raw = chunk.partition("/")
                if not step_raw.isdigit() or int(step_raw) < 1:
                    raise ScheduleError(f"Passo inválido em '{label}': {step_raw!r}")
                step = int(step_raw)
                chunk = chunk.strip() or "*"
            if chunk == "*":
                start, end = low, high
            elif "-" in chunk.lstrip("-"):
                start_raw, _, end_raw = chunk.partition("-")
                start, end = _int_or_die(start_raw, label), _int_or_die(end_raw, label)
            else:
                start = end = _int_or_die(chunk, label)
            if start > end or start < low or end > high:
                raise ScheduleError(
                    f"Valor fora da faixa em '{label}': {chunk!r} (permitido {low}-{high})"
                )
            values.update(range(start, end + 1, step))
        if not values:
            raise ScheduleError(f"Campo '{label}' não casa com nada.")
        return values

    def matches(self, dt: datetime) -> bool:
        if dt.minute not in self.minutes or dt.hour not in self.hours:
            return False
        if dt.month not in self.months:
            return False
        return self._day_matches(dt)

    def _day_matches(self, dt: datetime) -> bool:
        # cron conta domingo=0; python conta segunda=0.
        dow = (dt.weekday() + 1) % 7
        dom_ok = dt.day in self.doms
        if self.dom_last:
            dom_ok = dt.day == calendar.monthrange(dt.year, dt.month)[1]
        dow_ok = dow in self.dows
        if (self.dom_restricted or self.dom_last) and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def next_after(self, start_local: datetime) -> datetime | None:
        """Primeiro instante que casa, estritamente DEPOIS de ``start_local``.

        Anda por saltos (mês → dia → hora → minuto) em vez de minuto a minuto:
        um cron anual varreria meio milhão de minutos no laço ingênuo.
        """
        cursor = (start_local + timedelta(minutes=1)).replace(second=0, microsecond=0)
        limit = start_local + timedelta(days=_MAX_SEARCH_DAYS)
        while cursor <= limit:
            if cursor.month not in self.months:
                cursor = _first_of_next_month(cursor)
                continue
            if not self._day_matches(cursor):
                cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if cursor.hour not in self.hours:
                cursor = _next_hour(cursor)
                continue
            if cursor.minute not in self.minutes:
                cursor += timedelta(minutes=1)
                continue
            return cursor
        return None


def _int_or_die(raw: str, label: str) -> int:
    raw = raw.strip()
    if not raw.isdigit():
        raise ScheduleError(f"Valor não numérico em '{label}': {raw!r}")
    return int(raw)


def _first_of_next_month(dt: datetime) -> datetime:
    year, month = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return dt.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_hour(dt: datetime) -> datetime:
    return (dt + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


# ──────────────────────────────────────────────────────────────────────
# Janela (§7.2)
# ──────────────────────────────────────────────────────────────────────

def _parse_hhmm(raw: str | None) -> tuple[int, int] | None:
    if not raw:
        return None
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(raw))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def in_window(dt_local: datetime, window: dict | None) -> bool:
    """O instante cabe na janela? Janela vazia aceita tudo."""
    if not window:
        return True
    dias = window.get("dias")
    if dias:
        # 1=segunda … 7=domingo (ISO). Escolhido por ser o que o humano fala.
        if dt_local.isoweekday() not in {int(d) for d in dias}:
            return False
    start = _parse_hhmm(window.get("hora_inicio"))
    end = _parse_hhmm(window.get("hora_fim"))
    if start or end:
        minutes = dt_local.hour * 60 + dt_local.minute
        lo = start[0] * 60 + start[1] if start else 0
        hi = end[0] * 60 + end[1] if end else 24 * 60
        if lo <= hi:
            if not (lo <= minutes <= hi):
                return False
        # Janela que atravessa a meia-noite (22:00→06:00).
        elif not (minutes >= lo or minutes <= hi):
            return False
    return True


# ──────────────────────────────────────────────────────────────────────
# O cálculo do próximo disparo
# ──────────────────────────────────────────────────────────────────────

def next_fire(
    *,
    schedule_kind: str,
    schedule_expr: str,
    tz: str | None = DEFAULT_TZ,
    after: datetime | None = None,
    window: dict | None = None,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> datetime | None:
    """Próximo disparo em naive-UTC, ou ``None`` se não há mais nenhum.

    ``after`` também é naive-UTC. Disparo que cai fora da janela é **empurrado
    para o próximo horário válido**, nunca descartado (§7.2).
    """
    now = after or utcnow()
    floor = max(now, starts_at) if starts_at else now

    if schedule_kind == "once":
        moment = parse_once(schedule_expr, tz)
        if moment <= now:
            return None
        return moment if _within_end(moment, ends_at) else None

    if schedule_kind == "interval":
        delta = parse_interval(schedule_expr)
        candidate = floor + delta
        for _ in range(5000):
            if not _within_end(candidate, ends_at):
                return None
            if in_window(to_local(candidate, tz), window):
                return candidate
            candidate += delta
        return None

    if schedule_kind == "cron":
        cron = CronExpr(schedule_expr)
        cursor = to_local(floor, tz)
        for _ in range(5000):
            nxt = cron.next_after(cursor)
            if nxt is None:
                return None
            candidate = to_utc_naive(nxt)
            if not _within_end(candidate, ends_at):
                return None
            if in_window(nxt, window):
                return candidate
            cursor = nxt
        return None

    raise ScheduleError(f"Tipo de agendamento desconhecido: {schedule_kind!r}")


def _within_end(moment: datetime, ends_at: datetime | None) -> bool:
    return ends_at is None or moment <= ends_at


def upcoming(
    *,
    schedule_kind: str,
    schedule_expr: str,
    tz: str | None = DEFAULT_TZ,
    window: dict | None = None,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    count: int = 5,
    after: datetime | None = None,
) -> list[datetime]:
    """Os próximos ``count`` disparos (naive-UTC) — é o preview da tela."""
    out: list[datetime] = []
    cursor = after or utcnow()
    for _ in range(count):
        nxt = next_fire(
            schedule_kind=schedule_kind, schedule_expr=schedule_expr, tz=tz,
            after=cursor, window=window, starts_at=starts_at, ends_at=ends_at,
        )
        if nxt is None:
            break
        out.append(nxt)
        cursor = nxt
    return out


def apply_jitter(moment: datetime, jitter_seconds: int) -> datetime:
    """Espalha o disparo para cinco tarefas não colidirem às 7h em ponto."""
    if not jitter_seconds or jitter_seconds <= 0:
        return moment
    return moment + timedelta(seconds=random.randint(0, int(jitter_seconds)))


# ──────────────────────────────────────────────────────────────────────
# Quiet hours (§8.3)
# ──────────────────────────────────────────────────────────────────────

def in_quiet_hours(dt_local: datetime, quiet: dict | None) -> bool:
    if not quiet:
        return False
    start = _parse_hhmm(quiet.get("inicio"))
    end = _parse_hhmm(quiet.get("fim"))
    if not start or not end:
        return False
    minutes = dt_local.hour * 60 + dt_local.minute
    lo = start[0] * 60 + start[1]
    hi = end[0] * 60 + end[1]
    if lo <= hi:
        return lo <= minutes <= hi
    return minutes >= lo or minutes <= hi  # atravessa a meia-noite


def quiet_hours_end(dt_local: datetime, quiet: dict | None) -> datetime | None:
    """Quando o silêncio acaba (para o modo ``adiar``)."""
    end = _parse_hhmm((quiet or {}).get("fim"))
    if not end:
        return None
    candidate = dt_local.replace(hour=end[0], minute=end[1], second=0, microsecond=0)
    if candidate <= dt_local:
        candidate += timedelta(days=1)
    return candidate


# ──────────────────────────────────────────────────────────────────────
# Descrição humana (§10 — ninguém deve precisar saber cron para usar isto)
# ──────────────────────────────────────────────────────────────────────

def describe(schedule_kind: str, schedule_expr: str, tz: str | None = DEFAULT_TZ,
             window: dict | None = None) -> str:
    try:
        base = _describe_base(schedule_kind, schedule_expr, tz)
    except ScheduleError as exc:
        return f"(agendamento inválido: {exc})"
    extra = _describe_window(window)
    return f"{base}{extra}"


def _describe_base(schedule_kind: str, schedule_expr: str, tz: str | None) -> str:
    if schedule_kind == "once":
        moment = to_local(parse_once(schedule_expr, tz), tz)
        return f"uma vez, em {moment.strftime('%d/%m/%Y às %H:%M')}"
    if schedule_kind == "interval":
        delta = parse_interval(schedule_expr)
        return f"a cada {_humanize_delta(delta)}"
    if schedule_kind == "cron":
        return _describe_cron(CronExpr(schedule_expr))
    return f"{schedule_kind}: {schedule_expr}"


def _humanize_delta(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total % 86400 == 0:
        n = total // 86400
        return "dia" if n == 1 else f"{n} dias"
    if total % 3600 == 0:
        n = total // 3600
        return "hora" if n == 1 else f"{n} horas"
    if total % 60 == 0:
        n = total // 60
        return "minuto" if n == 1 else f"{n} minutos"
    return f"{total} segundos"


def _describe_cron(cron: CronExpr) -> str:
    parts = cron.raw.split()
    minute, hour, dom, month, dow = parts

    # "a cada N minutos/horas"
    if hour == "*" and dom == "*" and month == "*" and dow == "*":
        if minute.startswith("*/"):
            return f"a cada {minute[2:]} minutos"
        if minute == "*":
            return "a cada minuto"
    if dom == "*" and month == "*" and dow == "*" and hour.startswith("*/") and minute.isdigit():
        return f"a cada {hour[2:]} horas (no minuto {minute})"

    horario = _describe_time(cron)
    quando = _describe_days(dom, month, dow)
    return f"{quando} {horario}".strip()


def _describe_time(cron: CronExpr) -> str:
    if len(cron.hours) == 1 and len(cron.minutes) == 1:
        hour = next(iter(cron.hours))
        minute = next(iter(cron.minutes))
        return f"às {hour:02d}:{minute:02d}"
    if len(cron.minutes) == 1 and len(cron.hours) > 1:
        hours = sorted(cron.hours)
        minute = next(iter(cron.minutes))
        if len(hours) <= 4:
            return "às " + ", ".join(f"{h:02d}:{minute:02d}" for h in hours)
        return f"de hora em hora entre {hours[0]:02d}h e {hours[-1]:02d}h"
    return "em vários horários"


def _describe_days(dom: str, month: str, dow: str) -> str:
    if dow != "*":
        dows = sorted({0 if d == 7 else d for d in CronExpr(f"0 0 * * {dow}").dows})
        # cron: 0=domingo. A lista pt começa na segunda.
        nomes = [_WEEKDAY_PT[(d - 1) % 7] for d in dows]
        if dows == [1, 2, 3, 4, 5]:
            rotulo = "de segunda a sexta"
        elif dows in ([0, 6], [6, 0]):
            rotulo = "no fim de semana"
        elif len(nomes) == 1:
            rotulo = f"toda {nomes[0]}" if nomes[0] not in ("sábado", "domingo") else f"todo {nomes[0]}"
        else:
            rotulo = "em " + ", ".join(nomes)
        if dom != "*":
            rotulo += f" (e no dia {dom})"
        return rotulo
    if "L" in dom.upper():
        return "no último dia do mês"
    if dom.startswith("*/"):
        return f"a cada {dom[2:]} dias"
    if dom != "*":
        if month != "*":
            return f"no dia {dom} do mês {month}"
        return f"todo dia {dom}"
    return "todo dia"


def _describe_window(window: dict | None) -> str:
    if not window:
        return ""
    bits: list[str] = []
    dias = window.get("dias")
    if dias:
        nomes = [_WEEKDAY_PT_SHORT[(int(d) - 1) % 7] for d in sorted({int(x) for x in dias})]
        bits.append("só " + "/".join(nomes))
    start, end = window.get("hora_inicio"), window.get("hora_fim")
    if start or end:
        bits.append(f"só entre {start or '00:00'} e {end or '23:59'}")
    return f" — {', '.join(bits)}" if bits else ""
