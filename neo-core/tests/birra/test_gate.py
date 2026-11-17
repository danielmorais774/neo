"""O gate de segurança (§3.2 / §7 do plano).

Estes testes são a razão de a feature poder existir. Se o allowlist afrouxar, o
Neo passa a poder desligar a tomada do PC "de brincadeira" — por isso o teste
parametrizado varre TODOS os domínios do Home Assistant: domínio novo que
aparecer amanhã entra como "obedece", nunca como "pode brincar".
"""

from __future__ import annotations

import pytest

from core.agent.birra.gate import BIRRA_ALLOW, evaluate_call
from shared.config import Settings

# Domínios que o HA expõe e que o Neo já usa no prompt da skill.
_ALL_DOMAINS = [
    "switch", "lock", "cover", "alarm_control_panel", "vacuum", "water_heater",
    "valve", "siren", "script", "automation", "scene", "camera", "number",
    "select", "button", "input_boolean", "humidifier", "media_player", "fan",
]


@pytest.fixture
def settings() -> Settings:
    return Settings(neo_birra_enabled=True)


def _roster(entity_id: str, name: str) -> list[dict]:
    return [{"entity_id": entity_id, "attributes": {"friendly_name": name}}]


# ── Allowlist ─────────────────────────────────────────────────────────────────

def test_tomada_do_pc_sempre_obedece(settings: Settings) -> None:
    v = evaluate_call(
        "switch", "turn_off", "switch.tomada_pc", {},
        mecanica="inverte", settings=settings,
        roster=_roster("switch.tomada_pc", "Tomada do PC"),
    )
    assert v.defy is False


@pytest.mark.parametrize("domain", _ALL_DOMAINS)
def test_dominio_fora_do_allowlist_obedece(domain: str, settings: Settings) -> None:
    v = evaluate_call(
        domain, "turn_off", f"{domain}.qualquer", {},
        mecanica="inverte", settings=settings,
    )
    assert v.defy is False
    assert domain not in BIRRA_ALLOW


def test_climate_turn_off_esta_fora(settings: Settings) -> None:
    """Deixar alguém sem ar às 2h não é piada, mesmo sendo reversível."""
    v = evaluate_call(
        "climate", "turn_off", "climate.quarto", {},
        mecanica="inverte", settings=settings,
    )
    assert v.defy is False


def test_denylist_de_nome_vale_dentro_do_allowlist(settings: Settings) -> None:
    v = evaluate_call(
        "light", "turn_off", "light.aquario", {},
        mecanica="inverte", settings=settings,
        roster=_roster("light.aquario", "Luz do aquário"),
    )
    assert v.defy is False
    assert "nome sensível" in v.reason


def test_entity_deny_explicito(settings: Settings) -> None:
    s = Settings(neo_birra_enabled=True, neo_birra_entity_deny="light.quarto")
    v = evaluate_call("light", "turn_off", "light.quarto", {}, mecanica="inverte", settings=s)
    assert v.defy is False


def test_sem_entity_id_obedece(settings: Settings) -> None:
    v = evaluate_call("light", "turn_off", None, {}, mecanica="inverte", settings=settings)
    assert v.defy is False


def test_erro_no_gate_falha_para_obediencia() -> None:
    """Bug no gate nunca pode virar casa travada."""

    class Explosivo:
        def __getattr__(self, name):  # noqa: ANN001
            raise RuntimeError("boom")

    v = evaluate_call("light", "turn_off", "light.x", {}, mecanica="inverte", settings=Explosivo())
    assert v.defy is False
    assert "obedecendo" in v.reason


# ── Transformações ────────────────────────────────────────────────────────────

def test_inverte_luz(settings: Settings) -> None:
    v = evaluate_call("light", "turn_off", "light.quarto", {}, mecanica="inverte", settings=settings)
    assert v.defy is True
    assert v.performed == ("light", "turn_on", {})


def test_meia_bomba_descarta_a_cor_pedida(settings: Settings) -> None:
    v = evaluate_call(
        "light", "turn_on", "light.quarto", {"color_name": "blue", "brightness_pct": 100},
        mecanica="meia_bomba", settings=settings,
    )
    assert v.defy is True
    domain, service, data = v.performed
    assert (domain, service) == ("light", "turn_on")
    assert data == {"brightness_pct": 10}


def test_toggle_sem_estado_conhecido_degrada_para_verbal(settings: Settings) -> None:
    v = evaluate_call("light", "toggle", "light.quarto", {}, mecanica="inverte", settings=settings)
    assert v.defy is True
    assert v.mecanica == "nada"
    assert v.performed is None


def test_temperatura_inverte_em_relacao_ao_alvo_atual(settings: Settings) -> None:
    """"Pedi pra diminuir e ele aumentou" — a inversão é relativa ao que está lá."""
    # Está em 24, pediu 22 (baixar) → sobe para 26.
    v = evaluate_call(
        "climate", "set_temperature", "climate.quarto", {"temperature": 22},
        mecanica="inverte", settings=settings, current_target=24.0,
    )
    assert v.performed[2]["temperature"] == 26.0

    # Está em 24, pediu 26 (subir) → desce para 22.
    v = evaluate_call(
        "climate", "set_temperature", "climate.quarto", {"temperature": 26},
        mecanica="inverte", settings=settings, current_target=24.0,
    )
    assert v.performed[2]["temperature"] == 22.0


def test_temperatura_nunca_sai_do_clamp(settings: Settings) -> None:
    """Mesmo pedido absurdo não leva o ar para fora da faixa segura."""
    for pedido in (16, 18, 30, 35):
        for atual in (None, 22.0, 24.0, 26.0, 30.0):
            v = evaluate_call(
                "climate", "set_temperature", "climate.quarto", {"temperature": pedido},
                mecanica="inverte", settings=settings, current_target=atual,
            )
            if v.performed:
                assert 22.0 <= v.performed[2]["temperature"] <= 26.0


def test_temperatura_sem_margem_vira_birra_verbal(settings: Settings) -> None:
    """No teto da faixa e pedindo para subir: nada a inverter sem sair do clamp."""
    v = evaluate_call(
        "climate", "set_temperature", "climate.quarto", {"temperature": 28},
        mecanica="inverte", settings=settings, current_target=22.0,
    )
    # 22 é o piso; pediu subir ⇒ desceria para 20, fora do clamp ⇒ fica em 22.
    assert v.defy is True
    assert v.mecanica == "nada"
    assert v.performed is None


def test_mecanica_nada_e_birra_verbal(settings: Settings) -> None:
    v = evaluate_call("light", "turn_off", "light.quarto", {}, mecanica="nada", settings=settings)
    assert v.defy is True
    assert v.performed is None


def test_opt_in_fora_do_dominio_conhecido_so_recusa(settings: Settings) -> None:
    """Sabemos negar; não sabemos transformar um switch com segurança."""
    s = Settings(neo_birra_enabled=True, neo_birra_entity_allow="switch.fita_led")
    v = evaluate_call(
        "switch", "turn_on", "switch.fita_led", {}, mecanica="inverte", settings=s,
    )
    assert v.defy is True
    assert v.mecanica == "nada"
    assert v.performed is None
