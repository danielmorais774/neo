"""Phase 4 — MemoryPlanValidator.

Deterministic guard that runs *after* the planner/canonicalizer and *before* the
writer. It rejects any plan that would re-introduce the bugs described in the
report: envelope text saved as an active rule, raw text copied into a
``description``, hash-based ``rule_key``, or training escaping into the user's
personal workspace / preferences.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.pks.memory_ingestion_planner import (
    MEMORY_TYPE_AGENT_TRAINING,
    MemoryIngestionPlan,
)
from core.pks.training_envelope import NEO_AGENT, NEO_WORKSPACE, normalize_text

# Phrases that must never appear in an active rule description.
_FORBIDDEN_DESCRIPTION_FRAGMENTS = (
    "neo modo treino",
    "esta mensagem nao e uma preferencia pessoal",
    "salve este aprendizado no workspace do neo",
    "salve no workspace do neo",
)

# Old hash-style suffix, e.g. "evite_contexto_a1b2c3d4e5".
_HASH_KEY_SUFFIX = re.compile(r"_[0-9a-f]{8,}$")
# A semantic key must contain at least one alphabetic word.
_HAS_ALPHA = re.compile(r"[a-z]")

# Destination kinds that are forbidden for agent training (would become a
# personal user preference instead of a Neo behavior).
_USER_PREFERENCE_KINDS = {"user_preference", "personal_preference"}


class MemoryPlanValidationError(Exception):
    """Raised when a memory plan fails validation."""


@dataclass
class ValidationResult:
    ok: bool
    violations: list[str] = field(default_factory=list)


class MemoryPlanValidator:
    def validate(self, plan: MemoryIngestionPlan) -> ValidationResult:
        violations: list[str] = []

        if not plan.should_write:
            return ValidationResult(ok=True)

        if plan.memory_type == MEMORY_TYPE_AGENT_TRAINING:
            if plan.target_workspace != NEO_WORKSPACE:
                violations.append(
                    f"training_target_workspace_invalid:{plan.target_workspace}"
                )
            if plan.target_agent != NEO_AGENT:
                violations.append(f"training_target_agent_invalid:{plan.target_agent}")

        for destination in plan.destinations:
            if destination.kind in _USER_PREFERENCE_KINDS:
                violations.append(f"training_as_user_preference:{destination.entity}")
            for op in destination.updates:
                violations.extend(self._validate_op(destination.entity, op))

        return ValidationResult(ok=not violations, violations=violations)

    def validate_or_raise(self, plan: MemoryIngestionPlan) -> ValidationResult:
        result = self.validate(plan)
        if not result.ok:
            raise MemoryPlanValidationError("; ".join(result.violations))
        return result

    def _validate_op(self, entity: str, op: dict) -> list[str]:
        violations: list[str] = []
        description = op.get("description") or ""
        normalized_description = normalize_text(description)

        for fragment in _FORBIDDEN_DESCRIPTION_FRAGMENTS:
            if fragment in normalized_description:
                violations.append(f"envelope_in_description:{entity}:{fragment}")

        if op.get("target_agent") != NEO_AGENT:
            violations.append(f"op_target_agent_invalid:{entity}:{op.get('target_agent')}")
        if op.get("target_workspace") != NEO_WORKSPACE:
            violations.append(
                f"op_target_workspace_invalid:{entity}:{op.get('target_workspace')}"
            )

        raw_message = normalize_text((op.get("source") or {}).get("raw_message", ""))
        if raw_message and normalized_description and raw_message == normalized_description:
            violations.append(f"raw_copied_as_description:{entity}")

        key = op.get("semantic_rule_key") or op.get("rule_key") or ""
        if not key:
            violations.append(f"missing_rule_key:{entity}")
        elif _HASH_KEY_SUFFIX.search(key) or not _HAS_ALPHA.search(key):
            violations.append(f"hash_like_rule_key:{entity}:{key}")

        return violations
