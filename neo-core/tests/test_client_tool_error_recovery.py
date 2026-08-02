"""Classificação de erro de comando client-side (CLIENT_TOOL) — "No such file or
directory" (caminho ausente) precisa ser distinguido de "command not found"
(binário ausente). Confundir os dois manda o playbook de flatpak/snap/dpkg
(pensado pra achar um APP sumido) vasculhar o sistema atrás de um coreutil
comum como `cp`, o que nunca acha nada e é caro o bastante pra travar o turno
até o timeout do client (sessão c7e2d5ed: `cp /workspace/...` — caminho só
existe no sandbox remoto, não no disco do usuário).
"""

from uuid import uuid4

import pytest

from modules.desktop.ai_service import AIService
from shared.types import IntentType, RoutingDecision


def _svc():
    return object.__new__(AIService)


def test_no_such_file_is_path_not_found_not_command_not_found():
    err = "cp: /workspace/financas_2026-07.csv: No such file or directory"
    assert AIService._detect_error_type(err, command="cp /workspace/financas_2026-07.csv /Users/daniel/Desktop/x.csv") == "path_not_found"


def test_command_not_found_still_classified_correctly():
    assert AIService._detect_error_type("zsh: command not found: blender") == "command_not_found"
    assert AIService._detect_error_type("bash: foo: código 127") == "command_not_found"


def test_permission_denied_unaffected():
    assert AIService._detect_error_type("Permission denied") == "permission_denied"


def test_http_404_is_not_command_not_found():
    """sessão 74539cf0: save_generated_file com file_id inválido devolveu
    'Download falhou: 404 Not Found' — 'not found' sozinho casava com o
    comando_not_found e mandava procurar o "programa" save_generated_file no
    flatpak/snap (não é um binário, é um tool_name — nunca acha nada)."""
    assert AIService._detect_error_type("Download falhou: 404 Not Found") == "unknown"


@pytest.mark.asyncio
async def test_recovery_skips_app_search_for_path_not_found():
    """Sem isso, o recovery dispararia o comando de diagnóstico gigante
    (which/flatpak/snap/dpkg/rpm/find) pra procurar "cp" no sistema — nunca
    acha nada e é isso que estoura o timeout do client."""
    svc = _svc()
    recovery = await svc._recover_from_tool_error(
        user_message="coloca esse arquivo na minha area de trabalho",
        tool_name="run_command",
        command="cp /workspace/financas_2026-07.csv /Users/daniel/Desktop/financas_2026-07.csv",
        error="cp: /workspace/financas_2026-07.csv: No such file or directory",
        retry_count=0,
    )
    assert recovery is None


@pytest.mark.asyncio
async def test_recovery_skips_app_search_for_named_tool_404():
    """sessão 74539cf0, ponta a ponta: mesmo se o error_type escapasse pela
    classificação de texto, uma tool nomeada (não run_command) nunca deveria
    entrar no playbook de 'procurar o programa via flatpak/snap' — não é um
    binário, é uma chamada estruturada."""
    svc = _svc()
    recovery = await svc._recover_from_tool_error(
        user_message="tem um tool para isso save_generated_file_to_path",
        tool_name="save_generated_file",
        command="save_generated_file",
        error="Download falhou: 404 Not Found",
        tool_arguments={"file_id": "f8d", "file_name": "extrato.txt", "destination": "/Users/daniel/Desktop"},
        retry_count=0,
    )
    assert recovery is None


# ── Trava determinística contra comando cruzando sandbox↔disco do usuário ──
# sessão ef4f723c: a explicação de topologia estava no prompt do classificador
# NESSA MESMA chamada e ele gerou `mv /workspace/...` mesmo assim — prosa
# sozinha não é confiável o bastante, então trava por código também.

@pytest.mark.asyncio
async def test_client_tool_blocks_raw_command_referencing_workspace():
    svc = _svc()
    decision = RoutingDecision(
        intent=IntentType.CLIENT_TOOL,
        raw_command="mv /workspace/extrato.xlsx /Users/daniel/Desktop/extrato.xlsx",
        needs_plan=False,
    )
    responses = [
        r async for r in svc._handle_client_tool(
            session_id=uuid4(),
            user_message="coloca esse arquivo na minha area de trabalho",
            decision=decision,
        )
    ]
    assert len(responses) == 1
    assert responses[0].is_final is True
    assert responses[0].command_request is None
    assert "sandbox" in responses[0].message.lower()


@pytest.mark.asyncio
async def test_client_tool_blocks_workspace_in_tool_arguments_command():
    svc = _svc()
    decision = RoutingDecision(
        intent=IntentType.CLIENT_TOOL,
        tool_name="run_command",
        tool_arguments={"command": "curl -o /Users/daniel/Desktop/x.csv http://localhost/workspace/x.csv"},
        needs_plan=False,
    )
    responses = [
        r async for r in svc._handle_client_tool(
            session_id=uuid4(), user_message="baixa esse arquivo", decision=decision,
        )
    ]
    assert len(responses) == 1
    assert responses[0].is_final is True


# ── Camada 3: file_id não fundamentado nunca é despachado pro desktop ─────
# sessão bd7a304d: o cognitivo gerou o CSV mas nunca chamou file_save (não
# existe file_id real). Numa mensagem seguinte ("envia pra area de
# trabalho"), o classificador inventou file_id="f8d" a partir do PREFIXO de
# um id de TRANSAÇÃO financeira não relacionada que aparecia no histórico
# (fin_statement: "id f8d35492") — o mesmo 404 de test_http_404_is_not_command_not_found
# acima, só que pego ANTES do dispatch em vez de depois do erro.

class _FakeFileStore:
    def __init__(self, known: dict[str, object] | None = None):
        self._known = known or {}

    async def get(self, file_id):
        return self._known.get(str(file_id))


@pytest.mark.asyncio
async def test_client_tool_blocks_save_generated_file_with_ungrounded_file_id():
    svc = _svc()
    svc.file_store = _FakeFileStore()  # catálogo vazio — "f8d" não existe
    decision = RoutingDecision(
        intent=IntentType.CLIENT_TOOL,
        tool_name="save_generated_file",
        tool_arguments={
            "file_id": "f8d",
            "file_name": "relatorio_financas.csv",
            "destination": "desktop",
        },
        needs_plan=False,
    )
    responses = [
        r async for r in svc._handle_client_tool(
            session_id=uuid4(), user_message="envia para area de trabalho", decision=decision,
        )
    ]
    assert len(responses) == 1
    assert responses[0].is_final is True
    assert responses[0].tool_request is None


@pytest.mark.asyncio
async def test_client_tool_allows_save_generated_file_with_real_file_id():
    """Controle: file_id que existe de verdade no catálogo passa reto pro dispatch."""
    svc = _svc()
    real_id = "b6e6c9d0-6e60-4f7a-9f2a-3d9a4e9b2f10"
    svc.file_store = _FakeFileStore({real_id: object()})
    decision = RoutingDecision(
        intent=IntentType.CLIENT_TOOL,
        tool_name="save_generated_file",
        tool_arguments={
            "file_id": real_id,
            "file_name": "relatorio_financas.csv",
            "destination": "desktop",
        },
        needs_plan=False,
    )
    responses = [
        r async for r in svc._handle_client_tool(
            session_id=uuid4(), user_message="envia para area de trabalho", decision=decision,
        )
    ]
    assert any(r.tool_request is not None for r in responses)


@pytest.mark.asyncio
async def test_client_tool_allows_normal_command():
    """Comando legítimo (sem /workspace/) passa reto pela trava."""
    svc = _svc()
    decision = RoutingDecision(
        intent=IntentType.CLIENT_TOOL,
        raw_command="df -h",
        explanation="Verificando espaço em disco",
        needs_plan=False,
    )
    responses = [
        r async for r in svc._handle_client_tool(
            session_id=uuid4(), user_message="quanto espaço tenho no disco?", decision=decision,
        )
    ]
    assert len(responses) == 2
    assert responses[1].command_request is not None
    assert responses[1].command_request["command"] == "df -h"
