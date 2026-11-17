"""Editar um arquivo que já está no disco do usuário — sessão 30b1e484.

"atualiza o arquivo .md que fica em /Users/…/analise-perfil.md" rodou
6 turnos sem gravar nada e terminou com uma CÓPIA DO ORIGINAL no Desktop e um
"Pronto, salvei". Cada teste daqui trava uma junta que quebrou naquele log:

* o classificador redigiu o arquivo inteiro (inventado) em ``write_file``;
* ``mode="overwrite"`` fora do enum era ignorado → conflito atrás de conflito;
* a nota de conflito mandava rechamar ``save_generated_file`` depois de um
  ``write_file`` → o cérebro foi atrás do file_id do anexo ANTIGO;
* ``destination`` com o caminho do ARQUIVO → "File exists (os error 17)";
* a edição feita no /workspace não tinha ponte de volta pro disco;
* ``[[HANDOFF:save_generated_file]]`` descartado calado, com a promessa na tela.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.router.intent_router import IntentRouter, _text_is_dictated
from modules.desktop.ai_service import (
    _delivery_target_path,
    _handoff_block_names_tool,
    _norm_user_path,
    _split_file_destination,
    _truthy,
)
from modules.desktop.tools import DESKTOP_TOOLS
from shared import IntentType, RoutingDecision

HOME_MD = {
    "home_dir": "/Users/daniel",
    "desktop_dir": "/Users/daniel/Desktop",
    "downloads_dir": "/Users/daniel/Downloads",
}
ARQUIVO = "/Users/daniel/Desktop/plano-de-acao/analise-perfil.md"


# ── destination: pasta ou caminho completo ─────────────────────────────────


def test_destination_com_o_caminho_do_arquivo_vira_pasta_mais_nome() -> None:
    """O exato da sessão: create_dir_all no arquivo → os error 17."""
    assert _split_file_destination(ARQUIVO, "analise-perfil.md") == (
        "/Users/daniel/Desktop/plano-de-acao",
        "analise-perfil.md",
    )


def test_destination_com_outro_nome_mesma_extensao_renomeia() -> None:
    assert _split_file_destination("~/Desktop/novo.md", "analise.md") == ("~/Desktop", "novo.md")


@pytest.mark.parametrize(
    "dest",
    ["desktop", "downloads", "", "~/Projetos/site.com", "/Users/daniel/Desktop/plano/"],
)
def test_pasta_continua_pasta(dest) -> None:
    """Atalho, barra final, ou ponto no nome da PASTA não viram arquivo."""
    assert _split_file_destination(dest, "relatorio.pdf") == (dest, "relatorio.pdf")


def test_arquivo_direto_na_home_usa_o_atalho() -> None:
    """O resolvedor do cliente não expande um "~" sozinho."""
    assert _split_file_destination("~/notas.md", "notas.md") == ("home", "notas.md")


def test_caminho_final_da_entrega_resolve_atalho() -> None:
    assert _delivery_target_path("desktop", "x.md", HOME_MD) == "/Users/daniel/Desktop/x.md"
    assert _delivery_target_path("/tmp/a", "x.md", HOME_MD) == "/tmp/a/x.md"


def test_norm_user_path_expande_home_e_ignora_caixa() -> None:
    assert _norm_user_path("~/Desktop//Plano/", HOME_MD) == "/users/daniel/desktop/plano"
    assert _norm_user_path(ARQUIVO.upper(), HOME_MD) == _norm_user_path(ARQUIVO, HOME_MD)


def test_truthy_aceita_string() -> None:
    assert _truthy(True) and _truthy("true") and _truthy("Sim")
    assert not _truthy(None) and not _truthy("false") and not _truthy(0)


# ── schema ─────────────────────────────────────────────────────────────────


def test_write_file_aceita_source_path() -> None:
    params = next(t for t in DESKTOP_TOOLS if t.name == "write_file").parameters
    assert "source_path" in params and params["source_path"].required is False


# ── roteador: o classificador não redige arquivo ───────────────────────────


def _decisao(tool, args, speech_act="imperative") -> RoutingDecision:
    return RoutingDecision(
        intent=IntentType.CLIENT_TOOL, confidence=1.0, tool_name=tool,
        tool_arguments=args, speech_act=speech_act,
    )


def test_conteudo_inventado_pelo_classificador_vai_pro_cerebro() -> None:
    msg = f"Legal, atualiza o arquivo .md que fica em {ARQUIVO}"
    d = IntentRouter._reject_classifier_file_authoring(
        _decisao("write_file", {"path": ARQUIVO, "content": "# Análise\n\n## Headline", "mode": "write"}),
        msg,
    )
    assert d.intent is IntentType.DIRECT_RESPONSE
    assert d.tool_name is None and d.tool_arguments is None


def test_texto_ditado_fica_no_classificador_sem_overwrite() -> None:
    msg = "cria ~/Desktop/notas.txt com o texto: comprar pão e café"
    d = IntentRouter._reject_classifier_file_authoring(
        _decisao("write_file", {"path": "~/Desktop/notas.txt", "content": "comprar pão e  café", "overwrite": True}),
        msg,
    )
    assert d.intent is IntentType.CLIENT_TOOL
    assert "overwrite" not in d.tool_arguments


def test_pergunta_nunca_grava() -> None:
    """"atualizou o arquivo mesmo?" virou save_generated_file com overwrite=true."""
    d = IntentRouter._reject_classifier_file_authoring(
        _decisao(
            "save_generated_file",
            {"file_id": "68dae826", "destination": ARQUIVO, "overwrite": True},
            speech_act="question",
        ),
        "atualizou o arquivo mesmo?",
    )
    assert d.intent is IntentType.DIRECT_RESPONSE


def test_text_is_dictated() -> None:
    assert _text_is_dictated("oi   mundo", "grava 'Oi mundo' lá")
    assert not _text_is_dictated("texto que ninguém disse", "atualiza o arquivo")


# ── cérebro: HANDOFF com nome de tool ──────────────────────────────────────


def test_handoff_com_nome_de_tool_e_detectado() -> None:
    tools = [SimpleNamespace(name="save_generated_file"), SimpleNamespace(name="write_file")]
    texto = (
        "Vou forçar a sobrescrita.\n\n[[HANDOFF:save_generated_file]]\n"
        '{"file_id": "c7467a39"}\n[[/HANDOFF]]'
    )
    assert _handoff_block_names_tool(texto, tools) == "save_generated_file"
    assert _handoff_block_names_tool("[[HANDOFF:music]]toca[[/HANDOFF]]", tools) is None
    assert _handoff_block_names_tool(texto, None) is None


# ── emissão pelo cérebro ───────────────────────────────────────────────────


class _FakeSessions:
    def __init__(self) -> None:
        self.saved: list[str] = []

    async def add_message(self, **kw):
        self.saved.append(kw.get("content", ""))


class _FakeSandbox:
    def __init__(self, files: dict[str, str], cap: int = 10_000) -> None:
        self.files = files
        self.cap = cap

    async def read_file(self, path, *, max_bytes=None):
        if path not in self.files:
            return SimpleNamespace(ok=False, stderr="No such file", stdout="", truncated=False)
        body = self.files[path]
        return SimpleNamespace(
            ok=True, stderr="", stdout=body[: self.cap],
            truncated=len(body) > self.cap, size_bytes=len(body),
        )


def _service(sandbox=None):
    from modules.desktop.ai_service import AIService

    svc = object.__new__(AIService)
    svc.sessions = _FakeSessions()
    svc._pending_actuations = {}
    svc._pending_traces = {}
    svc._session_seen_files = {}
    svc.file_store = None
    svc.tool_executor = SimpleNamespace(
        _module_executors={"workbench": SimpleNamespace(sandbox=sandbox)} if sandbox else {}
    )
    return svc


class _Car:
    def __init__(self, tool, args):
        self.tool_name = tool
        self.arguments = args
        self.tool_call_id = "call-1"


async def _emit(svc, sid, tool, args):
    return [
        r
        async for r in svc._emit_client_actuation(
            _Car(tool, args), session_id=sid, user_message="atualiza o arquivo",
            user_id=uuid4(), metadata=HOME_MD, slog=None, trace=None, depth=0,
        )
    ]


def _args_enviados(saidas) -> dict:
    pedido = [r for r in saidas if r.tool_request]
    assert pedido, "nenhum tool_request emitido"
    return json.loads(pedido[0].tool_request["arguments"])


@pytest.mark.asyncio
async def test_source_path_puxa_o_texto_do_sandbox() -> None:
    svc = _service(_FakeSandbox({"/workspace/files/analise.md": "# versão editada\n"}))
    sid = uuid4()
    svc._remember_disk_file_seen(sid, ARQUIVO, HOME_MD)  # veio por request_user_file

    args = _args_enviados(await _emit(svc, sid, "write_file", {
        "path": ARQUIVO, "source_path": "/workspace/files/analise.md", "overwrite": True,
    }))

    assert args["content"] == "# versão editada\n"
    assert "source_path" not in args
    assert args["overwrite"] is True and args["mode"] == "write"


@pytest.mark.asyncio
async def test_overwrite_sem_ter_lido_o_arquivo_cai() -> None:
    """Sem a versão atual vista, sobrescrever é reescrever do zero."""
    svc = _service()
    args = _args_enviados(await _emit(svc, uuid4(), "write_file", {
        "path": ARQUIVO, "content": "texto novo", "mode": "overwrite",
    }))
    assert args["mode"] == "write"
    assert args["overwrite"] is False


@pytest.mark.asyncio
async def test_overwrite_com_til_casa_com_o_caminho_absoluto_visto() -> None:
    svc = _service()
    sid = uuid4()
    svc._remember_disk_file_seen(sid, ARQUIVO, HOME_MD)
    args = _args_enviados(await _emit(svc, sid, "write_file", {
        "path": "~/Desktop/plano-de-acao/analise-perfil.md",
        "content": "x", "overwrite": "true",
    }))
    assert args["overwrite"] is True


@pytest.mark.asyncio
async def test_source_path_ilegivel_volta_pro_cerebro_sem_enviar() -> None:
    svc = _service(_FakeSandbox({}))
    notas: list[str] = []

    async def _reenter(session_id, *, user_message, pend, extra_note, slog, trace, steps=None):
        notas.append(extra_note)
        return
        yield  # pragma: no cover

    svc._reenter_cognitive = _reenter
    saidas = await _emit(svc, uuid4(), "write_file", {
        "path": ARQUIVO, "source_path": "/workspace/files/nao-existe.md",
    })
    assert not [r for r in saidas if r.tool_request]
    assert notas and "NÃO foi enviado" in notas[0]


@pytest.mark.asyncio
async def test_source_path_cortado_nao_grava_meio_arquivo() -> None:
    svc = _service(_FakeSandbox({"/workspace/big.md": "a" * 50}, cap=10))
    notas: list[str] = []

    async def _reenter(session_id, *, user_message, pend, extra_note, slog, trace, steps=None):
        notas.append(extra_note)
        return
        yield  # pragma: no cover

    svc._reenter_cognitive = _reenter
    saidas = await _emit(svc, uuid4(), "write_file", {"path": ARQUIVO, "source_path": "/workspace/big.md"})
    assert not [r for r in saidas if r.tool_request]
    assert notas and "grande demais" in notas[0]


@pytest.mark.asyncio
async def test_save_generated_file_do_cerebro_separa_destination() -> None:
    svc = _service()
    args = _args_enviados(await _emit(svc, uuid4(), "save_generated_file", {
        "file_id": "f1", "file_name": "analise-perfil.md",
        "destination": ARQUIVO, "overwrite": True,
    }))
    assert args["destination"] == "/Users/daniel/Desktop/plano-de-acao"
    assert args["file_name"] == "analise-perfil.md"
    assert args["overwrite"] is False  # nunca viu a versão que está lá


# ── resultado: conflito e sucesso ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_conflito_pelo_classificador_cita_write_file_e_libera_o_reenvio() -> None:
    """Sem estado de retomada (caminho do classificador), a nota citava
    save_generated_file. O conflito também é o que libera o overwrite seguinte."""
    svc = _service()
    sid = uuid4()
    notas: list[str] = []

    async def _reenter(session_id, *, user_message, pend, extra_note, slog, trace):
        notas.append(extra_note)
        return
        yield  # pragma: no cover

    svc._reenter_cognitive = _reenter
    payload = json.dumps({
        "status": "conflict", "path": ARQUIVO,
        "existing": {"name": "analise-perfil.md", "size_bytes": 7081},
    })
    async for _ in svc._confirm_file_delivery(
        sid, success=True, result=payload, error=None, user_message="atualiza",
        pend={}, slog=None, trace=None, tool_name="write_file",
    ):
        pass

    assert "write_file com overwrite=true" in notas[0]
    assert "save_generated_file com overwrite=true" not in notas[0]
    assert svc._disk_file_was_seen(sid, ARQUIVO)


@pytest.mark.asyncio
async def test_pull_por_caminho_marca_versao_vista_e_ensina_a_gravar_de_volta() -> None:
    svc = _service()
    sid = uuid4()
    notas: list[str] = []

    async def _reenter(session_id, *, user_message, pend, extra_note, slog, trace, steps=None):
        notas.append(extra_note)
        return
        yield  # pragma: no cover

    svc._reenter_cognitive = _reenter
    svc._remember_session_attachments = lambda *a, **k: None

    async def _note(*a, **k):
        return None

    svc._note_client_step = _note
    async for _ in svc._resume_after_user_file(
        sid, success=True, result=json.dumps({"id": "abc", "name": "analise-perfil.md"}),
        error=None, user_message="atualiza",
        pend={"path": ARQUIVO, "metadata": HOME_MD}, slog=None, trace=None,
    ):
        pass

    assert svc._disk_file_was_seen(sid, "~/Desktop/plano-de-acao/analise-perfil.md", HOME_MD)
    assert "write_file" in notas[0] and "overwrite=true" in notas[0]
