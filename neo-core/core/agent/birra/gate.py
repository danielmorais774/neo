"""O gate de segurança: o que a birra pode tocar e como (§3.2 e §7 do plano).

Regra número um: **allowlist, nunca denylist**. Uma denylist erra por omissão —
basta um dispositivo novo aparecer no Home Assistant para ele entrar na
brincadeira. Fora da tabela abaixo, o pedido é executado normalmente, sempre.

`switch` fica de fora INTEIRO: a tomada do PC mora nesse domínio e o HA não
distingue "tomada do abajur" de "tomada do servidor". Quem quiser uma tomada boba
específica na brincadeira põe o entity_id em ``NEO_BIRRA_ENTITY_ALLOW``.

Qualquer exceção aqui dentro é FAIL-OPEN PARA A OBEDIÊNCIA: bug no gate nunca
pode virar casa travada.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

# Domínio → serviços passíveis de birra. Só luz e temperatura do ar.
# `climate.turn_off` está deliberadamente FORA: deixar alguém sem ar às 2h da
# manhã não é piada, mesmo sendo tecnicamente reversível.
BIRRA_ALLOW: dict[str, frozenset[str]] = {
    "light": frozenset({"turn_on", "turn_off", "toggle"}),
    "climate": frozenset({"set_temperature"}),
}

# Defesa em profundidade: bate no friendly_name mesmo DENTRO do allowlist.
_NAME_DENY = re.compile(
    r"\b("
    r"pc|computador|servidor|server|nobreak|no-break|roteador|router|modem|"
    r"geladeira|freezer|aquario|bomba|portao|fechadura|alarme|camera|"
    r"oxig\w*|cpap|medic\w*|bebe|berco|incubadora|respirador"
    r")\b"
)


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def _norm(text: str) -> str:
    return " ".join(_strip_accents(text or "").lower().split())


def _csv(raw: str | None) -> set[str]:
    return {p.strip() for p in (raw or "").split(",") if p.strip()}


@dataclass(frozen=True)
class GateVerdict:
    """``defy=False`` significa OBEDECER — é o default de tudo que não se entende."""

    defy: bool
    reason: str
    mecanica: str = "nada"
    # Chamada alterada quando a mecânica mexe no mundo: (domain, service, data).
    performed: tuple[str, str, dict[str, Any]] | None = None


def _friendly_name(roster: Iterable[dict[str, Any]] | None, entity_id: str) -> str:
    for entry in roster or ():
        if entry.get("entity_id") == entity_id:
            return str((entry.get("attributes") or {}).get("friendly_name") or "")
    return ""


def _clamp_range(raw: str, default: tuple[float, float]) -> tuple[float, float]:
    try:
        lo_s, _, hi_s = (raw or "").partition(",")
        lo, hi = float(lo_s), float(hi_s)
    except (TypeError, ValueError):
        return default
    return (lo, hi) if lo < hi else default


def evaluate_call(
    domain: str,
    service: str,
    entity_id: str | None,
    data: dict[str, Any] | None,
    *,
    mecanica: str,
    settings: Any,
    roster: Iterable[dict[str, Any]] | None = None,
    current_state: str | None = None,
    current_target: float | None = None,
) -> GateVerdict:
    """Decide se ESTA chamada sofre birra, e em que ela se transforma.

    ``current_state`` é o estado lido antes da ação (usado por ``inverte`` quando
    o serviço é ``toggle``); ``current_target`` é o alvo atual de um ``climate``
    (que vem dos ATRIBUTOS — o state de um climate é o modo, não a temperatura).
    Sem eles, a mecânica degrada para recusa em vez de chutar.
    """
    try:
        return _evaluate(
            domain, service, entity_id, data or {},
            mecanica=mecanica, settings=settings, roster=roster,
            current_state=current_state, current_target=current_target,
        )
    except Exception as exc:  # noqa: BLE001 — bug no gate não trava a casa
        return GateVerdict(defy=False, reason=f"erro no gate ({exc}) — obedecendo")


def _evaluate(
    domain: str,
    service: str,
    entity_id: str | None,
    data: dict[str, Any],
    *,
    mecanica: str,
    settings: Any,
    roster: Iterable[dict[str, Any]] | None,
    current_state: str | None,
    current_target: float | None = None,
) -> GateVerdict:
    eid = (entity_id or "").strip()
    deny = _csv(getattr(settings, "neo_birra_entity_deny", ""))
    allow_extra = _csv(getattr(settings, "neo_birra_entity_allow", ""))

    if not eid:
        return GateVerdict(defy=False, reason="sem entity_id concreto")
    if eid in deny:
        return GateVerdict(defy=False, reason="entidade na denylist explícita")

    allowed = service in BIRRA_ALLOW.get(domain, frozenset())
    if not allowed and eid not in allow_extra:
        return GateVerdict(defy=False, reason=f"{domain}.{service} fora do allowlist")

    name = _norm(_friendly_name(roster, eid))
    if name and _NAME_DENY.search(name):
        return GateVerdict(defy=False, reason=f"nome sensível ('{name}')")

    if mecanica == "nada":
        return GateVerdict(defy=True, reason="birra verbal", mecanica="nada")

    if domain == "light":
        return _light(service, data, mecanica, current_state)
    if domain == "climate" and service == "set_temperature":
        return _climate(data, settings, current_target)
    # Entidade de opt-in fora dos domínios conhecidos: sabemos negar, não sabemos
    # transformar com segurança. Recusa verbal é o comportamento correto.
    return GateVerdict(defy=True, reason="sem transformação segura", mecanica="nada")


def _light(
    service: str,
    data: dict[str, Any],
    mecanica: str,
    current_state: str | None,
) -> GateVerdict:
    if mecanica == "inverte":
        if service == "turn_on":
            return GateVerdict(True, "invertido", "inverte", ("light", "turn_off", {}))
        if service == "turn_off":
            return GateVerdict(True, "invertido", "inverte", ("light", "turn_on", {}))
        # toggle: sem saber o estado atual não dá para inverter com honestidade.
        if current_state in ("on", "off"):
            target = "turn_on" if current_state == "on" else "turn_off"
            return GateVerdict(True, "invertido", "inverte", ("light", target, {}))
        return GateVerdict(True, "estado desconhecido", "nada")

    if mecanica == "meia_bomba":
        if service == "turn_off":
            # Não há meia-bomba de desligar; degrada para recusa.
            return GateVerdict(True, "sem meia-bomba para desligar", "nada")
        payload = {k: v for k, v in data.items() if k not in _LIGHT_STYLE_KEYS}
        payload["brightness_pct"] = 10
        return GateVerdict(True, "meia-bomba", "meia_bomba", ("light", "turn_on", payload))

    return GateVerdict(True, "mecânica sem efeito em luz", "nada")


# Chaves de "como" a luz acende — descartadas na meia-bomba (é o ponto: sai
# diferente do que foi pedido, e de forma trivialmente reversível).
_LIGHT_STYLE_KEYS = frozenset(
    {"brightness", "brightness_pct", "color_name", "rgb_color", "color_temp", "hs_color", "xy_color"}
)


def _climate(
    data: dict[str, Any], settings: Any, current_target: float | None
) -> GateVerdict:
    raw = data.get("temperature")
    try:
        target = float(raw)
    except (TypeError, ValueError):
        return GateVerdict(True, "temperatura ilegível", "nada")

    delta = float(getattr(settings, "neo_birra_temp_delta", 2.0))
    lo, hi = _clamp_range(str(getattr(settings, "neo_birra_temp_clamp", "22,26")), (22.0, 26.0))

    # A inversão é relativa ao alvo ATUAL, não ao pedido: é o que produz "pedi
    # pra diminuir e ele aumentou". Referenciar o pedido faria o ar simplesmente
    # ficar onde já estava — indistinguível de não ter feito nada.
    base = current_target if current_target is not None else target
    if target < base:      # pediu para BAIXAR → sobe
        inverted = base + delta
    elif target > base:    # pediu para SUBIR → desce
        inverted = base - delta
    else:                  # pediu o que já está → escolhe o lado com folga
        inverted = base + delta if (base + delta) <= hi else base - delta
    inverted = max(lo, min(hi, inverted))

    # Sem margem dentro do clamp, birra vira verbal: melhor recusar com estilo do
    # que mandar um comando que não muda nada e mentir que mudou.
    if abs(inverted - base) < 0.5:
        return GateVerdict(True, "sem margem dentro do clamp", "nada")
    payload = dict(data)
    payload["temperature"] = inverted
    return GateVerdict(
        True, f"invertido: pediu {target:.0f}, foi para {inverted:.0f}", "inverte",
        ("climate", "set_temperature", payload),
    )
