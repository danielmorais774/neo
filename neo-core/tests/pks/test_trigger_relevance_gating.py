"""Trigger-relevance gating for verbatim PKS examples.

Regression for session 6794b3ba: the model copied a trigger-specific "good
example" (the "aspirador" line) into an unrelated compliment turn because the
example was rendered into the prompt regardless of whether its trigger matched.
"""

from core.pks.service import PKSService

_TRIGGER = (
    "Usuário usa frases como 'me respeita', 'responde direito' ou ameaças "
    "absurdas no meio de uma provocação/brincadeira."
)


def test_trigger_irrelevant_message_withholds_examples():
    # The exact failing message — a compliment, no pushback trigger.
    assert PKSService._trigger_is_relevant(_TRIGGER, "que isso cara, to te elogiando") is False


def test_trigger_relevant_message_allows_examples():
    assert PKSService._trigger_is_relevant(_TRIGGER, "me respeita seu lixo") is True
    assert PKSService._trigger_is_relevant(_TRIGGER, "responde direito vai") is True


def test_rule_without_trigger_is_always_relevant():
    assert PKSService._trigger_is_relevant(None, "qualquer coisa") is True


def test_empty_message_withholds_trigger_examples():
    assert PKSService._trigger_is_relevant(_TRIGGER, "") is False


def test_format_system_properties_gates_and_relabels_examples():
    svc = object.__new__(PKSService)
    props = {
        "rules": [
            {
                "rule_description": "Responder com deboche quando provocado.",
                "rule_key": "playful_respect_pushback_response",
                "priority": "high",
                "trigger": _TRIGGER,
                "examples_good": ["Haha, se você me transformar em aspirador..."],
            }
        ]
    }

    # Irrelevant turn: rule still rendered, but NO verbatim example leaks.
    out_irrelevant = "\n".join(
        svc._format_system_properties(props, user_message="que isso cara, to te elogiando")
    )
    assert "playful_respect_pushback_response" in out_irrelevant
    assert "aspirador" not in out_irrelevant

    # Relevant turn: example surfaces, labelled as style (not a script to copy).
    out_relevant = "\n".join(
        svc._format_system_properties(props, user_message="me respeita seu lixo")
    )
    assert "aspirador" in out_relevant
    assert "Exemplos de TOM" in out_relevant
    assert "NÃO copie" in out_relevant
