"""Claude Code acoplado ao Neo — um orquestrador, dois corpos.

Ver docs/claude-code-delegation-plan.md. O orquestrador (``orquestrador.py``) não
sabe onde o Claude roda; o corpo (``corpo_local.py``; o do servidor vem na F2)
só lança, entrega bytes do stream e para.
"""

from core.claude_code.corpo import (
    Corpo,
    CorpoIndisponivel,
    Lancamento,
    Leitura,
    PedidoDeRodada,
    Saude,
)
from core.claude_code.corpo_local import CorpoLocal
from core.claude_code.orquestrador import (
    Desfecho,
    Ganchos,
    Limites,
    Orquestrador,
    SupervisorLLM,
    Tarefa,
    regra_de_permissao,
)
from core.claude_code.stream import Cota, Fim, LeitorDeStream, Recusa

__all__ = [
    "Corpo",
    "CorpoIndisponivel",
    "CorpoLocal",
    "Cota",
    "Desfecho",
    "Fim",
    "Ganchos",
    "Lancamento",
    "LeitorDeStream",
    "Leitura",
    "Limites",
    "Orquestrador",
    "PedidoDeRodada",
    "Recusa",
    "Saude",
    "SupervisorLLM",
    "Tarefa",
    "regra_de_permissao",
]
