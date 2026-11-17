"""Unit tests for Home Assistant pure helpers (entity resolution + formatting)."""

from core.skills.home_assistant.service import (
    build_device_index,
    compact_entity,
    entity_domain,
    filter_entities,
    match_known_device,
    render_snapshot,
    resolve_entity,
    state_satisfies_spec,
    summarize_state,
    verify_spec,
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


# ── Relevance gate: match_known_device / build_device_index ───────────────────

_INDEX = build_device_index(_STATES)


def test_build_device_index_shape():
    assert {e["entity_id"] for e in _INDEX} == {s["entity_id"] for s in _STATES}
    assert all({"entity_id", "friendly_name", "domain"} <= set(e) for e in _INDEX)


def test_match_known_device_matches_climate_alias_no_space():
    # "arcondicionado" (sem espaço) deve casar com "Ar do Quarto" (climate).
    matched = match_known_device("liga o arcondicionado do quarto", _INDEX)
    assert any(e["entity_id"] == "climate.quarto" for e in matched)


def test_match_known_device_matches_friendly_name():
    matched = match_known_device("como está a luz da sala?", _INDEX)
    assert any(e["entity_id"] == "light.sala" for e in matched)


def test_match_known_device_ignores_unknown_device():
    # "computador" não casa com nenhum device do HA → gate vazio → não injeta/consulta.
    assert match_known_device("liga o meu computador", _INDEX) == []


def test_match_known_device_broad_house_terms():
    matched = match_known_device("como tá a casa?", _INDEX)
    # Gatilho amplo → traz todas as entidades de domínios relevantes.
    assert {e["entity_id"] for e in matched} >= {"light.sala", "light.quarto", "climate.quarto"}


# ── render_snapshot ──────────────────────────────────────────────────────────

def test_render_snapshot_narrows_to_query():
    out = render_snapshot(_STATES, query="ar do quarto")
    assert "Ar do Quarto" in out
    assert "Luz da Sala" not in out


def test_render_snapshot_grouped_by_domain_when_broad():
    out = render_snapshot(_STATES, query=None)
    assert "light:" in out and "climate:" in out
    assert "Luz da Sala" in out and "Ar do Quarto" in out


# ── verify_spec / state_satisfies_spec (climate) ─────────────────────────────

def test_verify_spec_climate_turn_on_is_not_off():
    assert verify_spec("climate", "turn_on") == ("state_ne", "off")
    assert state_satisfies_spec({"state": "cool"}, ("state_ne", "off")) is True
    assert state_satisfies_spec({"state": "off"}, ("state_ne", "off")) is False


def test_verify_spec_climate_turn_off():
    assert verify_spec("climate", "turn_off") == ("state_eq", "off")


def test_verify_spec_climate_set_temperature_attr():
    spec = verify_spec("climate", "set_temperature", {"temperature": 22})
    assert spec == ("attr_eq:temperature", 22)
    entity = {"state": "cool", "attributes": {"temperature": 22.0}}
    assert state_satisfies_spec(entity, spec) is True
    assert state_satisfies_spec({"state": "cool", "attributes": {"temperature": 25}}, spec) is False


def test_verify_spec_climate_set_hvac_mode():
    spec = verify_spec("climate", "set_hvac_mode", {"hvac_mode": "cool"})
    assert spec == ("state_eq", "cool")


def test_verify_spec_light_backcompat():
    assert verify_spec("light", "turn_on") == ("state_eq", "on")


def test_verify_spec_unknown_returns_none():
    assert verify_spec("media_player", "play_media") is None
