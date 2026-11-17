"""Fiação do parking dentro de ``AIService._run_pipeline``.

test_parked_client_stops_queue.py fixa o orquestrador em isolamento, com o
``parked`` vindo pronto de um fake. Este arquivo fixa o adaptador: quem ACENDE o
``parked`` a partir dos responses reais do handler.

O sinal é ``is_pending_client_turn`` — o mesmo predicado que impede o fechamento
forçado do turno. Os dois têm que concordar por construção: se o turno não acabou
(está esperando o desktop), a fila também não pode andar.

Sessão d9f9417b: os três steps da cascata saíram em 4 segundos, com um card de
aprovação pendurado na tela do usuário desde o primeiro.
"""

import pytest

from modules.desktop.ai_service import AIService, AIServiceResponse
from shared.config import Settings
from shared.types import IntentType, PipelineStepSpec, RoutingDecision


def _service() -> AIService:
    svc = AIService.__new__(AIService)
    svc.settings = Settings()
    return svc


def _decision_em_cascata() -> RoutingDecision:
    """A cascata exata da interação 10."""
    return RoutingDecision(
        intent=IntentType.CLIENT_TOOL,
        confidence=0.95,
        tool_name="run_command",
        tool_arguments={"command": "mkdir -p /Users/daniel/Desktop/teste-de-analise/relatorios-anuais"},
        needs_memory=False,
        needs_plan=False,
        pipeline_plan=[
            PipelineStepSpec(
                intent=IntentType.CLIENT_TOOL,
                input_text="criar pasta relatorios-anuais em teste-de-analise",
            ),
            PipelineStepSpec(
                intent=IntentType.SERVER_TOOL,
                input_text="buscar links oficiais dos relatórios anuais",
            ),
            PipelineStepSpec(
                intent=IntentType.CLIENT_TOOL,
                input_text="baixar os PDFs encontrados para a pasta criada",
            ),
        ],
    )


async def _run(svc: AIService, decision: RoutingDecision) -> list[AIServiceResponse]:
    return [
        r
        async for r in svc._run_pipeline(
            decision=decision,
            user_message="cria a pasta, voce vai ter que baixar os pdfs na internet",
            history=[],
            sid=None,
            trace=None,
            sys_context=None,
            slog=None,
            user_id=None,
            metadata={"client": "test"},
            saved_msg=None,
            allow_clarification=False,
            clarification_round=1,
        )
    ]


def _install_handlers(svc: AIService, *, client_estaciona: bool):
    """Handlers falsos que imitam o contrato real de cada trilho."""
    vistos: list[str] = []

    async def fake_client_tool(session_id, user_message, decision, *a, **kw):
        vistos.append(f"client:{user_message}")
        if client_estaciona:
            # Contrato real: o pedido sai NÃO-final, com `flow` na metadata — é
            # isso que `is_pending_client_turn` enxerga.
            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=session_id,
                command_request={
                    "command_id": "cmd-1",
                    "command": "mkdir -p /Users/daniel/Desktop/teste-de-analise/relatorios-anuais",
                    "explanation": "Criar a pasta",
                    "is_background": False,
                },
                is_final=False,
                metadata={"flow": "client_command", "command_id": "cmd-1"},
            )
        else:
            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=session_id,
                message="feito",
                is_final=True,
            )

    async def fake_server_tool(session_id, user_message, history, decision, *a, **kw):
        vistos.append(f"server:{user_message}")
        yield AIServiceResponse(
            intent=IntentType.SERVER_TOOL,
            session_id=session_id,
            message="Tá rodando a busca no navegador.",
            is_final=True,
        )

    svc._handle_client_tool = fake_client_tool
    svc._handle_server_tool = fake_server_tool
    return vistos


@pytest.mark.asyncio
async def test_command_request_real_acende_o_parked_e_para_a_fila():
    svc = _service()
    vistos = _install_handlers(svc, client_estaciona=True)

    responses = await _run(svc, _decision_em_cascata())

    # Só o primeiro step rodou. A busca no navegador e o download não chegaram
    # a ser despachados enquanto o card estava na tela.
    assert vistos == ["client:criar pasta relatorios-anuais em teste-de-analise"]
    assert any(r.command_request for r in responses)

    # E o turno NÃO foi fechado à força: o terminador mataria o canal pelo qual
    # a aprovação volta (ver test_pending_client_turn_not_closed.py).
    assert not any(r.is_final for r in responses)


@pytest.mark.asyncio
async def test_cascata_sem_parking_roda_os_tres_steps():
    """Regressão: sem card pendente a cascata continua completa."""
    svc = _service()
    vistos = _install_handlers(svc, client_estaciona=False)

    await _run(svc, _decision_em_cascata())

    assert vistos == [
        "client:criar pasta relatorios-anuais em teste-de-analise",
        "server:buscar links oficiais dos relatórios anuais",
        "client:baixar os PDFs encontrados para a pasta criada",
    ]
