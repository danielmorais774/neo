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

from core.files.tools import FILE_TOOLS
from modules.config.tools import CONFIG_TOOLS
from modules.devices.tools import DEVICES_TOOLS
from modules.finance import FINANCE_TOOL_NAMES
from modules.location.tools import LOCATION_TOOLS
from modules.pks_search.tools import PKS_SEARCH_TOOLS
from modules.pks_write import PKS_WRITE_TOOL_NAMES, PKS_WRITE_TOOLS
from modules.services.tools import SERVICE_TOOLS
from modules.skills_library.tools import SKILLS_LIBRARY_TOOLS
from modules.voice.tools import VOICE_TOOLS
from modules.weather.tools import WEATHER_TOOLS
from modules.web_search.tools import WEB_SEARCH_TOOLS
from shared import ToolCall, ToolResult, ToolSchema

logger = logging.getLogger(__name__)


def _by_name(tools: list[ToolSchema], name: str) -> ToolSchema:
    return next(t for t in tools if t.name == name)


COGNITIVE_READ_TOOLS: list[ToolSchema] = [
    _by_name(PKS_SEARCH_TOOLS, "search_pks"),
    _by_name(DEVICES_TOOLS, "list_connected_devices"),
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
]

# Tools that need a user to be meaningful — only offered when user_id is known.
# As fin_* NÃO entram no read set default (são gated pela skill 'financas' via
# tool gating), mas o dispatch delas passa por este executor e exige usuário.
_USER_SCOPED = frozenset(
    {"search_pks", "list_connected_devices", "get_user_location"}
    | FINANCE_TOOL_NAMES
    | PKS_WRITE_TOOL_NAMES
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
    if user_id is None:
        tools = [t for t in tools if t.name not in _USER_SCOPED]
    return tools


class CognitiveReadExecutor:
    """Per-request executor for the cognitive read toolset.

    Builds FRESH, user-scoped executors for the user-bound tools (no shared-state
    race) and delegates everything else to the global ToolExecutor.
    """

    def __init__(self, *, global_executor: Any, db: Any, pks_service: Any, user_id: UUID | None) -> None:
        self._global = global_executor
        self._user_id = user_id
        self._devices: Any = None
        self._pks_search: Any = None
        self._location: Any = None
        self._finance: Any = None
        self._pks_write: Any = None
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
            from modules.devices import DevicesExecutor
            from modules.finance import FinanceExecutor
            from modules.pks_search import PKSSearchExecutor

            self._devices = DevicesExecutor(db=db)
            self._devices.set_context(user_id)
            # Finanças (fin_*, gated pela skill 'financas') — executor fresco e
            # user-scoped por request, como devices (o global fica sem usuário).
            self._finance = FinanceExecutor(db=db)
            self._finance.set_context(user_id)
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

    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        start = asyncio.get_event_loop().time()
        name = tool_call.name
        try:
            if name in _USER_SCOPED and self._user_id is None:
                raise ValueError(f"{name} requer um usuário identificado.")
            if name == "list_connected_devices" and self._devices is not None:
                result = await self._devices.execute(name, tool_call.arguments)
            elif name == "search_pks" and self._pks_search is not None:
                result = await self._pks_search.execute(name, tool_call.arguments)
            elif name == "get_user_location" and self._location is not None:
                result = await self._location.execute(name, tool_call.arguments)
            elif name in ("voice_list", "voice_get"):
                result = await self._voice.execute(name, tool_call.arguments)
            elif name in ("config_get", "config_list"):
                result = await self._config.execute(name, tool_call.arguments)
            elif name in FINANCE_TOOL_NAMES and self._finance is not None:
                result = await self._finance.execute(name, tool_call.arguments)
            elif name in PKS_WRITE_TOOL_NAMES and self._pks_write is not None:
                result = await self._pks_write.execute(name, tool_call.arguments)
            else:
                # Read-only globals (e.g. get_weather) go through the global executor.
                return await self._global.execute_tool_call(tool_call)
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
