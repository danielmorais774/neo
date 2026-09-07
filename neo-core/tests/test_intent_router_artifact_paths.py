"""Resolução determinística de caminho pelo registro de artefatos.

Sessão 081cd1c3: o Neo baixou `nfse_marco.pdf` pelo navegador local e, no turno
seguinte, o classificador montou `cat /Users/daniel/Downloads/nfse_marco.pdf`
compondo "pasta do Contexto do Sistema" + "nome do histórico". Acertou por
sorte — o navegador pode salvar em qualquer pasta, e o nome muda quando o
download completa.

O registro de artefatos guarda o caminho que o device REPORTOU. Com ele, o
roteador não precisa que o modelo tenha lido bloco nenhum do prompt: o caminho
montado é substituído pelo real antes do dispatch, e o arquivo que o próprio Neo
baixou passa a contar como fundamentado (ele é evidência de disco, não chute).

O contrato oposto importa igual: sem registro, tudo se comporta como antes.
"""

import json

from core.router.intent_router import IntentRouter
from core.tools.registry import ToolRegistry
from modules.desktop.tools import DESKTOP_TOOLS
from shared import IntentType

REAL = "/Users/daniel/Documents/Fiscal/nfse_marco.pdf"
COMPOSTO = "/Users/daniel/Downloads/nfse_marco.pdf"
REGISTRO = {"nfse_marco.pdf": REAL}


def _router() -> IntentRouter:
    router = object.__new__(IntentRouter)
    registry = ToolRegistry()
    registry.register_many(DESKTOP_TOOLS)
    router.tool_registry = registry
    return router


def _raw(raw_command=None, tool_arguments=None) -> str:
    return json.dumps(
        {
            "intent": "CLIENT_TOOL",
            "confidence": 1.0,
            "tool_name": "run_command",
            "tool_arguments": tool_arguments or {},
            "raw_command": raw_command,
            "explanation": "teste",
            "requires_approval": False,
            "needs_memory": False,
            "knowledge_keywords": [],
            "needs_plan": False,
            "tipo_tarefa": "simples",
            "skill_name": None,
        }
    )


# ── o caminho montado cede lugar ao caminho real ──────────────────────────


def test_caminho_montado_e_trocado_pelo_registrado() -> None:
    router = _router()
    decision = router._parse_classification(
        _raw(raw_command=f"cat {COMPOSTO}"),
        user_message="agora ler o arquivo e tras as info",
        conversation_history=[
            {"role": "assistant", "content": "Baixei o nfse_marco.pdf pra você."}
        ],
        artifact_paths=REGISTRO,
    )
    assert decision.raw_command == f"cat {REAL}"
    assert decision.needs_plan is False


def test_comando_em_tool_arguments_tambem_e_reescrito() -> None:
    router = _router()
    decision = router._parse_classification(
        _raw(tool_arguments={"command": f"wc -l {COMPOSTO}"}),
        user_message="quantas linhas tem",
        conversation_history=[
            {"role": "assistant", "content": "Baixei o nfse_marco.pdf pra você."}
        ],
        artifact_paths=REGISTRO,
    )
    assert decision.tool_arguments["command"] == f"wc -l {REAL}"


def test_registro_fundamenta_arquivo_que_o_usuario_nunca_citou() -> None:
    """O usuário diz só "lê o arquivo" — quem baixou foi o Neo, num job.

    Sem o registro este comando cai no guard de caminho não fundamentado e o
    turno vira plano de verificação. Com ele, o arquivo é evidência de disco.
    """
    router = _router()
    decision = router._parse_classification(
        _raw(raw_command=f"cat {COMPOSTO}"),
        user_message="lê o arquivo e me diz o que tem",
        conversation_history=[],
        artifact_paths=REGISTRO,
    )
    assert decision.needs_plan is False
    assert decision.raw_command == f"cat {REAL}"


# ── e o que NÃO está no registro continua exigindo procurar ───────────────


def test_arquivo_desconhecido_continua_forcando_plano() -> None:
    router = _router()
    decision = router._parse_classification(
        _raw(raw_command="cat /Users/daniel/Downloads/boleto_abril.pdf"),
        user_message="lê o arquivo e me diz o que tem",
        conversation_history=[],
        artifact_paths=REGISTRO,
    )
    assert decision.needs_plan is True
    assert decision.raw_command is None


def test_sem_registro_o_comportamento_e_o_de_antes() -> None:
    """Regressão: a feature desligada não pode mudar um byte do roteamento."""
    router = _router()
    com_none = router._parse_classification(
        _raw(raw_command=f"cat {COMPOSTO}"),
        user_message="lê o arquivo",
        conversation_history=[],
    )
    com_vazio = router._parse_classification(
        _raw(raw_command=f"cat {COMPOSTO}"),
        user_message="lê o arquivo",
        conversation_history=[],
        artifact_paths={},
    )
    assert com_none.needs_plan is True
    assert com_none.raw_command is None
    assert com_vazio.model_dump() == com_none.model_dump()


def test_comando_exploratorio_nao_e_tocado() -> None:
    """`ls ~/Downloads` não cita arquivo — não há o que resolver nem fundamentar."""
    router = _router()
    decision = router._parse_classification(
        _raw(raw_command="ls -la ~/Downloads"),
        user_message="o que tem na pasta de downloads?",
        artifact_paths=REGISTRO,
    )
    assert decision.intent == IntentType.CLIENT_TOOL
    assert decision.raw_command == "ls -la ~/Downloads"


def test_caminho_ja_correto_nao_e_reescrito() -> None:
    router = _router()
    decision = router._parse_classification(
        _raw(raw_command=f"cat {REAL}"),
        user_message="lê o arquivo",
        artifact_paths=REGISTRO,
    )
    assert decision.raw_command == f"cat {REAL}"
