"""Unit tests for the PKSRepository no-change short-circuit on upsert."""

from core.pks.repository import PKSRepository


def test_field_value_matches_json_dict_equality():
    assert PKSRepository._field_value_matches("json", {"a": 1, "b": [2]}, {"a": 1, "b": [2]})
    assert not PKSRepository._field_value_matches("json", {"a": 1}, {"a": 2})
    # treat None and {} as equal — that's what the DB stores via _json_dumps({}).
    assert PKSRepository._field_value_matches("json", None, {})
    assert PKSRepository._field_value_matches("json", {}, None)


def test_field_value_matches_text_normalizes_none_to_empty():
    assert PKSRepository._field_value_matches("text", None, "")
    assert PKSRepository._field_value_matches("text", "", None)
    assert PKSRepository._field_value_matches("text", "abc", "abc")
    assert not PKSRepository._field_value_matches("text", "abc", "abcd")


def test_field_value_matches_number_handles_int_and_float():
    assert PKSRepository._field_value_matches("number", 1, 1.0)
    assert PKSRepository._field_value_matches("number", None, None)
    assert not PKSRepository._field_value_matches("number", 1, 2)
    assert not PKSRepository._field_value_matches("number", None, 0)


def test_field_value_matches_boolean_strict_none():
    assert PKSRepository._field_value_matches("boolean", True, True)
    assert PKSRepository._field_value_matches("boolean", None, None)
    assert not PKSRepository._field_value_matches("boolean", True, False)
    assert not PKSRepository._field_value_matches("boolean", None, False)
