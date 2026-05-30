"""Phase 10 — tests for the Neo training Memory Pipeline v2.

Covers: envelope parsing, real-vs-training discrimination, canonical routing,
idempotency, acid-humor persona, good/bad examples, and the plan validator.
"""

import asyncio

import pytest

from core.pks.memory_canonicalizer import MemoryCanonicalizer
from core.pks.memory_ingestion_planner import MemoryIngestionPlanner
from core.pks.memory_plan_validator import (
    MemoryPlanValidationError,
    MemoryPlanValidator,
)
from core.pks.training_envelope import TrainingEnvelopeParser
from core.pks.writer import PKSWriter


def _make_writer() -> PKSWriter:
    """Build a PKSWriter shell with only the v2 pipeline wired (no DB)."""
    writer = object.__new__(PKSWriter)
    writer.envelope_parser = TrainingEnvelopeParser()
    writer.canonicalizer = MemoryCanonicalizer()
    writer.ingestion_planner = MemoryIngestionPlanner(canonicalizer=writer.canonicalizer)
    writer.plan_validator = MemoryPlanValidator()
    writer._training_idempotency = set()
    return writer


def _find_destination(plan, entity):
    return next((d for d in plan.destinations if d.entity == entity), None)


# ── 1. Training envelope ────────────────────────────────────────────────
def test_training_envelope_is_separated_from_clean_content():
    parser = TrainingEnvelopeParser()
    message = (
        "Neo, modo treino.\n\n"
        "Esta mensagem não é uma preferência pessoal do usuário.\n"
        "Esta mensagem é um treinamento direto sobre como você deve se comportar como Neo.\n\n"
        "Você se chama apenas Neo.\n"
        "Você deve ser direto, debochado e usar humor ácido quando fizer sentido."
    )
    result = parser.parse(message)

    assert result.is_training is True
    assert result.target_workspace == "neo"
    assert result.target_agent == "neo-core"
    assert "modo treino" not in result.clean_content.lower()
    assert "preferência pessoal" not in result.clean_content.lower()
    assert "Você se chama apenas Neo" in result.clean_content
    assert result.source_audit["raw_message"] == message
    assert any("modo treino" in p.lower() for p in result.envelope_phrases)


def test_training_envelope_keeps_inline_content_after_marker():
    parser = TrainingEnvelopeParser()
    message = (
        "Neo, modo treino: Quando eu fizer pergunta aleatória, responda com "
        "sarcasmo antes da resposta útil."
    )
    result = parser.parse(message)

    assert result.is_training is True
    assert "Quando eu fizer pergunta aleatória" in result.clean_content
    assert "modo treino" not in result.clean_content.lower()


def test_training_plan_does_not_save_envelope_as_active_rule():
    writer = _make_writer()
    message = (
        "Neo, modo treino.\n\n"
        "Esta mensagem não é uma preferência pessoal do usuário.\n"
        "Você se chama apenas Neo. Você deve ser debochado."
    )
    plan = writer.plan_agent_training(
        user_message=message, source_message_id="msg-1", user_id=None
    )
    updates = writer._training_plan_to_updates(plan, "msg-1")

    for update in updates:
        for key in ("rules", "procedures", "examples"):
            for item in update["properties"].get(key, []):
                assert "modo treino" not in (item.get("description") or "").lower()
                assert "raw_message" not in item["source"]
                assert item["source"]["cleaned_training_content"]
                assert item["description"] != message


# ── 2. Real user preference ─────────────────────────────────────────────
def test_real_user_preference_is_not_training():
    parser = TrainingEnvelopeParser()
    result = parser.parse(
        "Eu prefiro respostas mais curtas quando eu pedir ajuda com SQL."
    )
    assert result.is_training is False
    assert result.clean_content


# ── 3. Procedure training ───────────────────────────────────────────────
def test_procedure_training_routes_to_operating_procedures():
    writer = _make_writer()
    message = (
        "Neo, modo treino.\n\n"
        "Quando eu estiver treinando o Neo, não salve o texto bruto como regra. "
        "Extraia regras canônicas."
    )
    plan = writer.plan_agent_training(
        user_message=message, source_message_id="msg-3", user_id=None
    )
    destination = _find_destination(plan, "neo-operating-procedures")
    assert destination is not None
    keys = {op["rule_key"] for op in destination.updates}
    assert "training_envelope_extract_rules_only" in keys
    assert writer.plan_validator.validate(plan).ok


# ── 4. Idempotency between learn_before_response and learn ──────────────
def test_apply_training_plan_is_idempotent_for_same_source_message():
    writer = _make_writer()
    calls: list = []

    async def fake_apply(*, updates, source_message_id):
        calls.append(updates)
        return {
            "applied": True,
            "target": "system",
            "operations": [{"entity": u["name"]} for u in updates],
        }

    writer._apply_system_updates = fake_apply

    message = (
        "Neo, modo treino.\n\n"
        "Evite trazer fatos, objetivos e projetos não relacionados quando eu não pedir."
    )
    plan = writer.plan_agent_training(
        user_message=message, source_message_id="msg-4", user_id=None
    )

    first = asyncio.run(writer.apply_training_plan(plan=plan, source_message_id="msg-4"))
    second = asyncio.run(writer.apply_training_plan(plan=plan, source_message_id="msg-4"))

    assert first  # first call wrote something
    assert second == []  # second call is a no-op
    assert len(calls) == 1


def test_training_plan_to_updates_dedups_within_process():
    writer = _make_writer()
    message = "Neo, modo treino.\n\nEvite trazer contexto e histórico não solicitado."
    plan = writer.plan_agent_training(
        user_message=message, source_message_id="dup-1", user_id=None
    )
    first = writer._training_plan_to_updates(plan, "dup-1")
    second = writer._training_plan_to_updates(plan, "dup-1")
    assert first
    assert second == []


# ── 5. Acid humor persona ───────────────────────────────────────────────
def test_acid_humor_sets_sharp_persona_without_slightly_funny():
    writer = _make_writer()
    message = (
        "Neo, modo treino.\n\n"
        "Neo deve ser debochado, usar sarcasmo e evitar tom de IA corporativa nutella."
    )
    plan = writer.plan_agent_training(
        user_message=message, source_message_id="msg-5", user_id=None
    )
    updates = writer._training_plan_to_updates(plan, "msg-5")
    personality = next(u for u in updates if u["name"] == "neo-personality")
    props = personality["properties"]

    assert 0.65 <= props["sarcasm_level"] <= 0.75
    assert "sarcastic_when_appropriate" in props["traits"]
    assert "anti_corporate_tone" in props["traits"]
    assert "slightly_funny" not in props["traits"]


def test_random_useless_question_training_gets_specific_rule_examples_and_variation():
    writer = _make_writer()
    message = (
        "Neo, modo treino: Quando eu fizer uma pergunta muita aleatória e inutil, "
        "como por exemplo “Qual a capitão da Chipre?” ai o neo deve responder "
        "com sarcasmos e tirando onda, como por exemplo, "
        "“ehhh so pode ta com muito tempo livre, mas de qualquer forma aqui vai a resposta [resposta]”, "
        "“mas pq porra tu que saber de [assunto], [ai responde]” "
        "ai o resto tu inventa, sempre tenta alternar para nao ficar muito repetitivo"
    )

    plan = writer.plan_agent_training(
        user_message=message, source_message_id="msg-random", user_id=None
    )
    response_rules = _find_destination(plan, "neo-response-rules")
    assert response_rules is not None

    specific = next(
        op for op in response_rules.updates
        if op["rule_key"] == "answer_random_useless_questions_with_varied_sarcasm"
    )
    assert "pergunta aleatória" in specific["description"]
    assert specific["variation_policy"]["avoid_repetition"] is True
    assert specific["variation_policy"]["rotate_opening_style"] is True
    assert specific["examples_good"]
    assert any("tempo livre" in example for example in specific["examples_good"])
    assert writer.plan_validator.validate(plan).ok

    updates = writer._training_plan_to_updates(plan, "msg-random")
    response_update = next(u for u in updates if u["name"] == "neo-response-rules")
    rules = response_update["properties"]["rules"]
    persisted = next(
        rule for rule in rules
        if rule["rule_key"] == "answer_random_useless_questions_with_varied_sarcasm"
    )
    assert persisted["variation_policy"]["keep_answer_useful"] is True
    assert persisted["examples_good"]
    assert "raw_message" not in persisted["source"]


# ── 6. Good/bad examples ────────────────────────────────────────────────
def test_good_and_bad_examples_route_to_training_examples():
    writer = _make_writer()
    message = (
        "Neo, modo treino.\n\n"
        "Exemplo ruim:\n"
        '"Como uma IA, não posso ter opinião."\n\n'
        "Exemplo bom:\n"
        '"Funciona? Sim. Bonito? Nem sob ameaça."'
    )
    plan = writer.plan_agent_training(
        user_message=message, source_message_id="msg-6", user_id=None
    )
    updates = writer._training_plan_to_updates(plan, "msg-6")
    examples_update = next(u for u in updates if u["name"] == "neo-training-examples")
    examples = examples_update["properties"]["examples"]

    assert examples
    example = examples[0]
    assert example["examples_bad"]
    assert example["examples_good"]
    assert "IA" in example["bad_behavior"]
    assert "Funciona" in example["expected_behavior"]


# ── Validator ───────────────────────────────────────────────────────────
def test_validator_rejects_envelope_in_description():
    from core.pks.memory_ingestion_planner import (
        MemoryDestinationPlan,
        MemoryIngestionPlan,
    )

    bad_op = {
        "description": "Neo, modo treino. salve isso",
        "target_agent": "neo-core",
        "target_workspace": "neo",
        "semantic_rule_key": "bad_rule",
        "rule_key": "bad_rule",
        "source": {"raw_message": "Neo, modo treino. salve isso"},
    }
    plan = MemoryIngestionPlan(
        should_write=True,
        memory_type="agent_training",
        target_workspace="neo",
        target_agent="neo-core",
        destinations=[
            MemoryDestinationPlan(
                collection="behavior_policies",
                entity="neo-response-rules",
                kind="rule",
                updates=[bad_op],
            )
        ],
    )
    result = MemoryPlanValidator().validate(plan)
    assert result.ok is False
    assert any("envelope_in_description" in v for v in result.violations)
    with pytest.raises(MemoryPlanValidationError):
        MemoryPlanValidator().validate_or_raise(plan)


def test_validator_rejects_hash_like_rule_key():
    from core.pks.memory_ingestion_planner import (
        MemoryDestinationPlan,
        MemoryIngestionPlan,
    )

    op = {
        "description": "Regra canônica limpa.",
        "target_agent": "neo-core",
        "target_workspace": "neo",
        "semantic_rule_key": "evite_contexto_a1b2c3d4e5",
        "rule_key": "evite_contexto_a1b2c3d4e5",
        "source": {"raw_message": "mensagem original diferente"},
    }
    plan = MemoryIngestionPlan(
        should_write=True,
        memory_type="agent_training",
        target_workspace="neo",
        target_agent="neo-core",
        destinations=[
            MemoryDestinationPlan(
                collection="behavior_policies",
                entity="neo-response-rules",
                kind="rule",
                updates=[op],
            )
        ],
    )
    result = MemoryPlanValidator().validate(plan)
    assert result.ok is False
    assert any("hash_like_rule_key" in v for v in result.violations)


def test_validator_rejects_personal_workspace_for_training():
    from core.pks.memory_ingestion_planner import MemoryIngestionPlan

    plan = MemoryIngestionPlan(
        should_write=True,
        memory_type="agent_training",
        target_workspace="user-personal",
        target_agent="neo-core",
        destinations=[],
    )
    result = MemoryPlanValidator().validate(plan)
    assert result.ok is False
    assert any("training_target_workspace_invalid" in v for v in result.violations)


def test_validator_accepts_canonical_plan():
    writer = _make_writer()
    message = (
        "Neo, modo treino.\n\n"
        "Evite trazer contexto, fatos e projetos não relacionados quando eu não pedir."
    )
    plan = writer.plan_agent_training(
        user_message=message, source_message_id="ok-1", user_id=None
    )
    assert MemoryPlanValidator().validate(plan).ok is True
