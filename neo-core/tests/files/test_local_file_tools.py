"""Leitura e edição de texto no disco do usuário pelo desktop (services/file_ops.rs).

O desktop passa o conteúdo pro core processar e só ele toca o disco. O que estes
testes travam no lado do core:

* liberação por turno — só o que o cliente anuncia em `client_tools` e só o motor
  escolhido no painel (Neo edita / opencode edita);
* o modelo e o prazo do opencode vêm do painel, nunca do LLM;
* cada `status` do desktop volta ao cérebro com a orientação certa (e o conteúdo
  lido fica inteiro no transcript, que é de onde sai o `old_string`);
* o schema do `edits` leva `items` — sem ele provedor estrito recusa a tool.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from modules.desktop.ai_service import (
    _client_tools_of,
    _gate_local_file_tools,
    _render_actuation_steps,
    _render_file_tool_result,
)
from modules.desktop.file_edit_config import (
    DEFAULTS,
    get_file_edit_config,
    invalidate_file_edit_config_cache,
    normalize_file_edit_config,
)
from modules.desktop.tools import DESKTOP_TOOLS

PATH = "/Users/daniel/Desktop/plano-de-acao/analise.md"
TODAS = json.dumps(["read_user_file", "edit_user_file", "write_file", "delegate_file_edit"])


def _nomes(tools):
    return {t.name for t in tools}


def _tools():
    return [SimpleNamespace(name=n) for n in (
        "read_user_file", "edit_user_file", "delegate_file_edit", "write_file", "run_command",
    )]


# ── liberação por turno ─────────────────────────────────────────────────────


def test_cliente_sem_client_tools_nao_recebe_as_tools_novas() -> None:
    """Desktop antigo: a tool sem braço do outro lado travaria o turno."""
    assert _nomes(_gate_local_file_tools(_tools(), {}, "neo")) == {"write_file", "run_command"}


def test_motor_neo_edita_com_as_proprias_tools() -> None:
    nomes = _nomes(_gate_local_file_tools(_tools(), {"client_tools": TODAS}, "neo"))
    assert {"read_user_file", "edit_user_file"} <= nomes
    assert "delegate_file_edit" not in nomes


def test_motor_opencode_troca_a_edicao_pela_delegacao() -> None:
    nomes = _nomes(_gate_local_file_tools(_tools(), {"client_tools": TODAS}, "opencode"))
    assert {"read_user_file", "delegate_file_edit"} <= nomes
    assert "edit_user_file" not in nomes


def test_opencode_escolhido_mas_ausente_na_maquina_cai_no_neo() -> None:
    sem_opencode = json.dumps(["read_user_file", "edit_user_file", "write_file"])
    nomes = _nomes(_gate_local_file_tools(_tools(), {"client_tools": sem_opencode}, "opencode"))
    assert "edit_user_file" in nomes and "delegate_file_edit" not in nomes


def test_client_tools_aceita_lista_ou_json_e_ignora_lixo() -> None:
    assert _client_tools_of({"client_tools": ["a"]}) == {"a"}
    assert _client_tools_of({"client_tools": '["a","b"]'}) == {"a", "b"}
    assert _client_tools_of({"client_tools": "{nao json"}) == set()
    assert _client_tools_of(None) == set()


# ── schema ──────────────────────────────────────────────────────────────────


def test_edits_declara_items_e_o_provider_manda() -> None:
    from core.llm.openai_provider import OpenAIProvider

    tool = next(t for t in DESKTOP_TOOLS if t.name == "edit_user_file")
    assert tool.parameters["edits"].items["required"] == ["old_string", "new_string"]
    formatted = object.__new__(OpenAIProvider).format_tools_for_api([tool])
    props = formatted[0]["function"]["parameters"]["properties"]
    assert props["edits"]["items"]["type"] == "object"
    assert "items" not in props["path"]


# ── config do painel ────────────────────────────────────────────────────────


def test_normalize_config_descarta_valor_invalido() -> None:
    cfg = normalize_file_edit_config(
        {"engine": "vim", "opencode_model": "sem barra", "opencode_timeout_s": 99999}
    )
    assert cfg["engine"] == "neo"
    assert cfg["opencode_model"] == DEFAULTS["opencode_model"]
    assert cfg["opencode_timeout_s"] == 1800


@pytest.mark.asyncio
async def test_config_do_usuario_cai_no_global_quando_nao_existe() -> None:
    invalidate_file_edit_config_cache()
    global_pref = SimpleNamespace(key="config", value=json.dumps({"engine": "opencode"}))

    class _Db:
        async def get_preferences(self, user_id=None, service=None, category=None):
            return [] if user_id else [global_pref]

    cfg = await get_file_edit_config(_Db(), uuid4())
    assert cfg["engine"] == "opencode"
    invalidate_file_edit_config_cache()


# ── emissão pelo cérebro ────────────────────────────────────────────────────


class _Sessions:
    async def add_message(self, **kw):
        return None


def _service():
    from modules.desktop.ai_service import AIService

    svc = object.__new__(AIService)
    svc.sessions = _Sessions()
    svc._pending_actuations = {}
    svc._pending_traces = {}
    svc._session_seen_files = {}
    svc.file_store = None
    svc.db = None
    return svc


class _Car:
    def __init__(self, tool, args):
        self.tool_name, self.arguments, self.tool_call_id = tool, args, "c1"


async def _emit(svc, tool, args):
    return [
        r async for r in svc._emit_client_actuation(
            _Car(tool, args), session_id=uuid4(), user_message="edita",
            user_id=uuid4(), metadata={"client_tools": TODAS}, slog=None, trace=None,
        )
    ]


@pytest.mark.asyncio
async def test_delegacao_usa_modelo_e_prazo_do_painel() -> None:
    invalidate_file_edit_config_cache()
    saidas = await _emit(_service(), "delegate_file_edit", {
        "path": PATH, "instruction": "troca x", "model": "openai/gpt-caro", "timeout_s": 9,
    })
    args = json.loads(next(r for r in saidas if r.tool_request).tool_request["arguments"])
    assert args["model"] == DEFAULTS["opencode_model"]
    assert args["timeout_s"] == DEFAULTS["opencode_timeout_s"]


@pytest.mark.asyncio
async def test_caminho_do_sandbox_nao_vira_leitura_no_disco() -> None:
    svc = _service()
    saidas = await _emit(svc, "read_user_file", {"path": "/workspace/files/x.md"})
    assert not [r for r in saidas if r.tool_request]
    assert svc._pending_actuations == {}


# ── retomada ────────────────────────────────────────────────────────────────


async def _resume(svc, tool, *, success=True, result=None, error=None):
    capturado = {}

    async def _reenter(session_id, *, user_message, pend, extra_note, slog, trace, steps=None):
        capturado.update(note=extra_note, steps=steps)
        return
        yield  # pragma: no cover

    async def _note(*a, **k):
        return None

    svc._reenter_cognitive = _reenter
    svc._note_client_step = _note
    sid = uuid4()
    async for _ in svc._resume_after_file_tool(
        sid, tool_name=tool, success=success,
        result=json.dumps(result) if result is not None else None, error=error,
        user_message="edita", pend={"path": PATH, "metadata": {}, "steps": []},
        slog=None, trace=None,
    ):
        pass
    return sid, capturado


@pytest.mark.asyncio
async def test_leitura_volta_com_sha_e_marca_versao_vista() -> None:
    svc = _service()
    sid, cap = await _resume(svc, "read_user_file", result={
        "status": "ok", "path": PATH, "sha256": "abc123", "start_line": 1, "end_line": 2,
        "total_lines": 2, "eol": "lf", "content": "# Título\nlinha\n",
    })
    assert "abc123" in cap["note"] and "edit_user_file" in cap["note"]
    passo = cap["steps"][-1]
    assert passo["kind"] == "file_read" and "# Título" in passo["output"]
    assert svc._disk_file_was_seen(sid, PATH)


@pytest.mark.asyncio
async def test_opencode_gravou_o_cerebro_confere_o_diff() -> None:
    _, cap = await _resume(_service(), "delegate_file_edit", result={
        "status": "saved", "path": PATH, "engine": "opencode", "model": "m",
        "session_id": "ses_9", "summary": "troquei", "diff": "@@ -1 +1 @@\n-a\n+b\n",
        "sha256": "novo", "lines_added": 1, "lines_removed": 1,
    })
    assert "CONFIRA" in cap["note"] and "session_id" in cap["note"]
    assert "-a\n+b" in cap["steps"][-1]["output"]


@pytest.mark.asyncio
async def test_trecho_que_nao_casou_mostra_linhas_parecidas() -> None:
    _, cap = await _resume(_service(), "edit_user_file", result={
        "status": "no_match", "path": PATH, "edit_index": 0, "message": "não existe",
        "similar_lines": [{"line": 7, "text": "  - banana"}],
    })
    assert "Nada foi gravado" in cap["note"]
    assert "7:   - banana" in cap["steps"][-1]["output"]


@pytest.mark.asyncio
async def test_falha_de_transporte_nao_afirma_nada() -> None:
    _, cap = await _resume(_service(), "edit_user_file", success=False, error="caminho vazio")
    assert "Não afirme" in cap["note"]
    assert cap["steps"][-1]["ok"] is False


def test_leitura_fica_inteira_no_transcript_mesmo_sem_ser_o_ultimo_passo() -> None:
    conteudo = "\n".join(f"linha {i} " + "x" * 40 for i in range(300))
    steps = [
        {"command": f"read_user_file {PATH}", "ok": True, "output": conteudo,
         "kind": "file_read", "path": PATH},
        {"command": f"edit_user_file {PATH}", "ok": True, "output": "status=no_match: x",
         "kind": "file", "path": PATH},
    ]
    texto = _render_actuation_steps(steps)
    assert "linha 299" in texto, "a leitura foi cortada no orçamento de passo antigo"


def test_status_desconhecido_nao_afirma_sucesso() -> None:
    _, nota = _render_file_tool_result("edit_user_file", {"status": "estranho"}, None, PATH)
    assert "sem afirmar" in nota


@pytest.mark.asyncio
async def test_write_file_com_versao_velha_volta_pro_cerebro() -> None:
    svc = _service()
    notas = []

    async def _reenter(session_id, *, user_message, pend, extra_note, slog, trace, steps=None):
        notas.append(extra_note)
        return
        yield  # pragma: no cover

    svc._reenter_cognitive = _reenter
    async for _ in svc._confirm_file_delivery(
        uuid4(), success=True,
        result=json.dumps({"status": "stale", "path": PATH, "message": "mudou", "current_sha256": "z"}),
        error=None, user_message="grava", pend={"tool_name": "write_file"}, slog=None, trace=None,
    ):
        pass
    assert notas and "Releia" in notas[0]
