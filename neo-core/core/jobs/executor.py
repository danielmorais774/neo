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

# Com janela ou sem: a escolha é da CONVERSA, lida do banco (`_modo_da_sessao`).
# Os alcances (quantas mensagens/tarefas, quanto tempo vale um site) moram no
# `config/neo.toml`, seção [navegador.janela].

# As falas do Daniel que vão para o agente do navegador (`_falas_do_daniel`).
_FALAS_JANELA = timedelta(minutes=60)
_FALAS_LOOKBACK = 6
_FALAS_MAX = 1500

# Tarefa de browser no servidor repetida: mesmo endereço, mesma conversa, ainda na
# fila ou rodando.
_BROWSER_DUP_WINDOW = timedelta(minutes=20)
_URL_RE = re.compile(r"https?://[^\s'\"<>()\[\]{}]+", re.IGNORECASE)

_PALAVRAS_RE = re.compile(r"\w{3,}", re.UNICODE)


def _recado_para_tarefa_viva(fala: Any, task: str, context: str = "") -> str:
    """O que vai para a tarefa que está rodando: as palavras DELE, se houver.

    A tarefa reescrita pelo cérebro só quando não há fala dele no turno (a
    continuação automática, por exemplo).
    """
    fala = str(fala or "").strip()
    if fala:
        return fala
    recado = f"Nova instrução: {task}"
    if context:
        recado += f"\n(contexto: {context[:600]})"
    return recado


def _mesma_tarefa(nova: Any, rodando: Any) -> bool:
    """O pedido é a tarefa que JÁ está rodando, dita de novo?

    Sobreposição de palavras (3+ letras) de 80% ou mais. É o caso do cérebro que
    repete o `browser_local_task` achando que não pegou: virar "nova instrução"
    faria o agente recomeçar o que está fazendo. Pedido diferente — "abre o AppDemo
    também" — fica bem abaixo disso.
    """
    a = set(_PALAVRAS_RE.findall(str(nova or "").lower()))
    b = set(_PALAVRAS_RE.findall(str(rodando or "").lower()))
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= 0.8


#: Nomes que o cérebro inventa para o `task` quando chama a tool de navegador sem
#: o esquema na mesa. Sessão f6a0be50 (12/11): `browser_local_task {"tarefa": …}`
#: voltou "task vazia.", e ele concluiu que a extensão estava offline.
_APELIDOS_DO_TASK = ("tarefa", "instrucao", "instrucoes", "instrução", "instruções", "objetivo")


def _task_do_apelido(nome: str, args: dict[str, Any]) -> dict[str, Any]:
    """A tarefa escrita com outro nome de parâmetro vale como ``task``."""
    if nome not in ("browser_task", "browser_local_task") or str(args.get("task") or "").strip():
        return args
    for apelido in _APELIDOS_DO_TASK:
        valor = args.get(apelido)
        if isinstance(valor, str) and valor.strip():
            return {**args, "task": valor}
    return args


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


def _hosts_de(*texts: Any) -> set[str]:
    """Os hosts citados (sem ``www.``), pela mesma normalização de ``_urls_of``."""
    return {url.split("/", 1)[0].split("#", 1)[0] for url in _urls_of(*texts)} - {""}




def _job_com_janela(job: Any) -> bool:
    """A tarefa rodou (ou foi criada para rodar) no navegador DELE, com janela?

    Duas provas, qualquer uma: a aba que ela deixou é do navegador dele
    (`tab_ref` positivo; negativo é o headless) ou o spec nasceu sem headless.
    Job antigo sem nenhuma das duas não conta — não diz onde rodou.
    """
    sessao = (getattr(job, "extra_metadata", None) or {}).get("browser_session") or {}
    try:
        if int(sessao.get("tab_ref") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return (getattr(job, "spec_json", None) or {}).get("headless") is False


def _onde_roda(job: Any) -> dict[str, Any]:
    """Onde a tarefa do navegador local roda, dito com todas as letras ao cérebro.

    Sem isto ele adivinhava: na sessão 7bc7caa1 a tarefa rodava sem janela, de
    propósito, e o Neo culpou uma extensão pareada que estava offline (que nem é
    o caminho do navegador local — ele passa pelo desktop).
    """
    spec = getattr(job, "spec_json", None) or {}
    sessao = (getattr(job, "extra_metadata", None) or {}).get("browser_session") or {}
    try:
        aba = int(sessao.get("tab_ref") or 0)
    except (TypeError, ValueError):
        aba = 0
    sem_janela = aba < 0 or (aba == 0 and spec.get("headless") is True)
    info: dict[str, Any] = {
        "janela": not sem_janela,
        "onde": (
            "SEM JANELA (headless) na máquina do Daniel: roda de verdade, mas ele não "
            "vê aba nenhuma. Não é falha de conexão nem de extensão."
            if sem_janela
            else "no navegador do Daniel, com janela: ele vê a aba."
        ),
    }
    if spec.get("motivo_janela"):
        info["por_que"] = str(spec["motivo_janela"])
    return info


def _sem_fuso(quando: Any) -> datetime | None:
    """Datas do banco são UTC sem fuso; uma com fuso não pode quebrar a comparação."""
    if not isinstance(quando, datetime):
        return None
    return quando.replace(tzinfo=None) if quando.tzinfo is not None else quando


def _fala_do_sistema(texto: str, meta: dict | None) -> bool:
    """Mensagem gravada como `user` que NÃO é dele: continuação automática, agenda."""
    return texto.startswith("[Continuação automática") or (meta or {}).get("source") == "schedule"

#: Tarefa do Claude que acabou e ainda aceita AJUSTE pelo `claude_reply` — a
#: mesma sessão volta, na mesma pasta (`continued` = já seguiu; vai à ponta).
_CLAUDE_TERMINADA = frozenset({"completed", "failed", "timeout", "cancelled", "continued"})
_ESTADO_DA_TAREFA = {
    "completed": "concluída",
    "failed": "falhou",
    "timeout": "passou do tempo",
    "cancelled": "cancelada",
    "continued": "seguiu em outra rodada",
}
_URL_DE_LOGIN = re.compile(
    r"(?:^|[/#!?._-])(?:login|signin|sign-in|logon|entrar|auth)(?:$|[/?#._-])", re.IGNORECASE
)


_CLAUDE_DESLIGADO = "o Claude Code integrado está desligado no neo-panel (Configurações → Claude Code)."

#: Vai no fim de todo pedido entregue numa sessão do Daniel. Do outro lado ele
#: chega como "mensagem de outra sessão, não digitada pelo seu usuário" (F0), e
#: sem o pedido de não responder a sessão tenta responder ao mensageiro, que já
#: saiu, e perde ~10 s num erro de socket (F0, docs/ambiente-de-trabalho-plan.md §3).
_RODAPE_DO_PEDIDO = (
    "(Pedido do Daniel, enviado pelo Neo, o assistente dele. Não responda por mensagem: "
    "responda aqui mesmo.)"
)

_ORIGEM_PT = {"claude-vscode": "VSCode", "cli": "terminal", "sdk-cli": "sem janela (claude -p)"}
_ESTADO_DA_SESSAO_PT = {"busy": "aberta, trabalhando agora", "idle": "aberta", "fechada": "fechada"}


def _ha_quanto(minutos: Any) -> str:
    try:
        m = int(minutos)
    except (TypeError, ValueError):
        return ""
    if m < 2:
        return "agora"
    if m < 60:
        return f"há {m} min"
    if m < 48 * 60:
        return f"há {m // 60} h"
    return f"há {m // (24 * 60)} dias"


def _ficha_para_o_cerebro(s: dict[str, Any]) -> dict[str, Any]:
    """A ficha que o Mac montou, sem o que só serve a ele (nome de mensagem, flags)."""
    ficha = {
        "sessionId": s.get("sessionId"),
        "titulo": s.get("titulo") or "(sem título)",
        "onde": _ORIGEM_PT.get(str(s.get("origem") or ""), str(s.get("origem") or "")),
        "estado": _ESTADO_DA_SESSAO_PT.get(str(s.get("status") or ""), str(s.get("status") or "")),
        "usada": _ha_quanto(s.get("ultimo_uso_min")),
        "pasta": s.get("pasta") or "",
        "objetivo": s.get("objetivo") or "",
        "ultimos_pedidos": s.get("ultimos_pedidos") or [],
        "ultima_resposta": s.get("ultima_resposta") or "",
    }
    if s.get("arquivos"):
        ficha["arquivos"] = s["arquivos"]
    return ficha


def _autoridade_do_turno() -> dict[str, Any] | None:
    """O que a autoridade do turno diz sobre quem pediu — serializável, para o spec.

    ``None`` fora de turno (agenda sem autoridade instalada, teste): aí o pedido
    que o job vier a fazer sai como do Daniel, que é o comportamento de sempre.
    """
    try:
        from core.authority import current_authority

        autoridade = current_authority()
    except Exception:  # noqa: BLE001
        return None
    if autoridade is None:
        return None
    return {
        "principal": autoridade.principal,
        "principal_ref": autoridade.principal_ref,
        "tainted": bool(autoridade.tainted),
        "taint_sources": list(autoridade.taint_sources or [])[:5],
    }


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
        args = _task_do_apelido(tool_call.name, tool_call.arguments or {})
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
            elif name == "claude_task":
                result = await self._claude_task(args)
            elif name == "claude_reply":
                result = await self._claude_reply(args)
            elif name == "claude_sessoes":
                result = await self._claude_sessoes(args)
            elif name == "claude_enviar":
                result = await self._claude_enviar(args)
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
            elif name == "job_falar":
                result = await self._job_falar(args)
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
            return {"ok": False, "error": "task vazia: a tarefa vai no parâmetro `task`, por extenso."}
        # O navegador do SERVIDOR não tem preenchimento escopado: um `«pks:cep»`
        # seria digitado como texto e enviado assim. Dado pessoal em formulário é
        # trabalho do navegador dele, que troca o marcador na hora de digitar e
        # pede a liberação sozinho.
        from core.release.fill import campos_do_texto

        marcados = campos_do_texto(f"{task}\n{args.get('context') or ''}")
        if marcados:
            return {
                "ok": False,
                "error": (
                    "o navegador do servidor não preenche dado pessoal: ele digitaria "
                    f"{', '.join(f'«pks:{c}»' for c in marcados)} como texto. Mande a "
                    "MESMA tarefa pelo `browser_local_task` — é ele que troca o marcador "
                    "pelo valor e, sem liberação, para no campo, pede ao Daniel e "
                    "continua sozinho quando ele aprovar."
                ),
            }
        # Mesma trava do local: o navegador do servidor digita em formulário de
        # terceiro igual, e sair pela porta de fora não torna o documento menos
        # inventado.
        recusa = self._recusar_dado_literal(
            task,
            str(args.get("context") or ""),
            str(args.get("start_url") or ""),
        )
        if recusa:
            return recusa
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
        navegador" sobre um job no servidor.

        **A escolha gruda na conversa** (sessão 7bc7caa1, pedido do Daniel): se
        ela já trabalhou no navegador dele com janela — um pedido dele ou uma
        tarefa que rodou lá —, todo pedido de navegador vai para lá, inclusive o
        que o cérebro mandou para o servidor. Quem decide é o banco
        (``_modo_da_sessao``), não o modelo; a saída é ele dizer "sem janela".

        Quem promove sem pedido na frase é um JOB local desta conversa, recente
        (sessão b230a40d): "ainda tá lá, dá pra continuar" depois de um quiz que
        rodava na aba dele foi parar no ``browser_task``, que não tem a aba nem o
        progresso. Aí o servidor não é uma escolha, é perder o trabalho; e o local
        decide a janela pelas mesmas regras de sempre (``_browser_local_task``).

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
            modo, motivo_modo = await self._modo_da_sessao()
            if modo == "janela":
                logger.info(
                    "[browser-gate] local mantido: a conversa trabalha no navegador dele (%s)",
                    motivo_modo,
                )
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
        if name == "browser_task" and self.device_id:
            modo, motivo_modo = await self._modo_da_sessao()
            if modo == "janela":
                logger.info(
                    "[browser-gate] promovido para browser_local_task "
                    "(a conversa trabalha no navegador dele, com janela: %s)",
                    motivo_modo,
                )
                return "browser_local_task"
        if name == "browser_task" and self.device_id and await self._inherits_local():
            logger.info(
                "[browser-gate] promovido para browser_local_task "
                "(continuação de tarefa local desta conversa)"
            )
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

    async def _falas_do_daniel(self) -> str:
        """As últimas falas do Daniel nesta conversa, em ordem, nas palavras dele.

        Só mensagens DELE: o "[Continuação automática…]" que o Neo grava como
        user fica de fora. Janela e quantidade são de propósito curtas — é o
        pedido em andamento, não a conversa inteira.
        """
        atual = (self.user_message or "").strip()
        if self.session_id is None:
            return atual[:_FALAS_MAX]
        try:
            msgs = await self.db.fetch_session_messages(
                self.session_id, limit=_FALAS_LOOKBACK, roles=["user"]
            )
        except Exception as exc:  # noqa: BLE001 — sem histórico, a fala do turno
            logger.debug("[browser-local] histórico indisponível para as falas: %s", exc)
            return atual[:_FALAS_MAX]
        cutoff = datetime.utcnow() - _FALAS_JANELA
        falas: list[str] = []
        for msg in msgs or []:
            created = getattr(msg, "created_at", None)
            if created is not None and created < cutoff:
                continue
            meta = getattr(msg, "extra_metadata", None) or {}
            texto = str(getattr(msg, "content", "") or "").strip()
            if not texto or meta.get("source") == "schedule" or texto.startswith("[Continuação automática"):
                continue
            if texto not in falas:
                falas.append(texto)
        if atual and atual not in falas:
            falas.append(atual)
        # As mais recentes valem mais e cabem primeiro: corta pelo começo.
        linhas = [f"- {f}" for f in falas]
        while linhas and len("\n".join(linhas)) > _FALAS_MAX:
            linhas.pop(0)
        return "\n".join(linhas)

    async def _modo_da_sessao(self) -> tuple[str | None, str]:
        """Com janela ou sem? A ÚLTIMA escolha desta conversa, lida do banco.

        Devolve ``("janela" | "sem_janela" | None, motivo)``. Quem decide é o
        código, nunca o modelo: ele troca de ideia entre um turno e outro
        (sessão 7bc7caa1: o Daniel pediu "no meu navegador local, quero ver
        acontecendo", respondeu uma pergunta do Neo 17 minutos depois e a tarefa
        foi para o headless — a janela de 10 minutos do pedido tinha passado, e
        o cérebro escreveu "com janela visível" só na prosa da tarefa).

        Os sinais, o mais recente vence:

        * mensagem DELE pedindo o navegador dele (``local_browser_requested``) → janela;
        * mensagem dele pedindo sem janela (``headless_requested``) → sem janela;
        * tarefa desta conversa que rodou com janela (``_job_com_janela``) → janela.

        Tarefa headless NÃO é sinal: ela é o padrão de quando ninguém escolheu,
        e contar com ela faria um headless errado se perpetuar. Sem prazo: a
        escolha vale pela conversa inteira, até ele dizer o contrário ou abrir um
        chat novo. A frase do turno vem primeiro (no passo de continuação do
        ciclo, ``user_message`` é a ordem que o Neo sintetizou — por isso a fala
        do sistema é pulada). Nunca levanta: sem banco, sem escolha.
        """
        from core.browser_local.gate import headless_requested, local_browser_requested

        atual = (self.user_message or "").strip()
        if atual and not _fala_do_sistema(atual, None):
            if headless_requested(atual):
                return "sem_janela", "ele pediu sem janela nesta mensagem"
            if local_browser_requested(atual):
                return "janela", "ele pediu o navegador dele nesta mensagem"
        if self.session_id is None:
            return None, ""
        from shared.ajustes import ajustes

        cfg = ajustes().navegador.janela
        sinais: list[tuple[datetime, str, str]] = []
        try:
            msgs = await self.db.fetch_session_messages(
                self.session_id, limit=cfg.mensagens_lidas, roles=["user"]
            )
        except Exception as exc:  # noqa: BLE001 — sem histórico, sem escolha
            logger.debug("[browser-gate] histórico da conversa indisponível: %s", exc)
            msgs = []
        for msg in msgs or []:
            texto = str(getattr(msg, "content", "") or "").strip()
            quando = _sem_fuso(getattr(msg, "created_at", None))
            if not texto or quando is None or _fala_do_sistema(
                texto, getattr(msg, "extra_metadata", None)
            ):
                continue
            if headless_requested(texto):
                sinais.append((quando, "sem_janela", f'ele pediu sem janela: "{texto[:60]}"'))
            elif local_browser_requested(texto):
                sinais.append((quando, "janela", f'ele pediu o navegador dele: "{texto[:60]}"'))
        if self.user_id is not None:
            try:
                jobs = await self.db.list_agent_jobs(self.user_id, limit=cfg.tarefas_lidas)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[browser-gate] histórico de jobs indisponível: %s", exc)
                jobs = []
            for job in jobs or []:
                if getattr(job, "kind", None) != "browser_local":
                    continue
                if str(getattr(job, "session_id", None)) != str(self.session_id):
                    continue
                quando = _sem_fuso(getattr(job, "created_at", None))
                if quando is not None and _job_com_janela(job):
                    titulo = str(getattr(job, "title", "") or "")[:60]
                    sinais.append((quando, "janela", f'tarefa com janela nesta conversa: "{titulo}"'))
        if not sinais:
            return None, ""
        _, modo, motivo = max(sinais, key=lambda sinal: sinal[0])
        return modo, motivo

    async def _trabalho_no_navegador_dele(self, start_url: str, texto: str) -> str | None:
        """O trabalho NESTE site está no navegador dele? Devolve o motivo.

        O headless é outro navegador: não tem o login dele, nem a aba em que ele
        está, nem o que ele acabou de fazer na página. Na sessão cb41834a (Portal)
        três de cinco tarefas foram para lá porque a mensagem não dizia "no meu
        navegador". Na 7bc7caa1 (Google Forms do AppDemo), o formulário tinha sido
        preenchido até a página 8 no Edge dele, em OUTRA conversa, quatro horas
        antes: o headless abriu a página 1 em branco, sem o login do Google.

        A última tarefa no MESMO site decide, de qualquer conversa dele, dentro
        de `site_vale_por_horas` (neo.toml): deixou aba no navegador dele
        (`tab_ref` positivo) → com janela; a do headless parou numa tela de
        login → com janela; a do headless TERMINOU (`completed`) → segue sem; a
        do headless que falhou ou foi cancelada não prova nada e é pulada. Da mesma
        conversa, uma tarefa sem endereço também conta. Tarefa que morreu sem
        abrir aba não diz onde o trabalho está e é pulada. Nunca levanta.
        """
        if self.user_id is None:
            return None
        from shared.ajustes import ajustes

        cfg = ajustes().navegador.janela
        try:
            recentes = await self.db.list_agent_jobs(self.user_id, limit=cfg.tarefas_lidas)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[browser-local] histórico de jobs indisponível: %s", exc)
            return None
        alvos = _hosts_de(start_url, texto)
        agora = datetime.utcnow()
        vale_por = timedelta(hours=float(cfg.site_vale_por_horas))
        for job in recentes:
            if getattr(job, "kind", None) != "browser_local":
                continue
            mesma = self.session_id is not None and str(getattr(job, "session_id", None)) == str(
                self.session_id
            )
            quando = _sem_fuso(getattr(job, "finished_at", None) or getattr(job, "created_at", None))
            idade = (agora - quando) if quando is not None else timedelta(0)
            if idade > vale_por:
                break
            sessao = (getattr(job, "extra_metadata", None) or {}).get("browser_session") or {}
            try:
                aba = int(sessao.get("tab_ref") or 0)
            except (TypeError, ValueError):
                aba = 0
            if not aba:
                continue
            url = str(sessao.get("url") or (getattr(job, "spec_json", None) or {}).get("start_url") or "")
            hosts = _hosts_de(url)
            if mesma:
                if alvos and hosts and not (alvos & hosts):
                    continue
            elif not (alvos and hosts and (alvos & hosts)):
                # Outra conversa só fala pelo MESMO site, com endereço dos dois lados.
                continue
            site = next(iter(hosts), "este site")
            onde = "desta conversa" if mesma else "de outra conversa"
            if aba > 0:
                return f"a última tarefa {onde} em {site} foi no navegador dele"
            if _URL_DE_LOGIN.search(url) or "login" in str(getattr(job, "result_summary", "") or "").lower():
                return f"o navegador sem janela parou no login de {site}"
            if getattr(job, "status", None) != "completed":
                # Sem janela que falhou ou foi cancelada não prova que o modo sem
                # janela serve aqui — e esconderia o trabalho no navegador dele
                # (7bc7caa1: o headless errado, cancelado, era o mais recente no
                # Forms e tapava três tarefas com janela). Olha a anterior.
                continue
            return None
        return None

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

    def _tarefa_viva(self, job_id: Any = None) -> Any:
        """A caixa de entrada da tarefa de navegador VIVA deste turno, ou None.

        Pelo id quando vier (e for do mesmo usuário); senão a desta conversa; senão
        a deste navegador. Ver ``core/browser_local/conversa.py``.
        """
        from core.browser_local import conversa as conversas

        if job_id:
            alvo = conversas.viva(job_id)
            if alvo is not None and str(alvo.user_id or "") == str(self.user_id or ""):
                return alvo
        return conversas.da_sessao(self.session_id) or conversas.do_device(self.device_id)

    async def _job_falar(self, args: dict[str, Any]) -> dict[str, Any]:
        """Recado para a tarefa que está rodando no navegador (docs/browser-conversa-plan.md §4.5)."""
        texto = str(args.get("mensagem") or "").strip()
        if not texto:
            return {"ok": False, "error": "mensagem vazia — escreva o recado para a tarefa."}
        alvo = self._tarefa_viva(str(args.get("job_id") or "").strip() or None)
        if alvo is None or str(alvo.user_id or "") != str(self.user_id or ""):
            return {
                "ok": False,
                "error": (
                    "nenhuma tarefa está rodando no navegador agora — o recado só chega "
                    "enquanto ela roda. Se ela PAROU esperando (waiting), continue com "
                    "`browser_local_task`: `continue_job` para uma autorização, ou a tarefa "
                    "de novo com a resposta dele no `context` (a aba continua onde ela parou)."
                ),
            }
        esperava = alvo.pergunta.texto if alvo.pergunta is not None else ""
        alvo.entregar(texto, "chat", autoriza=bool(args.get("autoriza")))
        logger.info("[browser-local] recado do chat para o job %s: %s", alvo.job_id, texto[:120])
        return {
            "ok": True,
            "job_id": alvo.job_id,
            "entregue": True,
            **({"respondeu_a": esperava} if esperava else {}),
            "note": (
                "Recado entregue à tarefa no navegador: ela lê no próximo passo e segue dali, "
                "sem recomeçar nem fechar aba. Diga isso ao Daniel em UMA frase e encerre o "
                "turno — o que ela fizer chega sozinho no chat."
            ),
        }

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
        if not pendente and sessao.get("release_pending"):
            return await self._retomada_por_liberacao(anterior, str(sessao["release_pending"]))
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
            "pedido": antigo.get("pedido"),
            "falas": antigo.get("falas"),
            "autoridade": antigo.get("autoridade"),
        }
        # Parou para o Daniel CONFERIR o formulário: o que ele viu é o que vai.
        # Sem isto a retomada consultaria a memória de novo, e uma segunda
        # redação poderia trocar o texto que ele aprovou.
        if sessao.get("respostas_memoria"):
            spec["respostas_memoria"] = sessao["respostas_memoria"]
        # E as abas que ela tinha abertas: a retomada continua com o mesmo conjunto.
        if sessao.get("abas"):
            spec["abas"] = sessao["abas"]
        if sessao.get("tab_ref"):
            spec["tab_ref"] = sessao["tab_ref"]
        # Se a aba sumiu, é este endereço que se reabre — e não about:blank.
        spec["reabrir_url"] = sessao.get("url") or ""
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
        # A parada deixou de esperar: na janela de tarefas ela some de "esperando
        # você" e aponta para quem continuou.
        try:
            await self.db.update_agent_job(anterior.id, status="continued")
        except Exception as exc:  # noqa: BLE001 — a continuação já existe
            logger.warning("[browser-local] não marquei %s como continuada: %s", anterior.id, exc)
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

    async def _retomada_por_liberacao(
        self, anterior: AgentJob, release_id: str
    ) -> dict[str, Any]:
        """``continue_job`` de uma tarefa que parou esperando LIBERAÇÃO de dado.

        O caminho normal nem passa por aqui: aprovar o card já continua a tarefa
        sozinho (``core/jobs/release_resume.py``). Isto cobre o resto — o
        navegador estava ocupado na hora da aprovação, o core reiniciou — e
        principalmente diz a verdade quando a aprovação NÃO aconteceu: "sim" no
        chat não aprova nada, e continuar sem liberação só pararia de novo no
        mesmo campo.
        """
        from core.jobs.release_resume import continuar, navegador_ocupado
        from core.release import ReleaseService

        try:
            row = await ReleaseService(self.db).get(release_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"não consegui ler a liberação {release_id}: {exc}"}
        if row is None:
            return {"ok": False, "error": f"a liberação {release_id} não existe mais."}
        sessao = (anterior.extra_metadata or {}).get("browser_session") or {}
        if sessao.get("release_resumed"):
            return {
                "ok": True,
                "job_id": str(sessao["release_resumed"]),
                "status": "queued",
                "reused": True,
                "note": (
                    "Essa tarefa JÁ continuou quando o Daniel aprovou — é este job. "
                    "O resultado chega sozinho no chat."
                ),
            }
        if row.status == "pendente":
            return {
                "ok": False,
                "error": (
                    "a liberação ainda NÃO foi aprovada. O aviso está nos aparelhos do "
                    "Daniel — é lá que ele aprova (o \"sim\" no chat não vale como "
                    "aprovação). Quando ele aprovar, a tarefa continua sozinha; não "
                    "chame solicitar_dado de novo."
                ),
            }
        if row.status != "aprovada":
            return {
                "ok": False,
                "error": (
                    f"a liberação está '{row.status}' — não há o que continuar com ela. "
                    "Se ele ainda quer preencher, mande a tarefa de novo com os «pks:…»: "
                    "ela para no campo e pede outra liberação."
                ),
            }
        device = str(sessao.get("device_id") or self.device_id or "")
        ocupado = await navegador_ocupado(self.db, self.user_id, device)
        if ocupado is not None:
            return {
                "ok": True,
                "job_id": str(ocupado.id),
                "title": ocupado.title,
                "status": ocupado.status,
                "reused": True,
                "note": "Já existe uma tarefa rodando neste navegador — espere ela terminar.",
            }
        novo = await continuar(self.db, self.jm, anterior, row)
        return {
            "ok": True,
            "job_id": str(novo.id),
            "title": novo.title,
            "status": "queued",
            "resumed_from": str(anterior.id),
            "note": "Continuando de onde parou, na mesma aba, com o que o Daniel liberou.",
        }

    @staticmethod
    def _recusar_dado_literal(
        task: str, context: str = "", start_url: str = ""
    ) -> dict[str, Any] | None:
        """Documento escrito à mão dentro da tarefa do navegador. ``None`` = ok.

        A trava de preenchimento mora no driver e olha cada `fill`. Ela não vê,
        porém, o dado que chega UMA CAMADA ACIMA: na sessão e0905aae o cérebro
        escreveu "preencha o CPF do destinatário com 00000000000 (se o campo
        validar CPF e recusar esse valor, use 11144477735)" dentro do próprio
        `task`. Para o agente do navegador aquilo não é mais dado pessoal a
        buscar — é parâmetro recebido. A decisão sobre documento acabou tomada
        por quem não tem como pedir aprovação, e com um número inventado.

        Recusar aqui é barato e honesto: o cérebro ainda não gastou o navegador
        do usuário, e a frase já diz o caminho certo.

        **Um veredito por tarefa, não um por rodada** (consertado em 2026-10-18).
        Antes isto nomeava só o que a FORMA do número denunciava, e entregava uma
        recusa de cada vez. No teste do §24 a tarefa trazia CEP *e* CPF; saiu "a
        tarefa traz CEP escrito por extenso", e o CPF — ``00000000000``, cujo
        dígito verificador não fecha — nunca chegou a ser visto. O Daniel leu no
        chat que o CPF tinha sido barrado, mas a tabela de necessidade não havia
        rodado nele uma única vez: o bloqueio era retórica do modelo, repetindo o
        exemplo que a própria frase de recusa dava. Agora a varredura acha todos
        os campos, a tabela julga todos, e a recusa volta com as duas listas
        prontas — o que vai por «pks:…», e o que não se pede.

        **Quem pede a liberação é a TAREFA, não o cérebro** (2026-11-01). A frase
        mandava chamar ``solicitar_dado`` antes de despachar; na sessão 1a605544
        quem leu foi o agente do SERVER_TOOL, que não tinha a tool, e ele
        respondeu "ficou esperando aprovação" sem pedido nenhum. Agora o job do
        navegador para no campo, cria o card e continua sozinho depois da
        aprovação — então o caminho certo cabe numa frase que não depende de
        tool nenhuma: reescrever com os marcadores e despachar.
        """
        try:
            from core.release.fill import host_of
            from core.release.necessity import (
                campos_desnecessarios,
                explicar,
                finalidade_conhecida,
            )
            from core.release.sensitive import campos_pessoais_no_texto, humano
        except Exception:  # noqa: BLE001 — a trava nunca derruba o turno
            return None
        achados = campos_pessoais_no_texto(f"{task}\n{context}")
        if not achados:
            return None

        # A finalidade sai da TAREFA, não do contexto: é a tarefa que diz o que
        # se foi fazer ali. Finalidade que a tabela não conhece não reprova nada
        # (necessity.py) — aí tudo vira card e quem julga é o Daniel.
        finalidade = finalidade_conhecida(task) or ""
        barrados = campos_desnecessarios(finalidade, achados) if finalidade else []
        pedir = [c for c in achados if c not in barrados]

        destino = host_of(start_url)
        linhas = [
            "Não despachei: a tarefa traz dado pessoal escrito por extenso "
            f"({', '.join(humano(c) for c in achados)}). Dado pessoal não vai no "
            "texto da tarefa — nem o real do Daniel, nem um que você componha para "
            "destravar o formulário (um CPF inventado cadastra outra pessoa)."
        ]
        if pedir:
            marcas = ", ".join(f"«pks:{c}»" for c in pedir)
            onde = f" para {destino}" if destino else ""
            linhas.append(
                "Reescreva a tarefa SEM valor nenhum e despache pelo "
                "`browser_local_task` (o navegador dele — o do servidor não troca o "
                f"marcador), mandando preencher cada campo com {marcas}. O valor real "
                "entra na hora de digitar, e só no site liberado. Se ainda não houver "
                f"liberação, a própria tarefa para no campo, manda o pedido ao Daniel{onde} "
                "e CONTINUA SOZINHA quando ele aprovar. Não chame `solicitar_dado` para "
                "isso e não diga que já pediu: quem pede é a tarefa."
            )
        if barrados:
            linhas.append(
                explicar(finalidade, barrados).strip()
                or f"Não peça liberação de {', '.join(humano(c) for c in barrados)}."
            )
            linhas.append(
                "Conte ao Daniel o que você barrou e por quê — a página pediu, não "
                "foi ele."
            )
        logger.info(
            "[release] tarefa de navegador recusada: literal=%s pedir=%s barrado=%s",
            ",".join(achados), ",".join(pedir) or "-", ",".join(barrados) or "-",
        )
        return {"ok": False, "error": " ".join(linhas)}

    async def _browser_local_task(self, args: dict[str, Any]) -> dict[str, Any]:
        """Cria agent_job(kind=browser_local) — o navegador do próprio usuário."""
        from shared.config import get_settings

        settings = get_settings()
        if not getattr(settings, "browser_local_enabled", False):
            return {"ok": False, "error": "o navegador local está desligado na config."}
        # ── Em qual navegador? ───────────────────────────────────────────────
        #
        # Antes esta pergunta era "tem desktop neste canal?", e um pedido vindo do
        # painel/celular **degradava em silêncio** para o navegador do SERVIDOR —
        # que sai por datacenter e leva 403/CAPTCHA em loja brasileira. O usuário
        # recebia "o site recusou a origem" sem saber que o navegador dele estava
        # aberto do outro lado.
        #
        # Agora a pergunta é "consigo resolver uma superfície?"
        # (docs/browser-surface-plan.md §5): o navegador pode ser o desta máquina
        # (herdado do canal) ou o de outra, citado por nome. A queda para o
        # servidor continua existindo — só deixou de ser o primeiro recurso.
        # A mudança é ADITIVA por construção: ela só pode ACRESCENTAR um caminho
        # (a superfície de outra máquina), nunca remover o que já funciona. Por
        # isso, resolução que falha com um device no canal cai **nesse device** —
        # cair no servidor aí seria trocar o navegador dele pelo do datacenter,
        # que é exatamente a regressão que este plano existe para evitar.
        superficie = None
        alvo_id = self.device_id
        provider = None
        try:
            from core.browser_local.surfaces import (
                PROVIDER_DESKTOP,
                resolve_browser_surface,
            )
            from modules.devices.control import DeviceResolutionError

            superficie = await resolve_browser_surface(
                self.db,
                self.user_id,
                str(args.get("browser") or args.get("device") or "").strip() or None,
                turn_device_id=self.device_id,
            )
            alvo_id = superficie.device_id
            provider = superficie.provider
        except DeviceResolutionError as erro:
            # Ambiguidade PERGUNTA — abrir a aba errada na tela dele é pior que
            # perguntar. Isso vale mesmo havendo device no canal: o usuário NOMEOU
            # um navegador, e chutar outro é desobedecer.
            if erro.candidates:
                return {
                    "ok": False,
                    "error": erro.message,
                    "candidates": erro.candidates,
                }
            logger.info("[browser-local] sem superfície: %s", erro.message)
        except Exception as exc:  # noqa: BLE001 — resolução nunca derruba o turno
            logger.warning("[browser-local] resolução indisponível: %s", exc)

        if superficie is None and alvo_id:
            logger.info(
                "[browser-local] usando o device do canal (%s) sem resolver superfície",
                alvo_id,
            )

        if superficie is None and not alvo_id:
            # Formulário com dado pessoal não cai para o servidor: lá não há
            # preenchimento escopado, e a recusa de `_browser_task` mandaria
            # voltar para cá — um laço sem saída.
            from core.release.fill import campos_do_texto

            if campos_do_texto(f"{args.get('task') or ''}\n{args.get('context') or ''}"):
                return {
                    "ok": False,
                    "error": (
                        "não há navegador do Daniel disponível agora (desktop fechado "
                        "ou extensão desligada), e dado pessoal só entra pelo navegador "
                        "dele. Diga isso a ele: com o navegador aberto, é só pedir de novo."
                    ),
                }
            resultado = await self._browser_task(args)
            if isinstance(resultado, dict) and resultado.get("ok"):
                resultado["note"] = (
                    "Feito pelo navegador do SERVIDOR: não havia navegador do "
                    "usuário disponível (nem nesta máquina, nem pareado em outra). "
                    "Se o site recusar a origem (403/CAPTCHA), o caminho é abrir o "
                    "navegador com a extensão do Neo, ou pedir pelo desktop."
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
            return {"ok": False, "error": "task vazia: a tarefa vai no parâmetro `task`, por extenso."}
        recusa = self._recusar_dado_literal(
            task,
            str(args.get("context") or ""),
            str(args.get("start_url") or ""),
        )
        if recusa:
            return recusa
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
        #
        # "Quis ver" é decidido AQUI, pelo banco, na ordem abaixo — o `visible`
        # do cérebro só acrescenta, nunca tira (ele troca de ideia de um turno
        # para o outro: na sessão 7bc7caa1 escreveu "com janela visível" na
        # prosa da tarefa e não no parâmetro, e ela foi para o headless).
        #   1. a escolha da conversa (`_modo_da_sessao`): a frase do turno, o
        #      último pedido dele ou a última tarefa com janela — sem prazo;
        #      "sem janela" dito por ele ganha de tudo, inclusive do `visible`;
        #   2. o `visible` do cérebro;
        #   3. onde está o trabalho neste site (`_trabalho_no_navegador_dele`).
        modo, motivo_modo = await self._modo_da_sessao()
        if modo == "sem_janela":
            headless = True
        elif modo == "janela":
            headless = False
        elif args.get("visible"):
            headless = False
            motivo_modo = "o cérebro pediu com janela"
        else:
            headless = True
            motivo_modo = "ninguém pediu janela"
            motivo_site = await self._trabalho_no_navegador_dele(
                str(args.get("start_url") or ""), f"{task}\n{args.get('context') or ''}"
            )
            if motivo_site:
                headless = False
                motivo_modo = motivo_site
        logger.info(
            "[browser-local] %s: %s",
            "sem janela (headless)" if headless else "com janela",
            motivo_modo,
        )
        # O `device_id` do spec é o da SUPERFÍCIE resolvida, que pode não ser o
        # device do canal (pedido pelo celular apontando o navegador do macbook).
        # O `act` do runner é `broadcaster.request(device_id, …)`, então apontar
        # para outra superfície é trocar este valor — o driver não muda.
        spec: dict[str, Any] = {
            "task": task,
            "device_id": alvo_id,
            "headless": headless,
        }
        if provider:
            spec["surface_provider"] = provider
        # Quem pediu, gravado AGORA: o job roda fora do turno, e se ele parar num
        # campo e pedir liberação sozinho, o card precisa dizer se o pedido
        # nasceu de conteúdo de terceiro — que é o que só o turno sabe.
        autoridade = _autoridade_do_turno()
        if autoridade:
            spec["autoridade"] = autoridade
        # O pedido nas palavras DELE: é a finalidade que a tabela de necessidade
        # julga se a tarefa parar num campo (`_finalidade_declarada` no runner).
        # O `task` é instrução do cérebro e traz palavra solta.
        pedido = (self.user_message or "").strip()
        if pedido:
            spec["pedido"] = pedido[:500]
        # O que o Daniel disse sobre o trabalho, nas palavras DELE, para o AGENTE
        # ler (o `pedido` acima é só a mensagem do turno e serve à conferência).
        # Sessão c469e4c7: o "qualquer decisão que for minha, me pergunta" ficou na
        # 1ª mensagem; o job seguinte nasceu de "continue e tente de novo", com a
        # tarefa do cérebro mandando preencher "respostas PROVISÓRIAS" — e o
        # agente obedeceu, porque nunca viu a frase dele.
        falas = await self._falas_do_daniel()
        if falas:
            spec["falas"] = falas
        # Headless é da MÁQUINA, não da superfície: extensão não inicia navegador.
        # Sem ele, cair para a aba visível é melhor que falhar — mesma escada de
        # sempre (fetch → headless → tela), só que agora escolhida por capacidade
        # declarada em vez de por tentativa e erro.
        if headless and superficie is not None and not superficie.pode("headless"):
            logger.info(
                "[browser-local] %s não tem headless — usando a aba visível",
                superficie.label,
            )
            headless = False
            spec["headless"] = False
            spec["headless_indisponivel"] = True
            motivo_modo = f"{superficie.label} não roda sem janela"
        # Por que foi com janela ou sem: o `job_status` devolve isto, e o
        # cérebro para de adivinhar (na 7bc7caa1 ele culpou uma extensão
        # pareada offline por uma tarefa que rodava, sem janela, de propósito).
        spec["motivo_janela"] = motivo_modo[:200]
        context = str(args.get("context") or "").strip()
        if context:
            spec["context"] = context[:2000]
        start_url = str(args.get("start_url") or "").strip()
        if start_url.startswith(("http://", "https://")):
            spec["start_url"] = start_url[:500]
        # O que o cérebro faz quando o navegador terminar ("compare e diga qual
        # compensa"): sem isto, o veredito pedido se perdia — a mensagem do job lista
        # os itens, e ninguém ficava encarregado de concluir (job b8648f0e).
        then = str(args.get("then") or "").strip()
        if then:
            spec["then"] = then[:4000]
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
            # A tarefa que está rodando OUVE (docs/browser-conversa-plan.md §4.5): um
            # pedido NOVO vira recado para ela, em vez de "espere". A MESMA tarefa
            # pedida de novo continua sendo só "é esta" — o cérebro às vezes a
            # repete achando que não pegou, e repetir a tarefa como "nova
            # instrução" faria o agente recomeçar o que já está fazendo.
            from core.browser_local import conversa as conversas

            viva = conversas.viva(emcurso.id)
            if viva is not None and not _mesma_tarefa(task, (emcurso.spec_json or {}).get("task")):
                # O recado leva as palavras DELE. A reescrita do cérebro desviava: na
                # sessão 74408e70 o "continue" virou uma tarefa nova com "respostas
                # neutras mínimas de teste" — o contrário do que ele tinha pedido — e
                # o "PARE e pergunte" dela foi lido como "pode parar".
                viva.entregar(_recado_para_tarefa_viva(self.user_message, task, context), "chat")
                logger.info("[browser-local] pedido novo virou recado para o job %s", emcurso.id)
                return {
                    "ok": True,
                    "job_id": str(emcurso.id),
                    "title": emcurso.title,
                    "status": emcurso.status,
                    "reused": True,
                    "entregue_como_recado": True,
                    "note": (
                        "Já havia uma tarefa rodando no navegador dele — NÃO abri outra: "
                        "mandei este pedido como RECADO para ela, que lê no próximo passo e "
                        "se ajusta sem fechar a aba nem recomeçar (pode abrir outra aba se "
                        "precisar). Diga isso ao Daniel em uma frase. Para TROCAR de tarefa "
                        "de verdade: `job_cancel` e depois a nova."
                    ),
                }
            return {
                "ok": True,
                "job_id": str(emcurso.id),
                "title": emcurso.title,
                "status": emcurso.status,
                "reused": True,
                "note": (
                    "JÁ EXISTE uma tarefa sua rodando no navegador deste usuário — esta é "
                    "ela, não uma nova. Só existe UMA aba: disparar outra derruba a "
                    "primeira no meio. Espere o resultado chegar; ele vem sozinho no chat. "
                    "Para mudar o que ela está fazendo, use `job_falar`."
                ),
                **_onde_roda(emcurso),
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
        if headless:
            # A nota era a mesma nos dois modos, e o Neo prometeu "a aba vai abrir
            # na sua tela agora" sobre uma tarefa sem janela (sessão 7bc7caa1).
            nota = (
                "Tarefa enfileirada SEM JANELA (headless) na máquina do usuário: ele NÃO "
                "vai ver aba nenhuma abrir. Não diga que abriu na tela dele. Diga que está "
                "rodando por trás e que você avisa quando terminar; se ele quiser "
                "acompanhar, é pedir 'no meu navegador'."
            )
        else:
            nota = (
                "Tarefa enfileirada no navegador DO USUÁRIO, com janela — ele vai ver "
                "acontecendo na tela dele. Diga que ele pode acompanhar e que você avisa "
                "quando terminar."
            )
        return {
            "ok": True,
            "job_id": str(job.id),
            "title": title,
            "status": "queued",
            "janela": not headless,
            "note": nota,
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

    # ------------------------------------------------------------------
    # Claude Code delegado (docs/claude-code-delegation-plan.md)
    # ------------------------------------------------------------------

    def _claude_ligado(self) -> bool:
        from core.claude_code import chave

        return bool(chave.ligado() and getattr(self.jm, "_claude_runner", None) is not None)

    async def _mac_do_claude(self) -> tuple[str, str]:
        """O Mac onde o Claude roda: ``(device_id, "")`` ou ``("", motivo)``.

        O do turno quando é um Mac com o canal de controle aberto (o caso normal:
        ele está falando pelo desktop). Pedido de outro aparelho cai no único Mac
        online; com dois, não se chuta.
        """
        from core.notifications.device_control import get_device_control_broadcaster

        b = get_device_control_broadcaster()
        try:
            registros = await self.db.get_connected_devices(self.user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[claude] não consegui listar os devices: %s", exc)
            registros = []
        macs = [
            r for r in registros
            if str(getattr(r, "os_platform", "") or "").lower() in ("macos", "darwin")
            and b.is_connected(r.device_id)
        ]
        if self.device_id and any(r.device_id == self.device_id for r in macs):
            return self.device_id, ""
        if len(macs) == 1:
            return macs[0].device_id, ""
        if not macs:
            return "", "o desktop do Neo não está aberto em nenhum Mac agora — o Claude roda lá."
        nomes = ", ".join(str(getattr(r, "display_name", None) or r.model) for r in macs[:5])
        return "", f"há mais de um Mac conectado ({nomes}); fale do Mac em que quer rodar."

    async def _claude_em_curso(self) -> "AgentJob | None":
        try:
            ativos = await self.db.list_agent_jobs(self.user_id, statuses=["queued", "running"], limit=20)
        except Exception:  # noqa: BLE001
            return None
        return next((j for j in ativos if j.kind == "claude_code"), None)

    async def _claude_task(self, args: dict[str, Any]) -> dict[str, Any]:
        """Enfileira a tarefa e volta na hora. A saúde do Mac é conferida AGORA:
        "não está logado" tem que chegar neste turno, não minutos depois."""
        if not self._claude_ligado():
            return {"ok": False, "error": "o Claude Code integrado está desligado no neo-panel (Configurações → Claude Code)."}
        objetivo = str(args.get("objetivo") or "").strip()
        if not objetivo:
            return {"ok": False, "error": "objetivo vazio: diga ao Claude o que é 'pronto'."}
        onde = str(args.get("onde") or "auto").strip().lower()
        if onde == "servidor":
            return {
                "ok": False,
                "error": (
                    "o Claude no servidor ainda não existe (F2 do plano). Ofereça rodar no Mac "
                    "do Daniel (onde='local')."
                ),
            }
        device_id, motivo = await self._mac_do_claude()
        if not device_id:
            return {"ok": False, "error": motivo}

        from core.claude_code import CorpoLocal
        from core.notifications.device_control import get_device_control_broadcaster
        from shared.config import get_settings

        pasta = str(args.get("pasta") or "").strip()
        saude = await CorpoLocal(get_device_control_broadcaster(), device_id).saude(pasta=pasta)
        if not saude.ok:
            return {"ok": False, "error": saude.motivo}
        if pasta and saude.pasta_ok is False:
            # No mesmo turno, para o modelo corrigir: no job 21b91d36 a pasta
            # inventada (`~/Projetos/…`) só falhou no Mac, um turno depois.
            return {
                "ok": False,
                "error": (
                    f"a pasta '{pasta}' não serve no Mac do Daniel ({saude.pasta_motivo or 'não existe'}). "
                    "Se ele NÃO pediu essa pasta, chame claude_task de novo SEM `pasta` — o Neo cria "
                    "uma nova em ~/Neo/claude/. Se ele pediu, diga que ela não existe e pergunte qual usar."
                ),
            }

        em_curso = await self._claude_em_curso()
        if em_curso is not None:
            return {
                "ok": True,
                "job_id": str(em_curso.id),
                "title": em_curso.title,
                "status": em_curso.status,
                "reused": True,
                "note": (
                    "Já existe uma tarefa do Claude em andamento — uma por vez. Para orientar essa, "
                    "use claude_reply; para trocar, job_cancel e peça de novo."
                ),
            }

        modelo = str(args.get("modelo") or "").strip().lower()
        if modelo not in ("sonnet", "opus", "haiku"):
            modelo = get_settings().claude_code_model
        entregaveis = [str(e).strip() for e in args.get("entregaveis") or [] if str(e).strip()]
        spec: dict[str, Any] = {
            "objetivo": objetivo,
            "contexto": str(args.get("contexto") or "").strip(),
            "entregaveis": entregaveis[:20],
            "pasta": pasta,
            "corpo": "local",
            "device_id": device_id,
            "modelo": modelo,
            "max_rodadas": 3,
            "autoridade": _autoridade_do_turno(),
        }
        then = str(args.get("then") or "").strip()
        if then:
            spec["then"] = then
        job = AgentJob(
            user_id=self.user_id,
            session_id=self.session_id,
            kind="claude_code",
            title=f"Claude: {objetivo[:200]}",
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
            "corpo": "Mac do Daniel",
            "claude_code": saude.versao,
            "note": (
                "Tarefa NA FILA para o Claude Code no Mac do Daniel — ainda NÃO começou. Na "
                "primeira vez, o Mac mostra um pedido de confirmação e ela só começa quando ele "
                "permitir. Diga isso (sem afirmar que já funcionou), que você acompanha e traz o "
                "resultado; NÃO fique esperando."
            ),
        }

    async def _claude_reply(self, args: dict[str, Any]) -> dict[str, Any]:
        """Rodando: a mensagem entra na fila do job. Parado esperando: nasce a
        continuação, pela MESMA sessão do Claude (mesmo padrão da retomada do
        navegador, que cria um job novo herdando o spec). Terminada: o mesmo, como
        AJUSTE — antes era "peça uma tarefa nova", e a tarefa nova nascia sem a
        memória do Claude e numa pasta nova, sem os arquivos de antes."""
        if not self._claude_ligado():
            return {"ok": False, "error": "o Claude Code integrado está desligado no neo-panel (Configurações → Claude Code)."}
        mensagem = str(args.get("mensagem") or "").strip()
        if not mensagem:
            return {"ok": False, "error": "mensagem vazia."}
        anterior = await self._get_owned(args.get("job_id"))
        if anterior is None or anterior.kind != "claude_code":
            return {"ok": False, "error": "não achei essa tarefa do Claude."}
        interromper = bool(args.get("interromper"))

        # A tarefa já seguiu numa continuação (ou num ajuste): o pedido vai para a
        # PONTA da cadeia, senão nasciam dois ramos da mesma sessão.
        if anterior.status not in ("queued", "running"):
            ponta = await self._ponta_da_cadeia(anterior)
            if ponta.id != anterior.id:
                return await self._claude_reply({**args, "job_id": str(ponta.id)})

        if anterior.status in ("queued", "running"):
            meta = dict(anterior.extra_metadata or {})
            caixa = list(meta.get("claude_mensagens") or [])
            caixa.append({
                "texto": mensagem[:4000],
                "interromper": interromper,
                "em": datetime.utcnow().isoformat(timespec="seconds"),
            })
            meta["claude_mensagens"] = caixa
            await self.db.update_agent_job(anterior.id, extra_metadata=meta)
            return {
                "ok": True,
                "job_id": str(anterior.id),
                "note": (
                    "Parei a rodada atual; a mensagem dele começa a próxima." if interromper
                    else "A mensagem entra quando a rodada atual terminar."
                ),
            }

        claude = (anterior.extra_metadata or {}).get("claude") or {}
        ajuste = anterior.status in _CLAUDE_TERMINADA
        if anterior.status != "waiting" and not ajuste:
            return {"ok": False, "error": f"essa tarefa está em '{anterior.status}' e não aceita mensagem."}
        estado = (anterior.spec_json or {}).get("claude") or {}
        if not claude.get("session_id") or (ajuste and not (estado.get("sessao_iniciada") or claude.get("rodadas"))):
            return {
                "ok": False,
                "error": (
                    f"essa tarefa nem chegou a começar ({anterior.status}), então não há sessão do "
                    "Claude para retomar. Para fazer, peça uma tarefa nova com claude_task."
                ),
            }
        if ajuste:
            em_curso = await self._claude_em_curso()
            if em_curso is not None:
                return {
                    "ok": False,
                    "error": (
                        f"já tem uma tarefa do Claude rodando (job {em_curso.id}). Mande para ela com "
                        f"claude_reply(job_id=\"{em_curso.id}\"), ou espere ela terminar."
                    ),
                }

        from core.claude_code import Recusa, regra_de_permissao

        antigo = dict(anterior.spec_json or {})
        liberadas = list(antigo.get("liberadas") or [])
        if args.get("liberar"):
            for r in claude.get("recusas") or []:
                regra = regra_de_permissao(Recusa(str(r.get("ferramenta") or ""), dict(r.get("entrada") or {})))
                if regra and regra not in liberadas:
                    liberadas.append(regra)
        if ajuste:
            instrucao = (
                f"Esta tarefa tinha terminado ({_ESTADO_DA_TAREFA.get(anterior.status, anterior.status)}). "
                f"Agora o Daniel pede, pelo Neo: {mensagem}"
            )
        else:
            instrucao = f"O Daniel respondeu, pelo Neo: {mensagem}"
        if args.get("liberar") and liberadas:
            instrucao += (
                "\n\nEle liberou estas ações, só nesta tarefa: "
                + ", ".join(liberadas)
                + ". Faça agora o que tinha ficado bloqueado e continue."
            )
        spec = {
            **{k: v for k, v in antigo.items() if k not in ("claude", "then")},
            "liberadas": liberadas,
            "instrucao_inicial": instrucao,
            "tarefa_id": claude.get("tarefa_id") or str(anterior.id),
            "continuacao_de": str(anterior.id),
            "autoridade": _autoridade_do_turno(),
            "claude": {
                "session_id": claude["session_id"],
                "sessao_iniciada": True,
                "cwd": claude.get("cwd") or antigo.get("pasta") or "",
                "tarefa_id": claude.get("tarefa_id") or str(anterior.id),
            },
        }
        # O combinado da tarefa pausada continua valendo; o da terminada, não: um
        # ajuste é pedido novo ("cole a saída da partida" não é sobre o placar).
        if antigo.get("then") and not ajuste:
            spec["then"] = antigo["then"]
        titulo = re.sub(r"( \((continuação|ajuste)\))+$", "", anterior.title or "Claude")
        job = AgentJob(
            user_id=self.user_id,
            session_id=self.session_id,
            kind="claude_code",
            title=f"{titulo} ({'ajuste' if ajuste else 'continuação'})"[:256],
            spec_json=spec,
            status="queued",
            notify_policy="chat",
        )
        job = await self.db.create_agent_job(job)
        if not ajuste:
            # A terminada fica como terminou (o balão dela diz "concluída"); a
            # cadeia é achada pelo `continuacao_de`.
            await self.db.update_agent_job(anterior.id, status="continued")
        try:
            self.jm.poke()
        except Exception:  # noqa: BLE001
            pass
        return {
            "ok": True,
            "job_id": str(job.id),
            "liberadas": liberadas,
            "note": (
                "O Claude retoma a MESMA sessão, na mesma pasta, lembrando do que fez. "
                if ajuste else "O Claude continua de onde parou, pela mesma sessão. "
            ) + "Avise e NÃO fique esperando.",
        }

    # ------------------------------------------------------------------
    # As sessões do Claude do DANIEL (docs/ambiente-de-trabalho-plan.md §9)
    # ------------------------------------------------------------------

    async def _pedir_ao_mac(
        self, device_id: str, acao: str, args: dict[str, Any], timeout: float
    ) -> tuple[dict[str, Any] | None, str]:
        """``(resultado, "")`` ou ``(None, motivo)``: o canal fora do ar vira frase para o modelo."""
        from core.notifications.device_control import (
            DeviceOffline,
            DeviceTimeout,
            get_device_control_broadcaster,
        )

        try:
            r = await get_device_control_broadcaster().request(device_id, acao, args, timeout=timeout)
        except DeviceOffline:
            return None, "o desktop do Neo não está conectado no Mac agora."
        except DeviceTimeout:
            return None, f"o Mac não respondeu a tempo ({acao})."
        if not r.get("ok"):
            return None, str(r.get("error") or f"{acao} falhou no Mac")
        return r.get("result") or {}, ""

    async def _claude_sessoes(self, args: dict[str, Any]) -> dict[str, Any]:
        """As fichas das sessões do Daniel, enxutas para o cérebro escolher."""
        if not self._claude_ligado():
            return {"ok": False, "error": _CLAUDE_DESLIGADO}
        device_id, motivo = await self._mac_do_claude()
        if not device_id:
            return {"ok": False, "error": motivo}
        from shared.ajustes import ajustes

        t = ajustes().trabalho
        r, motivo = await self._pedir_ao_mac(
            device_id,
            "claude.sessoes",
            {
                "recentes_horas": t.sessoes_recentes_horas,
                # As tarefas do próprio Neo saem da lista aqui: pede uma folga.
                "max": t.sessoes_max + 10,
                "objetivo_max": t.ficha_objetivo,
                "pedido_max": t.ficha_pedido,
                "pedidos": t.ficha_pedidos,
                "resposta_max": t.ficha_resposta,
                "arquivos_max": t.ficha_arquivos,
            },
            timeout=30,
        )
        if r is None:
            return {"ok": False, "error": motivo}
        # Tarefa que o Neo delegou (`~/Neo/claude/…`) é `claude_reply`, não daqui;
        # aba nova sem conversa não tem assunto para casar com pedido nenhum.
        sessoes = [
            _ficha_para_o_cerebro(s)
            for s in r.get("sessoes") or []
            if not s.get("tarefa_do_neo") and not s.get("vazia")
        ][: t.sessoes_max]
        if not sessoes:
            return {
                "ok": True,
                "sessoes": [],
                "note": "Nenhuma sessão do Claude aberta no Mac, nem fechada nas últimas horas.",
            }
        return {
            "ok": True,
            "sessoes": sessoes,
            "note": (
                "Escolha pelo ASSUNTO do pedido, comparando com título, últimos pedidos, última "
                "resposta e arquivos (o título pode estar velho: a sessão muda de assunto). Pedido "
                "genérico vai para a usada por último. Se duas combinam, ou nenhuma, pergunte "
                "citando os títulos. Depois: claude_enviar(sessao=<sessionId>, pedido=<o pedido>)."
            ),
        }

    async def _claude_enviar(self, args: dict[str, Any]) -> dict[str, Any]:
        """Entrega o pedido numa sessão do Daniel e põe um job para trazer a resposta.

        Aberta: o Mac entrega pelo SendMessage, e ela executa sozinha (F0). Fechada:
        o orquestrador do ``claude_task`` a retoma com ``--resume``, na pasta dela.
        Sem diálogo no Mac, decisão do Daniel (08/11): o Neo não depende dele
        apertar nada. Quem guarda é o portão (pedido dele, turno limpo).
        """
        if not self._claude_ligado():
            return {"ok": False, "error": _CLAUDE_DESLIGADO}
        pedido = str(args.get("pedido") or "").strip()
        if not pedido:
            return {"ok": False, "error": "pedido vazio: escreva o que o Daniel quer, completo."}
        try:
            sessao = str(UUID(str(args.get("sessao") or "").strip()))
        except ValueError:
            return {
                "ok": False,
                "error": "sessão inválida: use o sessionId que a claude_sessoes devolveu (chame-a antes).",
            }
        device_id, motivo = await self._mac_do_claude()
        if not device_id:
            return {"ok": False, "error": motivo}
        from shared.ajustes import ajustes

        r, motivo = await self._pedir_ao_mac(
            device_id,
            "claude.enviar",
            {"sessionId": sessao, "texto": f"{pedido}\n\n{_RODAPE_DO_PEDIDO}"},
            timeout=ajustes().trabalho.envio_timeout_s,
        )
        if r is None:
            return {"ok": False, "error": motivo}
        if not r.get("entregue"):
            if r.get("motivo") == "sessao_fechada":
                return await self._claude_retomar_sessao(device_id, sessao, pedido, r)
            return {"ok": False, "error": str(r.get("erro") or "o Mac não confirmou a entrega do pedido.")}

        titulo = str(r.get("titulo") or r.get("nome") or sessao[:8])
        job = AgentJob(
            user_id=self.user_id,
            session_id=self.session_id,
            kind="claude_sessao",
            title=f"Claude ({titulo[:60]}): {pedido[:160]}",
            spec_json={
                "device_id": device_id,
                "sessao": sessao,
                "nome": r.get("nome"),
                "titulo": titulo,
                # Relógio do Mac, o mesmo do registro que o job vai comparar.
                "enviado_em": int(r.get("enviado_em") or 0),
                "pedido": pedido[:2000],
                "autoridade": _autoridade_do_turno(),
            },
            status="queued",
            notify_policy="chat",
        )
        job = await self.db.create_agent_job(job)
        try:
            self.jm.poke()
        except Exception:  # noqa: BLE001
            pass
        nota = f"Entregue na sessão “{titulo}”, que já está executando"
        nota += (
            ": ela estava no meio de um trabalho, e o pedido entrou nele."
            if r.get("status") == "busy" else "."
        )
        if r.get("texto_igual") is False:
            nota += " Atenção: o texto que chegou não é idêntico ao pedido; diga isso ao Daniel."
        nota += (
            " Diga ao Daniel para qual sessão mandou e que avisa quando ela terminar; NÃO fique "
            "esperando e NÃO afirme o resultado."
        )
        return {"ok": True, "job_id": str(job.id), "sessao": titulo, "note": nota}

    async def _claude_retomar_sessao(
        self, device_id: str, sessao: str, pedido: str, r: dict[str, Any]
    ) -> dict[str, Any]:
        """Sessão fechada (§9.5): o orquestrador do ``claude_task`` a retoma com
        ``--resume``, na pasta dela, com o histórico. O Mac só responde
        ``sessao_fechada`` depois de conferir que nenhum processo a segura."""
        pasta = str(r.get("pasta") or "").strip()
        titulo = str(r.get("titulo") or sessao[:8])
        if not pasta:
            return {"ok": False, "error": f"a sessão “{titulo}” está fechada e não achei a pasta dela no Mac."}
        em_curso = await self._claude_em_curso()
        if em_curso is not None:
            return {
                "ok": False,
                "error": (
                    f"a sessão “{titulo}” está fechada, e retomá-la usa o braço das tarefas do Claude, "
                    "que já tem uma rodando (uma por vez). Espere ela terminar ou cancele."
                ),
            }
        from shared.config import get_settings

        job = AgentJob(
            user_id=self.user_id,
            session_id=self.session_id,
            kind="claude_code",
            title=f"Claude ({titulo[:60]}): {pedido[:160]}",
            spec_json={
                "objetivo": f"Continuar a sessão “{titulo}” do Daniel com o pedido dele: {pedido}",
                "contexto": "",
                "entregaveis": [],
                "pasta": pasta,
                "corpo": "local",
                "device_id": device_id,
                "modelo": get_settings().claude_code_model,
                "max_rodadas": 3,
                "instrucao_inicial": f"Pedido do Daniel, enviado pelo Neo: {pedido}",
                "autoridade": _autoridade_do_turno(),
                "sessao_do_daniel": sessao,
                "claude": {"session_id": sessao, "sessao_iniciada": True, "cwd": pasta},
            },
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
            "sessao": titulo,
            "fechada": True,
            "note": (
                f"A sessão “{titulo}” estava fechada: o Neo a retoma em segundo plano, na pasta "
                f"dela ({pasta}), com o histórico. Diga isso ao Daniel e que avisa quando terminar; "
                "não fique esperando."
            ),
        }

    async def _ponta_da_cadeia(self, job: "AgentJob") -> "AgentJob":
        """O job mais novo da cadeia de continuações e ajustes que começa em ``job``."""
        try:
            recentes = await self.db.list_agent_jobs(self.user_id, limit=50)
        except Exception:  # noqa: BLE001 — sem a lista, responde ao job pedido
            return job
        seguinte = {
            str((j.spec_json or {}).get("continuacao_de")): j
            for j in sorted(recentes, key=lambda j: j.created_at or datetime.min)
            if j.kind == "claude_code" and (j.spec_json or {}).get("continuacao_de")
        }
        atual = job
        for _ in range(20):
            proximo = seguinte.get(str(atual.id))
            if proximo is None:
                break
            atual = proximo
        return atual

    async def _status(self, args: dict[str, Any]) -> dict[str, Any]:
        job = await self._get_owned(args.get("job_id"))
        if job is None:
            return {"ok": False, "error": "job não encontrado."}
        resultado = {
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
        progresso = str(getattr(job, "progress_note", "") or "")
        if progresso:
            resultado["progresso"] = progresso
        if job.status == "running" and progresso.startswith("esperando você"):
            # Viva e parada numa pergunta: o `running` sozinho fez o Neo achar
            # que ela estava travada (sessão 7bc7caa1).
            resultado["esperando_resposta"] = True
            resultado["pergunta"] = progresso.split(":", 1)[-1].strip()
        if getattr(job, "kind", None) == "browser_local":
            resultado.update(_onde_roda(job))
        return resultado

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
    if getattr(job, "kind", None) == "comando_local":
        # Tarefa no computador do usuário: o log mora LÁ (services/local_jobs.rs) e
        # vem ao vivo pelo canal de controle; terminada, fica o resumo.
        from core.jobs.runner_comando_local import logs as _logs_comando_local

        return await _logs_comando_local(job, max_bytes)
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
    estava = job.status
    await db.update_agent_job(
        job.id,
        status="cancelled",
        finished_at=datetime.utcnow(),
        result_summary=(job.result_summary or "Cancelado pelo usuário."),
    )
    # Cancelar a tarefa que esperava um card de liberação encerra o card junto:
    # senão ele seguia no painel, e aprová-lo não retomaria nada.
    if estava == "waiting":
        await _encerrar_card_da_espera(db, job)
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


async def _encerrar_card_da_espera(db: Any, job: AgentJob) -> None:
    rid = ((job.extra_metadata or {}).get("browser_session") or {}).get("release_pending")
    if not rid:
        return
    try:
        from core.release.service import ReleaseService

        await ReleaseService(db).decide(
            rid, approved=False, decided_by=job.user_id, source="tarefa_cancelada"
        )
    except Exception as exc:  # noqa: BLE001 — já decidido ou vencido: nada a encerrar
        logger.info("[jobs] card %s da tarefa cancelada %s não foi encerrado: %s", rid, job.id, exc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
