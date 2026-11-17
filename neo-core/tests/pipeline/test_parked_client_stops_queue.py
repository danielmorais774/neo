"""A fila da cascata para no step que estacionou no cliente.

Sessão d9f9417b (2026-09-20), interação 10. O usuário pediu "cria a pasta, voce
vai ter que baixar os pdfs na internet" e o classificador devolveu uma cascata:

    1. CLIENT_TOOL  criar pasta relatorios-anuais em teste-de-analise
    2. SERVER_TOOL  buscar links oficiais dos relatórios anuais
    3. CLIENT_TOOL  baixar os PDFs encontrados para a pasta criada

O step 1 pôs um card de aprovação na tela às 17:31:05. A fila não esperou: o step
2 disparou o agente de browser no mesmo segundo e o step 3 ("baixar os PDFs
ENCONTRADOS") disparou sem nenhum link, porque a busca ainda estava rodando.

Um step CLIENT_TOOL não executa nada — ele emite o pedido e o gerador acaba. O
trabalho real acontece quando o desktop devolve o resultado, por
``process_tool_result``, que é outra entrada e não conhece esta fila. Andar a fila
aqui é despachar o próximo passo em cima de um resultado que não existe.

Do lado do usuário: "ele pediu permissão e falhou antes de eu autorizar".
"""

import pytest

from core.pipeline.orchestrator import PipelineOrchestrator
from core.pipeline.types import Handoff, PipelineStep, StepOutcome
from shared.types import IntentType


def _step(text, intent=IntentType.CLIENT_TOOL):
    return PipelineStep(intent=intent, input_text=text, origin="classifier", depth=0)


CASCATA_DA_SESSAO = [
    _step("criar pasta relatorios-anuais em teste-de-analise"),
    _step("buscar links oficiais dos relatórios anuais", IntentType.SERVER_TOOL),
    _step("baixar os PDFs encontrados para a pasta criada"),
]


@pytest.mark.asyncio
async def test_step_estacionado_no_cliente_para_a_fila():
    rodaram = []

    async def runner(step):
        rodaram.append(step.input_text)
        if step.input_text.startswith("criar pasta"):
            yield "card de aprovação", None
            yield None, StepOutcome(ok=True, final_text=None, parked=True)
        else:
            yield f"resposta de {step.input_text}", None
            yield None, StepOutcome(ok=True, final_text="ok")

    orch = PipelineOrchestrator(runner)
    out = [r async for r in orch.run(list(CASCATA_DA_SESSAO))]

    assert rodaram == ["criar pasta relatorios-anuais em teste-de-analise"]
    assert out == ["card de aprovação"]


@pytest.mark.asyncio
async def test_os_steps_descartados_sao_nomeados_no_notice():
    """Descartar em silêncio é como o bug se escondeu — o notice tem que dizer
    quais passos ficaram para trás."""
    avisos = []

    async def runner(step):
        yield "card", None
        yield None, StepOutcome(ok=True, parked=True)

    orch = PipelineOrchestrator(runner, on_notice=lambda k, m: avisos.append((k, m)))
    [r async for r in orch.run(list(CASCATA_DA_SESSAO))]

    assert len(avisos) == 1
    kind, msg = avisos[0]
    assert kind == "parked_client"
    assert "buscar links oficiais dos relatórios anuais" in msg
    assert "baixar os PDFs encontrados para a pasta criada" in msg


@pytest.mark.asyncio
async def test_ultimo_step_estacionado_nao_reclama_de_descarte():
    avisos = []

    async def runner(step):
        yield "card", None
        yield None, StepOutcome(ok=True, parked=True)

    orch = PipelineOrchestrator(runner, on_notice=lambda k, m: avisos.append((k, m)))
    [r async for r in orch.run([_step("criar a pasta")])]

    assert avisos[0][0] == "parked_client"
    assert "último passo" in avisos[0][1]


@pytest.mark.asyncio
async def test_handoff_de_step_estacionado_nao_entra_na_fila():
    """Um step que estacionou não terminou — o que ele "pediu" ainda não vale."""
    rodaram = []

    async def runner(step):
        rodaram.append(step.input_text)
        yield "card", None
        yield None, StepOutcome(
            ok=True,
            parked=True,
            handoffs=[Handoff(
                target_intent=IntentType.HOME_ASSISTANT,
                input_text="ligar o ar",
                target_skill="home_assistant",
            )],
        )

    orch = PipelineOrchestrator(runner)
    [r async for r in orch.run([_step("faz alguma coisa")])]

    assert rodaram == ["faz alguma coisa"]


# ── e a cascata que NÃO estaciona continua andando ────────────────────────


@pytest.mark.asyncio
async def test_cascata_sem_parking_roda_inteira():
    """Regressão: o caso comum não pode encolher."""
    rodaram = []

    async def runner(step):
        rodaram.append(step.input_text)
        yield f"r: {step.input_text}", None
        yield None, StepOutcome(ok=True, final_text="ok")

    orch = PipelineOrchestrator(runner)
    out = [r async for r in orch.run(list(CASCATA_DA_SESSAO))]

    assert rodaram == [s.input_text for s in CASCATA_DA_SESSAO]
    assert len(out) == 3


@pytest.mark.asyncio
async def test_parked_default_e_false():
    """`StepOutcome` sem o campo se comporta como antes."""
    rodaram = []

    async def runner(step):
        rodaram.append(step.input_text)
        yield "r", None
        yield None, StepOutcome(ok=True, final_text="ok")

    orch = PipelineOrchestrator(runner)
    [r async for r in orch.run(list(CASCATA_DA_SESSAO))]

    assert len(rodaram) == 3
