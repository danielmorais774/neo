"""Agenda do Neo — cron de tarefas, prompts e tools disparados sozinhos.

Ver ``docs/scheduler-plan.md``. O eixo que faltava era o TEMPO: até aqui o Neo
só agia quando alguém falava com ele.

Peças, na ordem em que um disparo acontece:
  timing    → quando é o próximo (cron/intervalo/uma vez, fuso, janela)
  tick      → acha o que venceu, avalia a guarda e materializa o agent_job
  conditions→ a guarda que evita gastar turno à toa
  runners   → executa o disparo (aviso, tool, job ou turno cognitivo)
  service   → CRUD/validação compartilhados pelo painel e pelas tools
  tools     → o cérebro criando e mexendo nos próprios horários
"""

from core.scheduler.conditions import ConditionError, fingerprint
from core.scheduler.executor import ScheduleToolExecutor
from core.scheduler.runners import build_scheduled_runner
from core.scheduler.service import ScheduleService, ScheduleValidationError, normalize_payload
from core.scheduler.tick import SchedulerTick
from core.scheduler.timing import ScheduleError, describe, next_fire, upcoming
from core.scheduler.tools import SCHEDULE_TOOL_NAMES, SCHEDULE_TOOLS

#: Kinds de ``agent_job`` que a agenda cria. O JobManager roteia estes para o
#: runner da agenda; qualquer outro continua indo para os runners de sempre.
SCHEDULED_JOB_KINDS = frozenset(
    {"scheduled_prompt", "scheduled_tool", "scheduled_notify", "scheduled_job"}
)

__all__ = [
    "ConditionError",
    "SCHEDULED_JOB_KINDS",
    "SCHEDULE_TOOLS",
    "SCHEDULE_TOOL_NAMES",
    "ScheduleError",
    "ScheduleService",
    "ScheduleToolExecutor",
    "ScheduleValidationError",
    "SchedulerTick",
    "build_scheduled_runner",
    "describe",
    "fingerprint",
    "next_fire",
    "normalize_payload",
    "upcoming",
]
