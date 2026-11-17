"""Policy Engine (docs/service-gateway-dsl-plan.md §1.2, §2.2).

O componente load-bearing do gateway: esconder o token não basta, porque o LLM
age COM a autoridade do token. É a política que confina a capacidade.

Avaliação por precedência de efeito, não por ordem de lista: **deny > confirm >
allow**. Uma regra deny em qualquer posição vence qualquer allow. Sem nenhuma
regra casando, cai no default da conexão (``default_read`` para GET/HEAD,
``default_write`` para o resto).
"""

from __future__ import annotations

import fnmatch

from core.gateway.ir import (
    READ_METHODS,
    ConnectionConfig,
    PolicyDecision,
    PolicyEffect,
    PolicyRule,
    ServiceRequestIR,
)

# Precedência: quanto maior, mais forte. deny nunca é sobreposto por allow.
_PRECEDENCE = {PolicyEffect.ALLOW: 0, PolicyEffect.CONFIRM: 1, PolicyEffect.DENY: 2}


def _rule_matches(rule: PolicyRule, method: str, path: str) -> bool:
    methods = {m.upper() for m in rule.methods}
    if "*" not in methods and method.upper() not in methods:
        return False
    return fnmatch.fnmatch(path, rule.path)


def evaluate(ir: ServiceRequestIR, conn: ConnectionConfig) -> PolicyDecision:
    """Decide allow/confirm/deny para a request sobre o path JÁ normalizado.

    ``ir.path`` deve vir do validator (normalizado). Passar path cru aqui é bug:
    a política casaria numa forma diferente da executada.
    """
    method = ir.method.upper()

    # Junta todas as regras que casam e escolhe a de MAIOR precedência (deny first).
    winning: PolicyDecision | None = None
    for rule in conn.policies:
        if not _rule_matches(rule, method, ir.path):
            continue
        if winning is None or _PRECEDENCE[rule.effect] > _PRECEDENCE[winning.effect]:
            winning = PolicyDecision(
                effect=rule.effect,
                matched_rule=rule,
                reason=rule.description or f"{rule.effect.value} {','.join(rule.methods)} {rule.path}",
            )
            if rule.effect is PolicyEffect.DENY:
                # Nada supera deny — corta cedo.
                return winning

    if winning is not None:
        return winning

    # Sem regra explícita → default da conexão por natureza (leitura vs escrita).
    if method in READ_METHODS:
        return PolicyDecision(
            effect=conn.default_read,
            reason=f"default de leitura da conexão ({conn.default_read.value})",
        )
    return PolicyDecision(
        effect=conn.default_write,
        reason=f"default de escrita da conexão ({conn.default_write.value})",
    )
