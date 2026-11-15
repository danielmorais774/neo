"""JobManager — reivindica e executa jobs em background sem bloquear o chat.

Um único loop (tick) roda no mesmo processo do neo-core (como o ``_offline_checker``
dos devices). A cada tick ele reivindica jobs prontos da tabela ``agent_job`` (claim
atômico no DB, à prova de dupla execução) e dispara um worker por job, limitado por
um semáforo. Ao terminar, marca o resultado e chama o :class:`ProactiveDispatcher`.

Restart-safe: no boot, todo job preso em ``running`` é reconciliado para ``failed``
(uma instância só roda), e jobs terminais ainda não entregues são reenviados. Nada
em memória é fonte da verdade — o DB é. Ver ``docs/workbench-background-jobs-plan.md``.
"""

from __future__ import annotations

import asyncio
import time
import logging
import re
import shlex
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from db.models.agent_job import AgentJob
from core.workbench.sandbox import WorkbenchSandbox, WorkbenchUnavailableError

if TYPE_CHECKING:
    from db.async_client import AsyncPostgresClient
    from core.jobs.dispatcher import ProactiveDispatcher

logger = logging.getLogger(__name__)

#: De quanto em quanto tempo um comando adotado carimba `heartbeat_at` — é o que
#: a tela de tarefas usa para mostrar que ele segue vivo.
_ADOPTED_HEARTBEAT_S = 30

#: O log de um comando adotado é escrito de uma vez, em base64 num argumento de
#: shell; acima disto o `write_file` bateria no limite de tamanho de argumento.
_ADOPTED_LOG_MAX_CHARS = 90_000

#: O pedido é um ARQUIVO? (ver `_itens_em_arquivo`)
_PEDE_ARQUIVO = re.compile(
    r"\barquivo\b|\.txt\b|\btxt\b|\bcsv\b|\bplanilha\b|\bmarkdown\b|\.md\b", re.IGNORECASE
)
#: Rodapé que o runner do navegador redige na entrega; o resto é coleta. Mesma
#: lista do `_com_itens` do dispatcher.
_RODAPE_DA_ENTREGA = ("-", "•", "⏸", "⬇", "⏳", "📸", "(")


def _nome_de_arquivo(titulo: str) -> str:
    """O título do job como nome de arquivo: sem acento, minúsculo, com hífen."""
    import unicodedata

    base = unicodedata.normalize("NFKD", titulo or "").encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return base[:40].strip("-") or "coleta"


class JobManager:
    """Fila + pool de workers para jobs em background do Neo."""

    def __init__(
        self,
        *,
        db: "AsyncPostgresClient",
        sandbox: WorkbenchSandbox,
        max_concurrent: int = 3,
        tick_interval: int = 3,
        default_timeout: int = 1800,
        workspace_root: str = "/workspace",
        dispatcher: "ProactiveDispatcher | None" = None,
        research_runner: "Callable[[AgentJob], Awaitable[dict]] | None" = None,
        browser_runner: "Callable[[AgentJob], Awaitable[dict]] | None" = None,
        browser_local_runner: "Callable[[AgentJob], Awaitable[dict]] | None" = None,
        download_watch_runner: "Callable[[AgentJob], Awaitable[dict]] | None" = None,
        scheduled_runner: "Callable[[AgentJob], Awaitable[dict]] | None" = None,
        deep_read_runner: "Callable[[AgentJob], Awaitable[dict]] | None" = None,
        claude_runner: "Callable[[AgentJob], Awaitable[dict]] | None" = None,
        claude_sessao_runner: "Callable[[AgentJob], Awaitable[dict]] | None" = None,
    ) -> None:
        self.db = db
        self.sandbox = sandbox
        self.tick_interval = tick_interval
        self.default_timeout = default_timeout
        self.workspace_root = workspace_root.rstrip("/") or "/workspace"
        self._dispatcher = dispatcher
        # Runner de jobs kind="research", injetado no bootstrap (o JobManager
        # NÃO importa core/research — docs/web-search-deep-research-plan.md §5.4).
        self._research_runner = research_runner
        # Runner de jobs kind="browser" (subagente browser-use no container
        # neo-browser-agent — docs/browser-agent-plan.md), mesmo padrão.
        self._browser_runner = browser_runner
        # Runner de jobs kind="browser_local" — o navegador do PRÓPRIO usuário,
        # dirigido passo a passo pelo core (docs/browser-local-plan.md).
        self._browser_local_runner = browser_local_runner
        self._download_watch_runner = download_watch_runner
        # Runner dos disparos da agenda (docs/scheduler-plan.md). Um só para os
        # quatro kinds `scheduled_*`: quem sabe a diferença entre avisar, rodar
        # uma tool e pagar um turno cognitivo é ele, não o JobManager.
        self._scheduled_runner = scheduled_runner
        # Leitura delegada de documentos (docs/deep-read-plan.md): subagentes
        # leem no sandbox e escrevem notas + síntese, fora do contexto do cérebro.
        self._deep_read_runner = deep_read_runner
        # Claude Code delegado (docs/claude-code-delegation-plan.md): o processo
        # roda fora do core e o job sobrevive a reinício (REATTACHABLE_JOB_KINDS).
        self._claude_runner = claude_runner
        # A volta de um pedido entregue numa sessão do Claude do DANIEL
        # (docs/ambiente-de-trabalho-plan.md §9.4): espera ela terminar no Mac.
        self._claude_sessao_runner = claude_sessao_runner
        self._sem = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._active: dict[str, asyncio.Task] = {}
        self._tick_task: asyncio.Task | None = None
        self._poke = asyncio.Event()
        self._stopped = False
        #: Tarefas leves que o tick roda de tempos em tempos: `[intervalo, fn, última]`.
        self._periodicas: list[list] = []

    def set_dispatcher(self, dispatcher: "ProactiveDispatcher") -> None:
        self._dispatcher = dispatcher

    def add_periodic(self, fn: "Callable[[], Awaitable[Any]]", every_s: float) -> None:
        """Roda `fn` no tick a cada `every_s` segundos (a varredura das esperas vencidas)."""
        self._periodicas.append([max(1.0, float(every_s)), fn, 0.0])

    async def _rodar_periodicas(self) -> None:
        agora = time.monotonic()
        for item in self._periodicas:
            intervalo, fn, ultima = item
            if agora - ultima < intervalo:
                continue
            item[2] = agora
            try:
                await fn()
            except Exception as e:  # noqa: BLE001 — uma varredura nunca derruba o tick
                logger.warning("[Jobs] tarefa periódica falhou: %s", e)

    def set_scheduled_runner(self, runner: "Callable[[AgentJob], Awaitable[dict]]") -> None:
        """Injeta o runner da agenda depois do boot (ele depende do AIService)."""
        self._scheduled_runner = runner

    def job_log_path(self, job_id) -> str:
        """Caminho absoluto do log do job dentro do sandbox."""
        return f"{self.workspace_root}/.jobs/{job_id}.log"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Reconcilia órfãos do boot e sobe o loop de tick."""
        try:
            orphans = await self.db.reconcile_orphan_agent_jobs()
            if orphans:
                logger.info("[Jobs] Reconciled %d orphan job(s) from a restart", len(orphans))
                for job in orphans:
                    await self._dispatch(job)
        except Exception as e:  # noqa: BLE001
            logger.warning("[Jobs] Boot reconcile failed: %s", e)

        # Reentrega jobs terminais que não chegaram a ser notificados.
        try:
            for job in await self.db.list_undelivered_terminal_jobs():
                await self._dispatch(job)
        except Exception as e:  # noqa: BLE001
            logger.warning("[Jobs] Undelivered catch-up failed: %s", e)

        self._tick_task = asyncio.create_task(self._tick_loop(), name="job-manager-tick")
        logger.info(
            "[Jobs] JobManager started (max_concurrent=%d, tick=%ds)",
            self._max_concurrent, self.tick_interval,
        )

    async def stop(self) -> None:
        self._stopped = True
        self._poke.set()
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except (asyncio.CancelledError, Exception):
                pass
        for task in list(self._active.values()):
            task.cancel()

    def poke(self) -> None:
        """Acorda o loop para reivindicar um job recém-enfileirado sem esperar o tick."""
        self._poke.set()

    # ------------------------------------------------------------------
    # Tick loop
    # ------------------------------------------------------------------

    async def _tick_loop(self) -> None:
        while not self._stopped:
            try:
                await self._claim_and_spawn()
            except Exception as e:  # noqa: BLE001
                logger.warning("[Jobs] tick error: %s", e)
            await self._rodar_periodicas()
            try:
                await asyncio.wait_for(self._poke.wait(), timeout=self.tick_interval)
            except asyncio.TimeoutError:
                pass
            finally:
                self._poke.clear()

    async def _claim_and_spawn(self) -> None:
        slots = self._max_concurrent - len(self._active)
        if slots <= 0:
            return
        jobs = await self.db.claim_due_agent_jobs(limit=slots)
        for job in jobs:
            self._spawn_worker(job)

    def _spawn_worker(self, job: AgentJob) -> None:
        job_id = str(job.id)
        if job_id in self._active:
            return
        task = asyncio.create_task(self._run_job(job), name=f"job-{job_id}")
        self._active[job_id] = task
        task.add_done_callback(lambda _t, jid=job_id: self._active.pop(jid, None))

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    async def _run_job(self, job: AgentJob) -> None:
        """Roda um job já reivindicado (status=running) até um estado terminal."""
        if job.kind == "comando_local":
            # Reatado depois de um reinício do core. Só consulta o computador a cada
            # poucos segundos: não ocupa vaga do pool (mesma regra da adoção).
            await self._run_comando_local(job)
            return
        async with self._sem:
            terminal: dict = {"status": "failed", "result_summary": "Falha desconhecida."}
            try:
                if job.kind == "sandbox_task":
                    terminal = await self._run_sandbox_task(job)
                elif job.kind == "research" and self._research_runner is not None:
                    terminal = await self._research_runner(job)
                elif job.kind == "browser" and self._browser_runner is not None:
                    terminal = await self._browser_runner(job)
                elif job.kind == "browser_local" and self._browser_local_runner is not None:
                    terminal = await self._browser_local_runner(job)
                elif job.kind == "download_watch" and self._download_watch_runner is not None:
                    terminal = await self._download_watch_runner(job)
                elif job.kind == "deep_read" and self._deep_read_runner is not None:
                    terminal = await self._deep_read_runner(job)
                elif job.kind == "claude_code" and self._claude_runner is not None:
                    terminal = await self._claude_runner(job)
                elif job.kind == "claude_sessao" and self._claude_sessao_runner is not None:
                    terminal = await self._claude_sessao_runner(job)
                elif job.kind.startswith("scheduled_") and self._scheduled_runner is not None:
                    terminal = await self._scheduled_runner(job)
                else:
                    terminal = {
                        "status": "failed",
                        "result_summary": f"Tipo de job não suportado nesta fase: {job.kind}",
                    }
            except WorkbenchUnavailableError as e:
                terminal = {
                    "status": "failed",
                    "result_summary": f"Ambiente de execução indisponível: {e}",
                }
            except asyncio.CancelledError:
                # Shutdown ou cancel explícito: deixa o estado como está (cancel já
                # marcou 'cancelled'); em shutdown, o boot reconcile cuida.
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception("[Jobs] job %s crashed", job.id)
                terminal = {"status": "failed", "result_summary": f"Erro no job: {e}"}

            await self._settle(job, terminal)

    async def _settle(self, job: AgentJob, terminal: dict) -> None:
        """Persiste o estado terminal, enfileira a continuação (se houver) e entrega."""
        then = str((job.spec_json or {}).get("then") or "").strip() or await self._then_pendurado(job)
        if (
            then
            and terminal.get("status") == "completed"
            and self._scheduled_runner is not None
            and await self._enqueue_continuation(job, then, terminal)
        ):
            # Quem fala com o usuário é a continuação, com o trabalho TERMINADO.
            # Avisar "terminei o OCR" agora e de novo com o arquivo pronto é ruído.
            # O caderno anota do mesmo jeito: `_note_result` fica acima do silent.
            #
            # O navegador é a exceção: o que ele entrega SÃO os itens (preço, link,
            # vendedor), colados íntegros pelo dispatcher. Silenciar trocaria a lista
            # pela paráfrase da continuação — ela vem logo depois, só com a conclusão.
            # O Claude Code também: o relatório dele vai literal. Silenciado, o do
            # job 8f83b37c virou "Arquivos salvos e disponíveis para download."
            if job.kind not in ("browser_local", "claude_code", "claude_sessao"):
                terminal = {**terminal, "notify_policy": "silent"}
        updated = None
        try:
            updated = await self.db.update_agent_job(
                job.id,
                finished_at=datetime.utcnow(),
                **terminal,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("[Jobs] failed to persist terminal state for %s: %s", job.id, e)
        if updated is not None:
            await self._dispatch(updated)

    async def _then_pendurado(self, job: AgentJob) -> str:
        """O `then` que chegou DEPOIS de o job começar (o verificador de promessa
        pendura a continuação no job que o turno abriu). O objeto em memória é de
        quando o job foi reivindicado; o banco tem o atual."""
        try:
            fresco = await self.db.get_agent_job(job.id)
        except Exception as e:  # noqa: BLE001 — sem banco, fica o que já se sabia
            logger.debug("[Jobs] não reli o spec do job %s: %s", job.id, e)
            return ""
        return str(((getattr(fresco, "spec_json", None) or {}) if fresco else {}).get("then") or "").strip()

    async def _itens_em_arquivo(self, job: AgentJob, terminal: dict) -> str:
        """Grava os itens íntegros quando o pedido é um ARQUIVO. Devolve o caminho, ou ``""``.

        A continuação recebe o resultado em até 6000 caracteres, e gravar o arquivo
        com ``neo_write_file`` obrigava o modelo a REDIGITAR o conteúdo: as dez
        questões do Portal (job d73fa72a) passam de 20 mil caracteres, mais do que
        cabe numa resposta. Os itens já estão guardados íntegros na entrega (o
        dispatcher os cola no chat sem redator), e o arquivo sai deles, sem passar
        por modelo nenhum.

        Só quando o pedido fala em arquivo: gravar toda coleta deixaria no
        sandbox uma cópia do que ele navega sem ninguém ter pedido. Nunca levanta:
        sem arquivo, a continuação é a de sempre.
        """
        spec = job.spec_json or {}
        pedido = " ".join(str(spec.get(k) or "") for k in ("pedido", "then", "task"))
        if not _PEDE_ARQUIVO.search(pedido):
            return ""
        entrega = (
            (terminal.get("extra_metadata") or {}).get("entrega")
            or (job.extra_metadata or {}).get("entrega")
            or {}
        )
        itens = [str(i).strip() for i in (entrega.get("itens") or []) if str(i).strip()]
        # Os rodapés que o runner redige (aba, downloads, diário) não são coleta.
        # Mesma regra do `_com_itens` do dispatcher.
        achados = [i for i in itens if not i.startswith(_RODAPE_DA_ENTREGA)]
        conteudo = "\n\n".join(achados) + "\n"
        # O `write_file` passa o conteúdo num argumento de shell (ver o teto acima).
        if not achados or len(conteudo) > _ADOPTED_LOG_MAX_CHARS:
            return ""
        nome = f"files/{_nome_de_arquivo(job.title)}-{str(job.id)[:8]}.txt"
        try:
            gravado = await self.sandbox.write_file(nome, conteudo)
        except Exception as e:  # noqa: BLE001
            logger.warning("[Jobs] itens do job %s não viraram arquivo: %s", job.id, e)
            return ""
        if not getattr(gravado, "ok", False):
            logger.warning("[Jobs] itens do job %s não viraram arquivo: %s", job.id,
                           getattr(gravado, "stderr", ""))
            return ""
        return self.sandbox.resolve_path(nome)

    async def _enqueue_continuation(self, job: AgentJob, then: str, terminal: dict) -> bool:
        """Enfileira o turno em que o Neo termina o que o job começou.

        Sem isto, "rodar em background" era "parar no meio": o resultado virava um
        aviso, e montar e entregar o que o usuário pediu dependia de ele voltar e
        pedir de novo. É um ``scheduled_prompt`` sem tarefa de agenda — o mesmo
        turno cognitivo sem ninguém na frente que a agenda já usa
        (docs/scheduler-plan.md §6), na MESMA sessão, então o histórico do pedido
        original está lá. ``False`` quando não deu para enfileirar: aí o job avisa
        do jeito de sempre, em vez de ficar calado esperando uma continuação que
        não existe.
        """
        resumo = str(terminal.get("result_summary") or "").strip()
        arquivo_dos_itens = (
            await self._itens_em_arquivo(job, terminal) if job.kind == "browser_local" else ""
        )
        if arquivo_dos_itens:
            # Para o caderno também, e não só para a continuação: a entrada do job
            # (`_note_result`) aponta o arquivo íntegro ao lado do achado. No job
            # f7794968 o arquivo existia e a nota não dizia onde.
            # Escrever a chave SOBRESCREVE a coluna inteira: sem o terminal ter
            # trazido a sua, parte-se da que o job já tinha.
            if terminal.get("extra_metadata") is None:
                terminal["extra_metadata"] = dict(job.extra_metadata or {})
            terminal["extra_metadata"]["itens_arquivo"] = arquivo_dos_itens
        # O turno de continuação nasce SEM a skill que o job usou (session-1fcaa440:
        # ignorou o aviso do script e resumiu em vez de montar o arquivo). Quem sabe
        # qual skill é o próprio comando.
        skill = re.search(r"/skills/([a-z0-9][a-z0-9-]{1,39})/", str((job.spec_json or {}).get("command") or ""))
        leitura_skill = (
            f"Este job rodou a skill `{skill.group(1)}`: chame skill_read(name=\"{skill.group(1)}\") "
            "ANTES de qualquer outra coisa e siga o que ela manda para o fim do job.\n"
            if skill else ""
        )
        texto = (
            "[Continuação automática — não é mensagem do usuário. Uma tarefa sua em "
            "segundo plano terminou.]\n"
            f"Job: '{job.title}' (job_id={job.id}), estado {terminal.get('status')}.\n"
            f"Resultado (pode estar truncado):\n{resumo[:6000] or '(sem saída)'}\n\n"
            + leitura_skill
            + (
                "Os itens que o navegador coletou JÁ foram entregues ao usuário, íntegros, "
                "numa mensagem logo antes desta. Responda SÓ o combinado (o veredito, a "
                "conclusão), citando os itens pelo nome e pelo preço — sem repetir a lista.\n"
                if job.kind == "browser_local" else ""
            )
            + (
                "O relatório do Claude, a lista de arquivos e o comando para retomar JÁ "
                "foram entregues ao usuário, íntegros, numa mensagem logo antes desta. "
                "Faça SÓ o combinado, sem repetir o relatório.\n"
                if job.kind == "claude_code" else ""
            )
            + (
                f"Os itens coletados JÁ estão gravados, íntegros, em `{arquivo_dos_itens}`. "
                "Se o combinado é um arquivo, entregue ESTE com file_save (ajuste o formato com "
                "neo_bash, se precisar). Não reescreva o conteúdo à mão: ele não cabe numa "
                "resposta e sairia cortado.\n"
                if arquivo_dos_itens else ""
            )
            + "O que você deixou combinado para este momento — faça AGORA, até o fim:\n"
            f"{then}\n"
            + (_deep_read_contract(job) if job.kind == "deep_read" else "")
            + "\n"
            "Se o combinado é entregar um arquivo, grave-o (neo_write_file) e entregue com "
            "file_save — o botão de download vai junto desta mensagem. Na resposta, diga em "
            "poucas frases o que entregou; NÃO cole o arquivo inteiro no chat. Se o combinado "
            "pede para MOSTRAR um trecho (uma saída, um resultado), cole esse trecho."
        )
        try:
            continuation = await self.db.create_agent_job(
                AgentJob(
                    user_id=job.user_id,
                    session_id=job.session_id,
                    kind="scheduled_prompt",
                    title=f"Continuação: {job.title}"[:256],
                    spec_json={
                        "action_kind": "prompt",
                        "action": {"texto": texto},
                        "continuation_of": str(job.id),
                    },
                    status="queued",
                    notify_policy="chat",
                )
            )
        except Exception as e:  # noqa: BLE001
            logger.error("[Jobs] continuação do job %s não enfileirou: %s", job.id, e)
            return False
        logger.info("[Jobs] continuação %s enfileirada para o job %s", continuation.id, job.id)
        self.poke()
        return True

    # ------------------------------------------------------------------
    # Comando adotado (neo_bash que passou do orçamento de primeiro plano)
    # ------------------------------------------------------------------

    async def adopt_running(
        self,
        run: "asyncio.Future",
        *,
        user_id,
        session_id,
        title: str,
        command: str,
        cwd: str | None = None,
        started_at: datetime | None = None,
    ) -> AgentJob:
        """Transforma um ``neo_bash`` em curso num job, sem interromper o processo.

        O comando nasceu em primeiro plano (``WorkbenchExecutor._bash``) e passou do
        orçamento ainda trabalhando. Em vez de matar (o teto antigo) ou prender a
        conversa (o teto de 15 min), ele vira um ``sandbox_task`` já ``running``: o
        turno responde na hora e o resultado sai pelo mesmo ``_settle`` de qualquer
        job — caderno, continuação e aviso incluídos. Não ocupa o semáforo: o
        trabalho já estava rodando antes de o JobManager saber dele.
        """
        now = datetime.utcnow()
        spec: dict = {"command": command, "adopted": True}
        if cwd:
            spec["cwd"] = str(cwd)
        job = await self.db.create_agent_job(
            AgentJob(
                user_id=user_id,
                session_id=session_id,
                kind="sandbox_task",
                title=title[:256],
                spec_json=spec,
                status="running",
                notify_policy="chat",
                started_at=started_at or now,
                heartbeat_at=now,
                progress_note=(
                    "Começou em primeiro plano e foi para background ainda trabalhando; "
                    "a saída completa aparece quando terminar."
                ),
                extra_metadata={"origin": "neo_bash_foreground"},
            )
        )
        job_id = str(job.id)
        waiter = asyncio.create_task(self._finish_adopted(job, run), name=f"job-{job_id}")
        self._active[job_id] = waiter
        waiter.add_done_callback(lambda _t, jid=job_id: self._active.pop(jid, None))
        # Cancelada antes de começar a rodar, a task nem entra no `except` de
        # `_finish_adopted` — e o comando ficaria vivo no container, sem dono.
        waiter.add_done_callback(
            lambda t, r=run: r.cancel() if t.cancelled() and not r.done() else None
        )
        return job

    async def _finish_adopted(self, job: AgentJob, run: "asyncio.Future") -> None:
        terminal: dict = {"status": "failed", "result_summary": "Falha desconhecida."}
        try:
            while not run.done():
                await asyncio.wait({run}, timeout=_ADOPTED_HEARTBEAT_S)
                if not run.done():
                    try:
                        await self.db.update_agent_job(job.id, heartbeat_at=datetime.utcnow())
                    except Exception:  # noqa: BLE001
                        pass
            terminal = await self._adopted_terminal(job, run.result())
        except asyncio.CancelledError:
            # job_cancel (tool ou tela) cancela esta task; cancelar o `run` é o que
            # mata a árvore do comando no container (WorkbenchSandbox.run).
            run.cancel()
            raise
        except WorkbenchUnavailableError as e:
            terminal = {
                "status": "failed",
                "result_summary": f"Ambiente de execução indisponível: {e}",
            }
        except Exception as e:  # noqa: BLE001
            logger.exception("[Jobs] comando adotado %s quebrou", job.id)
            terminal = {"status": "failed", "result_summary": f"Erro no job: {e}"}
        await self._settle(job, terminal)

    async def _adopted_terminal(self, job: AgentJob, result) -> dict:
        partes = [str(result.stdout or "").strip()]
        if str(result.stderr or "").strip():
            partes.append("[stderr]\n" + str(result.stderr).strip())
        output = "\n".join(p for p in partes if p)
        if result.stalled:
            status = "timeout"
            output += "\n[parado: o comando ficou sem progresso e foi interrompido]"
        elif result.timed_out:
            status = "timeout"
        elif result.ok:
            status = "completed"
        else:
            status = "failed"
        terminal: dict = {
            "status": status,
            "exit_code": result.exit_code,
            "result_summary": _summarize(output, result.exit_code, status),
            "progress_note": None,
        }
        # A saída só existe agora — o comando nasceu sem log. Grava para o
        # `job_logs` e a tela de tarefas lerem depois.
        try:
            gravado = await self.sandbox.write_file(
                f".jobs/{job.id}.log", (output or "(sem saída)")[-_ADOPTED_LOG_MAX_CHARS:]
            )
            if gravado.ok:
                terminal["log_path"] = self.job_log_path(job.id)
        except Exception as e:  # noqa: BLE001
            logger.warning("[Jobs] log do comando adotado %s não foi gravado: %s", job.id, e)
        return terminal

    # ------------------------------------------------------------------
    # Comando que virou tarefa no computador do usuário (run_command)
    # ------------------------------------------------------------------

    async def adopt_local_command(
        self,
        *,
        user_id,
        session_id,
        device_id: str,
        tarefa: dict,
    ) -> AgentJob:
        """Abre o job que acompanha uma tarefa que o DESKTOP adotou.

        O comando nasceu num turno (já aprovado pela política ou pelo card), passou
        do orçamento de primeiro plano ainda trabalhando e o desktop o manteve vivo
        em segundo plano (``services/local_jobs.rs``). Daqui ele é um job comum:
        janela Tarefas, cancelamento, reata depois de reload e aviso no chat ao fim.
        Como o comando adotado no sandbox, não ocupa o semáforo — o trabalho já
        estava rodando antes de o JobManager saber dele.
        """
        from core.jobs.runner_comando_local import KIND, progresso, titulo

        now = datetime.utcnow()
        try:
            decorrido = max(0, int(tarefa.get("elapsed_s") or 0))
        except (TypeError, ValueError):
            decorrido = 0
        comando = str(tarefa.get("command") or "")
        job = await self.db.create_agent_job(
            AgentJob(
                user_id=user_id,
                session_id=session_id,
                kind=KIND,
                title=titulo(comando)[:256],
                spec_json={
                    "device_id": str(device_id),
                    "tarefa_id": str(tarefa.get("id")),
                    "command": comando,
                    "cwd": tarefa.get("cwd"),
                    "max_runtime_s": tarefa.get("max_runtime_s"),
                    # Onde o log mora NO COMPUTADOR do usuário. Fica só lá.
                    "log_local": tarefa.get("log_path"),
                },
                status="running",
                notify_policy="chat",
                started_at=now - timedelta(seconds=decorrido),
                heartbeat_at=now,
                progress_note=progresso(tarefa),
                extra_metadata={"origin": "run_command_local"},
            )
        )
        job_id = str(job.id)
        waiter = asyncio.create_task(self._run_comando_local(job), name=f"job-{job_id}")
        self._active[job_id] = waiter
        waiter.add_done_callback(lambda _t, jid=job_id: self._active.pop(jid, None))
        return job

    async def _run_comando_local(self, job: AgentJob) -> None:
        from core.jobs import runner_comando_local

        try:
            terminal = await runner_comando_local.acompanhar(job, db=self.db)
        except asyncio.CancelledError:
            # job_cancel marcou 'cancelled' e o runner já parou a tarefa no computador.
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("[Jobs] acompanhamento do comando local %s quebrou", job.id)
            terminal = {"status": "failed", "result_summary": f"Erro ao acompanhar a tarefa: {e}"}
        await self._settle(job, terminal)

    async def _run_sandbox_task(self, job: AgentJob) -> dict:
        spec = job.spec_json or {}
        command = str(spec.get("command") or "").strip()
        if not command:
            return {"status": "failed", "result_summary": "Job sem comando."}
        cwd = spec.get("cwd")
        timeout = spec.get("timeout")
        try:
            timeout = int(timeout) if timeout is not None else self.default_timeout
        except (TypeError, ValueError):
            timeout = self.default_timeout

        log_abs = self.job_log_path(job.id)
        log_dir = log_abs.rsplit("/", 1)[0]
        # Redireciona a saída para o log dentro do sandbox (persistente, legível a
        # qualquer momento por job_logs). O exit code do pipeline é o do comando.
        wrapped = (
            f"mkdir -p {shlex.quote(log_dir)} && "
            f"( {command} ) > {shlex.quote(log_abs)} 2>&1"
        )
        try:
            await self.db.update_agent_job(
                job.id,
                log_path=log_abs,
                timeout_at=datetime.utcnow() + timedelta(seconds=timeout),
                heartbeat_at=datetime.utcnow(),
            )
        except Exception:  # noqa: BLE001
            pass

        result = await self.sandbox.run(command=wrapped, cwd=cwd, timeout=timeout)

        # Lê o conteúdo do log para virar o result_summary (tail).
        output = ""
        try:
            log_rel = f".jobs/{job.id}.log"
            log_res = await self.sandbox.read_file(log_rel)
            if log_res.ok:
                output = log_res.stdout
        except Exception:  # noqa: BLE001
            output = result.stdout or result.stderr
        if not output:
            # Comando que morreu ANTES de escrever o log (recusado pela guarda de
            # caminho, binário inexistente, sh que nem subiu) não pode chegar ao
            # usuário como job "failed" sem motivo nenhum.
            output = result.stderr or result.stdout

        if result.timed_out:
            status = "timeout"
        elif result.ok:
            status = "completed"
        else:
            status = "failed"

        summary = _summarize(output, result.exit_code, status)
        return {
            "status": status,
            "exit_code": result.exit_code,
            "result_summary": summary,
        }

    async def _dispatch(self, job: AgentJob) -> None:
        if self._dispatcher is None:
            # Sem dispatcher (F2 puro): resultado fica no DB; marca notificado para
            # não reprocessar em catch-up.
            try:
                await self.db.update_agent_job(job.id, notified_at=datetime.utcnow())
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            await self._dispatcher.dispatch(job)
        except Exception as e:  # noqa: BLE001
            logger.error("[Jobs] dispatch failed for %s: %s", job.id, e)


def _deep_read_contract(job: AgentJob) -> str:
    """Regras da continuação de uma leitura (docs/deep-read-plan.md §10)."""
    try:
        from core.documents.render import render_continuation_contract

        return render_continuation_contract(job.spec_json or {}) + "\n"
    except Exception as e:  # noqa: BLE001 — sem o contrato a continuação ainda roda
        logger.warning("[Jobs] contrato da leitura %s não montou: %s", job.id, e)
        return ""


def _summarize(output: str, exit_code: int | None, status: str, *, max_chars: int = 4000) -> str:
    """Resumo textual do resultado do job para o result_summary/dispatcher."""
    tail = output or ""
    if len(tail) > max_chars:
        tail = "…" + tail[-max_chars:]
    header = f"[{status}] exit={exit_code}"
    if not tail.strip():
        return f"{header}\n(sem saída)"
    return f"{header}\n{tail}"
