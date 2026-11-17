from core.pks.ontology import OntologyResolver, OntologyValidator, normalize_token


def test_normalize_token_removes_accents_and_noise():
    assert normalize_token("Séries / Repetições!") == "series repeticoes"


def test_entity_type_aliases_are_canonicalized():
    resolver = OntologyResolver()

    assert resolver.resolve_entity_type("notebook").value == "device"
    assert resolver.resolve_entity_type("treino").value == "workout"
    assert resolver.resolve_entity_type("problema").value == "hardware_problem"


def test_entity_type_resolves_pt_en_and_plurals():
    """device/devices/dispositivo/dispositivos all map to the same type."""
    resolver = OntologyResolver()

    for term in ("device", "devices", "dispositivo", "dispositivos", "hardware",
                 "computadores", "monitores", "keyboard"):
        assert resolver.resolve_entity_type(term).value == "device", term

    assert resolver.resolve_entity_type("treinos").value == "workout"
    assert resolver.resolve_entity_type("metas").value == "goal"
    assert resolver.resolve_entity_type("projetos").value == "project"
    assert resolver.resolve_entity_type("suplementos").value == "supplement"
    # non-entity words still resolve to nothing
    assert resolver.resolve_entity_type("tempo") is None
    assert resolver.resolve_entity_type("") is None


def test_validator_canonicalizes_field_keys_and_rejects_unknown_types():
    validator = OntologyValidator()

    assert validator.entity_type("dispositivo") == "device"
    assert validator.relation_type("possui") == "owns"
    assert validator.field_key("processador") == "cpu"
    assert validator.entity_type("tipo_inventado") is None
