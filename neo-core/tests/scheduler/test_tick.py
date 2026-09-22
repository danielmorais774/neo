"""O relógio: quem dispara, quem NÃO dispara, e o que acontece depois.

As decisões testadas aqui são as que separam uma agenda utilizável de um
gerador de ruído — e a de misfire é a mais importante deste repo, porque o
neo-core reinicia a cada save e vai perder disparo toda semana.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from core.scheduler import ScheduleService, SchedulerTick
from core.scheduler.timing import utcnow
from db.models.agent_job import AgentJob
from db.models.scheduled_task import ScheduledTask


class FakeDB:
    """Banco em memória com só o que a agenda usa."""

    def __init__(self) -> None:
        self.tasks: dict[uuid.UUID, ScheduledTask] = {}
        self.jobs: dict[uuid.UUID, AgentJob] = {}

    # ScheduledTask
    async def get_scheduled_task(self, task_id):
        return self.tasks.get(task_id)

    async def list_scheduled_tasks(self, user_id=None, *, enabled_only=False, limit=200):
        return [t for t in self.tasks.values() if not enabled_only or t.enabled]

    async def update_scheduled_task(self, task_id, **fields):
        task = self.tasks[task_id]
        for key, value in fields.items():
            setattr(task, key, value)
        return task

    async def claim_due_scheduled_tasks(self, limit=20):
        """Reproduz o contrato do banco real, inclusive a parte sutil.

        O UPDATE grava a sentinela (para ninguém reivindicar de novo), mas o
        objeto devolvido carrega o ``next_run_at`` ORIGINAL — é dele que sai a
        decisão de misfire. Um fake que devolvesse a sentinela faria todo
        disparo parecer adiantado, e o teste de atraso passaria por engano.
        """
        agora = utcnow()
        vencidas = [
            t for t in self.tasks.values()
            if t.enabled and t.next_run_at and t.next_run_at <= agora
        ][:limit]
        devolvidas = []
        for t in vencidas:
            venceu_em = t.next_run_at
            t.next_run_at = agora + timedelta(days=3650)  # sentinela persistida
            copia = t.model_copy()
            copia.next_run_at = venceu_em
            devolvidas.append(copia)
        return devolvidas

    # AgentJob
    async def create_agent_job(self, job):
        self.jobs[job.id] = job
        return job

    async def get_agent_job(self, job_id):
        return self.jobs.get(job_id)

    async def update_agent_job(self, job_id, **fields):
        job = self.jobs[job_id]
        for key, value in fields.items():
            setattr(job, key, value)
        return job

    async def get_active_session(self, user_id=None):
        return None


def make_task(**over) -> ScheduledTask:
    base = dict(
        user_id=uuid.uuid4(),
        name="Vigia",
        schedule_kind="interval",
        schedule_expr="15m",
        timezone="America/Fortaleza",
        action_kind="notify",
        action_json={"titulo": "oi", "corpo": "tudo certo"},
        notify_policy="chat",
        enabled=True,
        grace_seconds=600,
        misfire_policy="skip",
        overlap_policy="skip",
    )
    base.update(over)
    return ScheduledTask(**base)


@pytest.fixture
def ambiente():
    db = FakeDB()
    service = ScheduleService(db=db)
    tick = SchedulerTick(db=db, service=service, tick_interval=30)
    return db, service, tick


async def _rodar(db, tick, task) -> int:
    db.tasks[task.id] = task
    claimed = await db.claim_due_scheduled_tasks()
    for t in claimed:
        await tick._fire(t)
    return len(db.jobs)


def estado(db, task) -> ScheduledTask:
    """A linha como ficou no banco (o tick recebe uma cópia, como em produção)."""
    return db.tasks[task.id]


@pytest.mark.asyncio
async def test_dentro_da_carencia_dispara(ambiente) -> None:
    db, _, tick = ambiente
    task = make_task(next_run_at=utcnow() - timedelta(seconds=30))
    assert await _rodar(db, tick, task) == 1
    assert estado(db, task).next_run_at > utcnow()  # reagendada


@pytest.mark.asyncio
async def test_atrasado_com_skip_nao_dispara(ambiente) -> None:
    """O caso do restart: perdeu por muito, perdeu — e reagenda."""
    db, _, tick = ambiente
    task = make_task(next_run_at=utcnow() - timedelta(hours=3), misfire_policy="skip")
    assert await _rodar(db, tick, task) == 0
    assert estado(db, task).skipped_count == 1
    assert estado(db, task).next_run_at > utcnow()


@pytest.mark.asyncio
async def test_atrasado_com_run_once_dispara_uma_vez(ambiente) -> None:
    db, _, tick = ambiente
    task = make_task(next_run_at=utcnow() - timedelta(hours=3), misfire_policy="run_once")
    assert await _rodar(db, tick, task) == 1


@pytest.mark.asyncio
async def test_run_all_tem_teto(ambiente) -> None:
    """Um fim de semana fora do ar não pode virar avalanche."""
    db, _, tick = ambiente
    task = make_task(next_run_at=utcnow() - timedelta(days=2), misfire_policy="run_all")
    total = await _rodar(db, tick, task)
    assert 1 <= total <= 5


@pytest.mark.asyncio
async def test_condicao_falsa_nao_gasta_disparo(ambiente) -> None:
    db, _, tick = ambiente
    task = make_task(
        next_run_at=utcnow() - timedelta(seconds=10),
        condition_json={"tipo": "device", "device_id": "inexistente"},
    )

    async def sem_aparelhos(user_id=None):
        return []

    db.get_connected_devices = sem_aparelhos
    assert await _rodar(db, tick, task) == 0
    assert estado(db, task).skipped_count == 1
    assert estado(db, task).consecutive_errors == 0  # guarda não é erro


@pytest.mark.asyncio
async def test_sobreposicao_pula_quando_o_anterior_ainda_roda(ambiente) -> None:
    db, service, tick = ambiente
    task = make_task(next_run_at=utcnow() - timedelta(seconds=10), overlap_policy="skip")
    db.tasks[task.id] = task
    anterior = await service.materialize(task)
    anterior.status = "running"
    task.last_job_id = anterior.id
    claimed = await db.claim_due_scheduled_tasks()
    for t in claimed:
        await tick._fire(t)
    assert len(db.jobs) == 1  # só o anterior


@pytest.mark.asyncio
async def test_uma_vez_se_desliga_depois_de_disparar(ambiente) -> None:
    db, _, tick = ambiente
    futuro = (utcnow() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
    task = make_task(
        schedule_kind="once", schedule_expr=futuro, next_run_at=utcnow() - timedelta(seconds=20)
    )
    assert await _rodar(db, tick, task) == 1
    assert estado(db, task).enabled is False
    assert estado(db, task).next_run_at is None


@pytest.mark.asyncio
async def test_disparo_carrega_a_identidade_da_tarefa(ambiente) -> None:
    """Sem isto o histórico da tela não consegue ligar job → tarefa."""
    db, _, tick = ambiente
    task = make_task(next_run_at=utcnow() - timedelta(seconds=10))
    await _rodar(db, tick, task)
    job = next(iter(db.jobs.values()))
    assert job.kind == "scheduled_notify"
    assert job.extra_metadata["scheduled_task_id"] == str(task.id)
    assert job.spec_json["action"]["corpo"] == "tudo certo"


@pytest.mark.asyncio
async def test_kill_switch_para_tudo(ambiente) -> None:
    db, _, tick = ambiente
    tick.paused = True
    task = make_task(next_run_at=utcnow() - timedelta(seconds=10))
    db.tasks[task.id] = task
    await tick._tick()
    assert not db.jobs
