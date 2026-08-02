"""Falha de provider fecha o turno, mas NÃO vira a fala do assistant.

Antes, o `except` do `_handle_direct_response` fazia
`full_response = f"Erro ao processar: {e}"` e seguia o fluxo normal. O texto cru
do erro era então:
  - salvo no banco como a resposta do assistant;
  - reinjetado no histórico do turno seguinte (envenena o contexto);
  - mandado pro extrator de preferências E pro PKS planner como se fosse conteúdo.

Tudo isso aconteceu de verdade na sessão 1aec71b8, onde um 400 do DeepSeek
("Thinking mode does not support this tool_choice") virou a "resposta" e foi
parar nos dois pipelines de memória — dá pra ver as duas chamadas no app.log.

O `except Exception` do `_handle_direct_response` delega pro `_fail_turn`, que
é o que se exercita aqui — chegar no `except` pelo caminho completo exigiria
stub de meia dúzia de colaboradores e o teste quebraria a cada campo novo.

Molde de tests/test_brain_shell_actuation.py (método exercitado direto).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from modules.desktop.ai_service import TURN_FAILED_REPLY, AIService


class _FakeSessions:
    def __init__(self) -> None:
        self.added: list[dict] = []

    async def add_message(self, **kw):
        self.added.append(kw)

    async def get_history(self, *a, **kw):
        return []

    async def recover_context(self, *a, **kw):
        return []


def _svc() -> AIService:
    svc = AIService.__new__(AIService)
    svc.sessions = _FakeSessions()
    svc.file_store = None
    svc.preferences = None
    svc.pks = None
    svc._pending_actuations = {}
    svc._pending_traces = {}
    svc._live_sessions = set()
    svc._image_capable_sessions = set()
    svc._session_registers = {}
    svc._session_attachments = {}
    return svc


@pytest.mark.asyncio
async def test_falha_de_provider_nao_vira_resposta_salva():
    svc = _svc()

    # O 400 real que derrubou a sessão 1aec71b8.
    exc = RuntimeError(
        "Erro na completion DeepSeek: Error code: 400 - {'error': {'message': "
        "'Thinking mode does not support this tool_choice'}}"
    )

    sid = uuid4()
    out = [r async for r in svc._fail_turn(sid, exc)]

    # 1. O turno FECHA — o usuário não fica olhando pra spinner.
    assert out and out[-1].is_final

    # 2. O que ele vê é frase humana, não stack trace.
    reply = out[-1].message
    assert reply == TURN_FAILED_REPLY
    for vazamento in ("400", "tool_choice", "Traceback", "DeepSeek", "Error code"):
        assert vazamento not in reply, f"{vazamento!r} vazou pro usuário"

    # 3. O que foi pro banco é a MESMA frase curta — é isso que volta no
    #    histórico do próximo turno.
    salvos = [m for m in svc.sessions.added if m.get("role") == "assistant"]
    assert len(salvos) == 1
    assert salvos[0]["content"] == TURN_FAILED_REPLY
    assert "tool_choice" not in salvos[0]["content"]


def test_frase_de_falha_e_reconhecida_pelo_appraiser_de_emocao():
    """O afeto não pode reagir a uma falha de infra como se o Neo tivesse falado.

    Acoplamento por prefixo — se alguém mudar TURN_FAILED_REPLY sem mexer na
    lista do affect_updater, este teste cai antes de virar emoção fantasma.
    """
    from core.agent.affect_updater import _ERROR_RESPONSE_PREFIXES

    assert TURN_FAILED_REPLY.startswith(_ERROR_RESPONSE_PREFIXES)
