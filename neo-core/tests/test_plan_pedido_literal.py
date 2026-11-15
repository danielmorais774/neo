"""O pedido literal chega a quem refina o passo do plano.

Session-7b61bc5e (14/11/2026): "compacta a pasta projeto-loja em
~/Documents/work/projeto-loja.zip". O classificador montou o comando certo, mas
pediu plano; o planejador resumiu o objetivo como "compactar diretório de
trabalho", e o `advance_and_refine` (que só via esse resumo e descartava o
`system_context`) escreveu `zip -r projeto-loja.zip /Users/.../projeto-loja`, sem
pasta de trabalho: o zip de 4,5 GB foi parar na home, guardando a árvore
`Users/daniel/...` inteira.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.jobs.runner_comando_local import terminal
from core.planning.plan_manager import ExecutionPlan, PlanManager, PlanStep
from modules.desktop.ai_service import AIService

_PEDIDO = (
    "apaga o arquivo ziHU09pl que sobrou em ~/Documents/work e depois compacta a pasta "
    "projeto-loja em ~/Documents/work/projeto-loja.zip"
)


class _Provider:
    def __init__(self, resposta: dict):
        self.resposta = resposta
        self.mensagens: list = []

    async def complete(self, messages, **kw):
        self.mensagens.append(messages)
        return SimpleNamespace(get_content=lambda: json.dumps(self.resposta))


def _pm(resposta: dict) -> tuple[PlanManager, _Provider]:
    provider = _Provider(resposta)
    pm = PlanManager.__new__(PlanManager)
    pm.llm = SimpleNamespace(get_provider=lambda *a, **k: provider)
    pm.settings = None
    pm._plans = {}
    pm._subplan_history = {}
    return pm, provider


@pytest.mark.asyncio
async def test_plano_guarda_o_pedido_literal():
    pm, _ = _pm({
        "goal": "Remover arquivo temporário e compactar diretório de trabalho",
        "steps": [
            {"description": "Remover o ziHU09pl", "tool_name": "run_command",
             "tool_arguments": {"command": "rm -f /Users/daniel/Documents/work/ziHU09pl"},
             "requires_approval": True, "depends_on_previous": False},
            {"description": "Compactar a pasta projeto-loja em um arquivo zip",
             "tool_name": "run_command", "tool_arguments": {}, "depends_on_previous": True},
        ],
    })
    plan = await pm.generate_plan(user_message=_PEDIDO)
    assert plan is not None
    assert plan.pedido == _PEDIDO
    assert plan.goal != _PEDIDO  # o goal continua sendo a paráfrase


@pytest.mark.asyncio
async def test_refino_ve_o_pedido_e_o_contexto_do_sistema():
    pm, provider = _pm({
        "validation": {"success": True}, "decision": "continue", "reason": "ok",
        "refined_step": {"tool_name": "run_command", "tool_arguments": {"command": "x"}},
    })
    rm = PlanStep(index=0, description="Remover o ziHU09pl", tool_name="run_command",
                  tool_arguments={"command": "rm -f ~/Documents/work/ziHU09pl"})
    rm.status = "completed"
    rm.result = "(sem saída)"
    zip_ = PlanStep(index=1, description="Compactar a pasta projeto-loja em um arquivo zip",
                    tool_name="run_command", depends_on_previous=True)
    plan = ExecutionPlan(goal="compactar diretório de trabalho", pedido=_PEDIDO, steps=[rm, zip_])

    await pm.advance_and_refine(
        plan=plan, step_index=0, raw_output="(sem saída)",
        system_context="## Como escrever o comando (obrigatório)\n- É macOS",
    )

    prompt = provider.mensagens[0][-1].content
    assert "~/Documents/work/projeto-loja.zip" in prompt
    assert "valem exatamente como escritos" in prompt
    assert "Nunca troque um" in prompt
    assert "Como escrever o comando" in prompt


@pytest.mark.asyncio
async def test_plano_antigo_sem_pedido_continua_igual():
    pm, provider = _pm({"validation": {}, "decision": "finish", "reason": "ok"})
    passo = PlanStep(index=0, description="listar", tool_name="run_command",
                     tool_arguments={"command": "ls"})
    passo.status = "completed"
    plan = ExecutionPlan(goal="listar", steps=[passo])
    await pm.advance_and_refine(plan=plan, step_index=0, raw_output="a\nb")
    assert "Pedido do usuário" not in provider.mensagens[0][-1].content


def test_regra_de_zip_relativo_vai_para_o_comando():
    regras = AIService._build_shell_rules("darwin")
    assert "zip -rq" in regras
    assert "nome RELATIVO" in regras


def test_resumo_da_tarefa_cabe_no_aviso():
    """O dispatcher corta o resumo em 1200 no fallback; a lista do zip não pode empurrar o fim."""
    lista = "".join(f"  adding: projeto-loja/arquivo{i}.js (deflated 60%)\n" for i in range(400))
    fim = terminal(
        {"status": "exited", "exit_code": 0, "elapsed_s": 165, "tail": lista},
        {"command": "cd ~/Documents/work && zip -rq projeto-loja.zip projeto-loja", "cwd": "/Users/daniel"},
    )
    assert len(fim["result_summary"]) < 1200
    assert "arquivo399.js" in fim["result_summary"]
