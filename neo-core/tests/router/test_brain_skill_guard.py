"""O turno que morria em "A ferramenta falhou — data_list precisa de um usuário".

Sessão ``0bba3d38`` (09/09 02:21), medida no app.log da VPS. A frase era
"tem uma fila de musica criada né, me motra o que tem nela" — uma CONSULTA a um
cadastro (a skill `playlists` declara os datasets `playlists`, `musicas` e
`fila`). O que aconteceu, em três passos:

1. o classificador respondeu ``SERVER_TOOL`` / ``list_connected_devices``, com a
   explicação dele no log: "preciso primeiro identificar quais dispositivos de
   áudio/música estão conectados para então verificar a fila ativa";
2. o trilho SERVER_TOOL roda o ``AgentExecutor`` — e ele recebia o registry
   INTEIRO, então ignorou a tool escolhida, viu ``data_list`` e chamou;
3. naquele trilho as executoras user-scoped são as GLOBAIS, que nascem sem
   usuário de propósito. ``data_list`` recusou, e a recusa virou a resposta do
   turno (4 iterações, 19,7s).

Duas guardas, uma para cada metade: o assunto volta para o cognitivo (que
carrega a skill e tem o usuário), e o agente do SERVER_TOOL deixa de enxergar
tool que aquele trilho não sabe executar.
"""

from __future__ import annotations

import json

from core.router.intent_router import IntentRouter, SKILL_OWNED_TOOL_PREFIXES
from shared.types import IntentType


class _LibraryFalsa:
    """SkillLibrary mínima: casa `fila de musica` como a de verdade casa.

    ``tools`` importa: a guarda só desvia quando NENHUMA tool da skill roda no
    trilho SERVER_TOOL."""

    def __init__(
        self,
        keywords: list[str],
        nome: str = "playlists",
        tools: list[str] | None = None,
    ) -> None:
        self._keywords = keywords
        self._nome = nome
        self._tools = ["data_list", "data_create", "event_subscribe"] if tools is None else tools
        self.chamadas: list[str] = []

    def autoload_for(self, message, sticky=None):  # noqa: ANN001
        self.chamadas.append(message)
        msg = (message or "").lower()
        if any(k in msg for k in self._keywords):
            entry = type("Entry", (), {"name": self._nome, "tools": self._tools})()
            return (entry, "corpo da skill", self._tools)
        return None


def _router(library=None) -> IntentRouter:
    router = IntentRouter.__new__(IntentRouter)
    router.tool_registry = None
    router.skill_library = library
    return router


def _classificar(payload: dict, library=None):
    msg = payload.pop("_msg", "")
    return _router(library)._parse_classification(json.dumps(payload), msg)


# ---------------------------------------------------------------------------
# 1. o assunto da skill volta para o cognitivo
# ---------------------------------------------------------------------------

_FRASE = "tem uma fila de musica criada né, me motra o que tem nela"


def test_a_frase_real_da_sessao_0bba3d38_volta_para_o_cognitivo():
    lib = _LibraryFalsa(["fila de musica"])
    decisao = _classificar(
        {
            "intent": "SERVER_TOOL",
            "tool_name": "list_connected_devices",
            "tool_arguments": {"only_online": False},
            "confidence": 0.9,
            "_msg": _FRASE,
        },
        library=lib,
    )
    assert decisao.intent == IntentType.DIRECT_RESPONSE
    assert decisao.tool_name is None


def test_nao_marca_skill_hint():
    """O autoload do cognitivo acha a skill sozinho. O hint só mexeria no
    registro de tom (``derive_register`` manda todo skill_hint para OFICINA)."""
    decisao = _classificar(
        {"intent": "SERVER_TOOL", "tool_name": "list_connected_devices", "_msg": _FRASE},
        library=_LibraryFalsa(["fila de musica"]),
    )
    assert decisao.skill_hint is None


def test_sem_casamento_de_skill_o_server_tool_segue_intacto():
    lib = _LibraryFalsa(["fila de musica"])
    decisao = _classificar(
        {
            "intent": "SERVER_TOOL",
            "tool_name": "get_weather",
            "tool_arguments": {"city": "Belo Horizonte"},
            "confidence": 0.95,
            "_msg": "vai chover amanhã?",
        },
        library=lib,
    )
    assert decisao.intent == IntentType.SERVER_TOOL
    assert decisao.tool_name == "get_weather"


def test_sem_library_o_roteamento_nao_muda():
    """Fail-open: sem SkillLibrary o turno segue como antes (nada de exceção)."""
    decisao = _classificar(
        {"intent": "SERVER_TOOL", "tool_name": "get_weather", "_msg": _FRASE},
        library=None,
    )
    assert decisao.intent == IntentType.SERVER_TOOL


def test_library_que_explode_nao_derruba_a_classificacao():
    class _Explode:
        def autoload_for(self, message, sticky=None):  # noqa: ANN001
            raise RuntimeError("banco fora")

    decisao = _classificar(
        {"intent": "SERVER_TOOL", "tool_name": "get_weather", "_msg": _FRASE},
        library=_Explode(),
    )
    assert decisao.intent == IntentType.SERVER_TOOL


def test_client_tool_nao_e_desviado():
    """A guarda é só do SERVER_TOOL. "salva no meu disco" com uma keyword de
    skill na frase continua sendo entrega de arquivo na máquina do usuário."""
    decisao = _classificar(
        {
            "intent": "CLIENT_TOOL",
            "tool_name": "save_generated_file",
            "tool_arguments": {"file_id": "abc", "file_name": "fila.csv"},
            "_msg": "salva a fila de musica no meu desktop",
        },
        library=_LibraryFalsa(["fila de musica"]),
    )
    assert decisao.intent == IntentType.CLIENT_TOOL
    assert decisao.tool_name == "save_generated_file"


# ---------------------------------------------------------------------------
# 2. o toolset do agente do SERVER_TOOL
# ---------------------------------------------------------------------------


class _ToolFalsa:
    def __init__(self, name: str, execution: str | None = None) -> None:
        self.name = name
        if execution is not None:
            self.execution = execution


class _RegistryFalso:
    def __init__(self, nomes: list[str]) -> None:
        self._tools = [_ToolFalsa(n) for n in nomes]

    def get_all(self):
        return self._tools


def _toolset(nomes: list[str]) -> set[str]:
    from modules.desktop.ai_service import AIService

    servico = AIService.__new__(AIService)
    servico.agent = type("Agent", (), {"tool_registry": _RegistryFalso(nomes)})()
    return {t.name for t in servico._server_tool_toolset()}


def test_tool_gated_e_user_scoped_fica_de_fora():
    visiveis = _toolset(
        ["get_weather", "data_list", "event_list", "fin_add", "sql_query_ro", "config_list"]
    )
    assert "data_list" not in visiveis
    assert "event_list" not in visiveis
    assert "fin_add" not in visiveis
    assert "sql_query_ro" not in visiveis
    # as que o trilho SABE executar continuam
    assert {"get_weather", "config_list"} <= visiveis


def test_job_tools_continuam_visiveis_apesar_do_prefixo():
    """`neo_bash_background`, `deep_research` e `job_*` casam o prefixo gated, mas
    este trilho tem executor por request para elas (`_with_job_tools`). Filtrar
    por prefixo puro mataria o conserto anterior."""
    visiveis = _toolset(
        ["neo_bash_background", "deep_research", "browser_task", "job_status", "data_list"]
    )
    assert {"neo_bash_background", "deep_research", "browser_task", "job_status"} <= visiveis
    assert "data_list" not in visiveis


def test_tool_de_cliente_fica_de_fora_do_agente_do_servidor():
    """session-30b1e484: o agente do SERVER_TOOL via `run_command`, chamava, e o
    turno morria em "Executor para módulo 'desktop' não registrado" com o job do
    browser já na fila — o usuário leu fracasso e pediu de novo."""
    from modules.desktop.ai_service import AIService

    registry = _RegistryFalso([])
    registry._tools = [
        _ToolFalsa("run_command", "client-side"),
        _ToolFalsa("write_file", "client-side"),
        _ToolFalsa("get_weather", "server-side"),
        _ToolFalsa("browser_task", "server-side"),
        _ToolFalsa("browser_local_task", "server-side"),
    ]
    servico = AIService.__new__(AIService)
    servico.agent = type("Agent", (), {"tool_registry": registry})()
    visiveis = {t.name for t in servico._server_tool_toolset()}

    assert "run_command" not in visiveis
    assert "write_file" not in visiveis
    assert {"get_weather", "browser_task", "browser_local_task"} <= visiveis


def test_nenhuma_tool_de_cliente_do_desktop_roda_no_servidor():
    """Contra o registry REAL: tool nova do desktop não pode vazar para o agente."""
    from core.router.intent_router import server_tool_can_run
    from modules.desktop.tools import DESKTOP_TOOLS

    de_cliente = [t for t in DESKTOP_TOOLS if t.execution == "client-side"]
    assert "run_command" in {t.name for t in de_cliente}
    for tool in de_cliente:
        assert server_tool_can_run(tool.name, execution=tool.execution) is False, tool.name


def test_o_prefixo_data_realmente_esta_na_lista():
    """Se alguém tirar `data_`/`event_` da tupla, os dois testes acima passam a
    testar nada — esta asserção é o alarme."""
    assert "data_" in SKILL_OWNED_TOOL_PREFIXES
    assert "event_" in SKILL_OWNED_TOOL_PREFIXES
    assert "fin_" in SKILL_OWNED_TOOL_PREFIXES


# ---------------------------------------------------------------------------
# 3. o critério do desvio: só quando o trilho não serve NADA da skill
# ---------------------------------------------------------------------------


def test_skill_com_tool_servivel_aqui_nao_e_desviada():
    """`compras` tem `browser_task`/`meli_search`, que ESTE trilho executa — e o
    browser_task pelo SERVER_TOOL foi conserto anterior (jobs sem executor no
    registry global). Desviar aqui seria desfazer trabalho feito."""
    decisao = _classificar(
        {
            "intent": "SERVER_TOOL",
            "tool_name": "browser_task",
            "tool_arguments": {"task": "preço do monitor"},
            "_msg": "quanto custa esse monitor no mercado livre",
        },
        library=_LibraryFalsa(
            ["mercado livre", "quanto custa"],
            nome="compras",
            tools=["web_search", "web_fetch", "meli_search", "meli_product", "browser_task"],
        ),
    )
    assert decisao.intent == IntentType.SERVER_TOOL
    assert decisao.tool_name == "browser_task"


def test_skill_so_com_tool_gated_e_desviada():
    """`financas` só tem `fin_*` — nenhuma roda aqui."""
    decisao = _classificar(
        {
            "intent": "SERVER_TOOL",
            "tool_name": "get_weather",
            "_msg": "quanto eu gastei esse mes",
        },
        library=_LibraryFalsa(
            ["quanto eu gastei", "gastei"],
            nome="financas",
            tools=["fin_add_transaction", "fin_statement", "fin_balances"],
        ),
    )
    assert decisao.intent == IntentType.DIRECT_RESPONSE


def test_skill_sem_tool_declarada_e_desviada():
    """Skill de cadastro declara `datasets:` em vez de `tools:` — as `data_*`
    entram pelo agregador. Sem tool servível listada, o trilho não tem o que
    oferecer."""
    decisao = _classificar(
        {"intent": "SERVER_TOOL", "tool_name": "list_connected_devices", "_msg": _FRASE},
        library=_LibraryFalsa(["fila de musica"], tools=[]),
    )
    assert decisao.intent == IntentType.DIRECT_RESPONSE


def test_predicado_e_o_mesmo_dos_dois_lados():
    """O que o agente VÊ e o que decide o desvio têm que ser a MESMA regra —
    separadas, divergem no primeiro prefixo novo, e em silêncio."""
    from core.router.intent_router import server_tool_can_run

    visiveis = _toolset(["get_weather", "data_list", "browser_task", "fin_add"])
    for nome in ("get_weather", "data_list", "browser_task", "fin_add"):
        assert server_tool_can_run(nome) == (nome in visiveis), nome


def test_predicado_recusa_nome_vazio():
    from core.router.intent_router import server_tool_can_run

    assert server_tool_can_run("") is False
