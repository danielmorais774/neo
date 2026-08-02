"""Modo Birra — política de desobediência limitada (docs/modo-birra-plan.md).

Com a ira no teto E maltrato acumulado, um pedido BOBO de casa pode ser recusado
ou feito diferente até o usuário pedir desculpa. Três invariantes que o código
garante (o LLM só põe a graça):

  * **allowlist, nunca denylist** — fora de ``light``/``climate.set_temperature``
    o pedido é executado normalmente, sempre;
  * **toda alteração física se desfaz** — ledger em ``birra_act`` + sweeper;
  * **birra é declarada** — leitura de estado nunca sofre birra e o resultado da
    tool carrega ``defied:true``, para o modelo nunca confirmar o que não fez.
"""

from core.agent.birra.gate import (
    BIRRA_ALLOW,
    GateVerdict,
    evaluate_call,
)
from core.agent.birra.policy import (
    BirraDecision,
    detect_apology,
    detect_emphatic_order,
    evaluate as evaluate_birra,
)
from core.agent.birra.tactics import choose_tactic

__all__ = [
    "BIRRA_ALLOW",
    "BirraDecision",
    "GateVerdict",
    "choose_tactic",
    "detect_apology",
    "detect_emphatic_order",
    "evaluate_birra",
    "evaluate_call",
]
