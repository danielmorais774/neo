"""JobToolExecutor — executa ``neo_bash_background`` + ``job_*`` com escopo do turno.

Precisa saber o ``user_id`` e o ``session_id`` da conversa para amarrar o job à
sessão de origem (é para lá que o resultado volta). Por isso é construído por
request (como os executores user-scoped dos read tools) e roteado explicitamente
no loop cognitivo, não pelo executor global. Ver
``docs/workbench-background-jobs-plan.md``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from core.jobs.screenshots import image_markers
from db.models.agent_job import AgentJob, TERMINAL_STATUSES
from shared import ToolCall, ToolResult

from core.workbench.user_paths import refusal_for

logger = logging.getLogger(__name__)

_MAX_JOB_TIMEOUT = 21_600  # 6h

# Sentinela do portão: a chamada de navegador foi RECUSADA porque pedia play e
# não há navegador do usuário para tocar. Não é nome de tool — `execute_tool_call`
# a traduz num resultado que manda o cérebro tocar pelo caminho certo.
_NO_SERVER_PLAYBACK = "__no_server_playback__"

_NO_SERVER_PLAYBACK_ERROR = (
    "Não dá para TOCAR música/vídeo pelo navegador do servidor: ele é headless, "
    "não tem tela nem as caixas de som do usuário — ninguém ouviria nada. E o "
    "navegador DELE não está disponível neste turno. Não tente de novo por aqui: "
    "para tocar de verdade, feche a resposta com o bloco [[HANDOFF:music]] e a "
    "faixa/artista dentro — é ele que executa."
)

# O que o cérebro precisa saber quando o portão TROCOU o navegador por baixo dele
# (session-30b1e484). O resultado voltava idêntico nos dois casos — `ok`, `job_id`,
# `queued` — e o modelo, que tinha chamado `browser_local_task`, respondeu "abri
# no seu navegador local" sobre um job que rodava no Chromium da VPS. Sem esta
# linha ele não tem como saber que a frase é falsa.
_GATE_REWROTE_NOTE: dict[tuple[str, str], str] = {
    ("browser_local_task", "browser_task"): (
        "ATENÇÃO: esta tarefa foi para o navegador do SERVIDOR (headless), NÃO para "
        "a tela do usuário — ele não vê nada acontecendo. Não diga que abriu no "
        "navegador dele. Se ele quer acompanhar na tela, precisa pedir com todas "
        "as letras (\"no meu navegador\")."
    ),
    ("browser_task", "browser_local_task"): (
        "Esta tarefa foi para o navegador DO USUÁRIO (a tela dele), porque ele pediu "
        "para ver — não para o servidor."
    ),
}

# Por quanto tempo um pedido explícito de "navegador local" continua valendo para
# as mensagens seguintes da mesma conversa — a mesma régua da herança por job.
_LOCAL_ASK_WINDOW = timedelta(minutes=10)
_LOCAL_ASK_LOOKBACK = 3

# Tarefa de browser no servidor repetida: mesmo endereço, mesma conversa, ainda na
# fila ou rodando.
_BROWSER_DUP_WINDOW = timedelta(minutes=20)
_URL_RE = re.compile(r"https?://[^\s'\"<>()\[\]{}]+", re.IGNORECASE)


def _urls_of(*texts: Any) -> set[str]:
    """Endereços citados, normalizados (sem esquema, sem ``www.``, sem ``/`` final)."""
    found: set[str] = set()
    for text in texts:
        for raw in _URL_RE.findall(str(text or "")):
            url = raw.rstrip(".,;:!?").lower().split("://", 1)[-1]
            if url.startswith("www."):
                url = url[4:]
            url = url.rstrip("/")
            if url:
                found.add(url)
    return found


class JobToolExecutor:
    """Executor por-request das tools de jobs em background."""

    def __init__(
        self,
        *,
        db: Any,
        job_manager: Any,
        user_id: UUID | None,
        session_id: UUID | None,
        file_store: Any = None,
        can_show_images: bool = False,
        user_message: str = "",
        device_id: str | None = None,
        disk_evidence: "Any" = None,
        criteria_checker: "Any" = None,
    ) -> None:
        self.db = db
        # deep_read (docs/deep-read-plan.md §10): o que o Neo VIU no disco do
        # usuário (listagens reais) e o verificador de critérios do pedido. Os
        # dois moram no AIService; sem eles a tool só perde essas conferências.
        self.disk_evidence = disk_evidence
        self.criteria_checker = criteria_checker
        self.jm = job_manager
        self.user_id = user_id
        self.session_id = session_id
        # O portão do navegador local (docs/browser-local-plan.md §5.2) decide
        # entre a tela do usuário e o Chromium do servidor — e para isso precisa
        # da FRASE do turno, não do que o modelo achou dela.
        self.user_message = user_message or ""
        self.device_id = device_id
        # Só o browser_screenshot usa: o print vira arquivo do catálogo e o
        # cérebro devolve o marcador `[[IMAGE:file:…]]`. Sem store, a tool avisa.
        self.file_store = file_store
        # Cliente que NÃO renderiza o marcador (WhatsApp, painel) leria
        # "[[IMAGE:file:…]]" como texto — nesses o print fica guardado e a tool
        # manda o cérebro contar o que fez, não colar sintaxe na cara do usuário.
        self.can_show_images = can_show_images

    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        start = asyncio.get_event_loop().time()
        args = tool_call.arguments or {}
        name = await self._gate_browser(tool_call.name, args)
        if name == _NO_SERVER_PLAYBACK:
            logger.info("[browser-gate] recusado: play sem o navegador do usuário")
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                result={"ok": False, "error": _NO_SERVER_PLAYBACK_ERROR},
                execution_time=asyncio.get_event_loop().time() - start,
            )
        try:
            if name == "neo_bash_background":
                result = await self._enqueue(args)
            elif name == "neo_bash":
                result = await self._bash(args)
            elif name == "deep_research":
                result = await self._deep_research(args)
            elif name == "deep_read":
                result = await self._deep_read(args)
            elif name == "browser_task":
                result = await self._browser_task(args)
            elif name == "browser_local_task":
                result = await self._browser_local_task(args)
            elif name == "browser_screenshot":
                result = await self._browser_screenshot(args)
            elif name == "job_status":
                result = await self._status(args)
            elif name == "job_list":
                result = await self._list(args)
            elif name == "job_logs":
                result = await self._logs(args)
            elif name == "job_cancel":
                result = await self._cancel(args)
            else:
                raise ValueError(f"Tool de job desconhecida: {name}")
            aviso = _GATE_REWROTE_NOTE.get((tool_call.name, name))
            if aviso and isinstance(result, dict) and result.get("ok"):
                result = {
                    **result,
                    "onde_rodou": "servidor" if name == "browser_task" else "navegador do usuário",
                    "aviso": aviso,
                }
            return ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                result=result,
                execution_time=asyncio.get_event_loop().time() - start,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Job tool '%s' failed: %s", name, e)
            return ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                result=None,
                error=str(e),
                execution_time=asyncio.get_event_loop().time() - start,
            )

    # ------------------------------------------------------------------

    async def _bash(self, args: dict[str, Any]) -> dict[str, Any]:
        """``neo_bash`` com saída para background (ver ``WorkbenchExecutor._bash``).

        Passa por aqui, e não pelo executor global, porque só aqui existe o
        usuário e a sessão do turno — sem eles o job adotado não teria para onde
        mandar o resultado.
        """
        from core.workbench.executor import WorkbenchExecutor

        sandbox = getattr(self.jm, "sandbox", None)
        if sandbox is None:
            return {"ok": False, "error": "O ambiente de execução do Neo não está ligado.", "unavailable": True}
        return await WorkbenchExecutor(sandbox).execute(
            "neo_bash",
            args,
            adopt=self._adopt,
            background_ceiling=int(getattr(self.jm, "default_timeout", 1800) or 1800),
        )

    async def _adopt(self, run: Any, *, command: str, cwd: Any, elapsed_s: int) -> dict[str, Any]:
        title = _title_for_command(command)
        job = await self.jm.adopt_running(
            run,
            user_id=self.user_id,
            session_id=self.session_id,
            title=title,
            command=command,
            cwd=cwd,
            started_at=datetime.utcnow() - timedelta(seconds=elapsed_s),
        )
        return {
            "ok": True,
            "backgrounded": True,
            "job_id": str(job.id),
            "title": title,
            "status": "running",
            "elapsed_s": elapsed_s,
            "note": (
                f"O comando passou de {elapsed_s}s ainda trabalhando e foi para "
                f"BACKGROUND como o job {job.id} — ele NÃO foi interrompido. O resultado "
                "chega sozinho no chat quando terminar (acompanhe com job_status/"
                "job_logs se precisar). Avise o usuário em uma frase e encerre a "
                "resposta; NÃO rode o mesmo comando de novo."
            ),
        }

    async def _enqueue(self, args: dict[str, Any]) -> dict[str, Any]:
        command = str(args.get("command") or "").strip()
        if not command:
            return {"ok": False, "error": "command vazio."}
        # A guarda real vive em `WorkbenchSandbox.run` (ponto único). Repetir aqui
        # não é redundância: sem isto o job doomed é CRIADO, entra na fila, roda,
        # falha e só então o modelo descobre — minutos depois, por notificação
        # proativa. Na sessão d9f9417b foram três desses, e o usuário não viu
        # nenhum. Recusar no enfileiramento devolve o erro DENTRO do turno, que é
        # onde o modelo ainda pode corrigir a rota.
        recusa = refusal_for(command)
        if recusa is not None:
            logger.warning("[jobs] enfileiramento recusado: %.200s", command)
            return {"ok": False, "error": recusa}
        title = str(args.get("title") or "Tarefa em background").strip()[:256]
        cwd = args.get("cwd")
        timeout = args.get("timeout")
        spec: dict[str, Any] = {"command": command}
        if cwd:
            spec["cwd"] = str(cwd)
        if timeout is not None:
            try:
                spec["timeout"] = max(1, min(int(timeout), _MAX_JOB_TIMEOUT))
            except (TypeError, ValueError):
                pass
        then = str(args.get("then") or "").strip()
        if then:
            # Ordem do cérebro para ele mesmo quando o job acabar: o JobManager
            # a transforma num turno de continuação (`_enqueue_continuation`).
            spec["then"] = then[:4000]
        job = AgentJob(
            user_id=self.user_id,
            session_id=self.session_id,
            kind="sandbox_task",
            title=title,
            spec_json=spec,
            status="queued",
            notify_policy="chat",
        )
        job = await self.db.create_agent_job(job)
        # Acorda o loop para pegar já, sem esperar o tick.
        try:
            self.jm.poke()
        except Exception:  # noqa: BLE001
            pass
        return {
            "ok": True,
            "job_id": str(job.id),
            "title": title,
            "status": "queued",
            "note": "Job enfileirado. Avise o usuário que você avisará quando terminar.",
        }

    async def _browser_task(self, args: dict[str, Any]) -> dict[str, Any]:
        """Cria agent_job(kind=browser) e volta na hora (docs/browser-agent-plan.md).

        Sem segredos no ``spec_json`` (jsonb legível): credenciais entram só na F3,
        por labels resolvidos pelo runner na hora do POST.
        """
        from shared.config import get_settings

        settings = get_settings()
        if not settings.browser_agent_enabled:
            return {"ok": False, "error": "browser_task está desligado na config."}
        task = str(args.get("task") or "").strip()
        if not task:
            return {"ok": False, "error": "task vazia."}
        title = str(args.get("title") or "Tarefa no browser").strip()[:256]
        session = str(args.get("session") or settings.browser_default_session).strip()
        # Nome de sessão vira diretório de perfil no container — sanear.
        session = "".join(c for c in session if c.isalnum() or c in "-_")[:64] or "user-default"
        # Mesma tarefa já na fila ou rodando nesta conversa: devolve a que existe.
        # session-30b1e484: o mesmo site virou CINCO jobs em 6 minutos (o agente do
        # servidor, o cérebro e a guarda anti-promessa, cada um "garantindo" o seu)
        # numa sessão de browser que atende uma tarefa por vez — quatro morreram
        # esperando e o usuário recebeu quatro avisos de falha.
        duplicado = await self._browser_job_duplicado(task, args.get("start_url"))
        if duplicado is not None:
            logger.info("[browser] tarefa repetida — reusando o job %s", duplicado.id)
            return {
                "ok": True,
                "job_id": str(duplicado.id),
                "title": duplicado.title,
                "session": str((duplicado.spec_json or {}).get("session") or session),
                "status": duplicado.status,
                "reused": True,
                "note": (
                    "Essa tarefa já está na fila/rodando nesta conversa — NÃO abri outra. "
                    "O resultado chega sozinho no chat quando terminar: avise o usuário "
                    "em uma frase e encerre, sem chamar de novo."
                ),
            }
        spec: dict[str, Any] = {"task": task, "session": session}
        context = str(args.get("context") or "").strip()
        if context:
            spec["context"] = context[:2000]
        try:
            spec["max_steps"] = max(3, min(int(args.get("max_steps") or settings.browser_max_steps), 100))
        except (TypeError, ValueError):
            spec["max_steps"] = settings.browser_max_steps
        # Credenciais: só os RÓTULOS vão para o spec_json (jsonb legível). O
        # segredo é resolvido pelo runner na hora do POST — nunca fica em banco
        # de fila nem em log de job.
        labels = [
            part.strip()
            for part in str(args.get("credentials") or "").split(",")
            if part.strip()
        ]
        if labels:
            spec["credentials"] = labels[:4]
        # Prints: 'auto' já cobre o erro sozinho; 'always' é o "quero ver".
        mode = str(args.get("screenshots") or "").strip().lower()
        spec["screenshots"] = mode if mode in ("auto", "always", "never") else settings.browser_screenshots_default
        job = AgentJob(
            user_id=self.user_id,
            session_id=self.session_id,
            kind="browser",
            title=title,
            spec_json=spec,
            status="queued",
            notify_policy="chat",
        )
        job = await self.db.create_agent_job(job)
        try:
            self.jm.poke()
        except Exception:  # noqa: BLE001
            pass
        return {
            "ok": True,
            "job_id": str(job.id),
            "title": title,
            "session": session,
            "status": "queued",
            "note": "Tarefa de browser enfileirada. Avise o usuário que você avisará quando terminar.",
        }

    # ------------------------------------------------------------------
    # Navegador local (docs/browser-local-plan.md)
    # ------------------------------------------------------------------

    async def _gate_browser(self, name: str, args: dict[str, Any] | None = None) -> str:
        """O portão: navegador local só quando o usuário PEDIU, com todas as letras.

        Vale nas duas direções, porque os dois erros doem:

        * escolheu ``browser_local_task`` sem o usuário ter pedido → **rebaixa**
          para o servidor (o usuário só queria a tarefa feita; tomar conta do
          Chrome dele sem pedir é a falha grave);
        * escolheu ``browser_task`` e o usuário pediu a tela dele → **promove**
          (pediu para ver e o Neo ia fazer escondido).

        Continuação curta ("agora ordena por preço") herda o modo do job
        anterior desta sessão — sem isso o follow-up cai no servidor e perde a
        aba que o usuário está olhando.

        Pedido explícito RECENTE também segura o local (session-30b1e484): "vamos
        pelo meu navegador local — caminho A" e, na mensagem seguinte, "nao foi,
        acessa o meu portifolio ai". A segunda frase não tem âncora e não havia job
        local para herdar: o portão rebaixou duas vezes e o Neo disse "abri no seu
        navegador" sobre um job no servidor. O histórico só MANTÉM o local que o
        cérebro escolheu — nunca promove.

        **TOCAR mídia é a exceção que não passa por essa balança** (sessão
        0939a295): play num headless de VPS não é uma versão pior do pedido, é o
        pedido não existir lá — sem tela e sem as caixas de som dele, ninguém
        ouve nada. Então o destino é o navegador DELE ou nenhum; rebaixar é o
        único desfecho que está errado nos dois sentidos, porque ainda queima um
        job inteiro (o 1998e5fb rodou 5min38 antes de morrer) e deixa o
        verificador de música cego para a atuação que aconteceu.
        """
        if name not in ("browser_task", "browser_local_task"):
            return name
        from shared.config import get_settings

        settings = get_settings()
        local_on = bool(getattr(settings, "browser_local_enabled", False))

        # Play: ou vai para a tela dele, ou não vai — e aí quem toca é a skill
        # de música, não o navegador.
        if self._is_media_playback(args):
            if local_on and self.device_id:
                if name != "browser_local_task":
                    logger.info(
                        "[browser-gate] promovido para browser_local_task "
                        "(tocar mídia não existe no servidor)"
                    )
                return "browser_local_task"
            return _NO_SERVER_PLAYBACK

        if not local_on:
            # Desligado: o local simplesmente não existe.
            if name == "browser_local_task":
                logger.info("[browser-gate] rebaixado (navegador local desligado)")
                return "browser_task"
            return name

        from core.browser_local.gate import local_browser_requested

        asked = local_browser_requested(self.user_message)
        if not asked and name == "browser_local_task":
            if await self._inherits_local():
                logger.info("[browser-gate] local mantido por continuação da sessão")
                return name
            if await self._asked_local_recently():
                logger.info("[browser-gate] local mantido: o usuário pediu o navegador dele há pouco")
                return name
            # ⚠️ Este rebaixamento era a coisa certa quando "local" queria dizer
            # "tomar a janela que ele está usando". Não quer mais: sem pedido
            # explícito o local roda HEADLESS (ver `_browser_local_task`), que
            # não toca a tela dele. Rebaixar virou mandar o trabalho para a
            # origem PIOR de propósito — o servidor sai por IP de datacenter na
            # França e leva 403/CAPTCHA na web brasileira (medido 2026-09-20,
            # job 36a81dd6: 22 passos, 51 CAPTCHAs, nada entregue).
            #
            # Com device na mão, o local headless vence. Sem device, o
            # `_browser_local_task` cai para o servidor sozinho — e aí a queda é
            # por indisponibilidade, que é motivo legítimo.
            if self.device_id:
                logger.info(
                    "[browser-gate] local mantido em modo headless "
                    "(não toma a tela dele, e a origem é a certa)"
                )
                return name
            logger.info(
                "[browser-gate] rebaixado para browser_task (nenhuma máquina do usuário neste canal)"
            )
            return "browser_task"
        if asked and name == "browser_task":
            logger.info("[browser-gate] promovido para browser_local_task (pediu o navegador dele)")
            return "browser_local_task"
        return name

    def _is_media_playback(self, args: dict[str, Any] | None) -> bool:
        """Este turno pede REPRODUÇÃO de áudio/vídeo?

        Lê as duas fontes SEPARADAMENTE (nunca concatenadas, senão o verbo de um
        lado casa com o substantivo do outro):

        * a **tarefa que o cérebro escreveu** — é ele quem diz "dar play no
          vídeo", e o §5.2 do plano só olhava a frase do usuário;
        * a **frase do usuário** — "coloca uma musica do meu mix para tocar ai"
          não tem palavra de navegador nenhuma e mesmo assim é play.
        """
        from core.browser_local.gate import media_playback_requested

        spec = args or {}
        do_cerebro = " ".join(
            str(spec.get(campo) or "")
            for campo in ("task", "title", "context", "start_url")
        )
        return media_playback_requested(do_cerebro) or media_playback_requested(
            self.user_message
        )

    async def _inherits_local(self) -> bool:
        """Teve tarefa no navegador local NESTA sessão nos últimos 10 minutos?

        É o que faz "agora ordena por preço" continuar na aba que o usuário está
        olhando em vez de recomeçar escondido no servidor. Fonte da verdade é o
        banco de jobs, não um cache de processo — o core reinicia (--reload) no
        meio de uma conversa e a herança não pode morrer com ele.
        """
        if self.session_id is None or self.user_id is None:
            return False
        try:
            recent = await self.db.list_agent_jobs(self.user_id, limit=12)
        except Exception as exc:  # noqa: BLE001 — sem banco, o default é o servidor
            logger.warning("[browser-gate] histórico de jobs indisponível: %s", exc)
            return False
        cutoff = datetime.utcnow() - timedelta(minutes=10)
        for job in recent:
            if job.kind != "browser_local" or job.session_id != self.session_id:
                continue
            created = getattr(job, "created_at", None)
            if created is None or created >= cutoff:
                return True
        return False

    async def _asked_local_recently(self) -> bool:
        """O usuário pediu o navegador DELE numa das últimas mensagens desta conversa?

        Lê do banco pelo mesmo motivo de ``_inherits_local``, e por mais um: no passo
        de continuação do ciclo, ``user_message`` é a ordem que o próprio Neo
        sintetizou ("avisar o usuário quando…"), não a frase do usuário.
        """
        if self.session_id is None:
            return False
        try:
            msgs = await self.db.fetch_session_messages(
                self.session_id, limit=_LOCAL_ASK_LOOKBACK, roles=["user"]
            )
        except Exception as exc:  # noqa: BLE001 — sem histórico, o default é o servidor
            logger.warning("[browser-gate] histórico da conversa indisponível: %s", exc)
            return False
        from core.browser_local.gate import local_browser_requested

        cutoff = datetime.utcnow() - _LOCAL_ASK_WINDOW
        for msg in msgs or []:
            created = getattr(msg, "created_at", None)
            if created is not None and created < cutoff:
                continue
            if local_browser_requested(str(getattr(msg, "content", "") or "")):
                return True
        return False

    async def _browser_job_duplicado(self, task: str, start_url: Any = None) -> "AgentJob | None":
        """Job de browser do SERVIDOR, desta conversa, ainda vivo, com o mesmo endereço.

        Sem endereço nos dois lados, só a tarefa idêntica conta — "pesquise
        passagens" e "pesquise hotéis" são pedidos diferentes. Nunca levanta: sem
        banco, cria (o comportamento de antes).
        """
        if self.user_id is None or self.session_id is None:
            return None
        try:
            vivos = await self.db.list_agent_jobs(
                self.user_id, statuses=["queued", "running"], limit=20
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[browser] não consegui checar tarefa repetida: %s", exc)
            return None
        urls = _urls_of(task, start_url)
        alvo = " ".join(task.lower().split())
        cutoff = datetime.utcnow() - _BROWSER_DUP_WINDOW
        for job in vivos or []:
            if job.kind != "browser" or str(job.session_id) != str(self.session_id):
                continue
            if getattr(job, "status", None) not in ("queued", "running"):
                continue
            created = getattr(job, "created_at", None)
            if created is not None and created < cutoff:
                continue
            spec = job.spec_json or {}
            if urls:
                if urls & _urls_of(spec.get("task"), spec.get("start_url")):
                    return job
            elif " ".join(str(spec.get("task") or "").lower().split()) == alvo:
                return job
        return None

    async def _browser_local_em_curso(self) -> "AgentJob | None":
        """Job de navegador local ainda vivo NESTE device, se houver.

        O critério é o DEVICE e não a sessão: a aba reivindicada é uma só por
        máquina, então até um job de outra sessão do mesmo usuário atropelaria
        este. Nunca levanta — sem banco, o comportamento é o de antes (deixa
        criar), que é preferível a bloquear a tarefa por causa de uma consulta.
        """
        if self.user_id is None or not self.device_id:
            return None
        try:
            vivos = await self.db.list_agent_jobs(
                self.user_id, statuses=["queued", "running"], limit=20
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[browser-local] não consegui checar jobs em curso: %s", exc)
            return None
        for job in vivos:
            if job.kind != "browser_local":
                continue
            if str((job.spec_json or {}).get("device_id") or "") != str(self.device_id):
                continue
            return job
        return None

    async def _retomada(self, job_id: Any) -> dict[str, Any] | None:
        """Recria a tarefa que parou para perguntar. ``None`` = não é retomada.

        O que é herdado (e por quê):

        * a **tarefa original** — reescrever é o que estragou as tentativas
          anteriores; o texto que já tinha chegado no arquivo é o melhor que
          existe;
        * a **aba** — o agente continua de onde estava, e não em `about:blank`;
        * a **permissão do clique pendente** — e SÓ dele. Autorizar "excluir
          este arquivo" não pode virar um cheque em branco para comprar,
          enviar ou apagar qualquer outra coisa pelo resto da tarefa.
        """
        if not job_id:
            return None
        anterior = await self._get_owned(job_id)
        if anterior is None:
            return {"ok": False, "error": f"não achei o job {job_id} para continuar."}
        sessao = (anterior.extra_metadata or {}).get("browser_session") or {}
        pendente = str(sessao.get("pending") or "")
        if not pendente:
            return {
                "ok": False,
                "error": (
                    f"o job {job_id} não parou esperando confirmação — não há o que "
                    "continuar. Descreva a tarefa normalmente."
                ),
            }
        antigo = anterior.spec_json or {}
        spec: dict[str, Any] = {
            "task": antigo.get("task") or "",
            "device_id": self.device_id or antigo.get("device_id"),
            "context": antigo.get("context") or "",
            "max_steps": antigo.get("max_steps"),
            "resume_pending": pendente,
            "confirm_sensitive_ok": True,
        }
        if sessao.get("tab_ref"):
            spec["tab_ref"] = sessao["tab_ref"]
        spec = {k: v for k, v in spec.items() if v not in (None, "")}

        emcurso = await self._browser_local_em_curso()
        if emcurso is not None:
            return {
                "ok": True,
                "job_id": str(emcurso.id),
                "title": emcurso.title,
                "status": emcurso.status,
                "reused": True,
                "note": "Já existe uma tarefa rodando neste navegador — espere ela terminar.",
            }

        job = AgentJob(
            user_id=self.user_id,
            session_id=self.session_id,
            kind="browser_local",
            title=f"{anterior.title} (autorizado)"[:256],
            spec_json=spec,
            status="queued",
            notify_policy="chat",
        )
        job = await self.db.create_agent_job(job)
        try:
            self.jm.poke()
        except Exception:  # noqa: BLE001
            pass
        logger.info(
            "[browser-local] retomando %s na aba %s, autorizado a clicar em %r",
            anterior.id, spec.get("tab_ref"), pendente,
        )
        return {
            "ok": True,
            "job_id": str(job.id),
            "title": job.title,
            "status": "queued",
            "resumed_from": str(anterior.id),
            "note": (
                f'Retomando de onde parou, na mesma aba. Autorizado a clicar em '
                f'"{pendente}" — só nisso; qualquer outra ação que não desfaz vai '
                "parar e perguntar de novo."
            ),
        }

    async def _browser_local_task(self, args: dict[str, Any]) -> dict[str, Any]:
        """Cria agent_job(kind=browser_local) — o navegador do próprio usuário."""
        from shared.config import get_settings

        settings = get_settings()
        if not getattr(settings, "browser_local_enabled", False):
            return {"ok": False, "error": "o navegador local está desligado na config."}
        if not self.device_id:
            # Sem desktop (pedido veio pelo neo-panel, por exemplo) o navegador
            # local não existe — mas DESISTIR aqui era pior que o problema: o
            # trabalho simplesmente não acontecia. O navegador do servidor faz a
            # mesma coisa, só que de uma origem que parte da web recusa. Tentar
            # lá e dizer a verdade se apanhar é melhor que não tentar.
            logger.info("[browser-local] sem device — caindo para o navegador do servidor")
            resultado = await self._browser_task(args)
            if isinstance(resultado, dict) and resultado.get("ok"):
                resultado["note"] = (
                    "Feito pelo navegador do SERVIDOR: este canal não identifica "
                    "uma máquina do usuário, então o navegador local dele não "
                    "estava disponível. Se o site recusar a origem (403/CAPTCHA), "
                    "o caminho é o usuário pedir de novo pelo desktop."
                ) + (" " + str(resultado.get("note") or ""))
            return resultado
        # Retomada explícita: herda a tarefa, a aba e a permissão DO CLIQUE que
        # ficou pendente. Vem ANTES da validação de `task` porque numa retomada
        # o cérebro manda só o `continue_job` — a tarefa boa é a que já existe.
        # Reescrever é o que estragava: em 2026-09-01 cada nova tentativa
        # reinventava o texto, pior que a anterior, e as três morreram sem
        # carregar a página.
        retomada = await self._retomada(args.get("continue_job"))
        if retomada is not None:
            return retomada

        task = str(args.get("task") or "").strip()
        if not task:
            return {"ok": False, "error": "task vazia."}
        title = str(args.get("title") or "Tarefa no seu navegador").strip()[:256]
        # ── Com janela ou sem? ───────────────────────────────────────────────
        #
        # O portão (`_gate_browser`) existe porque TOMAR A TELA dele sem pedir é
        # a falha grave. O headless não toma tela nenhuma: roda na mesma máquina,
        # com o mesmo IP residencial, sem janela. Então a razão do portão
        # simplesmente não se aplica a ele.
        #
        # Daí a regra: com janela só quando o usuário quis VER; no resto, sem
        # janela. Isso dá o IP certo (que é o que destrava site brasileiro —
        # medido: o servidor sai por datacenter na França e leva 403/CAPTCHA)
        # sem o custo que fez o portão existir.
        from core.browser_local.gate import local_browser_requested

        quer_ver = local_browser_requested(self.user_message) or bool(
            args.get("visible")
        )
        headless = not quer_ver
        spec: dict[str, Any] = {
            "task": task,
            "device_id": self.device_id,
            "headless": headless,
        }
        context = str(args.get("context") or "").strip()
        if context:
            spec["context"] = context[:2000]
        start_url = str(args.get("start_url") or "").strip()
        if start_url.startswith(("http://", "https://")):
            spec["start_url"] = start_url[:500]
        try:
            spec["max_steps"] = max(3, min(int(args.get("max_steps") or settings.browser_local_max_steps), 60))
        except (TypeError, ValueError):
            spec["max_steps"] = settings.browser_local_max_steps
        # O "pode confirmar" do usuário para uma ação que não desfaz (§6.2). O
        # cérebro só põe isto quando o usuário disse explicitamente que pode.
        if bool(args.get("confirm_sensitive_ok")):
            spec["confirm_sensitive_ok"] = True

        # UMA tarefa por navegador de cada vez. Existe UMA aba reivindicada por
        # device: dois jobs em paralelo brigam por ela e o primeiro a terminar
        # solta a aba (`browser.release` → `detach`), derrubando o
        # `chrome.debugger` do outro no meio do passo — foi o
        # "Debugger unattached" do job 698446b8, que nasceu 17s antes de o
        # 7523d2e3 (que estava indo BEM) terminar.
        #
        # A duplicata veio do próprio cérebro: ele chamou `browser_local_task`,
        # viu `job_status: running`, viu `job_logs: (sem logs)` e concluiu que
        # não tinha pegado. Devolver o job existente — em vez de recusar — é o
        # que faz ele seguir esperando em vez de tentar outro caminho.
        emcurso = await self._browser_local_em_curso()
        if emcurso is not None:
            return {
                "ok": True,
                "job_id": str(emcurso.id),
                "title": emcurso.title,
                "status": emcurso.status,
                "reused": True,
                "note": (
                    "JÁ EXISTE uma tarefa sua rodando no navegador deste usuário — esta é "
                    "ela, não uma nova. Só existe UMA aba: disparar outra derruba a "
                    "primeira no meio. Espere o resultado chegar; ele vem sozinho no chat."
                ),
            }

        job = AgentJob(
            user_id=self.user_id,
            session_id=self.session_id,
            kind="browser_local",
            title=title,
            spec_json=spec,
            status="queued",
            notify_policy="chat",
        )
        job = await self.db.create_agent_job(job)
        try:
            self.jm.poke()
        except Exception:  # noqa: BLE001
            pass
        return {
            "ok": True,
            "job_id": str(job.id),
            "title": title,
            "status": "queued",
            "note": (
                "Tarefa enfileirada no navegador DO USUÁRIO — ele vai ver acontecendo na "
                "tela dele. Diga que ele pode acompanhar e que você avisa quando terminar."
            ),
        }

    async def _browser_screenshot(self, args: dict[str, Any]) -> dict[str, Any]:
        """Print AGORA da tela do browser → File Store → marcador para o chat.

        Síncrona de propósito (não é job): "me mostra o que tá aí" tem que ser
        respondido no mesmo turno, senão perde a graça. Devolve o MARCADOR pronto
        porque quem desenha é o cliente — o cérebro só precisa copiá-lo na resposta.
        """
        from shared.config import get_settings

        settings = get_settings()
        if not settings.browser_agent_enabled:
            return {"ok": False, "error": "o browser do servidor está desligado na config."}
        if self.file_store is None:
            return {
                "ok": False,
                "error": "o File Store está desligado — sem ele não dá para exibir o print no chat.",
            }
        session = str(args.get("session") or settings.browser_default_session).strip()
        session = "".join(c for c in session if c.isalnum() or c in "-_")[:64] or "user-default"

        import httpx

        from core.jobs.screenshots import ingest_shots

        base = settings.browser_agent_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10)) as client:
                resp = await client.post(f"{base}/sessions/{session}/screenshot")
        except httpx.ConnectError:
            return {"ok": False, "error": "o serviço de browser (neo-browser-agent) está fora do ar."}
        except httpx.TimeoutException:
            return {"ok": False, "error": "o browser não respondeu a tempo para o print."}
        if resp.status_code >= 400:
            return {"ok": False, "error": f"o browser recusou o print (HTTP {resp.status_code})."}
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "resposta ilegível do serviço de browser."}
        if not payload.get("ok"):
            return {"ok": False, "error": str(payload.get("error") or "não foi possível tirar o print.")}

        images = await ingest_shots(
            self.file_store,
            payload.get("screenshots"),
            user_id=self.user_id,
            session_id=self.session_id,
            label="browser-print",
            source={"session": session, "url": payload.get("current_url")},
        )
        markers = image_markers(images)
        if not markers:
            return {"ok": False, "error": "o print saiu mas não consegui guardá-lo para exibir."}
        return {
            "ok": True,
            "session": session,
            "current_url": payload.get("current_url"),
            "running": payload.get("running"),
            "markers": markers if self.can_show_images else [],
            "note": (
                "Print pronto. Escreva o(s) marcador(es) acima na sua resposta, em linha "
                "própria, para o usuário ver a imagem no chat."
                if self.can_show_images
                else (
                    "Print guardado no seu catálogo de arquivos, mas ESTE canal não "
                    "exibe imagem no chat: diga em que URL o browser está e o que "
                    "fazer a seguir, sem colar marcador nenhum."
                )
            ),
        }

    async def _deep_research(self, args: dict[str, Any]) -> dict[str, Any]:
        """Cria agent_job(kind=research) + research_task(queued) e volta na hora."""
        from shared.config import get_settings

        settings = get_settings()
        if not settings.research_enabled or not settings.web_search_enabled:
            return {"ok": False, "error": "deep_research está desligado na config."}

        topic = str(args.get("topic") or "").strip()
        if not topic:
            return {"ok": False, "error": "topic vazio."}
        instructions = str(args.get("instructions") or "").strip() or None
        depth = str(args.get("depth") or "standard").strip()
        if depth not in ("quick", "standard", "deep"):
            depth = "standard"

        from core.research.budgets import budget_for
        from db.models.research_task import ResearchTask

        budget = budget_for(depth)
        title = f"Pesquisa: {topic[:200]}"
        job = AgentJob(
            user_id=self.user_id,
            session_id=self.session_id,
            kind="research",
            title=title,
            spec_json={"topic": topic, "depth": depth},
            status="queued",
            notify_policy="chat",
        )
        job = await self.db.create_agent_job(job)
        try:
            task = ResearchTask(
                job_id=job.id,
                user_id=self.user_id,
                session_id=self.session_id,
                topic=topic,
                instructions=instructions,
                depth=depth,
                status="queued",
                model=settings.research_model,
            )
            task = await self.db.create_research_task(task)
        except Exception as e:  # noqa: BLE001 — sem a task o job não tem o que rodar
            await self.db.update_agent_job(
                job.id,
                status="failed",
                finished_at=datetime.utcnow(),
                result_summary=f"Falha ao criar a research_task: {e}",
                notified_at=datetime.utcnow(),
            )
            return {"ok": False, "error": f"não consegui criar a pesquisa: {e}"}

        # Amarra o job à task (o runner carrega por job_id; o id aqui é p/ debug).
        try:
            await self.db.update_agent_job(
                job.id, spec_json={**(job.spec_json or {}), "research_task_id": str(task.id)}
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            self.jm.poke()
        except Exception:  # noqa: BLE001
            pass
        eta = {"quick": "~3 minutos", "standard": "~10 minutos", "deep": "~25 minutos"}[depth]
        return {
            "ok": True,
            "job_id": str(job.id),
            "research_id": str(task.id),
            "depth": depth,
            "eta_hint": eta,
            "note": (
                "Pesquisa em background iniciada. Avise o usuário que você trará o "
                f"relatório quando terminar ({eta}); NÃO fique esperando."
            ),
        }

    async def _deep_read(self, args: dict[str, Any]) -> dict[str, Any]:
        """Leitura delegada — as conferências moram em ``core/jobs/deep_read_tool.py``."""
        from core.jobs.deep_read_tool import DeepReadEnqueuer

        return await DeepReadEnqueuer(
            db=self.db,
            job_manager=self.jm,
            user_id=self.user_id,
            session_id=self.session_id,
            file_store=self.file_store,
            user_message=self.user_message,
            disk_evidence=self.disk_evidence,
            criteria_checker=self.criteria_checker,
        ).enqueue(args)

    async def _status(self, args: dict[str, Any]) -> dict[str, Any]:
        job = await self._get_owned(args.get("job_id"))
        if job is None:
            return {"ok": False, "error": "job não encontrado."}
        return {
            "ok": True,
            "job_id": str(job.id),
            "title": job.title,
            "status": job.status,
            "exit_code": job.exit_code,
            "result_summary": (job.result_summary or "")[:2000],
            "created_at": _iso(job.created_at),
            "finished_at": _iso(job.finished_at),
            # Prints já guardados deste job: o usuário pode pedir para rever
            # ("mostra de novo aquele print") sem rodar nada.
            "image_markers": image_markers((job.extra_metadata or {}).get("images")),
        }

    async def _list(self, args: dict[str, Any]) -> dict[str, Any]:
        only_active = bool(args.get("only_active"))
        statuses = ["queued", "running"] if only_active else None
        jobs = await self.db.list_agent_jobs(
            user_id=self.user_id, statuses=statuses, limit=20
        )
        return {
            "ok": True,
            "jobs": [
                {
                    "job_id": str(j.id),
                    "title": j.title,
                    "status": j.status,
                    "created_at": _iso(j.created_at),
                }
                for j in jobs
            ],
        }

    async def _logs(self, args: dict[str, Any]) -> dict[str, Any]:
        job = await self._get_owned(args.get("job_id"))
        if job is None:
            return {"ok": False, "error": "job não encontrado."}
        tail = args.get("tail")
        max_bytes = None
        if tail is not None:
            try:
                max_bytes = max(100, int(tail))
            except (TypeError, ValueError):
                max_bytes = None
        lido = await read_job_logs(job, getattr(self.jm, "sandbox", None), max_bytes=max_bytes)
        return {
            "ok": True,
            "job_id": str(job.id),
            "status": job.status,
            "logs": lido["logs"],
            "truncated": lido["truncated"],
        }

    @staticmethod
    def _ler_diario_local(log_path: str, max_bytes: int | None) -> tuple[str, bool] | None:
        """Lê o diário do disco local. ``None`` = não é um caminho daqui.

        Devolve o FIM do arquivo, não o começo: um trace de navegador tem
        milhares de linhas e o que importa para "como está indo" são os últimos
        passos. Nunca levanta — log é diagnóstico, não pode derrubar a tool.
        """
        from pathlib import Path

        try:
            alvo = Path(log_path)
            if alvo.is_dir():
                alvo = alvo / "trace.md"
            if not alvo.is_file():
                return None
            teto = max_bytes or 4000
            tamanho = alvo.stat().st_size
            with alvo.open("rb") as fh:
                if tamanho > teto:
                    fh.seek(tamanho - teto)
                bruto = fh.read()
            texto = bruto.decode("utf-8", "replace")
            if tamanho > teto:
                # Um seek cego corta no meio de um caractere/linha; descartar a
                # primeira linha parcial evita entregar lixo ao modelo.
                texto = texto.split("\n", 1)[-1]
            return texto, tamanho > teto
        except Exception as exc:  # noqa: BLE001
            logger.warning("[jobs] não consegui ler o diário em %s: %s", log_path, exc)
            return None

    async def _cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        job = await self._get_owned(args.get("job_id"))
        if job is None:
            return {"ok": False, "error": "job não encontrado."}
        return await cancel_job(self.db, self.jm, job)

    async def _get_owned(self, job_id_raw: Any) -> AgentJob | None:
        if not job_id_raw:
            return None
        try:
            job_id = UUID(str(job_id_raw))
        except ValueError:
            return None
        job = await self.db.get_agent_job(job_id)
        if job is None:
            return None
        # Escopo: um usuário só enxerga os próprios jobs (jobs sem dono são globais).
        if job.user_id is not None and self.user_id is not None and job.user_id != self.user_id:
            return None
        return job


def _title_for_command(command: str) -> str:
    """Título legível para um comando adotado (a primeira linha, encurtada)."""
    primeira = " ".join(str(command or "").strip().splitlines()[0].split()) if command else ""
    if len(primeira) > 70:
        primeira = primeira[:67] + "..."
    return f"Comando longo: {primeira}" if primeira else "Comando longo"


async def read_job_logs(job: AgentJob, sandbox: Any, *, max_bytes: int | None = None) -> dict[str, Any]:
    """O log de um job, de onde ele mora — para a tool `job_logs` e a tela de tarefas.

    Diário que mora no DISCO do neo-core (o do navegador local:
    logs/browser_local/<job>/trace.md), não no sandbox. Sem este ramo, `job_logs`
    de um job de navegador lia `.jobs/<id>.log` no sandbox — que nunca existiu — e
    devolvia "(sem logs)" com a tarefa correndo bem. Foi essa resposta vazia que
    convenceu o cérebro de que nada estava acontecendo e o fez disparar uma
    SEGUNDA tarefa, que derrubou a primeira (job 698446b8: "Debugger unattached").

    Do sandbox vem o FIM do log (`read_tail`): num job longo, o começo é justamente
    a parte velha.
    """
    if not job.log_path:
        # Na fila, ou comando adotado ainda rodando (a saída dele só existe no fim).
        return {
            "logs": job.result_summary or job.progress_note or "(sem logs ainda)",
            "truncated": False,
            "source": "resumo",
        }
    local = JobToolExecutor._ler_diario_local(job.log_path, max_bytes)
    if local is not None:
        corpo, cortado = local
        return {
            "logs": corpo or "(o diário ainda está vazio — a tarefa acabou de começar)",
            "truncated": cortado,
            "source": "diario",
        }
    if sandbox is None:
        return {"logs": job.result_summary or "(sem logs)", "truncated": False, "source": "resumo"}
    res = await sandbox.read_tail(f".jobs/{job.id}.log", max_bytes=max_bytes)
    return {
        "logs": res.stdout if res.ok else (job.result_summary or "(sem logs)"),
        "truncated": bool(res.truncated),
        "source": "sandbox",
    }


async def cancel_job(db: Any, job_manager: Any, job: AgentJob) -> dict[str, Any]:
    """Cancela um job — a mesma regra para a tool `job_cancel` e a tela de tarefas."""
    if job.status in TERMINAL_STATUSES:
        return {"ok": False, "error": f"job já está {job.status}.", "status": job.status}
    await db.update_agent_job(
        job.id,
        status="cancelled",
        finished_at=datetime.utcnow(),
        result_summary=(job.result_summary or "Cancelado pelo usuário."),
    )
    # Job de research ainda na fila: o runner nunca vai rodar, então a fase
    # fina da research_task precisa ser fechada aqui (terminal ⇒ terminal).
    if job.kind == "research":
        try:
            task = await db.get_research_task_by_job(job.id)
            if task is not None and task.status not in ("completed", "failed", "timeout", "cancelled"):
                await db.update_research_task(
                    task.id, status="cancelled", finished_at=datetime.utcnow()
                )
        except Exception:  # noqa: BLE001
            pass
    # Aborta o worker em execução, se houver. Num sandbox_task (fila ou adotado),
    # cancelar a task é o que mata o processo no container.
    try:
        task = job_manager._active.get(str(job.id)) if job_manager is not None else None
        if task is not None:
            task.cancel()
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "job_id": str(job.id), "status": "cancelled"}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
