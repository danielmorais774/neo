"""O agente do SERVER_TOOL não pode encerrar o turno prometendo.

Sessão 8c449cb8 (2026-09-21). O usuário: "tenta baixar os pdfs do relatório
anual de novo". O classificador acertou tudo — escolheu `browser_local_task`,
montou `task`, `start_url`, `max_steps` e `visible: false`. E então:

    Iterações: 1
    "Deixa eu ver o estado atual dos jobs e tentar dar a volta por cima.
     (...) Deixa eu olhar o que tem por aqui."

O turno acabou aí. Nenhum job nasceu, nenhuma tool rodou, e o usuário só
descobriu 9 minutos depois — perguntando "iai rodou e baixou?".

Duas causas, as duas cobertas aqui:

1. `agent.execute` recebia só o texto da tarefa. Nem `decision.tool_name` nem
   `decision.tool_arguments` atravessavam, então o agente redecidia do zero e o
   trabalho do classificador ia para o lixo.
2. Nada forçava a chamada. `force_tool_first` (tool_choice="required") existia,
   mas só no trilho cognitivo — aqui "responder em texto" seguia disponível.

E o log mentia sobre o resultado: registrava a escolha do classificador como
"Tool Request" e a prosa do agente como "Tool Result: Sucesso, 9345ms".
"""

from __future__ import annotations

import inspect

import pytest

from core.agent.agent_executor import AgentExecutor
from modules.desktop.ai_service import AIService


# ── a capacidade de forçar existe e chega no LLM ──────────────────────────


def test_execute_aceita_force_tool_first():
    assert "force_tool_first" in inspect.signature(AgentExecutor.execute).parameters


def test_inner_loop_recebe_o_forcamento():
    assert "force_tool_first" in inspect.signature(AgentExecutor._inner_loop).parameters


def test_forcamento_vira_tool_choice_required_e_desliga_o_raciocinio():
    """`tool_choice` forçado + thinking ligado = 400 no deepseek-v4, e o
    raciocínio do OpenRouter come o orçamento antes de sobrar conteúdo. Os dois
    andam juntos, sempre."""
    src = inspect.getsource(AgentExecutor._inner_loop)
    assert 'extra["tool_choice"] = "required"' in src
    assert 'extra["disable_thinking"] = True' in src
    # "Só no PRIMEIRO round" já foi conferido aqui procurando a string
    # "iteration == 0" — e o teste passava com o forçamento MORTO (o laço
    # incrementa antes). O comportamento é testado de verdade em
    # tests/test_agent_stall_and_rounds.py.


# ── o trilho SERVER_TOOL usa as duas defesas ──────────────────────────────


def test_server_tool_forca_a_primeira_chamada():
    src = inspect.getsource(AIService._handle_server_tool)
    assert "force_tool_first=True" in src, (
        "sem isto o agente pode responder prosa e encerrar sem agir"
    )


def test_server_tool_repassa_a_escolha_do_classificador():
    src = inspect.getsource(AIService._handle_server_tool)
    assert "decision.tool_arguments" in src, "os argumentos montados não podem ser descartados"
    assert "[roteamento]" in src, "o agente precisa SABER o que já foi decidido"


def test_a_dica_permite_discordar_mas_nao_permite_parar():
    """A escolha do classificador é um bom ponto de partida, não uma ordem: ele
    vê 6 mensagens de histórico e pode errar. O que não pode é o agente usar
    isso de desculpa para não agir."""
    src = inspect.getsource(AIService._handle_server_tool)
    assert "chame OUTRA ferramenta" in src
    assert "sem agir" in src


# ── o log para de dizer "Sucesso" quando nada rodou ───────────────────────


def test_log_conta_as_chamadas_reais_de_tool():
    src = inspect.getsource(AIService._handle_server_tool)
    assert "chamadas" in src
    # Sucesso exige tool chamada: era assim que uma sessão sem NENHUMA execução
    # se lia como "Tool Result: Sucesso".
    assert "success=ok and chamadas > 0" in src


def test_log_avisa_quando_o_agente_nao_chamou_nada():
    src = inspect.getsource(AIService._handle_server_tool)
    assert "não chamou ferramenta nenhuma" in src


def test_contagem_aceita_pydantic_e_dict():
    """`steps` são AgentStep (pydantic); ler como dict daria zero em silêncio —
    e zero silencioso é exatamente o que escondeu o defeito original."""
    src = inspect.getsource(AIService._handle_server_tool)
    assert "isinstance(passo, dict)" in src
    assert 'getattr(passo, "tool_calls", None)' in src


# ── entrega de vários arquivos continua até acabar ────────────────────────


def test_entrega_bem_sucedida_reentra_no_cognitivo():
    """Um `save_generated_file` entrega UM arquivo. Fechar o turno aí fazia
    "põe os 6 PDFs na pasta" virar um por turno, com o usuário cobrando a cada
    um (8c449cb8: "e os outros? era pra ser os 6", "tem mais ainda" — e ainda
    assim só 3 dos 6 chegaram)."""
    src = inspect.getsource(AIService._confirm_file_delivery)
    assert "nota_continuar" in src
    assert "_reenter_cognitive" in src.split("status == \"saved\"")[-1], (
        "o ramo de sucesso precisa reentrar, não fechar com texto fixo"
    )


def test_a_nota_de_continuacao_proibe_esperar_o_usuario_cobrar():
    src = inspect.getsource(AIService._confirm_file_delivery)
    assert "não peça confirmação" in src
    assert "não espere" in src
