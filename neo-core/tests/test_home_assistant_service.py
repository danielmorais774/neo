"""Unit tests for Home Assistant pure helpers (entity resolution + formatting)."""

from core.skills.home_assistant.service import (
    compact_entity,
    entity_domain,
    filter_entities,
    resolve_entity,
    summarize_state,
)

_STATES = [
    {"entity_id": "light.sala", "state": "off", "attributes": {"friendly_name": "Luz da Sala"}},
    {"entity_id": "light.quarto", "state": "on", "attributes": {"friendly_name": "Luz do Quarto", "brightness": 128}},
    {"entity_id": "switch.tomada_tv", "state": "on", "attributes": {"friendly_name": "Tomada TV"}},
    {"entity_id": "climate.quarto", "state": "heat", "attributes": {"friendly_name": "Ar do Quarto", "temperature": 22, "current_temperature": 20}},
    {"entity_id": "scene.modo_cinema", "state": "scening", "attributes": {"friendly_name": "Modo Cinema"}},
]


def test_entity_domain():
    assert entity_domain("light.sala") == "light"
    assert entity_domain("noformat") == ""


def test_filter_by_domain():
    lights = filter_entities(_STATES, domain="light")
    assert {e["entity_id"] for e in lights} == {"light.sala", "light.quarto"}


def test_filter_by_query_accent_insensitive():
    # "salao" should not match; "sala" should match light.sala by friendly_name.
    matched = filter_entities(_STATES, query="sala")
    assert any(e["entity_id"] == "light.sala" for e in matched)


def test_resolve_entity_alias():
    e = resolve_entity("luz da sala", _STATES)
    assert e["entity_id"] == "light.sala"


def test_resolve_entity_with_domain_hint():
    e = resolve_entity("quarto", _STATES, domain="climate")
    assert e["entity_id"] == "climate.quarto"


def test_resolve_entity_returns_none_when_no_match():
    assert resolve_entity("geladeira inteligente xyz", _STATES) is None


def test_summarize_state_brightness():
    out = summarize_state(_STATES[1])  # light.quarto, brightness 128
    assert "Luz do Quarto" in out
    assert "brilho 50%" in out


def test_summarize_state_climate():
    out = summarize_state(_STATES[3])
    assert "Ar do Quarto" in out
    assert "22°" in out


def test_compact_entity_shape():
    c = compact_entity(_STATES[0])
    assert c == {
        "entity_id": "light.sala",
        "friendly_name": "Luz da Sala",
        "state": "off",
        "domain": "light",
    }
