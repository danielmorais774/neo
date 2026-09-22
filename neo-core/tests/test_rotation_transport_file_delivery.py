"""Sessão f33632b4 → 9b0b10cc: a conversa sobre o arquivo caiu numa sessão do clima.

Encadeamento medido em 2026-09-17:

1. "tenta salvar o arquivo lá agora" (26 min parado) → similaridade 0,398 → rotação;
2. a rotação chamou ``get_or_create_session(session_id=None)``, que devolveu a sessão
   ATIVA do clima da véspera — o desktop seguiu nela;
3. o ``ls`` da pasta pedido pelo cérebro foi guardado no gRPC pelo id antigo; a
   aprovação voltou com o id novo, sem comando → "comando ausente" → o cérebro
   respondeu "a pasta não existe";
4. "conclusao-relatorios-anuais-2026.md coloca lá nessa pasta" morreu duas vezes em
   "não achei no catálogo — peça de novo": o arquivo existia no sandbox, só nunca
   tinha passado por ``file_save``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.session.session_manager import PersistentSessionManager
from grpc_server import NeoDesktopServicer
from modules.desktop.ai_service import AIService, AIServiceResponse, _is_transport_failure
from shared.types import IntentType, RoutingDecision


# ── 1. continuação não rotaciona ────────────────────────────────────────────


@pytest.mark.parametrize("mensagem", [
    "tenta salvar o arquivo lá agora",
    "coloca de novo naquela pasta",
    "tenta novamente, a pasta existe",
    "quero que delete esse arquivo e traga o do workspace",
])
def test_retomada_com_lugar_ou_objeto_da_conversa_nao_e_assunto_novo(mensagem):
    res = PersistentSessionManager.detect_context_dependency(mensagem)
    assert res["is_context_dependent"] is True


@pytest.mark.parametrize("mensagem", [
    "como ta o clima aqui",
    "me explica como funciona a lei de Ohm em detalhes",
    "qual é a capital da França?",
])
def test_pergunta_avulsa_continua_podendo_rotacionar(mensagem):
    assert PersistentSessionManager.detect_context_dependency(mensagem)["is_context_dependent"] is False


# ── 2. rotação cria sessão nova ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rotacao_cria_sessao_nova_e_nunca_reusa_uma_ativa():
    chamadas = []

    class _DB:
        async def create_conversation_session(self, **kw):
            chamadas.append(kw)
            return SimpleNamespace(id=uuid4(), **kw)

        async def get_active_session(self, **kw):  # pragma: no cover - não pode ser chamado
            raise AssertionError("rotação não pode reaproveitar sessão ativa")

    svc = object.__new__(AIService)
    svc.db = _DB()
    antiga = uuid4()
    nova = await svc._new_session_after_rotation(antiga, user_id=uuid4(), service="neo-desktop-tauri")
    assert chamadas and chamadas[0]["metadata"]["created_after_rotated_session_id"] == str(antiga)
    assert nova.id != antiga


# ── 3. aprovação encontra o comando pelos dois ids ──────────────────────────


def test_pendente_fica_nas_duas_chaves_e_sai_das_duas():
    servicer = object.__new__(NeoDesktopServicer)
    servicer._pending = {}
    antiga, nova = str(uuid4()), uuid4()
    resp = SimpleNamespace(session_id=nova, metadata={"flow": "client_command", "command": "ls -la"})
    servicer._track_pending(antiga, resp, {**resp.metadata, "session_id": antiga})

    assert servicer._pending[str(nova)]["command"] == "ls -la"  # a aprovação chega com o id novo
    assert servicer._pending[antiga] is servicer._pending[str(nova)]
    tirado = servicer._take_pending(str(nova))
    assert tirado["command"] == "ls -la" and servicer._pending == {}


@pytest.mark.asyncio
async def test_aprovacao_sem_comando_recupera_do_estado_do_cerebro():
    sid = uuid4()
    svc = object.__new__(AIService)
    svc._pending_traces = {}
    svc._pending_actuations = {str(sid): {"kind": "shell", "command": 'ls -la "/Users/daniel/Desktop/teste-de-analise/"',
                                           "user_message": "tenta salvar o arquivo lá agora"}}

    class _Sessions:
        async def get_or_create_session(self, **kw):
            return SimpleNamespace(id=sid, user_id=None)

    svc.sessions = _Sessions()
    respostas = [r async for r in svc.process_command_approval(
        session_id=sid, command_id="c1", approved=True, command="",
    )]
    pedido = next(r.tool_request for r in respostas if r.tool_request)
    assert json.loads(pedido["arguments"])["command"] == 'ls -la "/Users/daniel/Desktop/teste-de-analise/"'


def test_falha_de_transporte_e_reconhecida():
    assert _is_transport_failure("comando ausente")
    assert _is_transport_failure("comando exige aprovação e chegou como tool_request (sem UI de aprovação) — não executado")
    assert not _is_transport_failure("ls: /Users/x: No such file or directory\n[exit 1]")


# ── 4. arquivo só no sandbox é publicado pelo nome ──────────────────────────


class _Sandbox:
    def __init__(self, achados):
        self.achados = achados
        self.comandos = []

    async def run(self, command, timeout=None, **kw):
        self.comandos.append(command)
        return SimpleNamespace(ok=True, stdout="\n".join(self.achados), stderr="")


class _Store:
    def __init__(self):
        self.harvested = []

    async def get(self, file_id):
        return None

    async def list_files(self, **kw):
        return []

    async def harvest(self, path, *, name=None, retention="perm", user_id=None, session_id=None, **kw):
        self.harvested.append(path)
        return SimpleNamespace(id=uuid4(), name=name, size_bytes=13756, sha256="c" * 64, mime="text/markdown")


def _svc_sandbox(achados):
    svc = object.__new__(AIService)
    svc.file_store = _Store()
    svc.db = None
    sandbox = _Sandbox(achados)
    svc.tool_executor = SimpleNamespace(_module_executors={"workbench": SimpleNamespace(sandbox=sandbox)})
    return svc, sandbox


@pytest.mark.asyncio
async def test_nome_que_so_existe_no_sandbox_e_publicado_e_entregue():
    svc, sandbox = _svc_sandbox(["/workspace/conclusao-relatorios-anuais-2026.md"])
    svc.settings = SimpleNamespace(neo_file_actuation_enabled=False)
    decision = RoutingDecision(
        intent=IntentType.CLIENT_TOOL, tool_name="save_generated_file", needs_plan=False,
        tool_arguments={"file_id": "conclusao-relatorios-anuais-2026.md", "file_name": "conclusao-relatorios-anuais-2026.md",
                        "destination": "/Users/daniel/Desktop/teste-de-analise/"},
    )
    respostas = [r async for r in svc._handle_client_tool(
        session_id=uuid4(), user_message="conclusao-relatorios-anuais-2026.md coloca la nessa pasta", decision=decision,
    )]
    pedido = next(r.tool_request for r in respostas if r.tool_request)
    args = json.loads(pedido["arguments"])
    assert svc.file_store.harvested == ["/workspace/conclusao-relatorios-anuais-2026.md"]
    assert args["file_name"] == "conclusao-relatorios-anuais-2026.md" and args["file_id"] != "conclusao-relatorios-anuais-2026.md"
    assert "-name conclusao-relatorios-anuais-2026.md" in sandbox.comandos[0]


@pytest.mark.asyncio
async def test_nome_em_varios_lugares_do_sandbox_nao_escolhe_no_chute():
    svc, _ = _svc_sandbox(["/workspace/a/relatorio.md", "/workspace/b/relatorio.md"])
    assert await svc._publish_sandbox_file_by_name("relatorio.md", user_id=None, session_id=uuid4()) is None
    assert svc.file_store.harvested == []


@pytest.mark.asyncio
async def test_id_inventado_sem_nome_de_arquivo_nao_vira_busca():
    svc, sandbox = _svc_sandbox(["/workspace/x.md"])
    assert await svc._publish_sandbox_file_by_name("file_1234567890abcdef", user_id=None, session_id=uuid4()) is None
    assert sandbox.comandos == []
