"""SessionManager — sessões de browser nomeadas, persistentes e quentes.

Dois níveis de persistência (docs/browser-agent-plan.md §3.2):

1. FRIO (para sempre): cada sessão tem um perfil Chromium em
   ``/data/profiles/<sid>`` (volume) — cookies/logins sobrevivem a TTL,
   restart e deploy.
2. QUENTE (entre prompts): ``keep_alive=True`` mantém o Chromium vivo depois
   que o Agent termina — a próxima task recebe o MESMO Browser (mesmas abas,
   mesma página). Um reaper fecha browsers ociosos além do TTL; só o estado
   quente morre, o auth fica no perfil.

Concorrência: uma task por vez por sessão (lock) + fila FIFO curta
(``max_queue`` esperando; acima disso ``SessionBusyError`` → HTTP 429).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from browser_use import Agent, Browser, BrowserProfile

import shots
from policy import POLICY

logger = logging.getLogger(__name__)


class SessionBusyError(Exception):
    """Fila da sessão cheia — o chamador (runner do neo-core) explica ao usuário."""


@dataclass
class ManagedSession:
    sid: str
    profile_dir: Path
    browser: Any = None  # Browser vivo (quente) ou None (fria)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiting: int = 0
    running: bool = False
    last_used: float = field(default_factory=time.monotonic)
    last_task: str | None = None
    last_result: str | None = None
    current_url: str | None = None

    @property
    def warm(self) -> bool:
        return self.browser is not None


#: Segundos por passo usados para converter o prazo de parede em teto de passos.
#: Medido no job 36a81dd6: 22 passos em 570s (~26s/passo), com navegação real,
#: screenshot e uma chamada de LLM por passo. Deliberadamente generoso — errar
#: para MENOS passos custa uma tarefa mais curta; errar para mais devolve o
#: fracasso mudo que este número existe para evitar.
_STEP_BUDGET_S = 26
#: Piso: abaixo disto a tarefa não tem chance nenhuma e o teto vira o problema.
_MIN_STEPS = 8


def _history_get(history: Any, method: str, default: Any = None) -> Any:
    """A superfície do AgentHistoryList muda entre versões — nunca confiar cega."""
    try:
        fn = getattr(history, method, None)
        return fn() if callable(fn) else default
    except Exception:  # noqa: BLE001
        return default


def _partial_diagnosis(agent: Any) -> str:
    """O que o agente JÁ SABIA quando o relógio o matou.

    Um agente que estourou o prazo não é um agente que não descobriu nada. Ele
    mantém, a cada passo, uma avaliação do que falhou e uma memória acumulada
    das tentativas — e o container loga tudo isso. Até aqui nada disso saía no
    retorno: o timeout devolvia só "timeout depois de 570s" e o usuário recebia
    um fracasso mudo.

    Job 36a81dd6 (2026-09-20, baixar o ZIP de propostas do portal de dados): o agente rodou
    22 passos e terminou sabendo exatamente o problema — "Todas as URLs do portal de dados
    estão bloqueadas pelo CDN EdgeSuite; DuckDuckGo e Google → CAPTCHA; Bing
    detectando localização fora do Brasil". Isso responde a pergunta do usuário
    ("por que falhou?") e aponta a saída (o navegador DELE, com IP residencial
    no Brasil). Jogar fora era o desperdício mais caro deste caminho.

    Best-effort por definição: histórico parcial, de uma lib cuja superfície
    muda entre versões. Falhar aqui devolve "" e o chamador segue com o texto
    curto de sempre.
    """
    hist = getattr(getattr(agent, "state", None), "history", None)
    if hist is None:
        return ""
    partes: list[str] = []
    try:
        pensamentos = _history_get(hist, "model_thoughts", default=[]) or []
    except Exception:  # noqa: BLE001
        pensamentos = []
    if pensamentos:
        ultimo = pensamentos[-1]
        # A memória é o acumulado ("tentei A, tentei B, tentei C"); a avaliação é
        # a leitura do último passo. A memória vale mais e vem primeiro.
        memoria = str(getattr(ultimo, "memory", "") or "").strip()
        avaliacao = str(getattr(ultimo, "evaluation_previous_goal", "") or "").strip()
        if memoria:
            partes.append(f"O que o agente já tinha tentado: {memoria[:900]}")
        if avaliacao and avaliacao[:120] not in "".join(partes):
            partes.append(f"Leitura do último passo: {avaliacao[:400]}")
    erros = [str(e).strip() for e in (_history_get(hist, "errors", default=[]) or []) if e]
    if erros:
        # Erro de browser repete muito (o mesmo 403 em cada tentativa); o que
        # informa é o CONJUNTO de erros distintos, não a contagem.
        vistos: list[str] = []
        for e in erros:
            corte = e[:180]
            if corte not in vistos:
                vistos.append(corte)
        partes.append("Erros distintos: " + " | ".join(vistos[:5]))
    urls = [str(u) for u in (_history_get(hist, "urls", default=[]) or []) if u]
    if urls:
        unicas: list[str] = []
        for u in urls:
            if u not in unicas:
                unicas.append(u)
        partes.append(f"Passou por {len(unicas)} endereço(s); últimos: " + ", ".join(unicas[-4:]))
    passos = _history_get(hist, "number_of_steps")
    if passos:
        partes.insert(0, f"Chegou ao passo {passos} antes de ser interrompido.")
    return "\n".join(partes)[:2000]


class SessionManager:
    def __init__(
        self,
        *,
        profiles_root: str,
        chromium_path: str,
        ttl_s: int = 900,
        max_warm: int = 1,
        max_queue: int = 2,
        task_timeout_s: int = 570,
    ) -> None:
        self.profiles_root = Path(profiles_root)
        self.profiles_root.mkdir(parents=True, exist_ok=True)
        self.chromium_path = chromium_path
        self.ttl_s = ttl_s
        self.max_warm = max_warm
        self.max_queue = max_queue
        self.task_timeout_s = task_timeout_s
        self._sessions: dict[str, ManagedSession] = {}
        self._reaper_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ util

    def _get(self, sid: str) -> ManagedSession:
        if sid not in self._sessions:
            self._sessions[sid] = ManagedSession(
                sid=sid, profile_dir=self.profiles_root / sid
            )
        return self._sessions[sid]

    def warm_sessions(self) -> list[str]:
        return [s.sid for s in self._sessions.values() if s.warm]

    async def _kill_browser(self, session: ManagedSession) -> None:
        """Fecha o Chromium da sessão (mantém o perfil). Defensivo entre versões."""
        browser = session.browser
        session.browser = None
        if browser is None:
            return
        for method in ("kill", "close", "stop"):
            fn = getattr(browser, method, None)
            if fn is None:
                continue
            try:
                res = fn()
                if asyncio.iscoroutine(res):
                    await res
                logger.info("[%s] browser fechado via .%s()", session.sid, method)
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] .%s() falhou: %s", session.sid, method, e)
        logger.warning("[%s] nenhum método de fechamento funcionou", session.sid)

    async def _evict_lru_if_needed(self, incoming_sid: str) -> None:
        """Garante espaço para mais um browser quente (max_warm)."""
        warm = [
            s for s in self._sessions.values()
            if s.warm and s.sid != incoming_sid and not s.running
        ]
        total_warm = sum(1 for s in self._sessions.values() if s.warm and s.sid != incoming_sid)
        if total_warm < self.max_warm:
            return
        if not warm:  # todas quentes estão rodando — não há o que despejar
            return
        victim = min(warm, key=lambda s: s.last_used)
        logger.info("[%s] evict LRU para abrir espaço p/ %s", victim.sid, incoming_sid)
        async with victim.lock:
            await self._kill_browser(victim)

    def _ensure_browser(self, session: ManagedSession) -> Any:
        if session.browser is None:
            profile = BrowserProfile(
                user_data_dir=str(session.profile_dir),
                keep_alive=True,
                headless=True,
                executable_path=self.chromium_path,
                args=["--no-sandbox"],
            )
            session.browser = Browser(browser_profile=profile)
            logger.info("[%s] browser novo (perfil %s)", session.sid, session.profile_dir)
        return session.browser

    def _handoff_prefix(self, session: ManagedSession, context: str | None) -> str:
        """Estado da TAREFA anterior (o browser quente já preserva o da página)."""
        parts = []
        if session.last_task:
            note = f'Última tarefa nesta sessão: "{session.last_task[:200]}"'
            if session.last_result:
                note += f" — resultado: {session.last_result[:300]}"
            if session.current_url:
                note += f" (a sessão parou em {session.current_url})"
            parts.append(note)
        if context:
            parts.append(f"Contexto do orquestrador: {context[:2000]}")
        if not parts:
            return ""
        return "[Sessão em andamento]\n" + "\n".join(parts) + "\n\nNova tarefa: "

    # ------------------------------------------------------------------ API

    async def run_task(
        self,
        sid: str,
        *,
        task: str,
        llm: Any,
        context: str | None = None,
        sensitive_data: dict | None = None,
        max_steps: int = 40,
        screenshots: str = shots.DEFAULT_MODE,
        max_screenshots: int = shots.DEFAULT_MAX_SHOTS,
    ) -> dict:
        session = self._get(sid)
        if session.waiting >= self.max_queue:
            raise SessionBusyError(
                f"Sessão '{sid}' com {session.waiting} tarefas na fila."
            )
        session.waiting += 1
        acquired = False
        try:
            async with session.lock:
                session.waiting -= 1
                acquired = True
                session.running = True
                try:
                    return await self._run_locked(
                        session,
                        task=task,
                        llm=llm,
                        context=context,
                        sensitive_data=sensitive_data,
                        max_steps=max_steps,
                        screenshots=screenshots,
                        max_screenshots=max_screenshots,
                    )
                finally:
                    session.running = False
                    session.last_used = time.monotonic()
        finally:
            # Cancelado ainda na ESPERA do lock: desfaz o incremento da fila.
            if not acquired:
                session.waiting -= 1

    async def _capture_failure(
        self,
        session: ManagedSession,
        agent: Any,
        *,
        mode: str,
        max_shots: int,
        caption: str,
    ) -> list[dict]:
        """Prints do momento da falha — ANTES de matar o browser.

        Ao vivo (a tela exata em que travou) + os passos que a lib guardou até
        aqui. Depois do ``_kill_browser`` não há mais o que fotografar, e é
        justamente no erro que o print vale.
        """
        if mode == "never":
            return []
        collected: list[dict] = []
        live = await shots.capture_live(
            session.browser,
            kind="error",
            caption=caption,
            url=session.current_url,
        )
        if live:
            collected.append(live)
        collected.extend(
            shots.from_history(
                shots.history_of(agent),
                limit=max(0, max_shots - len(collected)),
                url=session.current_url,
            )
        )
        return shots.budget(collected, max_count=max_shots)

    async def _run_locked(
        self,
        session: ManagedSession,
        *,
        task: str,
        llm: Any,
        context: str | None,
        sensitive_data: dict | None,
        max_steps: int,
        screenshots: str = shots.DEFAULT_MODE,
        max_screenshots: int = shots.DEFAULT_MAX_SHOTS,
    ) -> dict:
        await self._evict_lru_if_needed(session.sid)
        browser = self._ensure_browser(session)
        full_task = self._handoff_prefix(session, context) + task
        started = time.monotonic()

        # `max_steps` e o prazo de parede têm que caber um no outro.
        #
        # O chamador pede 40 passos; o prazo é de 570s. A ~25s por passo (medido
        # no job 36a81dd6: 22 passos em 570s) o agente NUNCA alcança o teto de
        # passos — ele morre sempre no relógio. E morrer no relógio é o pior dos
        # dois fins: o `asyncio.wait_for` corta no meio de um passo e o agente
        # não chega a escrever o próprio `final_result`, que é o veredito que o
        # usuário deveria receber. Estourar o teto de PASSOS, ao contrário,
        # termina limpo — com relatório.
        #
        # Então o teto de passos vira o mínimo entre o que pediram e o que cabe
        # no prazo, deixando folga para o agente concluir.
        cabe_no_prazo = max(_MIN_STEPS, int(self.task_timeout_s / _STEP_BUDGET_S))
        if max_steps > cabe_no_prazo:
            logger.info(
                "[%s] max_steps %d → %d (prazo de %ds a ~%ds/passo): passo é fim limpo, relógio não",
                session.sid, max_steps, cabe_no_prazo, self.task_timeout_s, _STEP_BUDGET_S,
            )
            max_steps = cabe_no_prazo

        agent = Agent(
            task=full_task,
            llm=llm,
            browser=browser,
            sensitive_data=sensitive_data or None,
            extend_system_message=POLICY,
            generate_gif=False,
        )
        try:
            history = await asyncio.wait_for(
                agent.run(max_steps=max_steps), timeout=self.task_timeout_s
            )
        except asyncio.TimeoutError:
            # Agente pendurado no meio de um passo: browser vai junto (o perfil
            # persiste; a próxima task reabre fria, mas logada).
            captured = await self._capture_failure(
                session, agent,
                mode=screenshots, max_shots=max_screenshots,
                caption=f"tela no timeout ({self.task_timeout_s}s)",
            )
            # Colhe ANTES do kill: depois não há mais agente para perguntar.
            diagnostico = _partial_diagnosis(agent)
            await self._kill_browser(session)
            return {
                "ok": False,
                "error": f"timeout depois de {self.task_timeout_s}s",
                "diagnosis": diagnostico,
                "current_url": session.current_url,
                "screenshots": captured,
            }
        except Exception as e:  # noqa: BLE001
            logger.exception("[%s] task crashou", session.sid)
            captured = await self._capture_failure(
                session, agent,
                mode=screenshots, max_shots=max_screenshots,
                caption="tela no momento do erro",
            )
            diagnostico = _partial_diagnosis(agent)
            # Browser em estado desconhecido (ex.: Chromium morreu) — recicla.
            await self._kill_browser(session)
            return {
                "ok": False,
                "error": str(e)[:500],
                "diagnosis": diagnostico,
                "current_url": session.current_url,
                "screenshots": captured,
            }

        final = (_history_get(history, "final_result") or "").strip() or None
        success = _history_get(history, "is_successful")
        actions = _history_get(history, "action_names", default=[]) or []
        errors = [e for e in (_history_get(history, "errors", default=[]) or []) if e]
        urls = [u for u in (_history_get(history, "urls", default=[]) or []) if u]
        current_url = urls[-1] if urls else session.current_url

        session.last_task = task
        session.last_result = final
        session.current_url = current_url

        # A task ACABOU, mas nem todo fim é sucesso. `success is False` é o
        # veredito da própria lib; `errors` são falhas de PASSO — e passo que
        # falhou e depois foi contornado não é problema do usuário (medido:
        # abrir o example.com deu 2 erros de JS e mesmo assim entregou o
        # título). Só vale print automático quando a lib NÃO carimbou sucesso.
        troubled = success is False or (bool(errors) and success is not True)
        captured: list[dict] = []
        if screenshots == "always" or (screenshots == "auto" and troubled):
            live = await shots.capture_live(
                session.browser,
                kind="error" if troubled else "final",
                caption="tela onde a tarefa parou (com erro)" if troubled
                else "tela onde a tarefa terminou",
                url=current_url,
            )
            if live:
                captured.append(live)
            if troubled:  # o caminho até o erro, não só o destroço
                captured.extend(
                    shots.from_history(
                        history,
                        limit=max(0, max_screenshots - len(captured)),
                        url=current_url,
                    )
                )
            captured = shots.budget(captured, max_count=max_screenshots)

        return {
            "ok": True,
            "success": success,  # is_successful() da lib: True/False/None
            "final_result": final,
            "current_url": current_url,
            "steps": len(actions),
            "actions": actions[-30:],
            "errors": [str(e)[:200] for e in errors[-5:]],
            "duration_s": round(time.monotonic() - started, 1),
            "handoff_note": self._handoff_prefix(session, None).strip() or None,
            "screenshots": captured,
        }

    async def screenshot(self, sid: str) -> dict:
        """Print da tela AGORA, sem rodar task ("me mostra o que tá aí").

        De propósito SEM o lock da sessão: capturar é só um ``captureScreenshot``
        no CDP (não mexe na página), e o caso mais útil é justamente espiar uma
        task longa enquanto ela roda. Sessão fria não tem tela — e dizer isso é
        melhor que devolver um quadro preto.
        """
        session = self._get(sid)
        if not session.warm:
            return {
                "ok": False,
                "error": (
                    f"A sessão de browser '{sid}' está fria (nenhum navegador aberto) — "
                    "não há tela para fotografar."
                ),
                "warm": False,
            }
        shot = await shots.capture_live(
            session.browser,
            kind="live",
            caption="tela do browser agora",
            url=session.current_url,
        )
        if shot is None:
            return {"ok": False, "error": "não consegui capturar a tela do browser.", "warm": True}
        session.last_used = time.monotonic()
        return {
            "ok": True,
            "warm": True,
            "running": session.running,
            "current_url": session.current_url,
            "screenshots": [shot],
        }

    async def state(self, sid: str) -> dict:
        session = self._get(sid)
        return {
            "sid": sid,
            "warm": session.warm,
            "running": session.running,
            "waiting": session.waiting,
            "last_task": session.last_task,
            "last_result": (session.last_result or "")[:500] or None,
            "current_url": session.current_url,
            "profile_exists": session.profile_dir.exists(),
        }

    async def close(self, sid: str, *, purge: bool = False) -> dict:
        session = self._get(sid)
        async with session.lock:
            await self._kill_browser(session)
            if purge and session.profile_dir.exists():
                shutil.rmtree(session.profile_dir, ignore_errors=True)
                logger.info("[%s] perfil apagado (purge)", sid)
            session.last_task = session.last_result = session.current_url = None
        if purge:
            self._sessions.pop(sid, None)
        return {"ok": True, "purged": purge}

    # ------------------------------------------------------------------ ciclo

    def start_reaper(self) -> None:
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            for session in list(self._sessions.values()):
                if not session.warm or session.running or session.waiting:
                    continue
                if now - session.last_used > self.ttl_s:
                    logger.info("[%s] TTL de %ss vencido — fechando browser", session.sid, self.ttl_s)
                    async with session.lock:
                        await self._kill_browser(session)

    async def shutdown(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
        for session in self._sessions.values():
            await self._kill_browser(session)
