"""F4 do plano Neo × Zion (docs/zion-integracao-plan.md §7–§8).

O que fica travado aqui:
- o cliente lê a resposta em SSE e em JSON, e reabre a sessão quando o Zion
  reiniciou (404);
- o portão nega quem é de fora e deixa a agenda e o turno contaminado passarem;
- com a consulta desligada no neo.toml, as tools não aparecem para o cérebro,
  para o roteador nem para o SERVER_TOOL, e o executor recusa;
- Zion fora do ar vira resultado legível, nunca exceção que derruba o turno;
- registry + executor ligados de verdade (o elo que matou o e-mail em prod).
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from core.authority import NEGAR, PERMITIR, PRINCIPAL_AGENDA, PRINCIPAL_EXTERNO, TurnAuthority, evaluate
from core.authority.risk import LEITURA_PRIVADA, classes_of, is_external_source, is_sink
from core.zion import ZION_TOOL_NAMES, ZION_TOOLS, ClienteZion, ZionIndisponivel, ZionToolExecutor
from core.zion.cliente import mensagens_sse
from core.zion.tools import caber, traduzir
from shared import ToolCall
from shared import ajustes as mod_ajustes

URL = "http://zion-teste:7077/mcp"


# ── ajustes ──────────────────────────────────────────────────────────────

def _usar_ajustes(tmp_path, monkeypatch, texto: str) -> None:
    caminho = tmp_path / "neo.toml"
    caminho.write_text(texto, encoding="utf-8")
    antes = caminho.stat().st_mtime_ns
    os.utime(caminho, ns=(antes + 10**9, antes + 10**9))
    monkeypatch.setenv("NEO_AJUSTES", str(caminho))
    monkeypatch.setattr(mod_ajustes, "_cache", None)


@pytest.fixture
def ligado(tmp_path, monkeypatch):
    _usar_ajustes(
        tmp_path, monkeypatch,
        f'[zion]\nconsulta_habilitada = true\nurl = "{URL}"\nmax_chars = 2000\n',
    )


@pytest.fixture
def desligado(tmp_path, monkeypatch):
    _usar_ajustes(tmp_path, monkeypatch, "[zion]\nconsulta_habilitada = false\n")


# ── um Zion de mentira ───────────────────────────────────────────────────

class ZionFalso:
    """Fala o MCP do Zion o bastante para o cliente: sessão, SSE ou JSON, 404."""

    def __init__(self, *, sse: bool = True, respostas: dict | None = None) -> None:
        self.sse = sse
        self.respostas = respostas or {}
        self.sessoes: set[str] = set()
        self.pedidos: list[dict] = []
        self.inicializacoes = 0

    def esquecer_sessoes(self) -> None:
        """O que acontece quando o container do Zion reinicia."""
        self.sessoes.clear()

    def _corpo(self, msg: dict, *, extra: dict | None = None) -> httpx.Response:
        headers = dict(extra or {})
        if self.sse:
            headers["content-type"] = "text/event-stream"
            corpo = f"event: message\ndata: {json.dumps(msg)}\n\n"
            return httpx.Response(200, headers=headers, text=corpo)
        return httpx.Response(200, headers=headers, json=msg)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        corpo = json.loads(request.content)
        self.pedidos.append({"corpo": corpo, "headers": dict(request.headers)})
        assert "text/event-stream" in request.headers["accept"]
        assert "application/json" in request.headers["accept"]
        metodo = corpo.get("method")
        if metodo == "initialize":
            self.inicializacoes += 1
            sessao = f"s{self.inicializacoes}"
            self.sessoes.add(sessao)
            return self._corpo(
                {"jsonrpc": "2.0", "id": corpo["id"], "result": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "serverInfo": {"name": "zion", "version": "0.2.0"},
                }},
                extra={"mcp-session-id": sessao},
            )
        sessao = request.headers.get("mcp-session-id")
        if sessao not in self.sessoes:
            return httpx.Response(404, json={"jsonrpc": "2.0", "error": {"code": -32001, "message": "Unknown MCP session; initialize again."}, "id": None})
        if metodo == "notifications/initialized":
            return httpx.Response(202)
        assert metodo == "tools/call"
        nome = corpo["params"]["name"]
        valor = self.respostas.get(nome, {"tool": nome, "args": corpo["params"]["arguments"]})
        return self._corpo({
            "jsonrpc": "2.0", "id": corpo["id"],
            "result": {"content": [{"type": "text", "text": json.dumps(valor)}], "structuredContent": valor},
        })

    def chamadas(self) -> list[tuple[str, dict]]:
        return [
            (p["corpo"]["params"]["name"], p["corpo"]["params"]["arguments"])
            for p in self.pedidos
            if p["corpo"].get("method") == "tools/call"
        ]


def _executor(falso) -> ZionToolExecutor:
    return ZionToolExecutor(transport=httpx.MockTransport(falso))


# ── cliente ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("sse", [True, False], ids=["sse", "json"])
async def test_cliente_le_sse_e_json_e_se_apresenta_como_neo(sse):
    falso = ZionFalso(sse=sse, respostas={"domain_list": {"domains": [{"slug": "neo"}]}})
    cliente = ClienteZion(URL, timeout_s=2, transport=httpx.MockTransport(falso))

    assert await cliente.chamar("domain_list", {}) == {"domains": [{"slug": "neo"}]}

    init = falso.pedidos[0]["corpo"]
    assert init["method"] == "initialize"
    # é pelo nome do cliente que o Zion sabe que o agente é o Neo
    assert init["params"]["clientInfo"]["name"] == "neo"
    assert [p["corpo"]["method"] for p in falso.pedidos] == [
        "initialize", "notifications/initialized", "tools/call",
    ]
    assert falso.pedidos[-1]["headers"]["mcp-session-id"] == "s1"
    assert falso.pedidos[-1]["headers"]["mcp-protocol-version"] == "2025-06-18"


@pytest.mark.asyncio
async def test_cliente_reaproveita_a_sessao_e_reabre_uma_vez_no_404():
    falso = ZionFalso()
    cliente = ClienteZion(URL, timeout_s=2, transport=httpx.MockTransport(falso))

    await cliente.chamar("domain_list", {})
    await cliente.chamar("domain_list", {})
    assert falso.inicializacoes == 1

    falso.esquecer_sessoes()  # o Zion reiniciou
    assert await cliente.chamar("record_get", {"id": "neo"}) == {"tool": "record_get", "args": {"id": "neo"}}
    assert falso.inicializacoes == 2


@pytest.mark.asyncio
async def test_cliente_desiste_se_o_404_persiste():
    def sempre_404(request: httpx.Request) -> httpx.Response:
        corpo = json.loads(request.content)
        if corpo.get("method") == "initialize":
            return httpx.Response(
                200, headers={"mcp-session-id": "x"},
                json={"jsonrpc": "2.0", "id": corpo["id"], "result": {"protocolVersion": "2025-06-18"}},
            )
        if corpo.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(404, json={"error": "sumiu"})

    cliente = ClienteZion(URL, timeout_s=2, transport=httpx.MockTransport(sempre_404))
    with pytest.raises(ZionIndisponivel, match="404"):
        await cliente.chamar("domain_list", {})


def test_sse_junta_linhas_de_dado_e_pula_evento_sem_json():
    texto = (
        "event: ping\ndata: não é json\n\n"
        'event: message\ndata: {"id": 7,\ndata:  "result": {}}\n\n'
    )
    assert mensagens_sse(texto) == [{"id": 7, "result": {}}]


# ── tradução e teto ──────────────────────────────────────────────────────

def test_traducao_para_as_tools_do_zion():
    assert traduzir("zion_overview", {}, limite_busca=8) == ("domain_list", {})
    assert traduzir("zion_overview", {"domain": " neo "}, limite_busca=8) == ("context_get", {"domain": "neo"})
    assert traduzir("zion_search", {"query": "forno", "domain": "neo", "kinds": ["failure", ""]}, limite_busca=5) == (
        "memory_search", {"query": "forno", "limit": 5, "domain": "neo", "kinds": ["failure"]},
    )
    # modelo que manda a lista como texto
    assert traduzir("zion_search", {"query": "x", "kinds": "rule, gotcha"}, limite_busca=5)[1]["kinds"] == ["rule", "gotcha"]
    assert traduzir("zion_get", {"id": "mem_1"}, limite_busca=8) == ("record_get", {"id": "mem_1"})
    with pytest.raises(ValueError, match="query"):
        traduzir("zion_search", {"query": "  "}, limite_busca=8)
    with pytest.raises(ValueError, match="id"):
        traduzir("zion_get", {}, limite_busca=8)


def test_teto_de_caracteres_e_erro_do_proprio_zion():
    assert caber({"a": 1}, 600) == {"ok": True, "zion": {"a": 1}}
    grande = caber({"texto": "x" * 5000}, 600)
    assert grande["ok"] and len(grande["zion_cortado"]) == 600 and "cortado" in grande["aviso"]
    erro = caber({"error": "not_found", "message": "No record 'mem_x'."}, 600)
    assert erro == {"ok": False, "error": "not_found: No record 'mem_x'."}


# ── executor ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_executor_traduz_e_devolve_o_valor(ligado):
    falso = ZionFalso(respostas={"context_get": {"domain": "neo", "rules": ["r1"]}})
    resultado = await _executor(falso).execute("zion_overview", {"domain": "neo"})
    assert resultado == {"ok": True, "zion": {"domain": "neo", "rules": ["r1"]}}
    assert falso.chamadas() == [("context_get", {"domain": "neo"})]


@pytest.mark.asyncio
async def test_zion_fora_do_ar_vira_resultado_e_nao_excecao(ligado):
    def fora(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Name or service not known", request=request)

    executor = ZionToolExecutor(transport=httpx.MockTransport(fora))
    resultado = await executor.execute("zion_search", {"query": "forno"})
    assert resultado["ok"] is False
    assert resultado["error"].startswith("a memória do Zion não respondeu")

    # pelo contrato do AgentExecutor também: ToolResult com erro, sem levantar
    tr = await executor.execute_tool_call(ToolCall(name="zion_get", arguments={"id": "neo"}))
    assert tr.error and "não respondeu" in tr.error


@pytest.mark.asyncio
async def test_host_recusado_vira_resultado(ligado):
    def recusa(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Invalid Host header")

    resultado = await ZionToolExecutor(transport=httpx.MockTransport(recusa)).execute("zion_overview", {})
    assert resultado["ok"] is False and "403" in resultado["error"]


@pytest.mark.asyncio
async def test_executor_recusa_com_a_consulta_desligada(desligado):
    falso = ZionFalso()
    resultado = await _executor(falso).execute("zion_overview", {})
    assert resultado["ok"] is False and "desligada" in resultado["error"]
    assert falso.pedidos == []  # nem chegou a falar com o Zion


@pytest.mark.asyncio
async def test_registry_e_executor_ligados_pelo_tool_executor_global(ligado):
    """O elo que matou email_inbox em prod: registrar ≠ ligar. Aqui passa pelo
    `ToolExecutor` de verdade (validação, portão, `_dispatch`)."""
    from core.tools.executor import ToolExecutor
    from core.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register_many(ZION_TOOLS)
    global_exec = ToolExecutor(registry)
    falso = ZionFalso(respostas={"domain_list": {"domains": [{"slug": "zion"}]}})
    global_exec.register_module_executor("zion", _executor(falso))

    tr = await global_exec.execute_tool_call(ToolCall(name="zion_overview", arguments={}))
    assert tr.error is None
    assert tr.result == {"ok": True, "zion": {"domains": [{"slug": "zion"}]}}


# ── risco e portão ───────────────────────────────────────────────────────

def test_classificacao_de_risco():
    for nome in ZION_TOOL_NAMES:
        assert classes_of(nome) == frozenset({LEITURA_PRIVADA}), nome
        # memória escrita pelos agentes do Daniel: não contamina o turno
        assert not is_external_source(nome)
        assert not is_sink(nome)


@pytest.mark.parametrize("tool", sorted(ZION_TOOL_NAMES))
def test_portao_nega_quem_e_de_fora_e_deixa_o_daniel_e_a_agenda(tool):
    assert evaluate(tool, TurnAuthority()).efeito == PERMITIR
    # a agenda pode: o cron de suporte da fase 2 precisa achar o projeto
    assert evaluate(tool, TurnAuthority(principal=PRINCIPAL_AGENDA)).efeito == PERMITIR
    # ler a própria memória num turno que leu web não vaza nada
    contaminado = TurnAuthority()
    contaminado.taint("web_fetch")
    assert evaluate(tool, contaminado).efeito == PERMITIR
    decisao = evaluate(tool, TurnAuthority(principal=PRINCIPAL_EXTERNO, principal_ref="5511999@s.whatsapp.net"))
    assert decisao.efeito == NEGAR
    assert "Zion" in decisao.motivo


# ── fora da mesa com a consulta desligada ────────────────────────────────

def _nomes(tools) -> set[str]:
    return {t.name for t in tools}


def test_cerebro_so_ve_as_tools_com_a_consulta_ligada(tmp_path, monkeypatch):
    from core.cognitive.read_tools import cognitive_tools_for

    _usar_ajustes(tmp_path, monkeypatch, "[zion]\nconsulta_habilitada = false\n")
    assert not (_nomes(cognitive_tools_for(uuid4())) & ZION_TOOL_NAMES)

    _usar_ajustes(tmp_path, monkeypatch, "[zion]\nconsulta_habilitada = true\n")
    assert ZION_TOOL_NAMES <= _nomes(cognitive_tools_for(uuid4()))
    # turno sem dono identificado não abre a memória dele
    assert not (_nomes(cognitive_tools_for(None)) & ZION_TOOL_NAMES)


def test_roteador_so_lista_as_tools_com_a_consulta_ligada(tmp_path, monkeypatch):
    from core.router.intent_router import IntentRouter

    registro = SimpleNamespace(get_all=lambda: list(ZION_TOOLS))
    falso_router = SimpleNamespace(tool_registry=registro)

    _usar_ajustes(tmp_path, monkeypatch, "[zion]\nconsulta_habilitada = false\n")
    assert IntentRouter._format_tools(falso_router, "server-side") == ""

    _usar_ajustes(tmp_path, monkeypatch, "[zion]\nconsulta_habilitada = true\n")
    catalogo = IntentRouter._format_tools(falso_router, "server-side")
    assert all(nome in catalogo for nome in ZION_TOOL_NAMES)


def test_server_tool_so_ve_as_tools_com_a_consulta_ligada(tmp_path, monkeypatch):
    from modules.desktop.ai_service import AIService

    falso = SimpleNamespace(
        agent=SimpleNamespace(tool_registry=SimpleNamespace(get_all=lambda: list(ZION_TOOLS))),
        _claude_code_available=lambda: False,
    )
    _usar_ajustes(tmp_path, monkeypatch, "[zion]\nconsulta_habilitada = false\n")
    assert not (_nomes(AIService._server_tool_toolset(falso)) & ZION_TOOL_NAMES)

    _usar_ajustes(tmp_path, monkeypatch, "[zion]\nconsulta_habilitada = true\n")
    assert ZION_TOOL_NAMES <= _nomes(AIService._server_tool_toolset(falso))


def test_ajustes_invalidos_deixam_a_consulta_fechada(tmp_path, monkeypatch):
    from core.zion import consulta_ligada

    _usar_ajustes(tmp_path, monkeypatch, "[zion]\nconsulta_habilitada = true\n")
    assert consulta_ligada() is True
    monkeypatch.setattr(mod_ajustes, "ajustes", lambda: (_ for _ in ()).throw(RuntimeError("quebrou")))
    assert consulta_ligada() is False


def test_main_registra_as_tools_e_o_executor_juntos():
    """Registrar sem executor faz o roteador oferecer algo que morre na hora de
    rodar (lição das job tools e do e-mail). O wiring de verdade só existe no
    boot; aqui a varredura confere que os dois lados estão no mesmo bloco."""
    from pathlib import Path

    fonte = (Path(__file__).resolve().parents[2] / "api" / "main.py").read_text(encoding="utf-8")
    assert "_tool_registry.register_many(ZION_TOOLS)" in fonte
    assert '_tool_executor.register_module_executor("zion", ZionToolExecutor())' in fonte
