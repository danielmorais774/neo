"""Phase 2 — MemoryIngestionPlanner.

Central, deterministic planner that decides the *destination* of a memory write.
For Neo training messages it produces a fully canonicalized plan and explicitly
lists the parallel routes that must be blocked (user preference extractor, etc.).

The planner does not write to the DB; ``PKSWriter`` consumes the validated plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from core.pks.memory_canonicalizer import (
    DEST_PROCEDURES,
    DEST_PERSONALITY,
    DEST_RESPONSE_RULES,
    DEST_TRAINING_EXAMPLES,
    MemoryCanonicalizer,
)
from core.pks.training_envelope import (
    NEO_AGENT,
    NEO_WORKSPACE,
    TrainingEnvelopeResult,
    normalize_text,
)

MEMORY_TYPE_AGENT_TRAINING = "agent_training"
MEMORY_TYPE_NONE = "none"

BLOCKED_TRAINING_ROUTES = [
    "user_preference_extractor",
    "assistant_communication_policy_extractor",
    "system_style_preference_extractor",
]

# Maps a logical destination entity to the collection it lives in.
_DESTINATION_COLLECTIONS = {
    DEST_PERSONALITY: "identity",
    DEST_RESPONSE_RULES: "behavior_policies",
    DEST_PROCEDURES: "procedures",
    DEST_TRAINING_EXAMPLES: "training_examples",
}


@dataclass
class MemoryDestinationPlan:
    collection: str
    entity: str
    kind: str
    updates: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MemoryIngestionPlan:
    should_write: bool
    memory_type: str
    target_workspace: str | None
    target_agent: str | None
    blocked_routes: list[str] = field(default_factory=list)
    destinations: list[MemoryDestinationPlan] = field(default_factory=list)
    source_audit: dict[str, Any] = field(default_factory=dict)


class MemoryIngestionPlanner:
    def __init__(self, canonicalizer: MemoryCanonicalizer | None = None) -> None:
        self.canonicalizer = canonicalizer or MemoryCanonicalizer()

    def plan(
        self,
        *,
        user_message: str,
        source_message_id: str | None,
        user_id: UUID | None,
        envelope: TrainingEnvelopeResult,
    ) -> MemoryIngestionPlan:
        if not envelope.is_training:
            return MemoryIngestionPlan(
                should_write=False,
                memory_type=MEMORY_TYPE_NONE,
                target_workspace=None,
                target_agent=None,
                source_audit={"raw_message": user_message},
            )

        ops = self.canonicalizer.canonicalize(
            clean_content=envelope.clean_content,
            source_message_id=source_message_id,
            user_id=user_id,
            source_audit=envelope.source_audit,
        )
        if not ops:
            fallback = self._fallback_rule(
                envelope=envelope,
                source_message_id=source_message_id,
                user_id=user_id,
            )
            ops = [fallback] if fallback else []

        destinations = self._group_destinations(ops)
        return MemoryIngestionPlan(
            should_write=bool(destinations),
            memory_type=MEMORY_TYPE_AGENT_TRAINING,
            target_workspace=envelope.target_workspace or NEO_WORKSPACE,
            target_agent=envelope.target_agent or NEO_AGENT,
            blocked_routes=list(BLOCKED_TRAINING_ROUTES),
            destinations=destinations,
            source_audit=envelope.source_audit,
        )

    def _group_destinations(
        self, ops: list[dict[str, Any]]
    ) -> list[MemoryDestinationPlan]:
        grouped: dict[str, MemoryDestinationPlan] = {}
        order: list[str] = []
        for op in ops:
            entity = op["destination"]
            if entity not in grouped:
                grouped[entity] = MemoryDestinationPlan(
                    collection=_DESTINATION_COLLECTIONS.get(entity, "behavior_policies"),
                    entity=entity,
                    kind=op.get("kind", "rule"),
                    updates=[],
                )
                order.append(entity)
            grouped[entity].updates.append(op)
        return [grouped[entity] for entity in order]

    def _fallback_rule(
        self,
        *,
        envelope: TrainingEnvelopeResult,
        source_message_id: str | None,
        user_id: UUID | None,
    ) -> dict[str, Any] | None:
        clean = " ".join((envelope.clean_content or "").split()).strip()
        if not clean:
            return None
        slug = "_".join(normalize_text(clean).split()[:6]) or "agent_behavior_rule"
        rule_key = f"custom_{slug}"[:80]
        created_by = str(user_id) if user_id else "daniel"
        return {
            "destination": DEST_RESPONSE_RULES,
            "kind": "rule",
            "semantic_rule_key": rule_key,
            "rule_key": rule_key,
            "description": clean[:240],
            "scope": "response_generation",
            "priority": "medium",
            "target_agent": envelope.target_agent or NEO_AGENT,
            "target_workspace": envelope.target_workspace or NEO_WORKSPACE,
            "source_message_id": source_message_id,
            "created_by_user": created_by,
            "confidence": 0.9,
            "source": {
                "raw_message": envelope.source_audit.get("raw_message", ""),
                "cleaned_training_content": clean,
                "removed_envelope_phrases": envelope.source_audit.get(
                    "removed_envelope_phrases", []
                ),
                "created_by_user": created_by,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_message_id": source_message_id,
            },
        }
