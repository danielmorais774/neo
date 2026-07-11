"""Registro nas regras PKS (F6 do docs/context-register-plan.md).

Duas metades: o canonicalizer emite rule_key ESCOPADO + ``registers`` quando o
treino traz condição situacional (fix da diluição — a dedup do writer é por
rule_key, então a variante escopada não sobrescreve a genérica), e o render
(`_should_render_training_item`) filtra a regra fora do registro ativo.
"""

from __future__ import annotations

from core.pks.memory_canonicalizer import MemoryCanonicalizer, _extract_register_scope
from core.pks.service import PKSService
from core.pks.training_envelope import normalize_text
from core.pks.writer import PKSWriter


def _ops(content: str) -> list[dict]:
    return MemoryCanonicalizer().canonicalize(
        clean_content=content, source_message_id="m1", user_id=None,
    )


# ── Extração de escopo ────────────────────────────────────────────────────────

def test_scope_extraction_maps_cues_to_registers():
    assert _extract_register_scope(normalize_text("quando eu estiver trabalhando")) == ["oficina"]
    assert _extract_register_scope(normalize_text("em incidente sério")) == ["combate"]
    assert _extract_register_scope(normalize_text("em reunião com cliente")) == ["protocolo"]
    assert _extract_register_scope(normalize_text("quando eu desabafar")) == ["confidente"]
    assert _extract_register_scope(normalize_text("use sarcasmo sempre")) == []


# ── Canonicalizer: rule_key escopado ──────────────────────────────────────────

def test_unscoped_acid_training_keeps_generic_rule_key():
    ops = _ops("Neo, use humor ácido e deboche quando fizer sentido.")
    rules = [op for op in ops if op.get("kind") == "rule"]
    assert any(op["rule_key"] == "use_acid_sarcasm_when_appropriate" for op in rules)
    assert all("registers" not in op for op in rules)


def test_scoped_acid_training_emits_scoped_rule_key_and_registers():
    ops = _ops(
        "Neo, quando eu estiver trabalhando você pode usar sarcasmo e deboche "
        "pesado comigo."
    )
    rules = [op for op in ops if op.get("kind") == "rule"]
    scoped = [op for op in rules if op["rule_key"] == "use_acid_sarcasm__oficina"]
    assert scoped, f"regra escopada ausente: {[op['rule_key'] for op in rules]}"
    assert scoped[0]["registers"] == ["oficina"]
    # A genérica NÃO é emitida neste treino — nada para diluir.
    assert all(op["rule_key"] != "use_acid_sarcasm_when_appropriate" for op in rules)


def test_scoped_and_generic_rule_keys_never_collide():
    """A raiz da diluição: writer deduplica por rule_key. Chaves distintas =
    a regra específica sobrevive ao lado da genérica."""
    generic = _ops("pode usar deboche e sarcasmo comigo")
    scoped = _ops("quando eu estiver trabalhando, pode usar deboche e sarcasmo")
    generic_keys = {op["rule_key"] for op in generic if op.get("kind") == "rule"}
    scoped_keys = {op["rule_key"] for op in scoped if op.get("kind") == "rule"}
    assert "use_acid_sarcasm_when_appropriate" in generic_keys
    assert "use_acid_sarcasm__oficina" in scoped_keys
    assert "use_acid_sarcasm_when_appropriate" not in scoped_keys


def test_writer_rule_item_preserves_registers():
    op = {
        "rule_key": "use_acid_sarcasm__oficina",
        "semantic_rule_key": "use_acid_sarcasm__oficina",
        "description": "sarcasmo só na oficina",
        "registers": ["oficina"],
        "priority": "high",
    }
    writer = PKSWriter.__new__(PKSWriter)  # só o método puro; sem DB
    item = writer._rule_item_from_op(op)
    assert item["registers"] == ["oficina"]


# ── Render: filtro por registro ativo ─────────────────────────────────────────

def _should_render(item: dict, register: str | None) -> bool:
    service = PKSService.__new__(PKSService)  # método puro; sem DB
    return service._should_render_training_item(
        item, user_message="qual a capital da Mongólia?", register=register,
    )


_SCOPED_RULE = {
    "rule_key": "use_acid_sarcasm__oficina",
    "description": "sarcasmo permitido na oficina",
    "priority": "high",
    "registers": ["oficina"],
}

_GENERIC_RULE = {
    "rule_key": "use_acid_sarcasm_when_appropriate",
    "description": "sarcasmo quando apropriado",
    "priority": "high",
}


def test_scoped_rule_renders_inside_its_register():
    assert _should_render(_SCOPED_RULE, "oficina") is True


def test_scoped_rule_hidden_outside_its_register():
    assert _should_render(_SCOPED_RULE, "protocolo") is False
    assert _should_render(_SCOPED_RULE, "combate") is False


def test_scoped_rule_renders_when_no_register_active():
    # Fail-open: feature off / caminho sem registro → comportamento atual.
    assert _should_render(_SCOPED_RULE, None) is True


def test_generic_rule_unaffected_by_register():
    assert _should_render(_GENERIC_RULE, "protocolo") is True
    assert _should_render(_GENERIC_RULE, None) is True
