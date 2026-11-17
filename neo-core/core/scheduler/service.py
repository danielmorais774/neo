"""ScheduleService — CRUD, validação e materialização de disparo.

O painel (api/routes/schedules.py) e as tools do cérebro (core/scheduler/tools.py)
entram pelo MESMO serviço. É de propósito: se a tela validasse de um jeito e a
tool de outro, o Neo criaria por conversa tarefas que a tela não consegue editar
— e o usuário descobriria isso no pior momento possível.

Validar na hora de SALVAR (e não na hora de disparar) é a outra regra: um cron
inválido tem que falhar na cara de quem criou, não às 3h da manhã em silêncio.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from core.scheduler import conditions as cond
from core.scheduler.timing import (
    DEFAULT_TZ,
    ScheduleError,
    describe,
    get_tz,
    next_fire,
    to_local,
    upcoming,
    utcnow,
)
from db.models.agent_job import AgentJob
from db.models.scheduled_task import (
    ACTION_KINDS,
    MISFIRE_POLICIES,
    OVERLAP_POLICIES,
    SCHEDULE_KINDS,
    SESSION_MODES,
    ScheduledTask,
)

logger = logging.getLogger(__name__)

NOTIFY_POLICIES = ("chat", "notch", "both", "silent")

#: Ação ``job`` → a tool que já sabe criar aquele job (com toda a validação e,
#: no caso do research, a ``research_task`` junto). Reusar a tool em vez de
#: montar o AgentJob à mão é o que impede a agenda de criar jobs meio-criados.
JOB_ACTION_TOOLS = {
    "research": "deep_research",
    "sandbox_task": "neo_bash_background",
    "browser_local": "browser_local_task",
    "browser": "browser_task",
}

#: Teto padrão de um turno agendado. Um prompt em laço de tool é a forma mais
#: fácil de queimar carteira de LLM dormindo (§6.6).
DEFAULT_TOOL_POLICY = {
    "client_side": False,
    "max_llm_calls": 12,
    "max_seconds": 600,
}


class ScheduleValidationError(ValueError):
    """Payload inválido, com mensagem pronta para o usuário."""


def _str(value: Any, default: str = "") -> str:
    return str(value if value is not None else default).strip()


def _dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _validate_window(window: dict) -> dict:
    if not window:
        return {}
    dias = window.get("dias")
    if dias is not None:
        try:
            dias = sorted({int(d) for d in dias})
        except (TypeError, ValueError) as exc:
            raise ScheduleValidationError("janela.dias deve ser uma lista de 1..7") from exc
        if any(d < 1 or d > 7 for d in dias):
            raise ScheduleValidationError("janela.dias vai de 1 (segunda) a 7 (domingo)")
        window["dias"] = dias
    for campo in ("hora_inicio", "hora_fim"):
        valor = window.get(campo)
        if valor and not _HHMM(valor):
            raise ScheduleValidationError(f"janela.{campo} deve estar no formato HH:MM")
    return window


def _HHMM(raw: Any) -> bool:
    import re

    return bool(re.match(r"^\s*\d{1,2}:\d{2}\s*$", str(raw)))


def _validate_action(action_kind: str, action: dict) -> dict:
    if action_kind == "prompt":
        if not _str(action.get("texto")):
            raise ScheduleValidationError(
                "Ação 'prompt' precisa do campo 'texto' — o que o Neo deve fazer na hora."
            )
        action["texto"] = _str(action["texto"])
    elif action_kind == "tool":
        if not _str(action.get("tool")):
            raise ScheduleValidationError("Ação 'tool' precisa do nome da tool em 'tool'.")
        action["tool"] = _str(action["tool"])
        action["arguments"] = _dict(action.get("arguments"))
    elif action_kind == "notify":
        if not _str(action.get("titulo")) and not _str(action.get("corpo")):
            raise ScheduleValidationError("Ação 'notify' precisa de 'titulo' ou 'corpo'.")
    elif action_kind == "job":
        kind = _str(action.get("kind"))
        if kind not in JOB_ACTION_TOOLS:
            raise ScheduleValidationError(
                f"Ação 'job' aceita {', '.join(JOB_ACTION_TOOLS)} — recebi {kind!r}."
            )
        action["spec"] = _dict(action.get("spec"))
    else:  # pragma: no cover — já barrado antes
        raise ScheduleValidationError(f"Ação desconhecida: {action_kind!r}")
    return action


def normalize_payload(payload: dict, *, user_id: UUID | None, now: datetime | None = None) -> dict:
    """Payload cru (painel ou tool) → campos prontos do :class:`ScheduledTask`.

    Aceita ``quando`` em português no lugar de ``schedule_kind``/``schedule_expr``
    — é o mesmo caminho que a tool ``schedule_create`` usa.
    """
    agora = now or utcnow()
    nome = _str(payload.get("name") or payload.get("nome"))
    if not nome:
        raise ScheduleValidationError("A tarefa precisa de um nome.")

    tz = _str(payload.get("timezone") or payload.get("fuso")) or DEFAULT_TZ
    try:
        get_tz(tz)
    except Exception as exc:  # noqa: BLE001
        raise ScheduleValidationError(f"Fuso inválido: {tz!r}") from exc

    schedule_kind = _str(payload.get("schedule_kind"))
    schedule_expr = _str(payload.get("schedule_expr"))
    quando = _str(payload.get("quando"))
    if not schedule_kind and quando:
        from core.scheduler.nlp import parse_quando

        parsed = parse_quando(quando, tz=tz, now=agora)
        schedule_kind = parsed["schedule_kind"]
        schedule_expr = parsed["schedule_expr"]
    if schedule_kind not in SCHEDULE_KINDS:
        raise ScheduleValidationError(
            f"schedule_kind deve ser um de {', '.join(SCHEDULE_KINDS)} (ou mande 'quando' em português)."
        )
    if not schedule_expr:
        raise ScheduleValidationError("schedule_expr vazio.")

    action_kind = _str(payload.get("action_kind") or payload.get("acao_tipo"))
    if action_kind not in ACTION_KINDS:
        raise ScheduleValidationError(
            f"action_kind deve ser um de {', '.join(ACTION_KINDS)}."
        )
    action = _validate_action(action_kind, _dict(payload.get("action_json") or payload.get("acao")))

    window = _validate_window(_dict(payload.get("window_json") or payload.get("janela")))
    quiet = _dict(payload.get("quiet_hours_json") or payload.get("silencio"))
    condition = cond.validate(_dict(payload.get("condition_json") or payload.get("condicao")))

    notify_policy = _str(payload.get("notify_policy"), "chat") or "chat"
    if notify_policy not in NOTIFY_POLICIES:
        raise ScheduleValidationError(f"notify_policy deve ser um de {', '.join(NOTIFY_POLICIES)}.")

    session_mode = _str(payload.get("session_mode"), "nova") or "nova"
    if session_mode not in SESSION_MODES:
        raise ScheduleValidationError(f"session_mode deve ser um de {', '.join(SESSION_MODES)}.")

    misfire = _str(payload.get("misfire_policy"), "skip") or "skip"
    if misfire not in MISFIRE_POLICIES:
        raise ScheduleValidationError(f"misfire_policy deve ser um de {', '.join(MISFIRE_POLICIES)}.")

    overlap = _str(payload.get("overlap_policy"), "skip") or "skip"
    if overlap not in OVERLAP_POLICIES:
        raise ScheduleValidationError(f"overlap_policy deve ser um de {', '.join(OVERLAP_POLICIES)}.")

    tool_policy = {**DEFAULT_TOOL_POLICY, **_dict(payload.get("tool_policy_json"))}

    fields: dict[str, Any] = {
        "user_id": user_id,
        "name": nome[:160],
        "description": _str(payload.get("description") or payload.get("descricao"))[:4000],
        "enabled": bool(payload.get("enabled", True)),
        "schedule_kind": schedule_kind,
        "schedule_expr": schedule_expr[:160],
        "timezone": tz,
        "window_json": window,
        "jitter_seconds": max(0, int(payload.get("jitter_seconds") or 0)),
        "starts_at": _parse_dt(payload.get("starts_at")),
        "ends_at": _parse_dt(payload.get("ends_at")),
        "max_runs": _parse_int(payload.get("max_runs")),
        "misfire_policy": misfire,
        "grace_seconds": max(0, int(payload.get("grace_seconds") or 600)),
        "overlap_policy": overlap,
        "action_kind": action_kind,
        "action_json": action,
        "tool_policy_json": tool_policy,
        "session_mode": session_mode,
        "condition_json": condition,
        "only_if_changed": bool(payload.get("only_if_changed", False)),
        "notify_policy": notify_policy,
        "quiet_hours_json": quiet,
    }

    # Valida o horário DE VERDADE calculando o próximo disparo agora.
    proximo = next_fire(
        schedule_kind=schedule_kind, schedule_expr=schedule_expr, tz=tz,
        after=agora, window=window, starts_at=fields["starts_at"], ends_at=fields["ends_at"],
    )
    if proximo is None:
        raise ScheduleValidationError(
            "Esse agendamento nunca dispara (data no passado, janela impossível ou "
            "cron insatisfazível). Revise o horário."
        )
    fields["next_run_at"] = proximo
    return fields


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScheduleValidationError(f"Data inválida: {value!r}") from exc
    if parsed.tzinfo:
        from datetime import timezone as _tz

        parsed = parsed.astimezone(_tz.utc).replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def _parse_int(value: Any) -> int | None:
    if value in (None, "", 0):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ScheduleService:
    """CRUD + materialização. Não executa nada: quem executa é o JobManager."""

    def __init__(self, *, db: Any, job_manager: Any = None) -> None:
        self.db = db
        self.job_manager = job_manager

    # ── CRUD ──────────────────────────────────────────────────────────

    async def create(self, payload: dict, *, user_id: UUID | None) -> ScheduledTask:
        fields = normalize_payload(payload, user_id=user_id)
        task = ScheduledTask(**fields)
        return await self.db.create_scheduled_task(task)

    async def update(self, task_id: UUID, payload: dict) -> ScheduledTask | None:
        task = await self.db.get_scheduled_task(task_id)
        if task is None:
            return None
        merged = {**self.to_dict(task, with_preview=False), **payload}
        # `quando` só vale se veio agora; senão o kind/expr atuais mandam.
        if "quando" in payload and payload.get("quando"):
            merged.pop("schedule_kind", None)
            merged.pop("schedule_expr", None)
        fields = normalize_payload(merged, user_id=task.user_id)
        # Editar uma tarefa pausada por erro é o gesto de "consertei": zera o
        # disjuntor, senão ela continuaria desligada sem motivo visível.
        fields["consecutive_errors"] = 0
        fields["paused_reason"] = ""
        return await self.db.update_scheduled_task(task_id, **fields)

    async def set_enabled(self, task_id: UUID, enabled: bool) -> ScheduledTask | None:
        task = await self.db.get_scheduled_task(task_id)
        if task is None:
            return None
        fields: dict[str, Any] = {"enabled": enabled, "paused_reason": "" if enabled else "pausada pelo usuário"}
        if enabled:
            fields["consecutive_errors"] = 0
            fields["next_run_at"] = self.compute_next(task)
        return await self.db.update_scheduled_task(task_id, **fields)

    async def delete(self, task_id: UUID) -> bool:
        return await self.db.delete_scheduled_task(task_id)

    async def get(self, task_id: UUID) -> ScheduledTask | None:
        return await self.db.get_scheduled_task(task_id)

    async def list(self, user_id: UUID | None, *, enabled_only: bool = False) -> list[ScheduledTask]:
        return await self.db.list_scheduled_tasks(user_id, enabled_only=enabled_only)

    # ── Horário ───────────────────────────────────────────────────────

    def compute_next(self, task: ScheduledTask, *, after: datetime | None = None) -> datetime | None:
        try:
            return next_fire(
                schedule_kind=task.schedule_kind,
                schedule_expr=task.schedule_expr,
                tz=task.timezone,
                after=after or utcnow(),
                window=task.window_json or {},
                starts_at=task.starts_at,
                ends_at=task.ends_at,
            )
        except ScheduleError as exc:
            logger.warning("[agenda] tarefa %s tem horário inválido: %s", task.id, exc)
            return None

    # ── Disparo ───────────────────────────────────────────────────────

    async def materialize(
        self,
        task: ScheduledTask,
        *,
        fire_at: datetime | None = None,
        reason: str = "agenda",
        deliver: bool = True,
    ) -> AgentJob:
        """Cria o ``agent_job`` deste disparo. Não executa — só enfileira.

        O job nasce ``queued`` com ``next_run_at`` nulo: o JobManager pega no
        próximo tick (ou no ``poke`` logo abaixo) e é ele quem paga o timeout, a
        concorrência e a entrega. A agenda não executa nada por conta própria.
        """
        momento = fire_at or utcnow()
        session_id = await self._resolve_session(task)
        job = AgentJob(
            user_id=task.user_id,
            session_id=session_id,
            kind=f"scheduled_{task.action_kind}",
            title=task.name[:256],
            spec_json={
                "scheduled_task_id": str(task.id),
                "action_kind": task.action_kind,
                "action": task.action_json or {},
                "tool_policy": task.tool_policy_json or {},
                "session_mode": task.session_mode,
                "only_if_changed": bool(task.only_if_changed),
                "last_fingerprint": task.last_fingerprint,
                "timezone": task.timezone,
                "quiet_hours": task.quiet_hours_json or {},
                "fire_at": momento.isoformat(),
                "reason": reason,
            },
            status="queued",
            # `silent` quando o disparo é um teste: a tela mostra o resultado,
            # o usuário não leva notificação de algo que ele está olhando.
            notify_policy=task.notify_policy if deliver else "silent",
            extra_metadata={
                "scheduled_task_id": str(task.id),
                "fire_at": momento.isoformat(),
                "run_seq": (task.run_count or 0) + 1,
                "reason": reason,
            },
        )
        job = await self.db.create_agent_job(job)
        if self.job_manager is not None:
            try:
                self.job_manager.poke()
            except Exception:  # noqa: BLE001
                pass
        return job

    async def _resolve_session(self, task: ScheduledTask) -> UUID | None:
        """Qual conversa recebe este disparo (§6.1).

        O padrão NÃO é a sessão ativa: injetar um turno agendado na conversa
        corrente dispara rotação de tópico e envenena o embedding de
        similaridade que decide quando o assunto mudou.
        """
        if task.action_kind != "prompt":
            # Ação sem turno entrega na conversa "própria" da tarefa, se houver.
            return task.session_id
        if task.session_mode == "propria":
            return task.session_id  # None na 1ª vez; o runner grava a que nasceu
        if task.session_mode == "ativa":
            try:
                sessao = await self.db.get_active_session(task.user_id)
                return sessao.id if sessao else None
            except Exception:  # noqa: BLE001
                return None
        return None  # "nova"

    # ── Serialização (painel + tools) ─────────────────────────────────

    def to_dict(self, task: ScheduledTask, *, with_preview: bool = True) -> dict:
        data = {
            "id": str(task.id),
            "user_id": str(task.user_id) if task.user_id else None,
            "name": task.name,
            "description": task.description,
            "enabled": task.enabled,
            "schedule_kind": task.schedule_kind,
            "schedule_expr": task.schedule_expr,
            "timezone": task.timezone,
            "window_json": task.window_json or {},
            "jitter_seconds": task.jitter_seconds,
            "starts_at": _iso(task.starts_at),
            "ends_at": _iso(task.ends_at),
            "max_runs": task.max_runs,
            "misfire_policy": task.misfire_policy,
            "grace_seconds": task.grace_seconds,
            "overlap_policy": task.overlap_policy,
            "action_kind": task.action_kind,
            "action_json": task.action_json or {},
            "tool_policy_json": task.tool_policy_json or {},
            "session_mode": task.session_mode,
            "session_id": str(task.session_id) if task.session_id else None,
            "condition_json": task.condition_json or {},
            "only_if_changed": task.only_if_changed,
            "notify_policy": task.notify_policy,
            "quiet_hours_json": task.quiet_hours_json or {},
            "next_run_at": _iso(task.next_run_at),
            "last_run_at": _iso(task.last_run_at),
            "last_job_id": str(task.last_job_id) if task.last_job_id else None,
            "last_status": task.last_status,
            "run_count": task.run_count,
            "skipped_count": task.skipped_count,
            "consecutive_errors": task.consecutive_errors,
            "paused_reason": task.paused_reason,
            "created_at": _iso(task.created_at),
            "updated_at": _iso(task.updated_at),
        }
        if with_preview:
            data["descricao_horario"] = describe(
                task.schedule_kind, task.schedule_expr, task.timezone, task.window_json or {}
            )
            data["proximos"] = [
                to_local(m, task.timezone).strftime("%d/%m/%Y %H:%M")
                for m in upcoming(
                    schedule_kind=task.schedule_kind,
                    schedule_expr=task.schedule_expr,
                    tz=task.timezone,
                    window=task.window_json or {},
                    starts_at=task.starts_at,
                    ends_at=task.ends_at,
                    count=5,
                )
            ]
            data["next_run_local"] = (
                to_local(task.next_run_at, task.timezone).strftime("%d/%m/%Y %H:%M")
                if task.next_run_at else None
            )
        return data


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
