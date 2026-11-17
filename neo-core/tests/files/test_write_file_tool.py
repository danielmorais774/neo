"""`write_file` — a tool que escreve no disco do usuário sem passar pelo shell.

Ela já existiu no catálogo e foi REMOVIDA em 2026-08-02 por ser schema sem
executor: o ToolRequest saía, nada respondia, e o turno ficava travado os 300s do
timeout do cliente (sessão 2afe5753). Voltou em 2026-08-30 com o handler de
verdade em `ai_agent.rs` (`write_text_file` em commands/mod.rs).

O que estes testes travam é justamente o que fez ela morrer da primeira vez —
schema no catálogo sem execução do outro lado — mais as duas guardas que a
tornam segura: não escrever no caminho do sandbox por engano, e nunca sobrescrever
um arquivo existente sem decisão explícita.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from modules.desktop.tools import DESKTOP_TOOLS, FILE_ACTUATION_TOOL_NAMES
from modules.desktop.ai_service import _CLIENT_ACTUATION_TOOLS, AIServiceResponse
from shared.types import IntentType


def _tool():
    return next(t for t in DESKTOP_TOOLS if t.name == "write_file")


# ── contrato do catálogo ──────────────────────────────────────────────────


def test_esta_no_catalogo_como_client_side() -> None:
    tool = _tool()
    assert tool.execution == "client-side"
    assert tool.module == "desktop"


def test_path_e_content_sao_obrigatorios() -> None:
    params = _tool().parameters
    assert params["path"].required is True
    # `content` OU `source_path` (arquivo editado no /workspace) — sessão 30b1e484.
    assert params["content"].required is False
    assert params["source_path"].required is False
    # mode/overwrite são opcionais: o padrão tem que ser o seguro (write, sem
    # sobrescrever), não algo que o modelo precise lembrar de passar.
    assert params["mode"].required is False
    assert params["overwrite"].required is False
    assert set(params["mode"].enum) == {"write", "append"}


def test_descricao_desvia_do_heredoc() -> None:
    """O motivo de ela existir tem que estar na descrição, senão o modelo
    continua escrevendo arquivo por `cat <<EOF` e corrompendo o conteúdo."""
    d = _tool().description
    assert "run_command" in d
    assert "EOF" in d or "heredoc" in d


def test_e_client_actuating_pelos_dois_lados() -> None:
    """O cérebro chama no meio do turno; o round-trip é o mesmo do irmão."""
    assert "write_file" in FILE_ACTUATION_TOOL_NAMES
    assert "write_file" in _CLIENT_ACTUATION_TOOLS


# ── as guardas ────────────────────────────────────────────────────────────


class _FakeSessions:
    def __init__(self) -> None:
        self.saved: list[str] = []

    async def add_message(self, **kw):
        self.saved.append(kw.get("content", ""))


def _service():
    from modules.desktop.ai_service import AIService

    svc = object.__new__(AIService)
    svc.sessions = _FakeSessions()
    svc._pending_actuations = {}
    svc._pending_traces = {}
    svc.file_store = None
    return svc


class _Car:
    def __init__(self, args):
        self.tool_name = "write_file"
        self.arguments = args
        self.tool_call_id = "call-1"


async def _emit(svc, args):
    return [
        r
        async for r in svc._emit_client_actuation(
            _Car(args),
            session_id=uuid4(),
            user_message="grava isso pra mim",
            user_id=uuid4(),
            metadata=None,
            slog=None,
            trace=None,
            depth=0,
        )
    ]


@pytest.mark.asyncio
async def test_caminho_do_sandbox_e_bloqueado() -> None:
    """`/workspace/x` na máquina DELE criaria uma pasta no disco errado e o
    arquivo sumiria da vista dos dois lados."""
    svc = _service()
    saidas = await _emit(svc, {"path": "/workspace/notas.md", "content": "oi"})

    assert len(saidas) == 1
    assert saidas[0].is_final is True
    assert "sandbox" in saidas[0].message
    assert saidas[0].tool_request is None
    assert svc._pending_actuations == {}


@pytest.mark.asyncio
async def test_caminho_normal_vira_tool_request_nao_final() -> None:
    svc = _service()
    saidas = await _emit(svc, {"path": "~/Desktop/notas.md", "content": "oi"})

    pedido = [r for r in saidas if r.tool_request]
    assert pedido, "nenhum tool_request emitido"
    req = pedido[0]
    assert req.is_final is False, "turno estacionado no cliente não pode ser final"
    assert req.tool_request["tool_name"] == "write_file"
    assert json.loads(req.tool_request["arguments"])["path"] == "~/Desktop/notas.md"
    # flow é o que o gRPC e o pipeline usam para saber que o turno está esperando
    assert req.metadata["flow"] == "client_tool"


# ── colisão: nunca sobrescreve calado ─────────────────────────────────────


@pytest.mark.asyncio
async def test_conflito_manda_rechamar_a_tool_certa() -> None:
    """Mandar reenviar por `save_generated_file` faria o cérebro procurar um
    file_id que nunca existiu — o arquivo foi escrito, não publicado."""
    svc = _service()
    notas: list[str] = []

    async def _reenter(session_id, *, user_message, pend, extra_note, slog, trace):
        notas.append(extra_note)
        return
        yield  # pragma: no cover — mantém a assinatura de gerador

    svc._reenter_cognitive = _reenter
    payload = json.dumps({
        "status": "conflict",
        "path": "/Users/daniel/Desktop/notas.md",
        "existing": {"name": "notas.md", "size_bytes": 2048, "modified_at": "2026-08-30 18:00"},
    })

    async for _ in svc._confirm_file_delivery(
        uuid4(), success=True, result=payload, error=None,
        user_message="grava isso", pend={"tool_name": "write_file"},
        slog=None, trace=None,
    ):
        pass

    assert notas, "não re-entrou no cognitivo"
    assert "write_file com overwrite=true" in notas[0]
    assert "save_generated_file com overwrite=true" not in notas[0]


@pytest.mark.asyncio
async def test_sucesso_confirma_com_os_bytes() -> None:
    svc = _service()
    payload = json.dumps({
        "status": "saved",
        "path": "/Users/daniel/Desktop/notas.md",
        "bytes_written": 128,
    })

    # Depois de gravar, o turno NÃO fecha sozinho: ele reentra no cognitivo
    # para o cérebro decidir se falta arquivo. Ver o comentário do ramo `saved`
    # — `save_generated_file` entrega UM por chamada, e fechar aqui fazia "põe
    # os 6 PDFs na pasta" virar um por turno (sessão 8c449cb8).
    notas: list[str] = []

    async def _fake_reenter(session_id, *, user_message, pend, extra_note, **kw):
        notas.append(extra_note)
        yield AIServiceResponse(
            intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
            message="Pronto, escrevi o arquivo em /Users/daniel/Desktop/notas.md (128 bytes).",
            is_final=True, iterations=1,
        )

    svc._reenter_cognitive = _fake_reenter

    saidas = [
        r
        async for r in svc._confirm_file_delivery(
            uuid4(), success=True, result=payload, error=None,
            user_message="grava isso", pend={"tool_name": "write_file"},
            slog=None, trace=None,
        )
    ]

    assert saidas[-1].is_final is True
    assert notas, "o sucesso tem que reentrar no cognitivo"
    # A nota diz o que foi entregue E cobra a decisão de seguir ou fechar.
    assert "/Users/daniel/Desktop/notas.md" in notas[0]
    assert "PRÓXIMO" in notas[0]


@pytest.mark.asyncio
async def test_save_generated_file_mantem_a_frase_dele() -> None:
    """Regressão: a tool irmã não pode herdar o texto do write_file."""
    svc = _service()
    payload = json.dumps({"status": "saved", "path": "/Users/daniel/Desktop/x.pdf"})

    notas: list[str] = []

    async def _fake_reenter(session_id, *, user_message, pend, extra_note, **kw):
        notas.append(extra_note)
        yield AIServiceResponse(
            intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
            message="ok", is_final=True, iterations=1,
        )

    svc._reenter_cognitive = _fake_reenter

    saidas = [
        r
        async for r in svc._confirm_file_delivery(
            uuid4(), success=True, result=payload, error=None,
            user_message="salva aí", pend={"tool_name": "save_generated_file"},
            slog=None, trace=None,
        )
    ]

    assert saidas[-1].is_final is True
    assert "/Users/daniel/Desktop/x.pdf" in notas[0]
