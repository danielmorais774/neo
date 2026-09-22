"""SchedulerTick — o relógio da agenda (docs/scheduler-plan.md §3.2).

Este laço faz **só** quatro coisas: achar o que venceu, decidir se ainda vale
disparar, avaliar a guarda e materializar o ``agent_job``. Quem executa é o
JobManager; quem entrega é o ProactiveDispatcher. Nenhuma concorrência nova,
nenhum caminho de entrega novo — a agenda é um produtor de jobs.

**Por que isto é restart-safe por construção.** O neo-core roda com ``--reload``:
salvar arquivo é deploy, e todo save derruba o processo. Então nada de estado em
memória: o ``next_run_at`` no banco é a única fonte da verdade, o claim é um
UPDATE atômico, e o disparo perdido durante o restart cai na política de misfire
(§7.3) em vez de virar catorze mensagens de uma vez quando o processo volta.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from core.scheduler import conditions as cond
from core.scheduler.timing import apply_jitter, to_local, utcnow

logger = logging.getLogger(__name__)

#: Teto de disparos atrasados que um ``run_all`` recupera por volta. Sem teto,
#: um fim de semana fora do ar viraria uma avalanche de jobs no boot.
MAX_CATCHUP_PER_TASK = 5

#: Quantas tarefas o tick reivindica por volta.
CLAIM_LIMIT = 25


class SchedulerTick:
    """Laço que transforma regra de agenda em disparo."""

    def __init__(
        self,
        *,
        db: Any,
        service: Any,
        ai_service: Any = None,
        tick_interval: int = 30,
        enabled: bool = True,
    ) -> None:
        self.db = db
        self.service = service
        self.ai_service = ai_service
        self.tick_interval = max(5, int(tick_interval))
        self.enabled = enabled
        #: Kill switch global (botão "pausar tudo" do painel). Em memória de
        #: propósito: é um freio de emergência, e o restart deve soltá-lo.
        self.paused = False
        self._task: asyncio.Task | None = None
        self._poke = asyncio.Event()
        self._stopped = False
        self._running: dict[str, asyncio.Task] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.enabled:
            logger.info("[agenda] desligada (NEO_SCHEDULER_ENABLED=false)")
            return
        atrasadas = await self._log_overdue()
        self._task = asyncio.create_task(self._loop(), name="scheduler-tick")
        logger.info(
            "[agenda] SchedulerTick iniciado (tick=%ds, %d tarefa(s) vencida(s) no boot)",
            self.tick_interval, atrasadas,
        )

    async def stop(self) -> None:
        self._stopped = True
        self._poke.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        for task in list(self._running.values()):
            task.cancel()

    def poke(self) -> None:
        self._poke.set()

    async def _log_overdue(self) -> int:
        """Conta (sem disparar) o que venceu enquanto o processo estava fora."""
        try:
            tarefas = await self.db.list_scheduled_tasks(None, enabled_only=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[agenda] não consegui ler as tarefas no boot: %s", exc)
            return 0
        agora = utcnow()
        return sum(1 for t in tarefas if t.next_run_at and t.next_run_at <= agora)

    async def _loop(self) -> None:
        while not self._stopped:
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[agenda] erro no tick: %s", exc)
            try:
                await asyncio.wait_for(self._poke.wait(), timeout=self.tick_interval)
            except asyncio.TimeoutError:
                pass
            finally:
                self._poke.clear()

    async def _tick(self) -> None:
        if self.paused:
            return
        tarefas = await self.db.claim_due_scheduled_tasks(limit=CLAIM_LIMIT)
        for tarefa in tarefas:
            # Um disparo lento (condição http, por exemplo) não pode segurar o
            # relógio das outras tarefas.
            chave = str(tarefa.id)
            if chave in self._running:
                continue
            task = asyncio.create_task(self._fire(tarefa), name=f"agenda-{chave[:8]}")
            self._running[chave] = task
            task.add_done_callback(lambda _t, k=chave: self._running.pop(k, None))

    # ── Um disparo ────────────────────────────────────────────────────

    async def _fire(self, tarefa: Any) -> None:
        venceu_em: datetime = tarefa.next_run_at
        agora = utcnow()
        atraso = (agora - venceu_em).total_seconds()

        try:
            quantos = self._quantos_disparos(tarefa, atraso, venceu_em, agora)
            if quantos == 0:
                logger.info(
                    "[agenda] '%s' venceu há %.0fs e foi pulada (misfire=%s)",
                    tarefa.name, atraso, tarefa.misfire_policy,
                )
                await self._reagendar(tarefa, agora, skipped=True)
                return

            if await self._sobrepoe(tarefa):
                logger.info("[agenda] '%s': disparo anterior ainda rodando — pulei", tarefa.name)
                await self._reagendar(tarefa, agora, skipped=True)
                return

            passou, explicacao, _payload = await self._condicao(tarefa)
            if not passou:
                logger.info("[agenda] '%s' barrada pela condição: %s", tarefa.name, explicacao)
                await self._reagendar(tarefa, agora, skipped=True)
                return

            for _ in range(quantos):
                job = await self.service.materialize(tarefa, fire_at=agora, reason="agenda")
                logger.info(
                    "[agenda] '%s' disparou → job %s (%s)", tarefa.name, job.id, tarefa.action_kind
                )
            await self._reagendar(tarefa, agora, skipped=False)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[agenda] falha ao disparar '%s'", tarefa.name)
            # A tarefa não pode ficar com a sentinela de claim: sem reagendar,
            # ela nunca mais dispararia e ninguém saberia por quê.
            await self._reagendar(tarefa, agora, skipped=True, erro=str(exc))

    def _quantos_disparos(
        self, tarefa: Any, atraso: float, venceu_em: datetime, agora: datetime
    ) -> int:
        """Quantas vezes disparar, dada a política de disparo perdido (§7.3).

        ``run_all`` é o único que conta ocorrências; e conta com teto, porque um
        fim de semana fora do ar não pode virar avalanche no boot.
        """
        if atraso <= max(0, int(tarefa.grace_seconds or 0)):
            return 1
        politica = tarefa.misfire_policy or "skip"
        if politica == "skip":
            return 0
        if politica == "run_once":
            return 1

        from core.scheduler.timing import ScheduleError, parse_interval, upcoming

        try:
            if tarefa.schedule_kind == "interval":
                passo = parse_interval(tarefa.schedule_expr).total_seconds()
                return max(1, min(int(atraso // passo), MAX_CATCHUP_PER_TASK))
            perdidos = upcoming(
                schedule_kind=tarefa.schedule_kind,
                schedule_expr=tarefa.schedule_expr,
                tz=tarefa.timezone,
                window=tarefa.window_json or {},
                starts_at=tarefa.starts_at,
                ends_at=tarefa.ends_at,
                count=MAX_CATCHUP_PER_TASK,
                after=venceu_em,
            )
            # +1: a própria ocorrência que venceu e não rodou.
            return max(1, min(1 + sum(1 for m in perdidos if m <= agora), MAX_CATCHUP_PER_TASK))
        except ScheduleError:
            return 1

    async def _sobrepoe(self, tarefa: Any) -> bool:
        """O disparo anterior ainda está de pé? (§7.4)"""
        politica = tarefa.overlap_policy or "skip"
        if politica == "queue" or not tarefa.last_job_id:
            return False
        try:
            anterior = await self.db.get_agent_job(tarefa.last_job_id)
        except Exception:  # noqa: BLE001
            return False
        if anterior is None or anterior.status not in ("queued", "running"):
            return False
        if politica == "cancel_previous":
            try:
                await self.db.update_agent_job(
                    tarefa.last_job_id,
                    status="cancelled",
                    finished_at=datetime.utcnow(),
                    result_summary="Cancelado: o disparo seguinte da mesma tarefa chegou.",
                )
            except Exception:  # noqa: BLE001
                pass
            return False
        return True

    async def _condicao(self, tarefa: Any) -> tuple[bool, str, Any]:
        condicao = tarefa.condition_json or {}
        if not condicao:
            return True, "sem condição", None

        async def run_tool(nome: str, args: dict) -> Any:
            if self.ai_service is None:
                raise RuntimeError("AIService indisponível")
            from shared import ToolCall

            executor = self.ai_service.build_request_executor(
                user_id=tarefa.user_id,
                session_id=tarefa.session_id,
                metadata={"source": "schedule", "unattended": True},
                user_message=tarefa.name,
            )
            resultado = await asyncio.wait_for(
                executor.execute_tool_call(ToolCall(name=nome, arguments=dict(args or {}))),
                timeout=60,
            )
            if getattr(resultado, "error", None):
                raise RuntimeError(resultado.error)
            return getattr(resultado, "result", resultado)

        async def devices_online() -> list[str]:
            try:
                aparelhos = await self.db.get_connected_devices(tarefa.user_id)
            except Exception:  # noqa: BLE001
                return []
            return [getattr(d, "device_id", "") for d in aparelhos]

        async def http_get(url: str, *, method: str = "GET", headers: dict | None = None,
                           json_body: Any = None) -> Any:
            import httpx

            async with httpx.AsyncClient(timeout=20) as client:
                resposta = await client.request(
                    method.upper(), url, headers=headers or {}, json=json_body
                )
                resposta.raise_for_status()
                try:
                    return resposta.json()
                except Exception:  # noqa: BLE001
                    return resposta.text

        return await cond.evaluate(
            condicao, run_tool=run_tool, devices_online=devices_online, http_get=http_get
        )

    async def _reagendar(
        self, tarefa: Any, agora: datetime, *, skipped: bool, erro: str | None = None
    ) -> None:
        """Recoloca a tarefa no relógio (ou a desliga, se acabou)."""
        campos: dict[str, Any] = {}
        if skipped:
            campos["skipped_count"] = (tarefa.skipped_count or 0) + 1
        if erro:
            campos["last_status"] = "erro_no_agendador"
            campos["paused_reason"] = f"falha no agendador: {erro[:200]}"

        if tarefa.schedule_kind == "once":
            campos.update(
                enabled=False,
                next_run_at=None,
                paused_reason=campos.get("paused_reason") or "disparo único concluído",
            )
            await self.db.update_scheduled_task(tarefa.id, **campos)
            return

        proximo = self.service.compute_next(tarefa, after=agora)
        if proximo is None:
            campos.update(
                enabled=False,
                next_run_at=None,
                paused_reason=campos.get("paused_reason") or "sem próximos disparos (fim da validade)",
            )
        else:
            campos["next_run_at"] = apply_jitter(proximo, tarefa.jitter_seconds or 0)
        await self.db.update_scheduled_task(tarefa.id, **campos)
        if proximo is not None:
            logger.debug(
                "[agenda] '%s' reagendada para %s (%s)",
                tarefa.name, proximo, to_local(proximo, tarefa.timezone).strftime("%d/%m %H:%M"),
            )
