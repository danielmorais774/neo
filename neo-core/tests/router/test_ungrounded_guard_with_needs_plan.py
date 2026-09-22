"""O guard de caminho/URL não fundamentado também roda com ``needs_plan=true``.

Sessão d9f9417b (2026-09-20): "coloca uma pasta com os relatórios anuais de cada
candidato na pasta teste-de-analise na area de trabalho". Três turnos seguidos, o
classificador devolveu `needs_plan=true` junto com um `curl` para um domínio que
ele inventou:

    curl -sL "https://relatorios.exemplo.com.br/$candidato.pdf" -o "$candidato.pdf"

O call site do guard era ``if intent == CLIENT_TOOL and not data.get("needs_plan")``
— a intuição sendo que a resposta do guard é forçar needs_plan, logo não haveria
o que forçar. Mas o guard faz DUAS coisas, e a segunda não é redundante: ele
DESCARTA o argumento chutado. Com ``needs_plan=true`` o comando inventado não é
ignorado — ele é herdado pelo primeiro step da cascata (``_seed_steps``) e
despachado literalmente para o disco do usuário.

O regex de fundamentação (``_PATH_WITH_EXT_RE``) já casava URL desde sempre
(``//relatorios.exemplo.com.br/$candidato.pdf``). Só nunca teve a chance de rodar.
"""

import json

from core.router.intent_router import IntentRouter
from core.tools.registry import ToolRegistry
from modules.desktop.tools import DESKTOP_TOOLS
from shared import IntentType

# Os comandos reais dos três turnos da sessão.
CURL_INVENTADO = (
    'mkdir -p /Users/daniel/Desktop/teste-de-analise/relatorios_anuais && '
    'cd /Users/daniel/Desktop/teste-de-analise/relatorios_anuais && '
    'for candidato in relatorio_charlie relatorio_foxtrot; do '
    'curl -sL "https://relatorios.exemplo.com.br/$candidato.pdf" -o "$candidato.pdf"; done'
)
CURL_GITHUB_INVENTADO = (
    'curl -sL "https://raw.githubusercontent.com/neo-core/government-plans/main/relatorio_charlie.pdf" '
    '-o "relatorio_charlie.pdf"'
)
PEDIDO = (
    "coloca uma pasta com os relatórios anuais de cada empresa na pasta "
    "teste-de-analise na area de trabalho, queor o pdf de cada um deles lá"
)


def _router() -> IntentRouter:
    router = object.__new__(IntentRouter)
    registry = ToolRegistry()
    registry.register_many(DESKTOP_TOOLS)
    router.tool_registry = registry
    return router


def _raw(command: str, *, needs_plan: bool, pipeline_plan=None) -> str:
    return json.dumps(
        {
            "intent": "CLIENT_TOOL",
            "confidence": 0.95,
            "tool_name": "run_command",
            "tool_arguments": {},
            "raw_command": command,
            "explanation": "teste",
            "requires_approval": False,
            "needs_memory": False,
            "knowledge_keywords": [],
            "needs_plan": needs_plan,
            "tipo_tarefa": "multi_etapas" if needs_plan else "simples",
            "skill_name": None,
            "pipeline_plan": pipeline_plan,
        }
    )


# ── o bug da sessão d9f9417b ──────────────────────────────────────────────


def test_url_inventada_e_descartada_mesmo_com_needs_plan_true() -> None:
    """Duas guardas pegam este comando, e a de download vem primeiro.

    A de download (``_NET_FETCH_COMMAND``, tests/router/test_net_fetch_wrong_rail.py)
    é uma resposta mais forte: ela devolve o turno ao cognitivo, que sabe
    PROCURAR o endereço real. A de caminho, que roda depois, só descartaria o
    argumento e pediria um plano de verificação. O que as duas garantem em comum
    — e é o ponto deste arquivo — é que o comando chutado NÃO sobrevive.
    """
    router = _router()
    decision = router._parse_classification(
        _raw(CURL_INVENTADO, needs_plan=True),
        user_message=PEDIDO,
        conversation_history=[],
    )
    assert decision.raw_command is None, "o curl inventado não pode sobreviver"
    assert decision.tool_arguments is None
    assert decision.intent is IntentType.DIRECT_RESPONSE


def test_caminho_inventado_sem_rede_fica_no_client_tool_com_plano() -> None:
    """O caso que isola a guarda DE CAMINHO, sem a de download por cima.

    É esta que regride se alguém reintroduzir o `and not needs_plan`: comando
    sem `curl`/`wget`, com um basename que ninguém citou, e `needs_plan` já
    vindo true do classificador.
    """
    router = _router()
    decision = router._parse_classification(
        _raw("cp /Users/daniel/Documents/relatorio_charlie.pdf "
             "/Users/daniel/Desktop/teste-de-analise/", needs_plan=True),
        user_message=PEDIDO,
        conversation_history=[],
    )
    assert decision.raw_command is None, "o caminho chutado não pode sobreviver"
    assert decision.tool_arguments is None
    assert decision.needs_plan is True
    assert decision.intent is IntentType.CLIENT_TOOL


def test_url_inventada_nao_atravessa_a_cascata() -> None:
    """O payload descartado não pode voltar pelo step 1 do ``pipeline_plan``.

    ``_seed_steps`` herda ``raw_command`` do topo da decisão para o primeiro step
    cujo intent bate. Se o guard não zerou o topo, o comando inventado é
    exatamente o que chega no Mac — foi o passo 1 da interação 8.
    """
    router = _router()
    decision = router._parse_classification(
        _raw(
            CURL_INVENTADO,
            needs_plan=True,
            pipeline_plan=[
                {"intent": "CLIENT_TOOL", "input": "criar pasta relatorios_anuais", "skill_name": None},
                {"intent": "CLIENT_TOOL", "input": "baixar os pdfs", "skill_name": None},
            ],
        ),
        user_message=PEDIDO,
        conversation_history=[],
    )
    assert decision.raw_command is None
    # A cascata declarada sobrevive à guarda (ela descarta o PAYLOAD, não o
    # plano) — e sem comando herdado, o step 1 vira plano em vez de despachar
    # o curl inventado. Ver `_seed_steps`.
    assert decision.pipeline_plan is not None
    assert len(decision.pipeline_plan) == 2


def test_dominio_inventado_no_github_tambem_cai() -> None:
    router = _router()
    decision = router._parse_classification(
        _raw(CURL_GITHUB_INVENTADO, needs_plan=True),
        user_message="tenta de novo",
        conversation_history=[{"role": "user", "content": PEDIDO}],
    )
    assert decision.raw_command is None


# ── e o que é legítimo continua passando ──────────────────────────────────


def test_url_que_o_usuario_deu_passa() -> None:
    """Fundamentação é por basename — URL citada pelo usuário não é chute."""
    router = _router()
    decision = router._parse_classification(
        _raw(
            'curl -sL "https://exemplo.com/relatorio_q3.pdf" -o ~/Desktop/relatorio_q3.pdf',
            needs_plan=True,
        ),
        user_message="baixa https://exemplo.com/relatorio_q3.pdf pro meu desktop",
        conversation_history=[],
    )
    assert decision.raw_command is not None


def test_comando_exploratorio_sem_extensao_passa() -> None:
    """``ls``/``find`` não citam extensão — não há o que fundamentar."""
    router = _router()
    decision = router._parse_classification(
        _raw("ls -la /Users/daniel/Desktop/teste-de-analise", needs_plan=True),
        user_message="o que tem na pasta teste-de-analise",
        conversation_history=[],
    )
    assert decision.raw_command == "ls -la /Users/daniel/Desktop/teste-de-analise"


def test_needs_plan_false_continua_igual() -> None:
    """Regressão: o caminho que já funcionava não pode mudar de comportamento."""
    router = _router()
    inventado = router._parse_classification(
        _raw("cat /Users/daniel/Desktop/relatorio.csv", needs_plan=False),
        user_message="me mostra meu relatorio de financas em csv",
        conversation_history=[],
    )
    assert inventado.raw_command is None
    assert inventado.needs_plan is True

    fundamentado = router._parse_classification(
        _raw("cat /Users/daniel/Desktop/relatorio.csv", needs_plan=False),
        user_message="abre o relatorio.csv que está no desktop",
        conversation_history=[],
    )
    assert fundamentado.raw_command == "cat /Users/daniel/Desktop/relatorio.csv"
    assert fundamentado.needs_plan is False
