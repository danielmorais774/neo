"""Runner de jobs ``kind="browser_local"`` — o navegador do PRÓPRIO usuário.

Irmão do ``runner_browser`` (que fala com o Chromium headless do servidor). A
diferença é o corpo: aqui não há container nenhum: o passo sai pelo canal
``/ws/devices/control`` até o neo-desktop, que traduz para CDP na aba que o
usuário está vendo. Quem decide o passo é o :class:`BrowserLocalDriver`, no
core (docs/browser-local-plan.md §3.4).

O que este arquivo faz de fato: resolve o device, confere que o navegador está
mesmo alcançável ANTES de prometer, roda o driver e traduz o resultado para o
estado terminal do job — com os prints indo para o File Store pelo mesmo
``ingest_shots`` do irmão.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from core.jobs.screenshots import ingest_shots
from db.models.agent_job import AgentJob

logger = logging.getLogger(__name__)


async def _enfileirar_vigia(
    db: Any, job: AgentJob, device_id: str, nome: str, download_id: str = ""
) -> None:
    """Cria o job que acompanha o download até o veredito. Melhor esforço.

    ``download_id`` é o id do download no navegador, quando o driver conseguiu
    um: com ele o vigia casa o item de forma exata (o nome muda no meio do
    caminho, quando o sufixo temporário cai) e enxerga falha e pausa em vez de
    esperar o cronômetro de travamento.
    """
    try:
        from db.models.agent_job import AgentJob as _Job

        spec: dict[str, Any] = {"device_id": device_id, "name": nome}
        if download_id:
            spec["download_id"] = download_id

        vigia = _Job(
            user_id=job.user_id,
            session_id=job.session_id,
            kind="download_watch",
            title=f"Acompanhar o download de {nome}"[:256],
            spec_json=spec,
            status="queued",
            notify_policy="chat",
        )
        await db.create_agent_job(vigia)
        logger.info("[BrowserLocal] vigia de download enfileirado para %s", nome)
    except Exception as exc:  # noqa: BLE001 — sem vigia a tarefa ainda foi cumprida
        logger.warning("[BrowserLocal] não consegui enfileirar o vigia: %s", exc)


async def _resolve_models(
    db: Any, env_model: str, env_vision: str | None
) -> tuple[str, str | None]:
    """Modelo do passo e modelo do OLHO, na hora do job.

    Precedência: o que o painel gravou > o que veio do ``.env`` no boot. Ler aqui
    (e não no lifespan) é o que faz a troca no painel valer no próximo job em vez
    de exigir restart.

    ``env_vision is None`` quer dizer visão DESLIGADA por configuração
    (``browser_local_vision=false``) — decisão de operador, e o painel não a
    reverte por acidente: sem essa guarda, um role com default preenchido
    religaria a visão que alguém desligou de propósito.

    Nunca levanta: falha de banco cai no valor do ``.env``, que é o
    comportamento de antes de isto existir.
    """
    step, eye = env_model, env_vision
    try:
        from modules.model_config import get_model_config

        cfg = await get_model_config(db, "browser_local", fallback_model=env_model)
        step = cfg.get("model") or env_model
        if env_vision is not None:
            olho = await get_model_config(
                db, "browser_local_vision", fallback_model=env_vision
            )
            eye = olho.get("model") or env_vision
    except Exception as exc:  # noqa: BLE001 — config nunca derruba o job
        logger.warning("[BrowserLocal] config de modelo indisponível (%s); usando o .env", exc)
        return env_model, env_vision

    # Cinto e suspensório: `normalize` já barra modelo cego no slot de visão, mas
    # ele só conhece o catálogo. Um id digitado à mão passa por lá, e é melhor
    # descobrir aqui — com log — do que no passo 1, com a visão morrendo calada.
    if eye is not None:
        from core.llm.router import supports_vision

        if supports_vision(eye) is False:
            logger.error(
                "[BrowserLocal] '%s' não lê imagem — voltando o olho para '%s'", eye, env_vision
            )
            eye = env_vision
    return step, eye


# Quanto tempo uma aba deixada por outra tarefa ainda conta como "a aba desta
# conversa". Curto de propósito: continuidade é sobre o que acabou de acontecer.
# Depois disso o usuário quase certamente usou o navegador para outra coisa, e
# começar numa aba nova é mais previsível do que herdar um estado desconhecido.
_ABA_VALIDA_POR = timedelta(minutes=15)


def chave_do_navegador(superficie: Any, device_id: str) -> str:
    """Que NAVEGADOR é este, independente de por onde chegamos nele.

    docs/browser-surface-plan.md §12.5. A continuidade de aba era chaveada pelo
    ``device_id``, ou seja, pelo **transporte**. Com duas vias para o mesmo
    navegador (o desktop via ``obu``, e a extensão direta), a mesma aba passaria a
    ter duas chaves — e o que se perde aí não é só conveniência: o
    ``browser_session`` carrega o ``pending``, a chave que autoriza *aquele*
    clique depois da sua aprovação de liberação. Trocar de via no meio perderia
    uma autorização **já dada**, que não pode ser reemitida.

    A chave é a MÁQUINA: para uma superfície direta, o hospedeiro; para o desktop,
    ele mesmo. As duas vias do mesmo Mac colidem de propósito — é o mesmo Edge.
    """
    if superficie is not None:
        return str(getattr(superficie, "host_device_id", None) or superficie.device_id)
    return str(device_id or "")


async def _resolver_aba(
    db: Any, job: AgentJob, device_id: str, spec: dict, chave: str | None = None
) -> int | None:
    """Qual aba esta tarefa deve reivindicar. ``None`` = abre uma nova.

    Duas fontes, nesta ordem:

    1. ``spec["tab_ref"]`` — o cérebro pediu continuidade explicitamente.
    2. a aba que a última tarefa no MESMO NAVEGADOR deixou aberta, se recente.

    O navegador é o critério (não a sessão de conversa) pelo mesmo motivo da trava
    de reentrância: a aba é uma por máquina. O driver ainda confere se ela
    existe antes de usar, então um palpite errado aqui custa nada — cai para
    aba nova.

    ``chave`` é a chave do navegador (ver ``chave_do_navegador``); sem ela, o
    ``device_id`` vale como chave, que é o comportamento histórico.

    Nunca levanta: sem banco, abre aba nova, que é o comportamento de sempre.
    """
    chave = chave or device_id
    try:
        pedida = spec.get("tab_ref")
        if pedida is not None:
            return int(pedida)
    except (TypeError, ValueError):
        pass
    if not device_id or job.user_id is None:
        return None
    try:
        recentes = await db.list_agent_jobs(job.user_id, limit=20)
    except Exception as exc:  # noqa: BLE001 — continuidade é luxo, não requisito
        logger.debug("[BrowserLocal] histórico indisponível para achar a aba: %s", exc)
        return None
    corte = datetime.utcnow() - _ABA_VALIDA_POR
    for anterior in recentes:
        if anterior.id == job.id or anterior.kind != "browser_local":
            continue
        sessao = (anterior.extra_metadata or {}).get("browser_session") or {}
        if not sessao.get("tab_ref"):
            continue
        # Linha antiga não tem `browser_key`: cai no `device_id`, como antes.
        anterior_chave = str(sessao.get("browser_key") or sessao.get("device_id") or "")
        if anterior_chave != chave:
            continue
        try:
            tab_ref = int(sessao["tab_ref"])
        except (TypeError, ValueError):
            return None
        # Aba do HEADLESS (ref negativa) é de um navegador que o desktop iniciou:
        # ela não existe para a extensão, e reivindicá-la de outra via daria
        # "Debugger unattached" no meio do primeiro passo.
        if tab_ref < 0 and str(sessao.get("device_id") or "") != str(device_id):
            continue
        fim = getattr(anterior, "finished_at", None)
        if fim is not None and fim < corte:
            return None  # a mais recente já é velha: as outras também são
        return tab_ref
    return None


async def _gravar_sessao(
    db: Any, job: AgentJob, device_id: str, outcome: Any, chave: str | None = None
) -> None:
    """Deixa registrado onde a tarefa parou, para a próxima continuar dali.

    `browser.release` DETACHA mas não fecha a aba — ela fica aberta na página. O
    que se perdia era só o id, e com ele a chance de continuar: em 2026-09-01 o
    agente chegou no arquivo, parou para pedir confirmação de exclusão, o
    usuário autorizou, e a tarefa seguinte recomeçou em `about:blank`.
    """
    if not device_id or outcome is None or not getattr(outcome, "tab_ref", None):
        return
    try:
        await db.update_agent_job(
            job.id,
            extra_metadata={
                **(job.extra_metadata or {}),
                "browser_session": {
                    "device_id": device_id,
                    # A chave do NAVEGADOR (a máquina), que é o que sobrevive à
                    # troca de via — ver `chave_do_navegador`.
                    "browser_key": chave or device_id,
                    "tab_ref": int(outcome.tab_ref),
                    "url": (outcome.url or "")[:500],
                    # Só existe quando a tarefa PAROU para perguntar. É a chave
                    # que autoriza exatamente aquele clique na retomada.
                    "pending": (getattr(outcome, "pending", "") or "")[:120],
                    "task": (job.spec_json or {}).get("task", "")[:2000],
                    "status": outcome.status,
                    "at": datetime.utcnow().isoformat(timespec="seconds"),
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 — registro não pode falhar o job
        logger.warning("[BrowserLocal] não consegui gravar a sessão de aba: %s", exc)


def _resolve_store(getter: Callable[[], Any] | None) -> Any:
    if getter is None:
        return None
    try:
        return getter()
    except Exception:  # noqa: BLE001
        return None


def _build_filler(db: Any, pks_service: Any) -> Any:
    """ReleaseFiller do job, ou ``None`` quando não há memória para resolver."""
    if pks_service is None:
        return None
    try:
        from core.release import ReleaseFiller, ReleaseService

        return ReleaseFiller(ReleaseService(db), pks_service)
    except Exception:  # noqa: BLE001 — sem filler o placeholder só não resolve
        logger.exception("[browser_local] não consegui montar o preenchimento escopado")
        return None


def build_browser_local_runner(
    *,
    db: Any,
    llm_router: Any,
    model: str,
    max_steps: int = 25,
    job_timeout_s: int = 600,
    blocked_domains: tuple[str, ...] = (),
    confirm_sensitive: bool = True,
    get_file_store: Callable[[], Any] | None = None,
    max_screenshots: int = 3,
    vision_model: str | None = None,
    trace_dir: str = "",
    get_pks_service: Callable[[], Any] | None = None,
) -> Callable[[AgentJob], Awaitable[dict]]:
    """Fecha as dependências e devolve ``run_browser_local_job(job) -> dict``."""

    async def run_browser_local_job(job: AgentJob) -> dict:
        from core.browser_local.driver import BrowserLocalDriver
        from core.browser_local.trace import RunTrace
        from core.notifications.device_control import (
            DeviceOffline,
            DeviceTimeout,
            get_device_control_broadcaster,
        )

        spec = job.spec_json or {}
        task = str(spec.get("task") or "").strip()
        if not task:
            return {"status": "failed", "result_summary": "Job de navegador local sem task."}

        # Sem janela (padrão) ou na tela dele? Decidido lá no `_browser_local_task`
        # — aqui só se propaga, e é cedo porque o health já precisa saber.
        headless = bool(spec.get("headless"))

        device_id = str(spec.get("device_id") or "").strip()
        if not device_id:
            return {
                "status": "failed",
                "result_summary": (
                    "Não sei em qual máquina abrir o navegador — o pedido veio sem device."
                ),
            }

        broadcaster = get_device_control_broadcaster()

        async def _cru(action: str, args: dict, timeout: float) -> dict:
            """Um passo no device. Traduz o envelope do canal em resultado/erro."""
            payload = await broadcaster.request(device_id, action, args, timeout=timeout)
            if payload.get("ok") is True:
                result = payload.get("result")
                return result if isinstance(result, dict) else {"ok": True}
            raise RuntimeError(str(payload.get("error") or "o navegador recusou a ação"))

        # A ESCADA fica entre o driver e o transporte
        # (docs/browser-surface-plan.md §13.3): com o desktop na frente, ela cai no
        # degrau 1 (implementação nativa) e o comportamento é idêntico ao de antes.
        # Superfície que não tem a ação nativa recebe a derivada, ou uma recusa que
        # diz o que falta — em vez de um "o navegador recusou a ação" genérico.
        #
        # Superfície desconhecida (registro sumiu, core reiniciado no meio) cai no
        # transporte cru: preservar o caminho que funciona é mais importante que
        # aplicar a escada.
        from core.browser_local.surfaces import AcaoIndisponivel, dispatch, get_surface

        superficie = None
        try:
            superficie = await get_surface(db, job.user_id, device_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[browser-local] superfície de %s indisponível: %s", device_id, exc)

        async def act(action: str, args: dict, timeout: float) -> dict:
            if superficie is None:
                return await _cru(action, args, timeout)
            try:
                return await dispatch(superficie, _cru, action, args, timeout)
            except AcaoIndisponivel as erro:
                # Vira RuntimeError porque é o que o driver entende — mas com o
                # texto que ensina, que é o ponto da escada.
                raise RuntimeError(erro.message) from erro

        async def registrar(items: list[dict], *, nota: str | None = None) -> None:
            """Grava os arquivos no registro de artefatos do caderno.

            Roda duas vezes por tarefa, de propósito: quando o download começa
            (o arquivo já está no disco dele, mesmo que ainda enchendo) e quando
            a tarefa fecha (tamanho confirmado). O registro deduplica por nome,
            então a segunda passada atualiza a primeira em vez de duplicar.
            """
            try:
                from core.working_memory import artifacts

                store = artifacts.store_for(db)
            except Exception as exc:  # noqa: BLE001 — caderno nunca derruba o job
                logger.debug("[BrowserLocal] registro de artefato indisponível: %s", exc)
                return
            if store is None:
                return
            await artifacts.record(
                store,
                user_id=job.user_id,
                session_id=job.session_id,
                items=[
                    {
                        "name": item.get("name"),
                        "path": item.get("path"),
                        "bytes": item.get("bytes"),
                        "incomplete": item.get("incomplete"),
                        "source": "baixado pelo navegador local",
                    }
                    for item in items
                    if item.get("name") and item.get("path")
                ],
                note=nota,
            )

        async def on_download(items: list[dict]) -> None:
            await registrar(items)

        async def on_progress(note: str) -> None:
            try:
                await db.update_agent_job(
                    job.id, progress_note=note[:200], heartbeat_at=datetime.utcnow()
                )
            except Exception:  # noqa: BLE001
                pass

        try:
            await db.update_agent_job(
                job.id,
                timeout_at=datetime.utcnow() + timedelta(seconds=job_timeout_s + 30),
                heartbeat_at=datetime.utcnow(),
                progress_note="Abrindo o navegador na sua máquina…",
            )
        except Exception:  # noqa: BLE001
            pass

        # Confere ANTES de prometer: o desktop pode estar aberto e o navegador
        # fechado (ou a extensão desligada) — são falhas diferentes e o usuário
        # precisa saber qual é (§5.4).
        try:
            # O health precisa perguntar pelo backend que ESTA tarefa vai usar:
            # headless não depende de extensão nem de navegador aberto, e
            # perguntar pela extensão matava todo job headless na largada.
            await act("browser.health", {"mode": "headless"} if headless else {}, 8.0)
        except DeviceOffline:
            return {
                "status": "failed",
                "result_summary": (
                    "Seu computador não está com o canal do Neo aberto agora "
                    "(desktop fechado?). Quer que eu faça no meu navegador do servidor?"
                ),
            }
        except DeviceTimeout:
            return {
                "status": "failed",
                "result_summary": (
                    "Sua máquina recebeu o pedido mas não respondeu a tempo. "
                    "Quer que eu tente no meu navegador do servidor?"
                ),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failed",
                "result_summary": (
                    f"O navegador local não está pronto: {exc}. "
                    "Confira se o navegador está aberto com a extensão do Neo ativa — "
                    "ou peça para eu usar o navegador do servidor."
                ),
            }

        # Modelos resolvidos AGORA, não no boot. O painel (Configurações →
        # Modelos) grava a escolha no banco e ela tem que valer no próximo job:
        # amarrar no closure do lifespan obrigaria a reiniciar o neo-core para
        # trocar o olho do navegador — que é justamente o que se quer trocar
        # depressa quando uma chave fica sem crédito.
        step_model, eye_model = await _resolve_models(db, model, vision_model)

        # Em qual aba. O `spec` manda quando o cérebro pediu continuidade; sem
        # ele, a aba que a última tarefa deste device deixou aberta.
        chave_navegador = chave_do_navegador(superficie, device_id)
        aba = await _resolver_aba(db, job, device_id, spec, chave_navegador)

        # Diário de bordo da tarefa (core/browser_local/trace.py). Nasce ANTES do
        # driver para pegar também o cabeçalho e qualquer falha de partida.
        trace = RunTrace(trace_dir, str(job.id)) if trace_dir else None
        if trace is not None:
            trace.header(**{
                "tarefa": task,
                "contexto": str(spec.get("context") or ""),
                "start_url": str(spec.get("start_url") or ""),
                "device": device_id,
                "modelo": step_model,
                "visão": eye_model or "desligada",
                "teto de passos": int(spec.get("max_steps") or max_steps),
                "aba": f"reaproveitando a {aba}" if aba else "nova",
                "autorizado a clicar": str(spec.get("resume_pending") or "") or "—",
                "início (UTC)": datetime.utcnow().isoformat(timespec="seconds"),
            })
            try:
                await db.update_agent_job(job.id, log_path=str(trace.dir)[:512])
            except Exception:  # noqa: BLE001 — diário não é motivo para falhar o job
                pass

        driver = BrowserLocalDriver(
            act=act,
            llm=llm_router,
            model=step_model,
            max_steps=int(spec.get("max_steps") or max_steps),
            blocked_domains=blocked_domains,
            confirm_sensitive=confirm_sensitive,
            allow_sensitive=bool(spec.get("confirm_sensitive_ok")),
            # Retomada: a autorização vale para O clique que parou a tarefa
            # anterior, não para toda ação irreversível do resto do job.
            allow_sensitive_label=str(spec.get("resume_pending") or ""),
            tab_ref=aba,
            on_progress=on_progress,
            max_screenshots=max_screenshots,
            vision_model=eye_model,
            trace=trace,
            follow_download=spec.get("follow_download"),
            # Sem janela, salvo quando o usuário quis VER (ver `_gate_browser`).
            headless=headless,
            on_download=on_download,
            # Preenchimento escopado (F3b): o driver substitui `«pks:campo»`
            # só com liberação aprovada para o domínio da aba. O dono do dado
            # vem do JOB, nunca do texto do modelo.
            filler=_build_filler(db, get_pks_service() if get_pks_service else None),
            user_id=job.user_id,
        )

        try:
            outcome = await driver.run(
                task,
                context=str(spec.get("context") or ""),
                start_url=str(spec.get("start_url") or ""),
            )
        except DeviceOffline:
            return {
                "status": "failed",
                "result_summary": (
                    "Perdi o contato com a sua máquina no meio da tarefa — parei onde estava."
                ),
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("[BrowserLocal] job %s explodiu", job.id)
            return {
                "status": "failed",
                "result_summary": f"A tarefa no seu navegador falhou: {exc}",
            }

        # Onde a aba parou fica registrado ANTES de montar o resumo: é o que a
        # próxima tarefa lê para continuar dali em vez de abrir aba nova.
        await _gravar_sessao(db, job, device_id, outcome, chave_navegador)

        # O aviso de passos falhos já vem do driver (_honest) — aqui só o lugar
        # onde a aba parou, que é o que permite continuar de lá.
        summary = outcome.summary
        if outcome.url:
            summary += f"\n\n(a aba ficou em {outcome.url})"

        # Instrução acionável para o cérebro. Sem ela ele reimprovisa a tarefa
        # do zero quando o usuário autoriza — foi o que produziu três jobs
        # mortos em sequência, cada um com um texto novo e pior que o anterior.
        if getattr(outcome, "pending", ""):
            summary += (
                f'\n\n⏸ PARADO ESPERANDO VOCÊ. Se o usuário autorizar, chame '
                f'`browser_local_task` com `continue_job="{job.id}"` e MAIS NADA — a aba, '
                f"a tarefa e a permissão do clique vêm de lá. Não reescreva a tarefa."
            )

        # Evidência de fora da página. Vale tanto para confirmar o sucesso quanto
        # para o usuário descobrir na hora que o mesmo arquivo veio duas vezes.
        if driver.downloaded:
            itens = "; ".join(
                f"{item.get('name')} ({item.get('bytes', 0)} bytes"
                + (", ainda baixando)" if item.get("incomplete") else ")")
                for item in driver.downloaded[:5]
            )
            plural = "arquivos" if len(driver.downloaded) > 1 else "arquivo"
            summary += f"\n\n⬇ {len(driver.downloaded)} {plural} na sua pasta de downloads: {itens}"
            # Confirmação no registro: mesma linha, agora com o tamanho final.
            await registrar(driver.downloaded)

        # Download ainda vindo: a tarefa do navegador acabou, mas o arquivo não.
        # Quem espera é um job de vigia — laço em código, sem gastar passo do
        # agente, e o usuário recebe o veredito no chat quando houver um.
        if driver.download_pending and not driver.follow_download:
            await _enfileirar_vigia(
                db, job, device_id, driver.download_pending, driver.download_pending_id
            )
            summary += (
                f"\n\n⏳ {driver.download_pending} ainda está baixando — eu aviso quando "
                "terminar."
            )

        if trace is not None:
            # O rodapé quem escreve é o driver (passa por todos os finais).
            summary += f"\n\n(diário do passo a passo: {trace.dir})"

        terminal = {
            "status": "completed" if outcome.status == "completed" else "failed",
            "result_summary": summary,
        }

        images = await ingest_shots(
            _resolve_store(get_file_store),
            [
                {"b64": data, "mime": "image/jpeg", "caption": "tela do seu navegador", "kind": "step"}
                for data in outcome.screenshots
            ],
            user_id=job.user_id,
            session_id=job.session_id,
            label="browser-local",
            source={"job_id": str(job.id), "url": outcome.url},
        )
        if images:
            terminal["extra_metadata"] = {**(job.extra_metadata or {}), "images": images}
            plural = "prints" if len(images) > 1 else "print"
            terminal["result_summary"] += f"\n\n📸 {len(images)} {plural} da tela em anexo."

        logger.info(
            "[BrowserLocal] job %s → %s em %d passo(s)",
            job.id, outcome.status, len(outcome.steps),
        )
        return terminal

    return run_browser_local_job
