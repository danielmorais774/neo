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


# ── Placeholder de passo dependente nunca chega no shell (sessão f80b3a90) ──
#
# "verifica se arquivo foi atualizado mesmo" (2026-09-18). O plano tinha 2 passos:
#   1. `ls -la /Users/daniel/Documents/analises`   → a pasta não existe, FALHOU
#   2. `ls -l {previous_output}` (depends_on_previous)
# O ramo de FALHA chama `advance_and_refine` com `raw_output="ERRO: …"`; ele voltou
# sem `refined_step` e o passo 2 saiu **com o template literal**. O usuário viu
# `ls: {previous_output}: No such file or directory` — um erro que não diz nada
# sobre o problema real (o passo 1 é que tinha falhado).
#
# Os outros TRÊS despachos de passo já passavam por `_dispatch_would_hang`; só este
# não. E a trava, mesmo onde existia, só pegava o comando que É um placeholder
# inteiro (`[SERÁ GERADO…]`), não o template EMBUTIDO num comando plausível.

from modules.desktop.ai_service import _dispatch_would_hang


def test_template_embutido_conta_como_indespachavel():
    def barra(cmd, **extra):
        return _dispatch_would_hang("run_command", {"command": cmd, **extra})

    assert barra("ls -l {previous_output}")
    assert barra("cat {previous_output_placeholder}")
    assert barra("echo {STEP_RESULT} > /tmp/x")
    assert barra("[SERÁ GERADO COM BASE NOS ARQUIVOS LIDOS]")
    assert barra("")
    # `working_dir` segue o mesmo caminho até o shell.
    assert barra("ls -la", working_dir="{previous_output}")


def test_chaves_legitimas_de_shell_nao_sao_placeholder():
    """A trava é estreita de propósito: barrar comando bom é pior que deixar
    passar um ruim, e `{}` é sintaxe corrente de shell."""
    def barra(cmd):
        return _dispatch_would_hang("run_command", {"command": cmd})

    assert not barra("echo ${HOME}")                      # variável
    assert not barra(r"find . -name x -exec rm {} \;")    # chaves vazias
    assert not barra("cp /a/{foto1,foto2}.jpg /b/")        # expansão de chaves
    assert not barra("echo {1..5}")                        # expansão de faixa
    assert not barra("awk '{print $1}' /tmp/a")            # bloco do awk
    assert not barra("jq '{name}' /tmp/a.json")            # objeto do jq
    assert not barra("ls -la /Users/daniel/Desktop")


class _PlannerComPlano:
    """Planner do ramo de falha: `advance_and_refine` não devolve `refined_step`
    (o fallback real quando o JSON vem vazio/inválido — o caso medido)."""

    def __init__(self, plan, refined=None):
        self._plan = plan
        self._refined = refined
        self.cleared = []

    def get_active_plan(self, sid):
        return self._plan

    def clear_plans(self, sid):
        self.cleared.append(sid)

    async def advance_and_refine(self, **kw):
        return {"refined_step": self._refined, "decision": "continue"}

    async def build_final_response(self, plan, user_message):
        return "; ".join(f"{s.description}: {s.error or 'ok'}" for s in plan.steps)

    build_plan_update = PlanManager.build_plan_update


class _SessionsFake:
    async def add_message(self, **kw):
        return None

    async def get_history(self, *a, **kw):
        return []

    async def get_or_create_session(self, **kw):
        class _S:
            id = kw.get("session_id")
            user_id = None

        return _S()


def _svc_apos_falha(refined=None) -> tuple[AIService, ExecutionPlan]:
    p1 = PlanStep(
        index=0, description="listar a pasta", tool_name="run_command",
        tool_arguments={"command": "ls -la /Users/daniel/Documents/analises"},
    )
    p2 = PlanStep(
        index=1, description="ver a data de modificação", tool_name="run_command",
        tool_arguments={"command": "ls -l {previous_output}"},
        depends_on_previous=True,
    )
    p2.raw_command = "ls -l {previous_output}"
    plan = ExecutionPlan(goal="verificar atualização", steps=[p1, p2])

    svc = _svc()
    svc.planner = _PlannerComPlano(plan, refined)
    svc.sessions = _SessionsFake()
    svc._pending_actuations = {}
    svc._pending_traces = {}
    svc._live_sessions = set()
    svc._image_capable_sessions = set()
    svc._session_registers = {}
    svc._session_disk_evidence = {}
    svc.tools = _FakeRegistry()
    return svc, plan


async def _falha_o_passo_1(svc) -> list:
    return [
        r async for r in svc.process_tool_result(
            session_id=uuid4(), tool_name="run_command", tool_call_id="t1",
            success=False, result=None,
            error="ls: /Users/daniel/Documents/analises: No such file or directory",
            user_message="verifica se arquivo foi atualizado mesmo",
            command="ls -la /Users/daniel/Documents/analises",
            tool_arguments={"command": "ls -la /Users/daniel/Documents/analises"},
            _has_plan=True,
        )
    ]


@pytest.mark.asyncio
async def test_passo_dependente_sem_refinamento_nao_vai_pro_shell():
    svc, plan = _svc_apos_falha(refined=None)

    responses = await _falha_o_passo_1(svc)

    assert all(r.tool_request is None for r in responses), (
        "o template `{previous_output}` não pode ser despachado"
    )
    assert plan.steps[1].status == "failed"
    assert "dependia do resultado do anterior" in plan.steps[1].error
    # E o usuário fica sabendo o que realmente aconteceu, não "ls: {previous_output}".
    final = next(r for r in responses if r.is_final)
    assert "{previous_output}" not in (final.message or "")
    assert "No such file or directory" in (final.message or "")


@pytest.mark.asyncio
async def test_passo_dependente_refinado_de_verdade_continua_despachando():
    """Controle: o conserto barra o placeholder, não a recuperação. Quando o
    refinamento devolve um comando real, o plano segue como sempre seguiu."""
    svc, plan = _svc_apos_falha(refined={
        "tool_name": "run_command",
        "tool_arguments": {"command": "ls -l /Users/daniel/Desktop/teste-de-analise"},
    })

    responses = await _falha_o_passo_1(svc)

    pedido = next((r for r in responses if r.tool_request), None)
    assert pedido is not None, "passo refinado tem que ser despachado"
    assert "{previous_output}" not in pedido.tool_request["arguments"]
    assert plan.steps[1].status == "running"
