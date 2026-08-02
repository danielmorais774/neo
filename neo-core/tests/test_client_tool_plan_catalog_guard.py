"""Passo de plano com tool_name fora do catálogo real nunca é despachado.

O PlanManager (LLM próprio, core/planning/plan_manager.py) pode nomear um
tool_name que não existe mais no catálogo real (mesma classe de bug do
classificador — ver tests/test_intent_router_client_tool_grounding.py — só
que vinda do planner). Antes do fix, o prompt do generate_plan() listava
read_file/write_file/list_directory como "ferramenta mais apropriada", mas
nenhuma delas tem handler no neo-desktop-tauri: despachar um passo assim
trava o turno até o timeout do client, do mesmo jeito que a sessão bd7a304d.
"""

from uuid import uuid4

import pytest

from core.planning.plan_manager import ExecutionPlan, PlanManager, PlanStep
from modules.desktop.ai_service import AIService
from shared.types import IntentType, RoutingDecision


def _svc():
    return object.__new__(AIService)


class _FakeToolSchema:
    def __init__(self, name, execution):
        self.name = name
        self.execution = execution


class _FakeRegistry:
    def get_all(self):
        return [
            _FakeToolSchema("run_command", "client-side"),
            _FakeToolSchema("save_generated_file", "client-side"),
        ]


class _FakePlanner:
    """Só implementa o suficiente pra exercitar o primeiro passo do plano."""

    def __init__(self, plan):
        self._plan = plan
        self.cleared = []

    async def generate_plan(self, **kwargs):
        return self._plan

    def store_plan(self, session_id, plan):
        pass

    def clear_plans(self, session_id):
        self.cleared.append(session_id)

    # O despacho de um passo emite o cartão da timeline junto (plan_update) —
    # o real vive no PlanManager e só lê o plano, então reusar é fiel.
    build_plan_update = PlanManager.build_plan_update


def _svc_with_plan(tool_name: str) -> AIService:
    svc = _svc()
    svc.tools = _FakeRegistry()
    svc.planner = _FakePlanner(_plan_with_step(tool_name))
    svc._pending_traces = {}
    return svc


def _plan_with_step(tool_name: str) -> ExecutionPlan:
    # tool_arguments com comando de verdade: um passo `run_command` VAZIO é
    # indespachável por si só (trava o turno — sessão e8e5664b) e passou a ser
    # barrado antes do guard de catálogo. Sem comando aqui, este teste mediria a
    # trava errada em vez do tool_name fora do catálogo, que é o assunto dele.
    step = PlanStep(
        index=0, description="Ler arquivo", tool_name=tool_name,
        tool_arguments={"command": "ls -la ~/Downloads"},
    )
    return ExecutionPlan(goal="teste", steps=[step])


@pytest.mark.asyncio
async def test_plan_first_step_with_dead_tool_name_is_not_dispatched():
    svc = _svc_with_plan("read_file")  # tool morta

    decision = RoutingDecision(intent=IntentType.CLIENT_TOOL, needs_plan=True)
    responses = [
        r async for r in svc._handle_client_tool(
            session_id=uuid4(),
            user_message="relatorio de financas em csv na minha area de trabalho",
            decision=decision,
        )
    ]

    assert all(r.tool_request is None for r in responses)
    assert any(r.is_final for r in responses)
    assert svc.planner.cleared  # o plano foi abortado, não deixado pendurado


@pytest.mark.asyncio
async def test_plan_first_step_with_known_tool_name_is_dispatched():
    """Controle: run_command (tool real) passa reto pro dispatch."""
    svc = _svc_with_plan("run_command")

    decision = RoutingDecision(intent=IntentType.CLIENT_TOOL, needs_plan=True)
    responses = [
        r async for r in svc._handle_client_tool(
            session_id=uuid4(), user_message="lista meus arquivos", decision=decision,
        )
    ]

    assert any(r.tool_request is not None for r in responses)
    # O cartão da timeline sai junto e ANTES do pedido — é ele que o desktop
    # transforma em lista numerada. Antes o plano ia como markdown cru numa
    # bolha de "Pensando" e o cartão nunca aparecia na criação do plano.
    plan_idx = next(i for i, r in enumerate(responses) if (r.metadata or {}).get("plan_update"))
    tool_idx = next(i for i, r in enumerate(responses) if r.tool_request)
    assert plan_idx < tool_idx
    steps = responses[plan_idx].metadata["plan_update"]["steps"]
    assert [s["status"] for s in steps] == ["running"]
    # `plan_update` e texto nunca no MESMO response: o oneof do proto só leva um
    # conteúdo por CoreMessage e o plano ganha, descartando a mensagem.
    assert all(not (r.message and (r.metadata or {}).get("plan_update")) for r in responses)


@pytest.mark.asyncio
async def test_plan_step_requiring_approval_goes_out_as_command_request():
    """Passo destrutivo não pode sair como tool_request: não há UI que o aprove.

    Um `tool_request` com requires_approval=true fica esperando um "aprovar" que
    o desktop não sabe emitir — o turno trava em "Executando" até a conexão cair.
    O `command_request` tem o card de aprovar/recusar, e o `_has_plan` sobrevive
    à aprovação (o gRPC funde a metadata pendente antiga com a nova).
    """
    svc = _svc_with_plan("run_command")
    svc.planner._plan.steps[0].requires_approval = True

    decision = RoutingDecision(intent=IntentType.CLIENT_TOOL, needs_plan=True)
    responses = [
        r async for r in svc._handle_client_tool(
            session_id=uuid4(), user_message="limpa a pasta", decision=decision,
        )
    ]

    assert all(r.tool_request is None for r in responses)
    approval = next(r for r in responses if r.command_request)
    assert approval.metadata["_has_plan"] is True
    assert approval.metadata["flow"] == "client_command"
    assert approval.command_request["command"] == "ls -la ~/Downloads"
