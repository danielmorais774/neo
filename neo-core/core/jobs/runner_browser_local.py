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
    db: Any,
    job: AgentJob,
    device_id: str,
    outcome: Any,
    chave: str | None = None,
    extra: dict | None = None,
) -> dict:
    """Deixa registrado onde a tarefa parou, para a próxima continuar dali.

    `browser.release` DETACHA mas não fecha a aba — ela fica aberta na página. O
    que se perdia era só o id, e com ele a chance de continuar: em 2026-09-01 o
    agente chegou no arquivo, parou para pedir confirmação de exclusão, o
    usuário autorizou, e a tarefa seguinte recomeçou em `about:blank`.

    **Devolve o `extra_metadata` resultante**, e isso não é conveniência: quem
    escreve depois (os prints) montava o dele a partir do `job.extra_metadata`
    EM MEMÓRIA, que é de antes desta gravação — e apagava a sessão de aba. O
    efeito era invisível job a job e total no conjunto: medido em 2026-10-01,
    **todo** job com `images` tinha `browser_session` nulo. Virou 100% quando o
    print do fim passou a existir sempre, e foi o que fez a tarefa seguinte
    abrir aba nova depois de o usuário dizer "na aba que já está aberta".

    ``extra`` entra na mesma sessão de aba: é onde mora o ``release_pending``,
    a liberação que esta tarefa está esperando — é por ele que a aprovação acha
    o job para continuar.
    """
    atual = dict(job.extra_metadata or {})
    if not device_id or outcome is None or not getattr(outcome, "tab_ref", None):
        return atual
    atual = {
        **atual,
        "browser_session": {
            "device_id": device_id,
            # A chave do NAVEGADOR (a máquina), que é o que sobrevive à troca de
            # via — ver `chave_do_navegador`.
            "browser_key": chave or device_id,
            "tab_ref": int(outcome.tab_ref),
            "url": (outcome.url or "")[:500],
            # Só existe quando a tarefa PAROU para perguntar. É a chave que
            # autoriza exatamente aquele clique na retomada.
            "pending": (getattr(outcome, "pending", "") or "")[:120],
            "task": (job.spec_json or {}).get("task", "")[:2000],
            "status": outcome.status,
            "at": datetime.utcnow().isoformat(timespec="seconds"),
            **(extra or {}),
        },
    }
    try:
        await db.update_agent_job(job.id, extra_metadata=atual)
    except Exception as exc:  # noqa: BLE001 — registro não pode falhar o job
        logger.warning("[BrowserLocal] não consegui gravar a sessão de aba: %s", exc)
    return atual


def _resolve_store(getter: Callable[[], Any] | None) -> Any:
    if getter is None:
        return None
    try:
        return getter()
    except Exception:  # noqa: BLE001
        return None


def _release_service(db: Any) -> Any:
    """O serviço de liberação COM o card: é daqui que o job pede, sozinho."""
    try:
        from core.release import ReleaseService
        from core.release.notify import build_release_notifier

        return ReleaseService(db, notifier=build_release_notifier(db))
    except Exception:  # noqa: BLE001 — sem serviço, não há liberação a pedir
        logger.exception("[browser_local] serviço de liberação indisponível")
        return None


class _LLMDoRespondedor:
    """O LLM do respondedor: o modelo do passo, JSON, temperatura baixa, sem raciocínio."""

    def __init__(self, llm_router: Any, model: str) -> None:
        self._llm = llm_router
        self._model = model

    async def complete(self, system: str, user: str, *, max_tokens: int) -> str:
        from shared.types import Message, MessageRole

        provider = self._llm.get_provider_for_model(self._model)
        resposta = await provider.complete(
            messages=[
                Message(role=MessageRole.SYSTEM, content=system),
                Message(role=MessageRole.USER, content=user),
            ],
            model=self._model,
            temperature=0.1,
            max_tokens=max_tokens,
            # Raciocínio ligado come o teto e devolve content vazio
            # (deepseek-empty-response-reasoning).
            disable_thinking=True,
            response_format={"type": "json_object"},
        )
        return resposta.get_content() or ""


def _build_respondedor(pks_service: Any, llm_router: Any, model: str, job: AgentJob) -> Any:
    """Respondedor do `consultar`, ou ``None`` quando não há memória para consultar."""
    if pks_service is None or job.user_id is None:
        return None
    if not hasattr(pks_service, "evidencia_de_formulario"):
        return None
    from core.browser_local.respondedor import Respondedor

    async def evidencia(perguntas: list[str]) -> dict:
        return await pks_service.evidencia_de_formulario(user_id=job.user_id, perguntas=perguntas)

    return Respondedor(
        evidencia=evidencia,
        llm=_LLMDoRespondedor(llm_router, model),
        finalidade=_finalidade_declarada(job),
    )


def _respostas_para_sessao(outcome: Any) -> dict:
    """O que a memória respondeu, para a sessão de aba de uma tarefa PARADA.

    Quem continua (pela conferência ou pela liberação) preenche com isto, e não
    com uma consulta nova que poderia redigir diferente do que o Daniel viu.
    """
    respostas = list(getattr(outcome, "respostas", None) or [])
    if respostas and getattr(outcome, "status", "") in ("needs_confirmation", "needs_release"):
        return {"respostas_memoria": respostas}
    return {}


def _com_revisao(outcome: Any, summary: str, itens: list[str]) -> tuple[str, list[str]]:
    """A conferência antes do envio: o que vai em cada campo e de onde veio.

    Vai ÍNTEGRA, na frente do caderno — um treino reescrito pelo redator não
    seria mais o que está na página.
    """
    revisao = list(getattr(outcome, "revisao", None) or [])
    if not revisao:
        return summary, itens
    summary += "\n\nO que vai no formulário:\n" + "\n".join(f"- {r}" for r in revisao)
    return summary, [*revisao, *itens]


def _build_filler(db: Any, pks_service: Any, service: Any = None) -> Any:
    """ReleaseFiller do job, ou ``None`` quando não há memória para resolver."""
    if pks_service is None:
        return None
    try:
        from core.release import ReleaseFiller, ReleaseService

        return ReleaseFiller(service or ReleaseService(db), pks_service)
    except Exception:  # noqa: BLE001 — sem filler o placeholder só não resolve
        logger.exception("[browser_local] não consegui montar o preenchimento escopado")
        return None


def _finalidade_declarada(job: AgentJob) -> str:
    """Para que serve o dado, nas palavras de quem pediu: o pedido do Daniel e o
    título da tarefa (que é a finalidade que o card mostra).

    Não é o texto da tarefa: aquilo é instrução do cérebro para o agente, e
    instrução traz palavra solta. Na sessão 9b2e29e2 ela dizia "use o e-mail do
    Neo quando pedir contato do respondente"; a tabela leu "contato" e barrou
    CPF, CEP e endereço num formulário que perguntava exatamente isso. Job sem o
    pedido gravado (anterior a 2026-11-01, ou da agenda) cai na tarefa.
    """
    spec = job.spec_json or {}
    base = str(spec.get("pedido") or "").strip() or str(spec.get("task") or "")
    return "\n".join(p for p in (base, str(job.title or "").strip()) if p)


async def _pedir_liberacao(
    *, job: AgentJob, outcome: Any, filler: Any, service: Any
) -> dict:
    """A tarefa parou num dado sem liberação: o JOB pede, sem passar pelo cérebro.

    Até 2026-11-01 quem pedia era o cérebro, lendo o motivo no resultado e
    chamando ``solicitar_dado``. Medido na sessão 1a605544: o motivo não
    chegava (o freio de repetição o engolia), e o agente do SERVER_TOOL nem tinha
    a tool — respondeu "ficou esperando aprovação" sem pedido nenhum criado.
    Pedir aqui não depende de o modelo obedecer, e é a mesma regra de sempre: a
    tabela de necessidade antes, o card com a origem real do turno, e só o Daniel
    aprova.

    Devolve ``{"id", "pedir", "faltando", "barrados", "host", "erro"}``. ``id``
    vazio = nenhum card nasceu (tudo faltando na memória, tudo injustificado, ou
    o serviço recusou) — e aí não há o que esperar.
    """
    pedido = dict(getattr(outcome, "release", None) or {})
    host = str(pedido.get("host") or "").strip()
    candidatos = [str(c).strip().lower() for c in pedido.get("campos") or [] if str(c).strip()]
    spec = job.spec_json or {}
    tarefa = str(spec.get("task") or "")
    saida: dict = {
        "id": "", "pedir": [], "faltando": [], "barrados": [], "host": host, "erro": "",
    }
    if not host or not candidatos:
        saida["erro"] = "não sei em que site a tarefa parou"
        return saida

    # Tabela de necessidade ANTES de tudo: o que a finalidade não justifica não
    # vira card — vira recado. Texto convincente da página não muda a tabela.
    from core.release import campos_desnecessarios, explicar
    from core.release.necessity import finalidade_conhecida

    declarada = _finalidade_declarada(job)
    barrados = campos_desnecessarios(declarada, candidatos)
    saida["barrados"] = barrados
    if barrados:
        # O nome da finalidade ("frete"), não o pedido inteiro com link dentro.
        saida["explicacao"] = explicar(
            finalidade_conhecida(declarada) or declarada[:80], barrados
        )
    restantes = [c for c in candidatos if c not in barrados]

    # Aprovar o que a memória não tem só adia a recusa para o passo seguinte.
    pedir: list[str] = []
    for campo in restantes:
        motivo = None
        if filler is not None:
            try:
                motivo = await filler.disponivel(user_id=job.user_id, campo=campo)
            except Exception:  # noqa: BLE001 — na dúvida, deixa ele decidir no card
                motivo = None
        if motivo:
            saida["faltando"].append(campo)
        else:
            pedir.append(campo)
    saida["pedir"] = pedir
    if not pedir:
        return saida
    if service is None:
        saida["erro"] = "o serviço de liberação não está disponível"
        return saida

    # A origem é a do TURNO que criou o job, gravada no spec pelo executor: o job
    # roda fora de turno e não tem autoridade própria. Sem ela, o pedido sai
    # como do Daniel — que é o que ele é quando ele mesmo pediu a tarefa.
    from core.release import ReleaseError, motivos_de_origem

    autoridade = spec.get("autoridade") or {}
    origem = str(autoridade.get("principal") or "admin")
    if origem not in ("admin", "externo", "agenda"):
        origem = "admin"
    rotulos = [str(r) for r in pedido.get("rotulos") or [] if str(r).strip()]
    try:
        vista = await service.create(
            user_id=job.user_id,
            session_id=job.session_id,
            campos=[f"pks:{c}" for c in pedir],
            destino=host,
            destino_tipo="dominio",
            finalidade=str(job.title or tarefa)[:200],
            origem=origem,
            origem_ref=autoridade.get("principal_ref"),
            evidencia=("a página pede: " + " · ".join(rotulos)) if rotulos else "",
            risco_motivos=motivos_de_origem(
                origem,
                autoridade.get("principal_ref"),
                list(autoridade.get("taint_sources") or []),
                tainted=autoridade.get("tainted"),
            ),
        )
    except ReleaseError as exc:
        saida["erro"] = exc.message
        return saida
    except Exception as exc:  # noqa: BLE001 — sem card a tarefa só não continua
        logger.exception("[BrowserLocal] job %s: não consegui criar o pedido", job.id)
        saida["erro"] = str(exc)
        return saida
    saida["id"] = str(vista.id)
    logger.warning(
        "[BrowserLocal] job %s parado esperando a liberação %s (%s → %s)",
        job.id, vista.id, ",".join(pedir), host,
    )
    return saida


def _estado_final(outcome: Any, pedido_liberacao: dict | None) -> str:
    """O estado do job no banco: ``completed``, ``waiting`` ou ``failed``.

    Parada ESPERANDO o Daniel não é falha: o card está no aparelho (ou a
    pergunta do clique, no chat) e a tarefa continua de lá. Como ``failed``, o
    redator abria a mensagem com "A tarefa falhou" e a janela de tarefas mostrava
    "falhou" (sessão 9b2e29e2). Sem card criado não há o que esperar — aí é falha.
    """
    if outcome.status == "completed":
        return "completed"
    if (pedido_liberacao or {}).get("id") or getattr(outcome, "pending", ""):
        return "waiting"
    return "failed"


def _texto_da_pausa(job: AgentJob, pedido: dict) -> tuple[str, list[str], str]:
    """``(fecho, itens, resumo_extra)`` da tarefa que parou pedindo liberação.

    Fatos íntegros (``itens``): o que foi pedido, onde aprovar e que a tarefa
    CONTINUA SOZINHA — e, para o cérebro, que não há nada a pedir de novo. O
    "sim" no chat não aprova nada (a aprovação exige o aparelho pareado), e foi
    exatamente o que ele respondeu na sessão 1a605544.
    """
    from core.release.sensitive import humano

    def _nomes(campos: list[str]) -> str:
        return ", ".join(humano(c) for c in campos)

    host = pedido.get("host") or "o site"
    itens: list[str] = []
    if pedido.get("id"):
        itens.append(
            f"⏸ ESPERANDO A SUA LIBERAÇÃO: pedi {_nomes(pedido['pedir'])} para {host}. "
            "O aviso está nos seus aparelhos — aprove por lá (um \"sim\" aqui no chat "
            "não vale como aprovação). Assim que você aprovar, esta tarefa continua "
            "sozinha, de onde parou."
        )
        itens.append(
            f"(para o Neo: o pedido {pedido['id']} já existe — NÃO chame solicitar_dado "
            "nem refaça a tarefa. Se ele disser que aprovou e nada aconteceu, chame "
            f'`browser_local_task` com `continue_job="{job.id}"` e mais nada.)'
        )
    if pedido.get("faltando"):
        itens.append(
            f"Não tenho na memória: {_nomes(pedido['faltando'])}. Sem isso esses campos "
            "ficam em branco — me diga o valor, ou guarde na memória, e eu sigo."
        )
    if pedido.get("barrados"):
        itens.append(
            str(pedido.get("explicacao") or "").strip()
            or f"{_nomes(pedido['barrados'])} não se justifica para esta tarefa — não pedi."
        )
    if pedido.get("erro") and not pedido.get("id"):
        itens.append(f"Não consegui pedir a liberação: {pedido['erro']}.")
    fecho = (
        "⏸ Parei e pedi a sua liberação para continuar."
        if pedido.get("id")
        else "Parei: falta dado seu para continuar."
    )
    return fecho, itens, "\n\n".join(itens)


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
            # O conselho acompanha a CAUSA. Enquanto "não achei o registro do
            # socket" e "a extensão não respondeu" saíam com o mesmo texto, o
            # usuário era mandado conferir a extensão — que estava ligada o tempo
            # todo (medido 2026-10-05: host de pé, socket respondendo `pong`, só
            # o `active.json` apagado pela limpeza do /tmp). Pior: essa frase
            # errada virou nota no caderno, e no turno seguinte o cérebro
            # desistiu do navegador e foi de `curl`.
            texto = str(exc)
            if "não está no ar" in texto:
                conselho = (
                    "São duas causas possíveis e elas pedem coisas diferentes: (a) a "
                    "extensão está mesmo desligada ou o navegador fechado — abra e "
                    "confira o ícone do Neo; (b) o host está vivo e quem sumiu foi só o "
                    "registro `/tmp/open-browser-use/active.json`, que a limpeza diária "
                    "do /tmp apaga depois de 3 dias sem uso (o socket sobrevive) — aí o "
                    "conserto é reiniciar o `tauri dev`, porque a versão nova do desktop "
                    "varre o diretório e acha o socket sozinho. Em qualquer uma das duas "
                    "eu também posso fazer no navegador do servidor, se você preferir."
                )
            else:
                conselho = (
                    "Confira se o navegador está aberto com a extensão do Neo ativa — "
                    "ou peça para eu usar o navegador do servidor."
                )
            return {
                "status": "failed",
                "result_summary": f"O navegador local não está pronto: {texto}. {conselho}",
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
                # BASE, não teto: produção devolve passo (até o dobro) e a falta de
                # retorno encerra antes. Ver `_teto_de_passos` no driver.
                "orçamento de passos": int(spec.get("max_steps") or max_steps),
                "aba": f"reaproveitando a {aba}" if aba else "nova",
                "autorizado a clicar": str(spec.get("resume_pending") or "") or "—",
                "início (UTC)": datetime.utcnow().isoformat(timespec="seconds"),
            })
            try:
                await db.update_agent_job(job.id, log_path=str(trace.dir)[:512])
            except Exception:  # noqa: BLE001 — diário não é motivo para falhar o job
                pass

        # Um serviço para as duas pontas da liberação: o preenchimento LÊ as
        # aprovações e o pedido de agora CRIA o card. O mesmo objeto garante que
        # o job veja o que ele mesmo acabou de pedir.
        liberacoes = _release_service(db)
        pks_service = get_pks_service() if get_pks_service else None
        filler = _build_filler(db, pks_service, liberacoes)

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
            filler=filler,
            user_id=job.user_id,
            finalidade=_finalidade_declarada(job),
            # `consultar`: a memória responde o formulário pergunta por pergunta.
            respondedor=_build_respondedor(pks_service, llm_router, step_model, job),
            # Retomada: o que a parada anterior respondeu e o Daniel conferiu.
            respostas_previas=list(spec.get("respostas_memoria") or []),
            pedido=str(spec.get("pedido") or ""),
        )

        try:
            outcome = await driver.run(
                task,
                context=str(spec.get("context") or ""),
                start_url=str(spec.get("start_url") or ""),
                # Continuação de uma tarefa que parou: se a aba dela sumiu, é
                # este endereço que se reabre (ver `core/jobs/release_resume.py`).
                reabrir_url=str(spec.get("reabrir_url") or ""),
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
        # próxima tarefa lê para continuar dali em vez de abrir aba nova. O
        # retorno é o metadata VIVO — quem escrever depois parte dele, nunca da
        # cópia em memória do `job` (ver a nota em `_gravar_sessao`).
        #
        # Parou num dado sem liberação: o pedido nasce AQUI, e o id dele vai para
        # a sessão de aba — é por ele que a aprovação acha esta tarefa e a
        # continua (`core/jobs/release_resume.py`). O fluxo não é linear: entre
        # o card e o clique em "Liberar" podem passar horas, e nada no meio
        # depende de o Daniel repetir o pedido no chat.
        pedido_liberacao: dict | None = None
        extra_sessao: dict = {}
        if outcome.status == "needs_release":
            pedido_liberacao = await _pedir_liberacao(
                job=job, outcome=outcome, filler=filler, service=liberacoes
            )
            if pedido_liberacao.get("id"):
                extra_sessao = {
                    "release_pending": pedido_liberacao["id"],
                    "release_campos": list(pedido_liberacao.get("pedir") or []),
                    "release_host": pedido_liberacao.get("host") or "",
                }
        extra_sessao.update(_respostas_para_sessao(outcome))
        metadata = await _gravar_sessao(
            db, job, device_id, outcome, chave_navegador, extra=extra_sessao
        )

        # O aviso de passos falhos já vem do driver (_honest) — aqui só o lugar
        # onde a aba parou, que é o que permite continuar de lá.
        summary = outcome.summary
        # A ENTREGA, em duas metades. O `result_summary` acima continua igual —
        # é contrato com o cérebro e com o caderno do servidor. Aqui ao lado se
        # declara, pedaço por pedaço, o que é PROSA (pode ser reescrito na
        # persona sem custo) e o que é FATO (tem de chegar caractere por
        # caractere). Preço, nome de arquivo, caminho e uuid de retomada não
        # sobrevivem a uma paráfrase — e hoje passam por uma.
        fecho: list[str] = [outcome.fecho or outcome.summary]
        itens: list[str] = list(outcome.notes)
        summary, itens = _com_revisao(outcome, summary, itens)
        if outcome.url:
            summary += f"\n\n(a aba ficou em {outcome.url})"
            fecho.append(f"(a aba ficou em {outcome.url})")

        # Instrução acionável para o cérebro. Sem ela ele reimprovisa a tarefa
        # do zero quando o usuário autoriza — foi o que produziu três jobs
        # mortos em sequência, cada um com um texto novo e pior que o anterior.
        if getattr(outcome, "pending", ""):
            retomada = (
                f'⏸ PARADO ESPERANDO VOCÊ. Se o usuário autorizar, chame '
                f'`browser_local_task` com `continue_job="{job.id}"` e MAIS NADA — a aba, '
                f"a tarefa e a permissão do clique vêm de lá. Não reescreva a tarefa."
            )
            summary += f"\n\n{retomada}"
            # VERBATIM, e não prosa: o cérebro só aprende este uuid lendo a
            # mensagem entregue, e até agora ele chegava lá depois de passar
            # pelo redator. Um uuid parafraseado não retoma nada — e o "MAIS
            # NADA / não reescreva a tarefa" é o que impediu três jobs mortos em
            # sequência, então ele também não pode ser amaciado.
            itens.append(retomada)
            # E uma linha de PROSA no fecho, para o redator abrir a mensagem
            # falando da autorização em vez de anunciar uma tarefa concluída. O
            # fato (o uuid) vai acima, íntegro; isto aqui é só o enquadramento.
            fecho.append("⏸ Parei e preciso da sua autorização para continuar.")

        if pedido_liberacao is not None:
            linha, fatos, extra_resumo = _texto_da_pausa(job, pedido_liberacao)
            fecho.append(linha)
            # Fatos íntegros, como o uuid da retomada acima: "aprove no aviso,
            # não no chat" e o id do pedido não sobrevivem a uma paráfrase.
            itens.extend(fatos)
            if extra_resumo:
                summary += f"\n\n{extra_resumo}"

        # Evidência de fora da página. Vale tanto para confirmar o sucesso quanto
        # para o usuário descobrir na hora que o mesmo arquivo veio duas vezes.
        if driver.downloaded:
            nomes = "; ".join(
                f"{item.get('name')} ({item.get('bytes', 0)} bytes"
                + (", ainda baixando)" if item.get("incomplete") else ")")
                for item in driver.downloaded[:5]
            )
            plural = "arquivos" if len(driver.downloaded) > 1 else "arquivo"
            baixados = f"⬇ {len(driver.downloaded)} {plural} na sua pasta de downloads: {nomes}"
            summary += f"\n\n{baixados}"
            itens.append(baixados)  # nome de arquivo não se parafraseia
            # Confirmação no registro: mesma linha, agora com o tamanho final.
            await registrar(driver.downloaded)

        # Download ainda vindo: a tarefa do navegador acabou, mas o arquivo não.
        # Quem espera é um job de vigia — laço em código, sem gastar passo do
        # agente, e o usuário recebe o veredito no chat quando houver um.
        if driver.download_pending and not driver.follow_download:
            await _enfileirar_vigia(
                db, job, device_id, driver.download_pending, driver.download_pending_id
            )
            vindo = (
                f"⏳ {driver.download_pending} ainda está baixando — eu aviso quando "
                "terminar."
            )
            summary += f"\n\n{vindo}"
            itens.append(vindo)  # o nome do arquivo é o que o usuário vai procurar

        if trace is not None:
            # O rodapé quem escreve é o driver (passa por todos os finais).
            summary += f"\n\n(diário do passo a passo: {trace.dir})"
            itens.append(f"(diário do passo a passo: {trace.dir})")  # caminho

        terminal = {
            "status": _estado_final(outcome, pedido_liberacao),
            "result_summary": summary,
            # Sempre, e não só quando há print: escrever esta chave sobrescreve a
            # coluna JSON inteira, então o `metadata` que o `_gravar_sessao`
            # acabou de persistir tem de vir junto — era o que a linha dos
            # prints já fazia, e agora ela não é mais o único caminho.
            "extra_metadata": {
                **metadata,
                "entrega": {"fecho": "\n\n".join(p for p in fecho if p), "itens": itens},
            },
        }

        # A legenda vem do driver, que é quem sabe qual print é o do FIM. Sem
        # ela as três imagens chegavam com o mesmo rótulo e o usuário tinha que
        # adivinhar qual mostrava a resposta. `zip` curto não perde imagem: o
        # fallback cobre o driver que não mandou legenda nenhuma.
        legendas = list(outcome.screenshot_captions)
        images = await ingest_shots(
            _resolve_store(get_file_store),
            [
                {
                    "b64": data,
                    "mime": "image/jpeg",
                    "caption": (
                        legendas[i] if i < len(legendas) else "tela do seu navegador"
                    ),
                    "kind": "step",
                }
                for i, data in enumerate(outcome.screenshots)
            ],
            user_id=job.user_id,
            session_id=job.session_id,
            label="browser-local",
            source={"job_id": str(job.id), "url": outcome.url},
        )
        if images:
            terminal["extra_metadata"]["images"] = images
            plural = "prints" if len(images) > 1 else "print"
            anexo = f"📸 {len(images)} {plural} da tela em anexo."
            terminal["result_summary"] += f"\n\n{anexo}"
            # Prosa: uma frase que o redator pode reescrever à vontade. Os
            # arquivos em si vão pelo `_append_images`, que já passa por fora.
            terminal["extra_metadata"]["entrega"]["fecho"] += f"\n\n{anexo}"

        logger.info(
            "[BrowserLocal] job %s → %s em %d passo(s)",
            job.id, outcome.status, len(outcome.steps),
        )
        return terminal

    return run_browser_local_job
