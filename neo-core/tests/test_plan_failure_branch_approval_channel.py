"""O ramo de FALHA do plano despacha pelo canal que sabe pedir permissão.

Sessão d9f9417b (2026-09-20), interação 9. Plano de 4 passos:

    1. ls -la ~/Desktop/teste-de-analise            → ok
    2. find ... -name '*plano*.pdf'                 → ok (sem saída)
    3. mkdir ~/Desktop/teste-de-analise/planos-...  → FALHOU (exit 1, já existia)
    4. mv *.pdf para a pasta                        → nunca aconteceu

O passo 4 exigia aprovação. O ramo de falha era o único dos quatro despachos que
montava o `tool_request` na mão, repassando `requires_approval` cru. O desktop não
tem UI para aprovar um `tool_request` — ele responde com um veto seco
("comando exige aprovação e chegou como tool_request (sem UI de aprovação) — não
executado", services/ai_agent.rs) e o plano aborta na hora.

Do lado do usuário isso aparece como "ele pediu permissão e falhou antes de eu
autorizar": o card nunca existiu para ser autorizado.

O contrato: TODO despacho de passo passa por `_plan_step_dispatch`, que escolhe o
canal — `command_request` (com card) quando o passo exige aprovação, `tool_request`
com a flag baixa no resto, deixando a política do cliente decidir.
"""

from uuid import uuid4

import pytest

from core.planning.plan_manager import ExecutionPlan, PlanManager, PlanStep
from modules.desktop.ai_service import AIService


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
    def __init__(self, plan):
        self._plan = plan
        self.cleared = []

    def get_active_plan(self, session_id):
        return self._plan

    def clear_plans(self, session_id):
        self.cleared.append(session_id)

    async def advance_and_refine(self, **kwargs):  # pragma: no cover — não usado aqui
        return {"decision": "continue", "refined_step": None, "reason": ""}

    async def build_final_response(self, plan, user_message):
        return "resumo"

    build_plan_update = PlanManager.build_plan_update


class _FakeSessions:
    async def add_message(self, **kwargs):
        return None

    async def get_history(self, *a, **k):
        return []


class _FakeSettings:
    neo_brain_shell_enabled = False
    file_actuation_enabled = False


def _plano_da_interacao_9() -> ExecutionPlan:
    """Passo 3 rodando (vai falhar), passo 4 pendente e exigindo aprovação."""
    passos = [
        PlanStep(
            index=0, description="Listar a pasta", tool_name="run_command",
            tool_arguments={"command": "ls -la /Users/daniel/Desktop/teste-de-analise"},
            status="completed",
        ),
        PlanStep(
            index=1, description="Procurar PDFs", tool_name="run_command",
            tool_arguments={"command": "find /Users/daniel/Documents -name '*plano*.pdf'"},
            status="completed",
        ),
        PlanStep(
            index=2, description="Criar a pasta relatorios-anuais", tool_name="run_command",
            tool_arguments={"command": "mkdir /Users/daniel/Desktop/teste-de-analise/relatorios-anuais"},
            status="running",
        ),
        PlanStep(
            index=3, description="Mover os PDFs para a pasta", tool_name="run_command",
            tool_arguments={"command": "mv /Users/daniel/Documents/*.pdf /Users/daniel/Desktop/teste-de-analise/relatorios-anuais/"},
            requires_approval=True,
            status="pending",
        ),
    ]
    return ExecutionPlan(goal="Organizar os relatórios anuais", steps=passos)


def _svc(plan: ExecutionPlan) -> AIService:
    svc = object.__new__(AIService)
    svc.tools = _FakeRegistry()
    svc.planner = _FakePlanner(plan)
    svc._pending_traces = {}
    svc._live_sessions = set()
    svc._image_capable_sessions = set()
    svc._session_registers = {}
    svc._pending_actuations = {}
    svc._pending_music = {}
    svc._pending_browser_local = {}
    svc.sessions = _FakeSessions()
    svc.settings = _FakeSettings()
    return svc


async def _falhar_passo_3(svc: AIService):
    return [
        r async for r in svc.process_tool_result(
            session_id=uuid4(),
            tool_name="run_command",
            tool_call_id=str(uuid4()),
            success=False,
            error="[exit 1]",
            user_message="tenta de novo",
            command="mkdir /Users/daniel/Desktop/teste-de-analise/relatorios-anuais",
            tool_arguments={"command": "mkdir /Users/daniel/Desktop/teste-de-analise/relatorios-anuais"},
            _has_plan=True,
        )
    ]


@pytest.mark.asyncio
async def test_passo_seguinte_que_exige_aprovacao_sai_como_command_request():
    plan = _plano_da_interacao_9()
    svc = _svc(plan)

    responses = await _falhar_passo_3(svc)

    # Nenhum tool_request com a flag ligada — é isso que o cliente veta.
    assert all(
        not (r.tool_request and r.tool_request.get("requires_approval"))
        for r in responses
    ), "tool_request com requires_approval=true é vetado pelo desktop sem UI"

    aprovacao = next((r for r in responses if r.command_request), None)
    assert aprovacao is not None, "o passo destrutivo tem que sair pelo canal com card"
    assert aprovacao.metadata["_has_plan"] is True
    assert aprovacao.metadata["flow"] == "client_command"
    assert "mv " in aprovacao.command_request["command"]
    assert aprovacao.command_request["explanation"] == "Mover os PDFs para a pasta"


@pytest.mark.asyncio
async def test_o_cartao_da_timeline_sai_antes_do_pedido():
    """`_plan_step_dispatch` traz o `plan_update` de brinde — o ramo de falha
    não emitia nenhum, então a timeline ficava congelada no passo que falhou."""
    plan = _plano_da_interacao_9()
    svc = _svc(plan)

    responses = await _falhar_passo_3(svc)

    plan_idx = next(
        (i for i, r in enumerate(responses) if (r.metadata or {}).get("plan_update")), None
    )
    pedido_idx = next(
        (i for i, r in enumerate(responses) if r.command_request or r.tool_request), None
    )
    assert plan_idx is not None and pedido_idx is not None
    assert plan_idx < pedido_idx
    # `plan_update` e texto nunca no MESMO response (oneof do proto).
    assert all(not (r.message and (r.metadata or {}).get("plan_update")) for r in responses)


@pytest.mark.asyncio
async def test_passo_seguinte_sem_aprovacao_continua_como_tool_request():
    """Controle: o caminho comum não muda de canal."""
    plan = _plano_da_interacao_9()
    plan.steps[3].requires_approval = False
    svc = _svc(plan)

    responses = await _falhar_passo_3(svc)

    pedido = next((r for r in responses if r.tool_request), None)
    assert pedido is not None
    assert pedido.tool_request["requires_approval"] is False
    assert all(r.command_request is None for r in responses)


@pytest.mark.asyncio
async def test_usuario_e_avisado_de_que_o_passo_anterior_falhou():
    """A mensagem de contexto continua saindo antes do pedido."""
    plan = _plano_da_interacao_9()
    svc = _svc(plan)

    responses = await _falhar_passo_3(svc)

    assert any(
        r.message and "Passo anterior falhou" in r.message for r in responses
    )
