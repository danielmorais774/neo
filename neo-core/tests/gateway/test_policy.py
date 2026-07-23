"""Policy Engine — precedência deny>confirm>allow e defaults por natureza.

Ver docs/service-gateway-dsl-plan.md §1.2, §2.2.
"""

from core.gateway.ir import (
    AuthType,
    ConnectionConfig,
    PolicyEffect,
    PolicyRule,
    ServiceRequestIR,
)
from core.gateway.policy import evaluate
from core.gateway.registry import _ha_seed_policies


def _conn(policies=None, **kw) -> ConnectionConfig:
    base = dict(
        id="svc", name="Svc", base_url="https://svc.local", auth_type=AuthType.NONE,
        policies=policies or [],
    )
    base.update(kw)
    return ConnectionConfig(**base)


def _ir(method="GET", path="/api/x") -> ServiceRequestIR:
    return ServiceRequestIR(service="svc", method=method, path=path)


def test_default_read_allow_write_confirm():
    conn = _conn()
    assert evaluate(_ir("GET", "/x"), conn).effect is PolicyEffect.ALLOW
    assert evaluate(_ir("POST", "/x"), conn).effect is PolicyEffect.CONFIRM


def test_deny_beats_allow_regardless_of_order():
    # allow amplo vem ANTES do deny específico; deny ainda vence.
    conn = _conn(policies=[
        PolicyRule(effect=PolicyEffect.ALLOW, methods=["*"], path="/api/*"),
        PolicyRule(effect=PolicyEffect.DENY, methods=["POST"], path="/api/danger/*"),
    ])
    assert evaluate(_ir("POST", "/api/danger/x"), conn).effect is PolicyEffect.DENY
    assert evaluate(_ir("GET", "/api/safe"), conn).effect is PolicyEffect.ALLOW


def test_confirm_beats_allow():
    conn = _conn(policies=[
        PolicyRule(effect=PolicyEffect.ALLOW, methods=["*"], path="/api/*"),
        PolicyRule(effect=PolicyEffect.CONFIRM, methods=["POST"], path="/api/x"),
    ])
    assert evaluate(_ir("POST", "/api/x"), conn).effect is PolicyEffect.CONFIRM


def test_method_glob_and_specific():
    conn = _conn(policies=[
        PolicyRule(effect=PolicyEffect.ALLOW, methods=["POST"], path="/api/services/light/*"),
    ])
    assert evaluate(_ir("POST", "/api/services/light/turn_on"), conn).effect is PolicyEffect.ALLOW
    # POST fora do allow explícito → default de escrita (confirm)
    assert evaluate(_ir("POST", "/api/services/lock/unlock"), conn).effect is PolicyEffect.CONFIRM


def test_ha_seed_blocks_shell_command_allows_light():
    conn = _conn(policies=_ha_seed_policies())
    assert evaluate(_ir("POST", "/api/services/shell_command/rm"), conn).effect is PolicyEffect.DENY
    assert evaluate(_ir("POST", "/api/services/hassio/host_reboot"), conn).effect is PolicyEffect.DENY
    assert evaluate(_ir("POST", "/api/services/homeassistant/restart"), conn).effect is PolicyEffect.DENY
    assert evaluate(_ir("POST", "/api/services/light/turn_on"), conn).effect is PolicyEffect.ALLOW
    assert evaluate(_ir("GET", "/api/states"), conn).effect is PolicyEffect.ALLOW
    # domínio não listado → confirm (allowlist + confirm no resto)
    assert evaluate(_ir("POST", "/api/services/persistent_notification/create"), conn).effect is PolicyEffect.CONFIRM
