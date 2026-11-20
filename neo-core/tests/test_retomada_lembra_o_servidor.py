"""A retomada depois de uma ida ao Mac lembra o que o SERVIDOR já leu.

Session-6166abaa (20/11/2026), "Quais projetos você conhece no Zion? Me diz a pasta
de cada um no meu Mac.": o cérebro leu o Zion (`zion_overview`), foi conferir as
pastas no Mac (`run_command`) e o turno estacionou no cliente. A retomada remonta o
prompt do zero e só levava os passos do CLIENTE: a leitura do Zion sumia, e o
cérebro lia de novo e voltava ao Mac. 16 chamadas de LLM, `zion_overview` 6 vezes,
4 idas ao Mac. Mesma família da session-5efdf38d (o trabalho do sandbox sumindo).

Trava aqui:
- o loop anota cada tool do servidor que rodou (`server_steps`);
- a pausa leva essas anotações para o estado de retomada, antes do passo do cliente;
- o transcript da retomada mostra uma seção própria com as respostas;
- o teto de idas ao Mac e as guardas de repetição não contam esses passos, e a
  numeração dos passos da máquina não muda.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from core.cognitive import cognitive_tools_for
from core.zion import ZION_TOOLS
from modules.desktop.ai_service import (
    ClientActuationRequested,
    _actuation_counts,
    _near_repeated_actuation,
    _render_actuation_steps,
    _repeated_actuation,
    _server_step,
)
from modules.desktop.tools import DESKTOP_TOOLS
from shared import ToolCall, ToolResult
from shared import ajustes as mod_ajustes
from shared.types import Message, MessageRole

from tests.test_actuation_transcript import _collect, _spy_reenter
from tests.test_actuation_transcript import _svc as _svc_retomada
from tests.test_cognitive_url_de_memoria_e_imagem import _Provider, _Resp
from tests.test_cognitive_url_de_memoria_e_imagem import _svc as _svc_loop

_ZION = {
    "ok": True,
    "zion": {"domains": [
        {"slug": "neo", "repos": [{"path": "/Users/daniel/Documents/projects/neo"}]},
        {"slug": "zion", "repos": [{"path": "/Users/daniel/Documents/projects/zion-memory-mcp"}]},
    ]},
}
_CONFERE = 'for d in /Users/daniel/Documents/projects/neo; do [ -d "$d" ] && echo "EXISTE: $d"; done'


def _tr(result=None, error=None) -> ToolResult:
    return ToolResult(tool_call_id="x", name="t", result=result, error=error, execution_time=0.0)


def _passo_zion(**kw):
    return _server_step("zion_overview", kw, _tr(_ZION))


def _passo_mac(command=_CONFERE, output="EXISTE: /Users/daniel/Documents/projects/neo"):
    return {"tool": "run_command", "command": command, "ok": True, "output": output}


@pytest.fixture
def ajustes_de(tmp_path, monkeypatch):
    def _usar(texto: str) -> None:
        caminho = tmp_path / "neo.toml"
        caminho.write_text(texto, encoding="utf-8")
        antes = caminho.stat().st_mtime_ns
        os.utime(caminho, ns=(antes + 10**9, antes + 10**9))
        monkeypatch.setenv("NEO_AJUSTES", str(caminho))
        monkeypatch.setattr(mod_ajustes, "_cache", None)

    return _usar


# ── o passo do servidor ──────────────────────────────────────────────────

def test_passo_do_servidor_guarda_a_chamada_e_a_resposta():
    passo = _server_step("zion_overview", {"domain": "neo"}, _tr(_ZION))
    assert passo["kind"] == "servidor" and passo["tool"] == "zion_overview"
    assert passo["command"] == 'zion_overview {"domain": "neo"}'
    assert passo["ok"] is True
    assert "/Users/daniel/Documents/projects/neo" in passo["output"]

    falhou = _server_step("zion_search", {"query": "x"}, _tr(error="a memória do Zion não respondeu"))
    assert falhou["ok"] is False and "não respondeu" in falhou["error"] and falhou["output"] == ""


def test_saida_do_passo_respeita_o_teto_do_neo_toml(ajustes_de):
    ajustes_de("[cerebro]\nretomada_servidor_chars = 300\n")
    passo = _server_step("neo_read_file", {"path": "/workspace/a.txt"}, _tr("x" * 5000))
    assert len(passo["output"]) == 300


# ── o transcript da retomada ─────────────────────────────────────────────

def test_transcript_mostra_o_que_o_servidor_ja_leu_antes_da_maquina():
    nota = _render_actuation_steps([_passo_zion(), _passo_mac()])
    assert "## O que você JÁ LEU neste turno (tools do servidor)" in nota
    assert "`zion_overview {}`" in nota
    assert "/Users/daniel/Documents/projects/zion-memory-mcp" in nota
    # a seção da máquina continua lá, numerada como antes (sem contar o servidor)
    assert "### Passo 1:" in nota and "### Passo 2:" not in nota
    assert nota.index("JÁ LEU") < nota.index("JÁ FEZ")
    assert "Não chame de novo com os mesmos argumentos" in nota


def test_mesma_chamada_aparece_uma_vez_com_a_resposta_mais_nova():
    velho = _server_step("wm_read", {"note": "s"}, _tr("versão velha"))
    novo = _server_step("wm_read", {"note": "s"}, _tr("versão nova"))
    nota = _render_actuation_steps([velho, _passo_mac(), novo, _passo_mac("ls /b", "b")])
    assert nota.count("`wm_read") == 1
    assert "versão nova" in nota and "versão velha" not in nota


def test_passo_do_servidor_que_falhou_aparece_como_falha():
    falhou = _server_step("zion_search", {"query": "x"}, _tr(error="a memória do Zion não respondeu"))
    nota = _render_actuation_steps([falhou, _passo_mac()])
    assert "FALHOU: a memória do Zion não respondeu" in nota


def test_teto_de_passos_do_servidor_e_desligar(ajustes_de):
    ajustes_de("[cerebro]\nretomada_servidor_passos = 2\n")
    passos = [_server_step("search_pks", {"query": f"q{i}"}, _tr(f"r{i}")) for i in range(5)]
    nota = _render_actuation_steps([*passos, _passo_mac()])
    assert "q3" in nota and "q4" in nota and "q2" not in nota  # ficam as mais recentes

    ajustes_de("[cerebro]\nretomada_servidor_passos = 0\n")
    nota = _render_actuation_steps([*passos, _passo_mac()])
    assert "JÁ LEU" not in nota and "### Passo 1:" in nota


def test_sem_passo_do_servidor_o_transcript_fica_como_era():
    nota = _render_actuation_steps([_passo_mac()])
    assert "JÁ LEU" not in nota
    assert nota.startswith("## O que você JÁ FEZ neste turno (na máquina do usuário)")


# ── tetos e guardas não contam o servidor ────────────────────────────────

def test_teto_de_idas_ao_mac_nao_conta_o_servidor():
    passos = [_passo_zion(), _server_step("wm_read", {}, _tr("x")), _passo_mac()]
    assert _actuation_counts(passos) == (1, 0)


def test_guardas_de_repeticao_ignoram_o_servidor_e_numeram_so_a_maquina():
    lista = _server_step("neo_list_files", {"path": "/Users/daniel/a"}, _tr("x"))
    passos = [lista, _passo_mac("ls -la /Users/daniel/a", "arquivos")]
    # o passo da máquina é o 1 do transcript, não o 2
    assert _near_repeated_actuation(passos, "ls -la /Users/daniel/a/") == [1]
    assert not _repeated_actuation([lista], lista["command"])


# ── o loop anota, a pausa leva, a retomada mostra ────────────────────────

class _ExecutorZion:
    def __init__(self) -> None:
        self.chamadas: list[str] = []

    async def execute_tool_call(self, tool_call):
        self.chamadas.append(tool_call.name)
        return ToolResult(
            tool_call_id=tool_call.id, name=tool_call.name, result=_ZION, execution_time=0.0
        )


@pytest.mark.asyncio
async def test_loop_anota_a_tool_do_servidor_antes_de_estacionar_no_mac():
    provider = _Provider([
        _Resp(tool_calls=[ToolCall(id="1", name="zion_overview", arguments={})]),
        _Resp(tool_calls=[ToolCall(id="2", name="run_command", arguments={"command": _CONFERE})]),
    ])
    executor = _ExecutorZion()
    server_steps: list[dict] = []
    with pytest.raises(ClientActuationRequested) as pausa:
        await _svc_loop()._run_cognitive_tool_loop(
            provider=provider,
            messages=[Message(role=MessageRole.USER, content="Quais projetos você conhece no Zion?")],
            req_model=None, model_name="m", provider_name="p",
            tools=[*cognitive_tools_for(uuid4()), *DESKTOP_TOOLS, *ZION_TOOLS],
            tool_executor=executor, slog=None, trace=None,
            server_steps=server_steps,
        )
    assert pausa.value.tool_name == "run_command"
    assert executor.chamadas == ["zion_overview"]
    assert [p["command"] for p in server_steps] == ["zion_overview {}"]
    assert "zion-memory-mcp" in server_steps[0]["output"]


@pytest.mark.asyncio
async def test_a_retomada_recebe_o_servidor_e_o_mac_em_ordem():
    """A cadeia da session-6166abaa: pausa no `run_command` com o Zion já lido."""
    svc = _svc_retomada()
    sid = uuid4()
    vistos: list[dict] = []
    _spy_reenter(svc, vistos)

    car = ClientActuationRequested("run_command", {"command": _CONFERE}, "tc1")
    await _collect(svc._emit_client_actuation(
        car, session_id=sid, user_message="Quais projetos você conhece no Zion?", user_id=None,
        metadata={}, trace=None, slog=None, depth=0, steps=[_passo_zion()],
    ))
    pend = svc._pending_actuations[str(sid)]
    assert [p.get("kind") for p in pend["steps"]] == ["servidor"]

    await _collect(svc._resume_after_command(
        sid, success=True, result="EXISTE: /Users/daniel/Documents/projects/neo", error=None,
        user_message="Quais projetos você conhece no Zion?", pend=pend, slog=None, trace=None,
    ))
    passos = vistos[0]["steps"]
    assert [p.get("kind") == "servidor" for p in passos] == [True, False]
    nota = _render_actuation_steps(passos)
    assert "zion-memory-mcp" in nota and "EXISTE: /Users/daniel/Documents/projects/neo" in nota
