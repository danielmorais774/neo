"""Curated READ-ONLY toolset for the cognitive core + a per-request executor.

The cognitive core (DIRECT_RESPONSE/COGNITIVE) may call these to read/verify
ground truth INLINE (PKS, devices, weather) before answering — never to act.
Side-effecting actions stay behind handoff. See docs/cognitive-agentic-tools.md.

MVP toolset (all globally dispatchable today): search_pks, list_connected_devices,
get_weather. ha_get_state is deferred until a home_assistant module executor is
wired into the global ToolExecutor.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from core.authority import gate_tool_call, note_tool_result
from core.files.tools import FILE_TOOLS
from modules.config.tools import CONFIG_TOOLS
from modules.conversation_recall import (
    CONVERSATION_RECALL_TOOL_NAMES,
    CONVERSATION_RECALL_TOOLS,
)
from modules.devices.tools import DEVICE_CONTROL_TOOLS, DEVICES_TOOLS
from modules.finance import FINANCE_TOOL_NAMES
from modules.location.tools import LOCATION_TOOLS
from modules.pks_search.tools import PKS_SEARCH_TOOLS
from modules.pks_write import PKS_WRITE_TOOL_NAMES, PKS_WRITE_TOOLS
from modules.release.tools import RELEASE_TOOL_NAMES, RELEASE_TOOLS
from modules.services.tools import SERVICE_TOOLS
from modules.skills_library.tools import SKILLS_LIBRARY_TOOLS
from modules.datasets.tools import DATA_DEFINE_TOOL_NAMES, DATA_TOOL_NAMES
from modules.events.tools import EVENT_TOOL_NAMES
from modules.tool_index.tools import TOOL_INDEX_TOOL_NAMES

#: data_* + data_define — todas user-scoped (os cadastros são de um dono).
DATASET_TOOL_NAMES = DATA_TOOL_NAMES | DATA_DEFINE_TOOL_NAMES
from modules.voice.tools import VOICE_TOOLS
from modules.weather.tools import WEATHER_TOOLS
from modules.web_search.tools import WEB_SEARCH_TOOLS
from modules.working_memory.tools import WORKING_MEMORY_TOOL_NAMES, WORKING_MEMORY_TOOLS
from shared import ToolCall, ToolResult, ToolSchema

logger = logging.getLogger(__name__)


def _by_name(tools: list[ToolSchema], name: str) -> ToolSchema:
    return next(t for t in tools if t.name == name)


COGNITIVE_READ_TOOLS: list[ToolSchema] = [
    _by_name(PKS_SEARCH_TOOLS, "search_pks"),
    # F2 (docs/release-request-plan.md §7): a volta da redação. Fica no read
    # set porque o dono do dado perguntando o próprio dado é o caso NORMAL —
    # quem decide se a chamada vale num turno contaminado é o portão.
    _by_name(PKS_SEARCH_TOOLS, "pks_reveal"),
    # Arquivo de conversas (docs/conversation-recall-plan.md): o cognitivo só tem
    # no contexto as mensagens DESTA sessão — "a gente já falou de X?" só tem
    # resposta honesta indo ao banco. Sem ela o modelo nega de cabeça (foi o que
    # aconteceu na session-860d0fb1: "não tem nada sobre a França registrado",
    # com 61 mensagens sobre França no banco).
    _by_name(CONVERSATION_RECALL_TOOLS, "conversation_recall"),
    _by_name(DEVICES_TOOLS, "list_connected_devices"),
    # Controle de dispositivo (docs/device-control-plan.md): as 2ª e 3ª tools com
    # efeito colateral neste conjunto, junto de pks_remember/wm_write. Estão aqui
    # e não atrás de handoff porque o efeito é imediato e verificável no mesmo
    # turno — o device responde com o estado REAL, então o cérebro pode dizer
    # "ficou em 30" em vez de "mandei abaixar". Gated em cognitive_tools_for.
    _by_name(DEVICE_CONTROL_TOOLS, "device_control"),
    _by_name(DEVICE_CONTROL_TOOLS, "device_power"),
    _by_name(LOCATION_TOOLS, "get_user_location"),
    _by_name(WEATHER_TOOLS, "get_weather"),
    # Busca web inline (docs/web-search-deep-research-plan.md): leitura GLOBAL
    # (fora de _USER_SCOPED), despachada pelo executor global como get_weather.
    # Gated em cognitive_tools_for por web_search_enabled.
    _by_name(WEB_SEARCH_TOOLS, "web_search"),
    _by_name(WEB_SEARCH_TOOLS, "web_fetch"),
    # Read-only voice introspection (which voices exist / which is active). The
    # side-effecting config_set stays out of the cognitive read set — it routes as
    # a SERVER_TOOL action.
    _by_name(VOICE_TOOLS, "voice_list"),
    _by_name(VOICE_TOOLS, "voice_get"),
    # Read-only config introspection (what's configurable / a setting's value).
    _by_name(CONFIG_TOOLS, "config_get"),
    _by_name(CONFIG_TOOLS, "config_list"),
    # Skills library (docs/skills-library-plan.md): read a skill's instructions /
    # list the catalog. Pure reads; gated at call time by skills_library_enabled.
    _by_name(SKILLS_LIBRARY_TOOLS, "skill_read"),
    _by_name(SKILLS_LIBRARY_TOOLS, "skill_list"),
    # Service Gateway (docs/service-gateway-dsl-plan.md): descoberta (list/docs) +
    # chamada HTTP arbitrária a serviço cadastrado. service_request/service_confirm
    # NÃO são "read", mas o gate real é a POLÍTICA da conexão (escrita → confirm),
    # não a presença da tool. Todos gated por neo_gateway_enabled. Despacho pelo
    # executor global "services" (fall-through), como web_search.
    _by_name(SERVICE_TOOLS, "service_list"),
    _by_name(SERVICE_TOOLS, "service_docs"),
    _by_name(SERVICE_TOOLS, "service_request"),
    _by_name(SERVICE_TOOLS, "service_confirm"),
    # File Store (docs/file-store-plan.md): catálogo de arquivos. Ficam SEMPRE no
    # subset (gated por neo_files_enabled) porque o usuário pode citar um arquivo
    # sem anexar agora ("aquele CSV de ontem" → file_list). Quem de fato ATIVA o
    # uso é o manifesto do anexo injetado no system_context (§5) — tool disponível
    # não é tool usada (lição da skill financas).
    *FILE_TOOLS,
    # Escrita sob demanda no PKS (docs/pks-write-on-demand-plan.md): pks_remember
    # é a ÚNICA tool com efeito colateral no read set — o cognitivo a chama quando
    # o usuário pede explicitamente pra lembrar de algo. Gated por pks_write_enabled;
    # user-scoped; despacho inline por este executor. Por baixo é o subagente
    # (PKSMemoryPlanner) que estrutura — a tool não escreve cru.
    *PKS_WRITE_TOOLS,
    # Caderno de trabalho (docs/working-memory-plan.md): wm_write grava a
    # CONCLUSÃO do trabalho do turno, wm_read a reabre no turno seguinte. É a
    # 2ª tool com efeito colateral aqui, e a fronteira com pks_remember está
    # escrita nas duas descriptions — trocar uma pela outra é o risco R1.
    *WORKING_MEMORY_TOOLS,
    # Solicitação de Liberação (docs/release-request-plan.md §6): o caminho
    # honesto quando a tarefa precisa de dado redigido e o turno não é um
    # pedido limpo do dono. Fica SEMPRE disponível — se ela também passasse
    # pelo portão, a saída de emergência teria fechadura.
    *RELEASE_TOOLS,
]

# Tools that need a user to be meaningful — only offered when user_id is known.
# As fin_* NÃO entram no read set default (são gated pela skill 'financas' via
# tool gating), mas o dispatch delas passa por este executor e exige usuário.
_USER_SCOPED = frozenset(
    {
        "search_pks",
        "pks_reveal",
        "list_connected_devices",
        "get_user_location",
        "device_control",
        "device_power",
    }
    | CONVERSATION_RECALL_TOOL_NAMES
    | FINANCE_TOOL_NAMES
    | PKS_WRITE_TOOL_NAMES
    | WORKING_MEMORY_TOOL_NAMES
    | DATASET_TOOL_NAMES
    | EVENT_TOOL_NAMES
    | RELEASE_TOOL_NAMES
)

_SKILLS_LIBRARY_TOOL_NAMES = frozenset(t.name for t in SKILLS_LIBRARY_TOOLS)

_WEB_SEARCH_TOOL_NAMES = frozenset(t.name for t in WEB_SEARCH_TOOLS)

_SERVICE_TOOL_NAMES = frozenset(t.name for t in SERVICE_TOOLS)

_FILE_TOOL_NAMES = frozenset(t.name for t in FILE_TOOLS)


def cognitive_tools_for(user_id: UUID | None) -> list[ToolSchema]:
    """Read tools the cognitive core may use this turn.

    Without a user, user-scoped tools are withheld (the model can't usefully read
    a specific user's PKS/devices), leaving only global reads like get_weather.
    Skills-library tools are withheld when the feature flag is off.
    """
    from shared.config import get_settings

    settings = get_settings()
    tools = list(COGNITIVE_READ_TOOLS)
    if not settings.skills_library_enabled:
        tools = [t for t in tools if t.name not in _SKILLS_LIBRARY_TOOL_NAMES]
    if not settings.web_search_enabled:
        tools = [t for t in tools if t.name not in _WEB_SEARCH_TOOL_NAMES]
    if not getattr(settings, "neo_gateway_enabled", False):
        tools = [t for t in tools if t.name not in _SERVICE_TOOL_NAMES]
    if not getattr(settings, "neo_files_enabled", False):
        tools = [t for t in tools if t.name not in _FILE_TOOL_NAMES]
    if not getattr(settings, "pks_write_enabled", True):
        tools = [t for t in tools if t.name not in PKS_WRITE_TOOL_NAMES]
    if not getattr(settings, "working_memory_enabled", False):
        tools = [t for t in tools if t.name not in WORKING_MEMORY_TOOL_NAMES]
    if not getattr(settings, "conversation_recall_enabled", True):
        tools = [t for t in tools if t.name not in CONVERSATION_RECALL_TOOL_NAMES]
    if not getattr(settings, "neo_device_control_enabled", True):
        tools = [t for t in tools if t.name != "device_control"]
    # Energia é gated separado e default OFF: desligar a máquina errada é
    # irreversível de um jeito que abaixar o volume não é.
    if not getattr(settings, "neo_device_power_enabled", False):
        tools = [t for t in tools if t.name != "device_power"]
    # Cofre + caixa de email: só existem com NEO_CRED_KEY (o cofre é o que guarda
    # a senha da caixa). Entram INLINE porque o bloco de identidade do prompt diz
    # "você tem email, use email_inbox" — se a tool só existisse no catálogo do
    # roteador, o cérebro leria a instrução e não teria como cumpri-la. Import
    # lazy pelo mesmo motivo das DESKTOP_TOOLS abaixo: evitar ciclo no import.
    if getattr(settings, "neo_cred_key", ""):
        from core.credentials.tools import VAULT_TOOLS
        from core.mailbox.tools import EMAIL_SEND_TOOLS, EMAIL_TOOLS

        # `email_send` é a única tool deste bloco que ATUA para fora; segue a
        # flag própria, como device_power segue a dele.
        email = list(EMAIL_TOOLS)
        if getattr(settings, "neo_email_send_enabled", True):
            email += EMAIL_SEND_TOOLS
        have = {t.name for t in tools}
        tools += [t for t in (*VAULT_TOOLS, *email) if t.name not in have]
    # Tools client-actuating de arquivo (plano "arquivos sob iniciativa do
    # cérebro"): request_user_file (puxa um arquivo do disco do usuário) e
    # save_generated_file (entrega no disco). Só entram com o File Store ligado E
    # a flag de atuação. Import lazy: evita o ciclo core.cognitive ↔ modules.desktop
    # (modules.desktop.__init__ → ai_service → core.cognitive). NÃO executam
    # in-process — o loop cognitivo levanta ClientActuationRequested.
    if getattr(settings, "neo_files_enabled", False) and getattr(
        settings, "neo_file_actuation_enabled", False
    ):
        from modules.desktop.tools import DESKTOP_TOOLS, FILE_ACTUATION_TOOL_NAMES

        have = {t.name for t in tools}
        tools += [
            t
            for t in DESKTOP_TOOLS
            if t.name in FILE_ACTUATION_TOOL_NAMES and t.name not in have
        ]
    # Shell na máquina do usuário (`run_command`), mesmo trilho client-actuating.
    # Independe do File Store: o buraco que ela fecha é outro — sem ela, TODO pedido
    # que precisa do terminal e cai no cognitivo (classificador que errou, follow-up
    # curto, fallback) morre em "não tenho acesso, roda aí e me manda a saída".
    if getattr(settings, "neo_brain_shell_enabled", True):
        from modules.desktop.tools import DESKTOP_TOOLS, SHELL_ACTUATION_TOOL_NAMES

        have = {t.name for t in tools}
        tools += [
            t
            for t in DESKTOP_TOOLS
            if t.name in SHELL_ACTUATION_TOOL_NAMES and t.name not in have
        ]
    # Agregador de tools (docs/datasets-crud-plan.md §2.3): o dispatcher genérico
    # é FALLBACK — só entra no prompt quando ligado explicitamente, porque custa
    # ~150 tokens por turno e o caminho preferido continua sendo o schema tipado
    # destravado pelo skill_read.
    if getattr(settings, "neo_tool_index_enabled", False):
        from modules.tool_index.tools import TOOL_INDEX_TOOLS

        have = {t.name for t in tools}
        tools += [t for t in TOOL_INDEX_TOOLS if t.name not in have]
    if user_id is None:
        tools = [t for t in tools if t.name not in _USER_SCOPED]
    return tools


class CognitiveReadExecutor:
    """Per-request executor for the cognitive read toolset.

    Builds FRESH, user-scoped executors for the user-bound tools (no shared-state
    race) and delegates everything else to the global ToolExecutor.
    """

    def __init__(
        self,
        *,
        global_executor: Any,
        db: Any,
        pks_service: Any,
        user_id: UUID | None,
        session_id: UUID | None = None,
        embeddings: Any = None,
        current_device_id: str | None = None,
        dataset_registry: Any = None,
        event_router: Any = None,
        media_context: dict[str, Any] | None = None,
    ) -> None:
        self._global = global_executor
        self._user_id = user_id
        self._session_id = session_id
        self._devices: Any = None
        self._pks_search: Any = None
        self._location: Any = None
        self._finance: Any = None
        self._datasets: Any = None
        self._events: Any = None
        self._tool_index: Any = None
        self._pks_write: Any = None
        self._release: Any = None
        self._recall: Any = None
        self._working_memory: Any = None
        # Voice reads (voice_list/voice_get) work with or without a user — a fresh,
        # user-scoped executor avoids mutating the shared global one (no race).
        from modules.voice import VoiceExecutor

        self._voice = VoiceExecutor(db=db)
        self._voice.set_context(user_id)
        # Config reads (config_get/config_list) — fresh, user-scoped executor too.
        from modules.config import ConfigExecutor, build_default_registry

        self._config = ConfigExecutor(db=db, registry=build_default_registry())
        self._config.set_context(user_id)
        if user_id is not None:
            from modules.conversation_recall import ConversationRecallExecutor
            from modules.devices import DevicesExecutor
            from modules.finance import FinanceExecutor
            from modules.pks_search import PKSSearchExecutor

            # Recall de conversas: só precisa do banco (a busca é SQL). O
            # session_id serve para CORTAR a busca no turno de agora (senão a
            # própria pergunta casa com a busca) e para guardar o cursor do
            # "foi antes ainda". Os turnos anteriores desta mesma sessão FICAM
            # pesquisáveis — ver docs/context-recall-fixes.md §3.
            self._recall = ConversationRecallExecutor(db=db, embeddings=embeddings)
            self._recall.set_context(user_id, session_id)

            self._devices = DevicesExecutor(db=db)
            # `current_device_id` é o que faz "abaixa o volume" (sem citar
            # aparelho) significar ESTE aparelho.
            self._devices.set_context(user_id, current_device_id)
            # Caderno de trabalho: precisa do usuário (dono da nota) e da sessão
            # (é o que faz a nota da tarefa em andamento subir no índice).
            from modules.working_memory import WorkingMemoryExecutor

            self._working_memory = WorkingMemoryExecutor(db=db)

            # Liberação: o serviço só precisa do banco, então o executor nasce
            # aqui em vez de atravessar o AIService inteiro por parâmetro.
            from core.release import ReleaseService
            from core.release.notify import build_release_notifier
            from modules.release import ReleaseToolExecutor

            self._release = ReleaseToolExecutor(
                ReleaseService(db, notifier=build_release_notifier(db))
            )
            self._release.set_context(user_id, session_id)
            self._working_memory.set_context(user_id, session_id)
            # Finanças (fin_*, gated pela skill 'financas') — executor fresco e
            # user-scoped por request, como devices (o global fica sem usuário).
            self._finance = FinanceExecutor(db=db)
            self._finance.set_context(user_id)
            # Cadastros (data_*, gated pela skill que declara `datasets:`) —
            # executor fresco e user-scoped, como devices/finance. O registry é
            # o singleton do app: o cache das definições é compartilhado, o
            # CONTEXTO (quem é o dono das linhas) é por request.
            try:
                from core.datasets import DatasetRegistry
                from modules.datasets import DatasetsExecutor

                registry = dataset_registry or DatasetRegistry(db=db)
                self._datasets = DatasetsExecutor(db=db, registry=registry)
                self._datasets.set_context(user_id)
            except Exception as exc:  # noqa: BLE001 — feature off/ausente não derruba o turno
                logger.debug("[cognitive] datasets indisponíveis: %s", exc)
            # Inscrições de evento (event_*, gated pela skill que declara
            # `eventos:`). O contexto é o ponto: device + aba tocando vêm daqui,
            # não do modelo. É o que permite `event_subscribe` recusar inscrição
            # no vazio sem depender do bom senso do LLM.
            if event_router is not None:
                try:
                    from modules.events import EventsExecutor

                    self._events = EventsExecutor(
                        db=db,
                        registry=getattr(event_router, "_subs", None),
                        router=event_router,
                    )
                    self._events.set_context(
                        user_id,
                        session_id=session_id,
                        device_id=current_device_id,
                        media_context=media_context or {},
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[cognitive] eventos indisponíveis: %s", exc)
            if pks_service is not None:
                self._pks_search = PKSSearchExecutor(pks_service=pks_service)
                self._pks_search.set_context(user_id)
                from modules.location import LocationExecutor

                self._location = LocationExecutor(pks_service=pks_service)
                self._location.set_context(user_id)
                # Escrita sob demanda (pks_remember) — delega ao subagente de
                # memória via PKSService.learn(confirmed_save=True).
                from modules.pks_write import PKSWriteExecutor

                self._pks_write = PKSWriteExecutor(pks_service=pks_service)
                self._pks_write.set_context(user_id)

    def _tool_index_executor(self) -> Any:
        """tool_run despachando por ESTE executor — é o que faz o dispatcher
        genérico alcançar as tools user-scoped (fin_*, data_*). Mandá-lo para o
        executor global traria resultado vazio: lá não há usuário."""
        if self._tool_index is None:
            from modules.tool_index import ToolIndexExecutor

            self._tool_index = ToolIndexExecutor(dispatcher=self.execute_tool_call)
        return self._tool_index

    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        start = asyncio.get_event_loop().time()
        name = tool_call.name
        try:
            if name in _USER_SCOPED and self._user_id is None:
                raise ValueError(f"{name} requer um usuário identificado.")
            # Portão de autoridade — o OUTRO trilho é o `ToolExecutor`. Mesma
            # função, uma definição só (docs/release-request-plan.md §5.2). O
            # fallthrough para o executor global passa pelo portão lá, e a
            # segunda checagem é idempotente (a decisão não tem efeito colateral).
            _decisao = await gate_tool_call(name, tool_call.arguments, tool_call.id)
            if _decisao.blocked:
                raise ValueError(_decisao.mensagem())
            if (
                name in ("list_connected_devices", "device_control", "device_power")
                and self._devices is not None
            ):
                result = await self._devices.execute(name, tool_call.arguments)
            elif name in ("search_pks", "pks_reveal") and self._pks_search is not None:
                result = await self._pks_search.execute(name, tool_call.arguments)
            elif name == "get_user_location" and self._location is not None:
                result = await self._location.execute(name, tool_call.arguments)
            elif name in CONVERSATION_RECALL_TOOL_NAMES and self._recall is not None:
                result = await self._recall.execute(name, tool_call.arguments)
            elif name in ("voice_list", "voice_get"):
                result = await self._voice.execute(name, tool_call.arguments)
            elif name in ("config_get", "config_list"):
                result = await self._config.execute(name, tool_call.arguments)
            elif name in FINANCE_TOOL_NAMES and self._finance is not None:
                result = await self._finance.execute(name, tool_call.arguments)
            elif name in DATASET_TOOL_NAMES and self._datasets is not None:
                result = await self._datasets.execute(name, tool_call.arguments)
            elif name in EVENT_TOOL_NAMES and self._events is not None:
                result = await self._events.execute(name, tool_call.arguments)
            elif name in TOOL_INDEX_TOOL_NAMES:
                result = await self._tool_index_executor().execute(name, tool_call.arguments)
            elif name in PKS_WRITE_TOOL_NAMES and self._pks_write is not None:
                result = await self._pks_write.execute(name, tool_call.arguments)
            elif name in WORKING_MEMORY_TOOL_NAMES and self._working_memory is not None:
                result = await self._working_memory.execute(name, tool_call.arguments)
            elif name in RELEASE_TOOL_NAMES and self._release is not None:
                result = await self._release.execute(name, tool_call.arguments)
            else:
                # Read-only globals (e.g. get_weather) go through the global executor.
                return await self._global.execute_tool_call(tool_call)
            note_tool_result(name, result)
            return ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                result=result,
                execution_time=asyncio.get_event_loop().time() - start,
            )
        except Exception as e:
            logger.warning("Cognitive read tool '%s' failed: %s", name, e)
            return ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                result=None,
                error=str(e),
                execution_time=asyncio.get_event_loop().time() - start,
            )
