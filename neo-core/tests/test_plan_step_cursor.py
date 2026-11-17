"""O cursor do plano anda — e o validador recebe o passo certo.

`ExecutionPlan.advance()` marcava o passo como concluído/falho e devolvia o
próximo, mas nunca tocava em `current_step_index`. O campo ficava 0 pela vida
inteira do plano, e três consumidores liam dele:

1. `process_tool_result` deriva `completed_step_idx` daqui e passa pro
   `advance_and_refine`, que monta "## 2. Último Resultado / Step N: <descrição>
   / Tool: <tool>". Com o cursor travado, TODO passo depois do primeiro chegava
   no validador com o output de um passo e a descrição/tool de outro.
2. O log da sessão escrevia "Passo 1/N" em toda conclusão (sessão d9f9417b:
   "Passo 1/5" duas vezes seguidas na interação 8, "Passo 1/4" duas vezes na 9).
3. `build_plan_update`/`get_plan_state_for_metadata` publicavam 0.

O bug era silencioso porque "sempre 0" *parece* certo enquanto o plano tem um
passo só — que é o caso da maioria dos testes.
"""

from core.planning.plan_manager import ExecutionPlan, PlanStep


def _plano(n: int = 4) -> ExecutionPlan:
    passos = [
        PlanStep(
            index=i,
            description=f"passo {i + 1}",
            tool_name="run_command",
            tool_arguments={"command": f"echo {i}"},
            status="running" if i == 0 else "pending",
        )
        for i in range(n)
    ]
    return ExecutionPlan(goal="teste", steps=passos)


def test_cursor_anda_a_cada_advance():
    plan = _plano()
    assert plan.current_step_index == 0
    assert plan.last_advanced_index == 0

    proximo = plan.advance(step_result="saída 1")
    assert plan.current_step_index == 1
    assert plan.last_advanced_index == 0          # quem fechou foi o passo 0
    assert proximo.index == 1

    proximo.status = "running"
    plan.advance(step_result="saída 2")
    assert plan.current_step_index == 2
    assert plan.last_advanced_index == 1


def test_o_indice_aponta_pro_passo_que_realmente_fechou():
    """É este pareamento que o `advance_and_refine` usa pra montar o prompt."""
    plan = _plano()
    for esperado in range(4):
        atual = next(s for s in plan.steps if s.status in ("running", "pending"))
        atual.status = "running"
        plan.advance(step_result=f"saída do passo {esperado + 1}")
        fechado = plan.steps[plan.last_advanced_index]
        assert fechado.index == esperado
        assert fechado.description == f"passo {esperado + 1}"
        assert fechado.result == f"saída do passo {esperado + 1}"


def test_cursor_anda_tambem_quando_o_passo_falha():
    """O ramo de falha lê o mesmo índice — sessão d9f9417b, passo 3 → 4."""
    plan = _plano()
    plan.advance(step_result="ok")
    plan.steps[1].status = "running"
    plan.advance(step_error="[exit 1]")

    assert plan.last_advanced_index == 1
    assert plan.steps[plan.last_advanced_index].status == "failed"
    assert plan.steps[plan.last_advanced_index].error == "[exit 1]"


def test_plan_update_publica_o_cursor_real():
    from core.planning.plan_manager import PlanManager

    plan = _plano()
    plan.advance(step_result="ok")
    plan.steps[1].status = "running"
    plan.advance(step_result="ok")

    update = PlanManager.build_plan_update(None, plan)
    assert update["current_step"] == 2


def test_plano_de_um_passo_continua_igual():
    """Regressão: o caso que escondia o bug não pode mudar."""
    plan = _plano(1)
    assert plan.advance(step_result="ok") is None
    assert plan.last_advanced_index == 0
    assert plan.status == "completed"


def test_advance_sem_passo_nenhum_nao_mexe_no_cursor():
    plan = ExecutionPlan(goal="vazio", steps=[])
    assert plan.advance(step_result="ok") is None
    assert plan.current_step_index == 0
    assert plan.status == "completed"
