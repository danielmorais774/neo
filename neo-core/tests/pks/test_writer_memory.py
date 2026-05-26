from datetime import date

from core.pks.writer import PKSWriter


def test_extract_name_statement_supports_full_name_and_ignores_question():
    writer = object.__new__(PKSWriter)

    assert writer._extract_name_statement("o meu nome completo é Carlos Daniel Morais") == "Carlos Daniel Morais"
    assert writer._extract_name_statement("Quem sou eu?") is None


def test_normalize_birthday_value_supports_partial_dates():
    writer = object.__new__(PKSWriter)

    value_type, value, unit = writer._normalize_field_payload(
        key="birthday",
        value_type="date",
        value="11-23",
        unit=None,
    )

    assert value_type == "date"
    assert value == date(1900, 11, 23)
    assert unit is None


def test_extract_workout_plan_sections_from_markdown_tables():
    writer = object.__new__(PKSWriter)
    text = """
## Segunda-feira — Peito (Peitoral)

| Exercício | Séries | Repetições | Descanso |
|---|---|---|---|
| Supino reto com barra | 4 | 8-12 | 90s |
| Crucifixo reto na máquina | 3 | 12-15 | 60s |

## Quinta-feira — Braços (Bíceps + Tríceps)

| Exercício | Séries | Repetições | Descanso |
|---|---|---|---|
| Rosca direta com barra W | 4 | 8-12 | 60s |
| Tríceps testa com barra | 4 | 8-12 | 60s |
"""

    sections = writer._extract_workout_plan_sections(text)

    assert [section["day_key"] for section in sections] == ["monday", "thursday"]
    assert sections[0]["muscle_groups"] == ["Peito"]
    assert "Bíceps" in sections[1]["muscle_groups"]
    assert "Tríceps" in sections[1]["muscle_groups"]
    assert [exercise["name"] for exercise in sections[0]["exercises"]] == [
        "Supino reto com barra",
        "Crucifixo reto na máquina",
    ]


def test_find_confirmed_artifact_prefers_real_workout_plan_over_ack():
    writer = object.__new__(PKSWriter)
    workout_plan = """
## Segunda-feira — Peito (Peitoral)
| Exercício | Séries | Repetições | Descanso |
|---|---|---|---|
| Supino reto com barra | 4 | 8-12 | 90s |

## Sexta-feira — Pernas
| Exercício | Séries | Repetições | Descanso |
|---|---|---|---|
| Leg press 45° | 4 | 10-12 | 90s |
"""
    artifact = writer._find_confirmed_assistant_artifact(
        assistant_response="Perfeito, Daniel. Já salvei na memória.",
        recent_messages=[
            {"role": "assistant", "content": workout_plan},
            {"role": "assistant", "content": "Vou salvar aqui na memória pra você."},
        ],
    )

    assert artifact == workout_plan.strip()


def test_extract_system_style_preference_detects_explicit_directive():
    writer = object.__new__(PKSWriter)
    pref = writer._extract_system_style_preference(
        "Eu quero que voce tenha personalidade mais alpha, mais direta e com piadinhas leves."
    )
    assert pref is not None
    assert pref.startswith("Estilo do Neo:")


def test_build_system_updates_routes_assistant_communication_policy():
    writer = object.__new__(PKSWriter)
    updates = writer._build_system_updates(
        user_message=(
            "eu tenho preferencia sobre voce, evite incluir fatos ou objetivos toda hora, "
            "ao menos que faca sentido"
        ),
        assistant_response=None,
        allow_confirmation_fallback=False,
    )

    update = next(item for item in updates if item["name"] == "neo-communication-preferences")
    properties = update["properties"]

    assert update["type"] == "preference"
    assert update["metadata"]["target_agent"] == "neo-core"
    assert update["metadata"]["target_workspace"] == "neo"
    assert update["metadata"]["priority"] == "high"
    assert properties["subtype"] == "assistant_communication_policy"
    assert properties["owner"] == "daniel"
    assert properties["rule_key"] == "avoid_unrelated_historical_context"
    assert "contexto histórico não relacionado" in properties["rule_description"]


def test_extract_assistant_communication_policy_ignores_unrelated_question():
    writer = object.__new__(PKSWriter)

    assert writer._extract_assistant_communication_policy("qual meu treino hoje?") is None


def test_build_system_updates_routes_agent_training_behavior_rule():
    writer = object.__new__(PKSWriter)

    updates = writer._build_system_updates(
        user_message="Neo deve responder sem trazer contexto antigo quando eu fizer uma pergunta simples.",
        assistant_response=None,
        allow_confirmation_fallback=False,
        source_message_id="msg-1",
    )

    update = next(item for item in updates if item["name"] == "neo-response-rules")
    rule = update["properties"]["rules"][0]

    assert update["metadata"]["collection_name"] == "behavior_policies"
    assert rule["priority"] == "high"
    assert rule["source_message_id"] == "msg-1"
    assert rule["target_agent"] == "neo-core"
    assert rule["target_workspace"] == "neo"


def test_build_system_updates_routes_when_then_training_to_procedures():
    writer = object.__new__(PKSWriter)

    updates = writer._build_system_updates(
        user_message="Quando acontecer erro de conexão, faça diagnóstico antes de responder.",
        assistant_response=None,
        allow_confirmation_fallback=False,
    )

    assert any(item["name"] == "neo-operating-procedures" for item in updates)


def test_build_system_updates_routes_correction_to_examples_and_reflection():
    writer = object.__new__(PKSWriter)

    updates = writer._build_system_updates(
        user_message="Isso foi errado, o correto seria responder só sobre a pergunta atual.",
        assistant_response=None,
        allow_confirmation_fallback=False,
    )
    types = {item["type"] for item in updates}

    assert "correction_example" in types
    assert "learned_lesson" in types


def test_infer_personality_properties_for_leon_kennedy_style():
    writer = object.__new__(PKSWriter)

    properties = writer._infer_personality_properties(
        "eu quero que voce tenha personalidade mais alpha mas que saiba fazer uma "
        "piadinhas, estilo Leon Kennedy do Resident Evil"
    )

    assert properties["tone"] == "confident"
    assert properties["humor"] == "dry"
    assert properties["communication_style"] == "direct"
    assert properties["pressure_behavior"] == "calm"
    assert properties["sarcasm_level"] == 0.4
    assert properties["inspiration"] == "Leon Kennedy energy"
    assert set(properties["traits"]) >= {
        "confident",
        "efficient",
        "calm_under_pressure",
        "slightly_funny",
    }


def test_extract_system_style_preference_ignores_non_directive_message():
    writer = object.__new__(PKSWriter)
    pref = writer._extract_system_style_preference("Que horas sao agora?")
    assert pref is None


def test_build_system_updates_routes_goals_with_memory_intent():
    writer = object.__new__(PKSWriter)
    updates = writer._build_system_updates(
        user_message="salva na sua memoria que minha meta principal eh dobrar o MRR no Q3",
        assistant_response=None,
        allow_confirmation_fallback=False,
    )
    entities = {item["name"] for item in updates}
    assert "neo-goals" in entities


def test_build_system_updates_routes_projects_with_memory_intent():
    writer = object.__new__(PKSWriter)
    updates = writer._build_system_updates(
        user_message="guarda no seu workspace que o projeto neo-panel sera prioridade essa semana",
        assistant_response=None,
        allow_confirmation_fallback=False,
    )
    entities = {item["name"] for item in updates}
    assert "active-projects" in entities


def test_extract_entertainment_habit_persists_stable_series_preference():
    writer = object.__new__(PKSWriter)

    habit = writer._extract_entertainment_habit(
        "estou assistindo serie nesse exato momento, geralmente assisto as noites"
    )

    assert habit == {
        "fields": {
            "entertainment_habit": "assiste series à noite",
            "preferred_entertainment_time": "night",
        }
    }


def test_extract_entertainment_habit_ignores_transient_series_activity():
    writer = object.__new__(PKSWriter)

    assert writer._extract_entertainment_habit("estou assistindo serie nesse exato momento") is None
