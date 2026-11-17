"""O pipeline não pode encerrar um turno que está ESPERANDO o desktop.

Sessões 081cd1c3 e 15b6b825: o Neo pedia permissão para rodar um comando, o
usuário via "Aprovar e Executar" na tela, clicava — e não acontecia nada.

A causa não estava no botão. Um ``tool_request``/``command_request`` sai
NÃO-final de propósito (o turno está estacionado no cliente), e o pipeline lia
"nenhum response final" como cadeia furada: emitia um terminador, o Rust via
``is_final`` → ``break`` + ``drop(tx)`` (ai_agent.rs) e fechava a metade de
envio da stream — ou seja, o canal por onde a aprovação ia voltar. O turno
morria milissegundos depois de pedir a permissão que ainda estava na tela.

A invariante original continua valendo e tem teste aqui: cadeia que termina de
verdade sem final AINDA recebe terminador, senão o chat gira para sempre.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

import modules.desktop.ai_service as ai_service_mod
from modules.desktop.ai_service import (
    AIService,
    AIServiceResponse,
    is_pending_client_turn,
)
from shared import get_settings
from shared.types import IntentType, RoutingDecision


# ── o predicado ───────────────────────────────────────────────────────────


def _resp(**kw) -> AIServiceResponse:
    base = {"intent": IntentType.DIRECT_RESPONSE, "session_id": uuid4()}
    base.update(kw)
    return AIServiceResponse(**base)


def test_tool_request_nao_final_e_turno_estacionado() -> None:
    assert is_pending_client_turn(
        _resp(is_final=False, metadata={"flow": "client_tool"})
    )


def test_command_request_nao_final_e_turno_estacionado() -> None:
    assert is_pending_client_turn(
        _resp(is_final=False, metadata={"flow": "client_command"})
    )


def test_response_final_nunca_e_estacionado() -> None:
    assert not is_pending_client_turn(
        _resp(is_final=True, message="pronto", metadata={"flow": "client_tool"})
    )


def test_metadata_sem_flow_nao_e_estacionado() -> None:
    """`plan_update` só desenha a timeline — não deixa ninguém devendo nada."""
    assert not is_pending_client_turn(
        _resp(is_final=False, metadata={"plan": {"steps": []}})
    )
    assert not is_pending_client_turn(_resp(is_final=False, message="pensando"))


# ── o comportamento do pipeline ───────────────────────────────────────────


class _FakeOrchestrator:
    """Só devolve o roteiro; o que está sob teste é o epílogo do _run_pipeline."""

    scripted: list[AIServiceResponse] = []

    def __init__(self, *_a, **_kw) -> None:
        pass

    async def run(self, _steps):
        for resp in self.scripted:
            yield resp


def _service() -> AIService:
    service = object.__new__(AIService)
    service.settings = get_settings()
    return service


async def _drain(service: AIService, decision: RoutingDecision):
    return [
        resp
        async for resp in service._run_pipeline(
            decision=decision,
            user_message="lê o arquivo",
            history=[],
            sid=uuid4(),
            trace=None,
            sys_context=None,
            slog=None,
            user_id=uuid4(),
            metadata=None,
            saved_msg=None,
            allow_clarification=False,
            clarification_round=0,
        )
    ]


def _decision() -> RoutingDecision:
    return RoutingDecision(
        intent=IntentType.CLIENT_TOOL,
        confidence=1.0,
        needs_memory=False,
        needs_plan=False,
        requires_approval=True,
    )


@pytest.mark.asyncio
async def test_turno_esperando_aprovacao_nao_recebe_terminador(monkeypatch) -> None:
    """O caso da tela: a permissão ainda está pendente, o turno segue aberto."""
    _FakeOrchestrator.scripted = [
        _resp(is_final=False, message="Preciso de permissão para executar um comando."),
        _resp(
            is_final=False,
            command_request={"command_id": "c1", "command": "ls -la", "explanation": "x"},
            metadata={"flow": "client_command", "command_id": "c1"},
        ),
    ]
    monkeypatch.setattr(ai_service_mod, "PipelineOrchestrator", _FakeOrchestrator)

    saidas = await _drain(_service(), _decision())

    assert len(saidas) == 2, "o terminador voltou e vai fechar a stream de novo"
    assert not any(r.is_final for r in saidas)


@pytest.mark.asyncio
async def test_cadeia_morta_sem_final_ainda_recebe_terminador(monkeypatch) -> None:
    """A invariante que o fechamento forçado existe para proteger."""
    _FakeOrchestrator.scripted = [
        _resp(is_final=False, message="pensando alto"),
    ]
    monkeypatch.setattr(ai_service_mod, "PipelineOrchestrator", _FakeOrchestrator)

    saidas = await _drain(_service(), _decision())

    assert len(saidas) == 2
    assert saidas[-1].is_final is True
    assert saidas[-1].message


@pytest.mark.asyncio
async def test_turno_normal_com_final_nao_ganha_segundo_final(monkeypatch) -> None:
    _FakeOrchestrator.scripted = [
        _resp(is_final=True, message="pronto, são 42 linhas"),
    ]
    monkeypatch.setattr(ai_service_mod, "PipelineOrchestrator", _FakeOrchestrator)

    saidas = await _drain(_service(), _decision())

    assert len(saidas) == 1
    assert saidas[0].message == "pronto, são 42 linhas"


@pytest.mark.asyncio
async def test_grpc_usa_o_mesmo_predicado() -> None:
    """Divergir significaria o pipeline fechar o turno que o gRPC está guardando."""
    from grpc_server import NeoDesktopServicer

    parado = _resp(is_final=False, metadata={"flow": "client_tool"})
    assert NeoDesktopServicer._is_pending_flow(parado) is is_pending_client_turn(parado)
