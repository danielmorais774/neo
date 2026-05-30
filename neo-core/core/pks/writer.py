"""PKS learning pipeline: LLM proposes, backend validates, repository executes."""

from __future__ import annotations

import logging
import re
import hashlib
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from core.llm.router import LLMRouter
from core.pks.audit import AuditLogger
from core.pks.bootstrap import PKSBootstrap
from core.pks.entity_resolver import EntityResolver
from core.pks.memory_planner import PKSMemoryPlanner
from core.pks.memory_canonicalizer import (
    DEST_PERSONALITY,
    DEST_PROCEDURES,
    DEST_RESPONSE_RULES,
    DEST_TRAINING_EXAMPLES,
    MemoryCanonicalizer,
)
from core.pks.memory_ingestion_planner import (
    MemoryIngestionPlan,
    MemoryIngestionPlanner,
)
from core.pks.memory_plan_validator import (
    MemoryPlanValidationError,
    MemoryPlanValidator,
)
from core.pks.training_envelope import TrainingEnvelopeParser
from core.pks.ontology import OntologyValidator, normalize_token
from core.pks.repository import PKSRepository
from core.pks.schemas import (
    PKSOperation,
    UpsertEntityInput,
    UpsertFieldInput,
    UpsertRelationInput,
)
from core.pks.system_bootstrap import (
    COMMUNICATION_POLICY_DESCRIPTION,
    COMMUNICATION_POLICY_RULE_KEY,
    DEFAULT_COMMUNICATION_PREFS_PROPERTIES,
    DEFAULT_OPERATING_PROCEDURES_PROPERTIES,
    DEFAULT_PERSONALITY_PROPERTIES,
    DEFAULT_GOALS_PROPERTIES,
    DEFAULT_PROFILE_PROPERTIES,
    DEFAULT_PROJECTS_PROPERTIES,
    DEFAULT_RESPONSE_RULES_PROPERTIES,
    DEFAULT_WORLD_STATE_PROPERTIES,
    SYSTEM_AGENT_NAME,
    SYSTEM_BEHAVIOR_POLICIES_COLLECTION_NAME,
    SYSTEM_COMMUNICATION_PREFS_COLLECTION_NAME,
    SYSTEM_COMMUNICATION_PREFS_NAME,
    SYSTEM_COMMUNICATION_PREFS_TYPE,
    SYSTEM_CORRECTION_EXAMPLE_TYPE,
    SYSTEM_LEARNED_LESSON_TYPE,
    SYSTEM_OPERATING_PROCEDURES_NAME,
    SYSTEM_OPERATING_PROCEDURES_TYPE,
    SYSTEM_PROCEDURES_COLLECTION_NAME,
    SYSTEM_REFLECTION_COLLECTION_NAME,
    SYSTEM_RESPONSE_RULES_NAME,
    SYSTEM_RESPONSE_RULES_TYPE,
    SYSTEM_TRAINING_EXAMPLES_COLLECTION_NAME,
    SYSTEM_TRAINING_EXAMPLES_NAME,
    SYSTEM_GOALS_NAME,
    SYSTEM_GOALS_TYPE,
    SYSTEM_PROFILE_NAME,
    SYSTEM_PROFILE_TYPE,
    SYSTEM_PROJECTS_NAME,
    SYSTEM_PROJECTS_TYPE,
    SYSTEM_PERSONALITY_NAME,
    SYSTEM_PERSONALITY_TYPE,
    SYSTEM_WORLD_STATE_NAME,
    SYSTEM_WORLD_STATE_TYPE,
    SYSTEM_WORKSPACE_NAME,
)
from db.async_client import AsyncPostgresClient

logger = logging.getLogger(__name__)

MIN_APPLY_CONFIDENCE = 0.80
MAX_PLANNER_OPERATIONS = 40


class PKSWriter:
    def __init__(
        self,
        db: AsyncPostgresClient,
        llm_router: LLMRouter,
        repository: PKSRepository,
        bootstrap: PKSBootstrap,
        resolver: EntityResolver,
        validator: OntologyValidator,
        audit: AuditLogger,
    ) -> None:
        self.db = db
        self.llm = llm_router
        self.repository = repository
        self.bootstrap = bootstrap
        self.resolver = resolver
        self.validator = validator
        self.audit = audit
        self.memory_planner = PKSMemoryPlanner(llm_router)
        # Memory Pipeline v2 for Neo training messages.
        self.envelope_parser = TrainingEnvelopeParser()
        self.canonicalizer = MemoryCanonicalizer()
        self.ingestion_planner = MemoryIngestionPlanner(canonicalizer=self.canonicalizer)
        self.plan_validator = MemoryPlanValidator()
        # In-process idempotency guard keyed by
        # (source_message_id, semantic_rule_key, target_agent, target_workspace).
        self._training_idempotency: set[tuple[str | None, str, str, str]] = set()

    async def learn(
        self,
        *,
        user_id: UUID,
        user_message: str,
        assistant_response: str,
        memory_target: Literal["user", "system"] = "user",
        pks_context: str = "",
        keywords: list[str] | None = None,
        recent_history: str = "",
        recent_messages: list[dict[str, str]] | None = None,
        source_message_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if memory_target == "system":
            return await self._learn_system(
                user_id=user_id,
                user_message=user_message,
                assistant_response=assistant_response,
                source_message_id=source_message_id,
            )
        if self._extract_assistant_communication_policy(user_message) or self._is_agent_training_intent(normalize_token(user_message)):
            logger.info("[PKS][plan_memory] agent training routed to system workspace")
            return []
        workspace_id = await self.bootstrap.ensure_workspace(user_id)
        logger.info(
            "[PKS][plan_memory] learn_start workspace_id=%s source_message_id=%s",
            workspace_id,
            source_message_id,
        )
        confirmed = await self._learn_from_confirmed_artifact(
            workspace_id=workspace_id,
            user_message=user_message,
            assistant_response=assistant_response,
            recent_messages=recent_messages,
            source_message_id=source_message_id,
        )
        if confirmed:
            logger.info(
                "[PKS][plan_memory] learn_complete path=confirmed_artifact applied=%d",
                len(confirmed),
            )
            return confirmed

        operations = await self._plan_operations(
            user_message=user_message,
            assistant_response=assistant_response,
            pks_context=pks_context,
            recent_history=recent_history,
        )
        logger.info("[PKS][plan_memory] planned_operations=%d", len(operations))
        if not operations:
            logger.info("[PKS][plan_memory] learn_complete path=planner applied=0")
            return []

        applied: list[dict[str, Any]] = []
        async with self.db.get_session() as session:
            for op in operations[:MAX_PLANNER_OPERATIONS]:
                try:
                    if op.confidence < MIN_APPLY_CONFIDENCE:
                        await self.audit.log(
                            session,
                            workspace_id=workspace_id,
                            actor_type="agent",
                            actor_id="pks_writer",
                            action="discard_low_confidence",
                            target_type="operation",
                            before=None,
                            after=op.model_dump(mode="json"),
                            reason=op.reason,
                            confidence=op.confidence,
                            source_message_id=source_message_id,
                        )
                        continue
                    result = await self._apply_operation(
                        session,
                        workspace_id=workspace_id,
                        op=op,
                        source_message_id=source_message_id,
                    )
                    if result:
                        applied.append(result)
                except Exception as exc:
                    logger.warning("[PKS] Failed to apply operation %s: %s", op.op, exc, exc_info=True)
            await session.commit()
        logger.info("[PKS][plan_memory] learn_complete path=planner applied=%d", len(applied))
        return applied

    async def learn_before_response(
        self,
        *,
        user_id: UUID,
        user_message: str,
        memory_target: Literal["user", "system"] = "user",
        source_message_id: str | None = None,
    ) -> dict[str, Any]:
        if memory_target == "system":
            return await self._learn_system_before_response(
                user_id=user_id,
                user_message=user_message,
                source_message_id=source_message_id,
            )
        workspace_id = await self.bootstrap.ensure_workspace(user_id)
        async with self.db.get_session() as session:
            summary = await self._apply_direct_fact_writes(
                session,
                workspace_id=workspace_id,
                user_message=user_message,
                source_message_id=source_message_id,
            )
            await session.commit()
            return summary

    async def _learn_system_before_response(
        self,
        *,
        user_id: UUID,
        user_message: str,
        source_message_id: str | None,
    ) -> dict[str, Any]:
        # Memory Pipeline v2: explicit Neo training is routed to the canonical,
        # idempotent training pipeline and bypasses the legacy heuristics.
        envelope = self.envelope_parser.parse(user_message)
        if envelope.is_training:
            plan = self.ingestion_planner.plan(
                user_message=user_message,
                source_message_id=source_message_id,
                user_id=user_id,
                envelope=envelope,
            )
            operations = await self.apply_training_plan(
                plan=plan,
                source_message_id=source_message_id,
            )
            return {
                "applied": bool(operations),
                "target": "system",
                "memory_type": "agent_training",
                "operations": operations,
            }

        updates = self._build_system_updates(
            user_id=user_id,
            user_message=user_message,
            assistant_response=None,
            allow_confirmation_fallback=False,
            source_message_id=source_message_id,
        )
        if not updates:
            return {"applied": False, "target": "system", "operations": []}
        return await self._apply_system_updates(
            updates=updates,
            source_message_id=source_message_id,
        )

    async def _learn_system(
        self,
        *,
        user_id: UUID,
        user_message: str,
        assistant_response: str,
        source_message_id: str | None,
    ) -> list[dict[str, Any]]:
        envelope = self.envelope_parser.parse(user_message)
        if envelope.is_training:
            plan = self.ingestion_planner.plan(
                user_message=user_message,
                source_message_id=source_message_id,
                user_id=user_id,
                envelope=envelope,
            )
            return await self.apply_training_plan(
                plan=plan,
                source_message_id=source_message_id,
            )

        updates = self._build_system_updates(
            user_id=user_id,
            user_message=user_message,
            assistant_response=assistant_response,
            allow_confirmation_fallback=True,
            source_message_id=source_message_id,
        )
        if not updates:
            return []
        result = await self._apply_system_updates(
            updates=updates,
            source_message_id=source_message_id,
        )
        return result.get("operations", []) if result.get("applied") else []

    # ── Memory Pipeline v2: training plan application ────────────────────
    def plan_agent_training(
        self,
        *,
        user_message: str,
        source_message_id: str | None,
        user_id: UUID | None,
    ) -> MemoryIngestionPlan:
        """Parse + plan a Neo training message (no DB writes)."""
        envelope = self.envelope_parser.parse(user_message)
        return self.ingestion_planner.plan(
            user_message=user_message,
            source_message_id=source_message_id,
            user_id=user_id,
            envelope=envelope,
        )

    async def apply_training_plan(
        self,
        *,
        plan: MemoryIngestionPlan,
        source_message_id: str | None,
    ) -> list[dict[str, Any]]:
        """Validate and apply a canonical training plan, idempotently."""
        if not plan.should_write or not plan.destinations:
            return []
        try:
            self.plan_validator.validate_or_raise(plan)
        except MemoryPlanValidationError as exc:
            logger.warning("[PKS][training_v2] plan rejected by validator: %s", exc)
            return []

        updates = self._training_plan_to_updates(plan, source_message_id)
        if not updates:
            logger.info("[PKS][training_v2] nothing new to apply (idempotent)")
            return []
        result = await self._apply_system_updates(
            updates=updates,
            source_message_id=source_message_id,
        )
        return result.get("operations", []) if result.get("applied") else []

    def _training_plan_to_updates(
        self,
        plan: MemoryIngestionPlan,
        source_message_id: str | None,
    ) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        for destination in plan.destinations:
            rule_items: list[dict[str, Any]] = []
            example_items: list[dict[str, Any]] = []
            persona_props: dict[str, Any] = {}

            for op in destination.updates:
                key = (
                    source_message_id,
                    str(op.get("semantic_rule_key") or op.get("rule_key") or ""),
                    str(op.get("target_agent")),
                    str(op.get("target_workspace")),
                )
                if key in self._training_idempotency:
                    continue
                self._training_idempotency.add(key)

                if destination.kind == "example":
                    example_items.append(self._example_item_from_op(op))
                elif destination.kind == "persona":
                    persona_props = self._merge_persona(
                        persona_props, op.get("persona_properties") or {}
                    )
                    if op.get("description"):
                        rule_items.append(self._rule_item_from_op(op))
                else:
                    rule_items.append(self._rule_item_from_op(op))

            update = self._build_training_update(
                destination=destination,
                rule_items=rule_items,
                example_items=example_items,
                persona_props=persona_props,
                source_message_id=source_message_id,
            )
            if update:
                updates.append(update)
        return updates

    def _build_training_update(
        self,
        *,
        destination,
        rule_items: list[dict[str, Any]],
        example_items: list[dict[str, Any]],
        persona_props: dict[str, Any],
        source_message_id: str | None,
    ) -> dict[str, Any] | None:
        entity = destination.entity
        common_metadata = {
            "workspace_name": SYSTEM_WORKSPACE_NAME,
            "target_agent": SYSTEM_AGENT_NAME,
            "target_workspace": SYSTEM_WORKSPACE_NAME,
        }
        if entity == DEST_PERSONALITY:
            properties: dict[str, Any] = dict(persona_props)
            if rule_items:
                properties["rules"] = rule_items
            if not properties:
                return None
            return {
                "name": SYSTEM_PERSONALITY_NAME,
                "type": SYSTEM_PERSONALITY_TYPE,
                "aliases": ["personality", "neo identity", "neo persona"],
                "metadata": {
                    **common_metadata,
                    "collection_name": "identity",
                    "collection_type": SYSTEM_PERSONALITY_TYPE,
                    "is_core_identity": True,
                },
                "default_properties": DEFAULT_PERSONALITY_PROPERTIES,
                "properties": properties,
                "reason": "treinamento canônico de personalidade do Neo",
                "confidence": 0.97,
            }
        if entity == DEST_RESPONSE_RULES:
            if not rule_items:
                return None
            return {
                "name": SYSTEM_RESPONSE_RULES_NAME,
                "type": SYSTEM_RESPONSE_RULES_TYPE,
                "aliases": ["behavior policies", "response rules", "neo behavior rules"],
                "metadata": {
                    **common_metadata,
                    "collection_name": SYSTEM_BEHAVIOR_POLICIES_COLLECTION_NAME,
                    "collection_type": SYSTEM_RESPONSE_RULES_TYPE,
                    "is_behavior_policy_store": True,
                },
                "default_properties": DEFAULT_RESPONSE_RULES_PROPERTIES,
                "properties": {"rules": rule_items},
                "reason": "treinamento canônico de regra de resposta do Neo",
                "confidence": 0.97,
            }
        if entity == DEST_PROCEDURES:
            if not rule_items:
                return None
            return {
                "name": SYSTEM_OPERATING_PROCEDURES_NAME,
                "type": SYSTEM_OPERATING_PROCEDURES_TYPE,
                "aliases": ["operating procedures", "when then procedures", "neo procedures"],
                "metadata": {
                    **common_metadata,
                    "collection_name": SYSTEM_PROCEDURES_COLLECTION_NAME,
                    "collection_type": SYSTEM_OPERATING_PROCEDURES_TYPE,
                    "is_operating_procedure_store": True,
                },
                "default_properties": DEFAULT_OPERATING_PROCEDURES_PROPERTIES,
                "properties": {"procedures": rule_items},
                "reason": "treinamento canônico de procedimento operacional do Neo",
                "confidence": 0.97,
            }
        if entity == DEST_TRAINING_EXAMPLES:
            if not example_items:
                return None
            return {
                "name": SYSTEM_TRAINING_EXAMPLES_NAME,
                "type": SYSTEM_CORRECTION_EXAMPLE_TYPE,
                "aliases": ["training examples", "correction examples", "neo examples"],
                "metadata": {
                    **common_metadata,
                    "collection_name": SYSTEM_TRAINING_EXAMPLES_COLLECTION_NAME,
                    "collection_type": SYSTEM_CORRECTION_EXAMPLE_TYPE,
                    "is_training_examples_store": True,
                },
                "default_properties": {"examples": []},
                "properties": {"examples": example_items},
                "reason": "exemplo de correção canônico do Neo",
                "confidence": 0.97,
            }
        return None

    def _rule_item_from_op(self, op: dict[str, Any]) -> dict[str, Any]:
        semantic_key = str(op.get("semantic_rule_key") or op.get("rule_key") or "")
        item = {
            "rule_key": semantic_key,
            "semantic_rule_key": semantic_key,
            "description": op.get("description", ""),
            "scope": op.get("scope", "agent_behavior"),
            "priority": op.get("priority", "high"),
            "target_agent": op.get("target_agent", SYSTEM_AGENT_NAME),
            "target_workspace": op.get("target_workspace", SYSTEM_WORKSPACE_NAME),
            "source_message_id": op.get("source_message_id"),
            "created_by_user": op.get("created_by_user", "daniel"),
            "source": self._properties_source_from_op(op),
            "examples_good": list(op.get("examples_good", [])),
            "examples_bad": list(op.get("examples_bad", [])),
            "confidence": op.get("confidence", 0.96),
        }
        if op.get("trigger"):
            item["trigger"] = op["trigger"]
        if op.get("action"):
            item["action"] = op["action"]
        for key in ("variation_policy", "response_style", "applicability"):
            if op.get(key) is not None:
                item[key] = op[key]
        return item

    def _example_item_from_op(self, op: dict[str, Any]) -> dict[str, Any]:
        example = op.get("example") or {}
        lesson_key = str(example.get("lesson_key") or op.get("semantic_rule_key") or "")
        return {
            "rule_key": lesson_key,
            "semantic_rule_key": lesson_key,
            "type": example.get("type", "correction_example"),
            "lesson_key": lesson_key,
            "bad_behavior": example.get("bad_behavior", ""),
            "expected_behavior": example.get("expected_behavior", ""),
            "examples_bad": list(example.get("examples_bad", [])),
            "examples_good": list(example.get("examples_good", [])),
            "description": op.get("description", ""),
            "priority": op.get("priority", "high"),
            "source_message_id": op.get("source_message_id"),
            "source": self._properties_source_from_op(op),
        }

    @staticmethod
    def _properties_source_from_op(op: dict[str, Any]) -> dict[str, Any]:
        """Keep audit pointers in properties without embedding the raw prompt."""
        source = op.get("source")
        if not isinstance(source, dict):
            return {}
        sanitized = dict(source)
        sanitized.pop("raw_message", None)
        return sanitized

    @staticmethod
    def _merge_persona(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        merged = {**current}
        for key, value in incoming.items():
            if key == "traits":
                existing = merged.get("traits")
                existing = existing if isinstance(existing, list) else []
                incoming_traits = value if isinstance(value, list) else []
                merged["traits"] = sorted(
                    {str(item) for item in [*existing, *incoming_traits] if item}
                )
            elif value is not None:
                merged[key] = value
        return merged

    async def _apply_system_updates(
        self,
        *,
        updates: list[dict[str, Any]],
        source_message_id: str | None,
    ) -> dict[str, Any]:
        if not updates:
            return {"applied": False, "target": "system", "operations": []}
        async with self.db.get_session() as session:
            workspace = await self.repository.get_system_workspace(session)
            if not workspace:
                return {"applied": False, "target": "system", "operations": []}
            operation_rows: list[dict[str, Any]] = []
            last_payload: dict[str, Any] = {}
            for update in updates:
                entity = await self._resolve_or_create_system_entity(
                    session,
                    workspace_id=workspace["id"],
                    name=update["name"],
                    entity_type=update["type"],
                    aliases=update.get("aliases", []),
                    metadata=update.get("metadata", {}),
                    source_message_id=source_message_id,
                )
                existing_field = await self.repository.get_field(session, entity.id, "properties")
                default_properties = update.get("default_properties") or {}
                payload: dict[str, Any] = (
                    existing_field.value
                    if existing_field and isinstance(existing_field.value, dict)
                    else dict(default_properties)
                )
                payload = self._merge_structured_properties(payload, update["properties"])
                record, before = await self.repository.upsert_field(
                    session,
                    UpsertFieldInput(
                        entity_id=entity.id,
                        key="properties",
                        value_type="json",
                        value=payload,
                        confidence=update.get("confidence", 0.95),
                        sensitivity="private",
                        source="conversation",
                        source_message_id=source_message_id,
                        actor="agent",
                        metadata={"structured_properties": True},
                    ),
                )
                await self.audit.log(
                    session,
                    workspace_id=workspace["id"],
                    actor_type="agent",
                    actor_id="pks_writer",
                    action="upsert_field",
                    target_type="field",
                    target_id=record.id,
                    before=before,
                    after=record.model_dump(mode="json"),
                    reason=update.get("reason"),
                    confidence=update.get("confidence", 0.95),
                    source_message_id=source_message_id,
                )
                operation_rows.append(
                    {
                        "op": "upsert_field",
                        "target": "system",
                        "entity": update["name"],
                        "key": "properties",
                        "reason": update.get("reason"),
                    }
                )
                last_payload = payload
            await session.commit()
            return {
                "applied": True,
                "target": "system",
                "operations": operation_rows,
                "properties": last_payload,
            }

    async def _resolve_or_create_system_entity(
        self,
        session,
        *,
        workspace_id: UUID,
        name: str,
        entity_type: str,
        aliases: list[str],
        metadata: dict[str, Any],
        source_message_id: str | None,
    ):
        entity = await self.repository.find_entity_by_name(session, workspace_id, name, entity_type)
        if entity and not metadata.get("collection_name"):
            return entity
        space_id = await self.repository.ensure_space(session, workspace_id, "Sistema", "system")
        collection_name = metadata.get("collection_name") or "Estrutura do Sistema"
        collection_type = metadata.get("collection_type") or entity_type
        collection_id = await self.repository.ensure_collection(
            session,
            workspace_id,
            space_id,
            collection_name,
            collection_type,
        )
        entity, _ = await self.repository.upsert_entity(
            session,
            UpsertEntityInput(
                workspace_id=workspace_id,
                space_id=space_id,
                collection_id=collection_id,
                name=name,
                type=entity_type,
                aliases=aliases,
                metadata=metadata,
                confidence=1.0,
                sensitivity="private",
                source="conversation",
                source_message_id=source_message_id,
            ),
        )
        return entity

    def _build_system_updates(
        self,
        *,
        user_id: UUID | None = None,
        user_message: str,
        assistant_response: str | None,
        allow_confirmation_fallback: bool,
        source_message_id: str | None = None,
    ) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        normalized = normalize_token(user_message)
        training_updates = self._build_agent_training_updates(
            user_message=user_message,
            source_message_id=source_message_id,
            user_id=user_id,
        )
        communication_policy = self._extract_assistant_communication_policy(user_message)
        style_pref = self._extract_system_style_preference(user_message)
        confirmation = self._is_memory_confirmation(user_message)
        has_memory_intent = self._is_system_memory_intent(user_message)

        updates.extend(training_updates)

        if communication_policy:
            updates.append(
                {
                    "name": SYSTEM_COMMUNICATION_PREFS_NAME,
                    "type": SYSTEM_COMMUNICATION_PREFS_TYPE,
                    "aliases": [
                        "assistant communication policy",
                        "estilo de resposta",
                        "preferência de comunicação",
                        "como você responde",
                        "como o Neo responde",
                    ],
                    "metadata": {
                        "workspace_name": SYSTEM_WORKSPACE_NAME,
                        "collection_name": SYSTEM_COMMUNICATION_PREFS_COLLECTION_NAME,
                        "collection_type": SYSTEM_COMMUNICATION_PREFS_TYPE,
                        "is_assistant_communication_policy": True,
                        "owner_user_id": str(user_id) if user_id else None,
                        "owner_user_name": "Daniel Morais",
                        "target_agent": SYSTEM_AGENT_NAME,
                        "target_workspace": SYSTEM_WORKSPACE_NAME,
                        "scope": "assistant_response_generation",
                        "priority": "high",
                        "rule_key": COMMUNICATION_POLICY_RULE_KEY,
                    },
                    "default_properties": DEFAULT_COMMUNICATION_PREFS_PROPERTIES,
                    "properties": {
                        **communication_policy,
                        "owner_user_id": str(user_id) if user_id else None,
                    },
                    "reason": "preferência explícita sobre política de comunicação do Neo",
                    "confidence": 0.99,
                }
            )

        if style_pref:
            updates.append(
                {
                    "name": SYSTEM_PERSONALITY_NAME,
                    "type": SYSTEM_PERSONALITY_TYPE,
                    "aliases": ["personality", "neo identity", "neo persona"],
                    "metadata": {
                        "workspace_name": SYSTEM_WORKSPACE_NAME,
                        "collection_name": "identity",
                        "collection_type": SYSTEM_PERSONALITY_TYPE,
                        "is_core_identity": True,
                    },
                    "default_properties": DEFAULT_PERSONALITY_PROPERTIES,
                    "properties": self._infer_personality_properties(style_pref),
                    "reason": "preferência explícita de estilo/persona para o Neo",
                    "confidence": 0.96,
                }
            )

        if has_memory_intent:
            goals_properties = self._infer_goals_properties(user_message)
            if goals_properties:
                updates.append(
                    {
                        "name": SYSTEM_GOALS_NAME,
                        "type": SYSTEM_GOALS_TYPE,
                        "aliases": ["goals", "neo goals", "objective tracker"],
                        "metadata": {"workspace_name": SYSTEM_WORKSPACE_NAME, "is_core_goals": True},
                        "default_properties": DEFAULT_GOALS_PROPERTIES,
                        "properties": goals_properties,
                        "reason": "atualização de metas do Neo",
                        "confidence": 0.94,
                    }
                )
            profile_properties = self._infer_profile_properties(user_message)
            if profile_properties:
                updates.append(
                    {
                        "name": SYSTEM_PROFILE_NAME,
                        "type": SYSTEM_PROFILE_TYPE,
                        "aliases": ["profile", "daniel profile", "user profile"],
                        "metadata": {"workspace_name": SYSTEM_WORKSPACE_NAME, "is_core_profile": True},
                        "default_properties": DEFAULT_PROFILE_PROPERTIES,
                        "properties": profile_properties,
                        "reason": "atualização de perfil do usuário para o Neo",
                        "confidence": 0.94,
                    }
                )
            projects_properties = self._infer_projects_properties(user_message)
            if projects_properties:
                updates.append(
                    {
                        "name": SYSTEM_PROJECTS_NAME,
                        "type": SYSTEM_PROJECTS_TYPE,
                        "aliases": ["projects", "active projects", "project tracker"],
                        "metadata": {"workspace_name": SYSTEM_WORKSPACE_NAME, "is_core_projects": True},
                        "default_properties": DEFAULT_PROJECTS_PROPERTIES,
                        "properties": projects_properties,
                        "reason": "atualização de projetos ativos",
                        "confidence": 0.94,
                    }
                )
            world_state_properties = self._infer_world_state_properties(user_message)
            if world_state_properties:
                updates.append(
                    {
                        "name": SYSTEM_WORLD_STATE_NAME,
                        "type": SYSTEM_WORLD_STATE_TYPE,
                        "aliases": ["world state", "neo world state", "session state"],
                        "metadata": {"workspace_name": SYSTEM_WORKSPACE_NAME, "is_core_world_state": True},
                        "default_properties": DEFAULT_WORLD_STATE_PROPERTIES,
                        "properties": world_state_properties,
                        "reason": "atualização de estado de contexto do Neo",
                        "confidence": 0.93,
                    }
                )

        if not updates and allow_confirmation_fallback and confirmation and assistant_response:
            artifact = " ".join((assistant_response or "").strip().split())[:320]
            if artifact:
                updates.append(
                    {
                        "name": SYSTEM_WORLD_STATE_NAME,
                        "type": SYSTEM_WORLD_STATE_TYPE,
                        "aliases": ["world state", "neo world state", "session state"],
                        "metadata": {"workspace_name": SYSTEM_WORKSPACE_NAME, "is_core_world_state": True},
                        "default_properties": DEFAULT_WORLD_STATE_PROPERTIES,
                        "properties": {"recent_events": [artifact]},
                        "reason": "confirmação explícita de memória sem classificação específica",
                        "confidence": 0.88,
                    }
                )
        return updates

    def _build_agent_training_updates(
        self,
        *,
        user_message: str,
        source_message_id: str | None,
        user_id: UUID | None,
    ) -> list[dict[str, Any]]:
        normalized = normalize_token(user_message)
        if not self._is_agent_training_intent(normalized):
            return []

        item = self._training_item(
            user_message=user_message,
            source_message_id=source_message_id,
            user_id=user_id,
        )
        updates: list[dict[str, Any]] = []

        if self._looks_like_identity_training(normalized):
            updates.append(
                {
                    "name": SYSTEM_PERSONALITY_NAME,
                    "type": SYSTEM_PERSONALITY_TYPE,
                    "aliases": ["personality", "neo identity", "neo persona"],
                    "metadata": {
                        "workspace_name": SYSTEM_WORKSPACE_NAME,
                        "collection_name": "identity",
                        "collection_type": SYSTEM_PERSONALITY_TYPE,
                        "is_core_identity": True,
                        "target_agent": SYSTEM_AGENT_NAME,
                        "target_workspace": SYSTEM_WORKSPACE_NAME,
                    },
                    "default_properties": DEFAULT_PERSONALITY_PROPERTIES,
                    "properties": {"training_rules": [item]},
                    "reason": "treinamento explícito de identidade/tom/persona do Neo",
                    "confidence": item["confidence"],
                }
            )

        if self._looks_like_procedure_training(normalized):
            updates.append(
                {
                    "name": SYSTEM_OPERATING_PROCEDURES_NAME,
                    "type": SYSTEM_OPERATING_PROCEDURES_TYPE,
                    "aliases": ["operating procedures", "when then procedures", "neo procedures"],
                    "metadata": {
                        "workspace_name": SYSTEM_WORKSPACE_NAME,
                        "collection_name": SYSTEM_PROCEDURES_COLLECTION_NAME,
                        "collection_type": SYSTEM_OPERATING_PROCEDURES_TYPE,
                        "is_operating_procedure_store": True,
                        "target_agent": SYSTEM_AGENT_NAME,
                        "target_workspace": SYSTEM_WORKSPACE_NAME,
                    },
                    "default_properties": DEFAULT_OPERATING_PROCEDURES_PROPERTIES,
                    "properties": {"procedures": [item]},
                    "reason": "treinamento explícito de procedimento operacional do Neo",
                    "confidence": item["confidence"],
                }
            )

        if self._looks_like_correction_training(normalized):
            updates.append(
                {
                    "name": f"training-example-{item['rule_key']}",
                    "type": SYSTEM_CORRECTION_EXAMPLE_TYPE,
                    "aliases": ["correction example", "bad good example", item["rule_key"]],
                    "metadata": {
                        "workspace_name": SYSTEM_WORKSPACE_NAME,
                        "collection_name": SYSTEM_TRAINING_EXAMPLES_COLLECTION_NAME,
                        "collection_type": SYSTEM_CORRECTION_EXAMPLE_TYPE,
                        "target_agent": SYSTEM_AGENT_NAME,
                        "target_workspace": SYSTEM_WORKSPACE_NAME,
                        "rule_key": item["rule_key"],
                    },
                    "default_properties": {"examples": []},
                    "properties": {"examples": [item]},
                    "reason": "exemplo/correção explícita fornecida pelo usuário",
                    "confidence": item["confidence"],
                }
            )
            updates.append(
                {
                    "name": f"learned-lesson-{item['rule_key']}",
                    "type": SYSTEM_LEARNED_LESSON_TYPE,
                    "aliases": ["learned lesson", "reflection", item["rule_key"]],
                    "metadata": {
                        "workspace_name": SYSTEM_WORKSPACE_NAME,
                        "collection_name": SYSTEM_REFLECTION_COLLECTION_NAME,
                        "collection_type": SYSTEM_LEARNED_LESSON_TYPE,
                        "target_agent": SYSTEM_AGENT_NAME,
                        "target_workspace": SYSTEM_WORKSPACE_NAME,
                        "rule_key": item["rule_key"],
                    },
                    "default_properties": {"lessons": []},
                    "properties": {"lessons": [item]},
                    "reason": "lição aprendida a partir de correção do usuário",
                    "confidence": item["confidence"],
                }
            )

        if not updates or self._looks_like_behavior_policy_training(normalized):
            updates.append(
                {
                    "name": SYSTEM_RESPONSE_RULES_NAME,
                    "type": SYSTEM_RESPONSE_RULES_TYPE,
                    "aliases": ["behavior policies", "response rules", "neo behavior rules"],
                    "metadata": {
                        "workspace_name": SYSTEM_WORKSPACE_NAME,
                        "collection_name": SYSTEM_BEHAVIOR_POLICIES_COLLECTION_NAME,
                        "collection_type": SYSTEM_RESPONSE_RULES_TYPE,
                        "is_behavior_policy_store": True,
                        "target_agent": SYSTEM_AGENT_NAME,
                        "target_workspace": SYSTEM_WORKSPACE_NAME,
                    },
                    "default_properties": DEFAULT_RESPONSE_RULES_PROPERTIES,
                    "properties": {"rules": [item]},
                    "reason": "treinamento explícito de regra global de comportamento do Neo",
                    "confidence": item["confidence"],
                }
            )

        return updates

    def _training_item(
        self,
        *,
        user_message: str,
        source_message_id: str | None,
        user_id: UUID | None,
    ) -> dict[str, Any]:
        description = " ".join((user_message or "").strip().split())[:800]
        rule_key = self._rule_key(description)
        examples_bad: list[str] = []
        examples_good: list[str] = []
        normalized = normalize_token(user_message)
        if any(token in normalized for token in ("errado", "nao aja assim", "não aja assim")):
            examples_bad.append(description)
        if "correto seria" in normalized:
            examples_good.append(description)

        return {
            "rule_key": rule_key,
            "description": description,
            "scope": "agent_behavior",
            "priority": "high",
            "source_message_id": source_message_id,
            "created_by_user": str(user_id) if user_id else "daniel",
            "target_agent": SYSTEM_AGENT_NAME,
            "target_workspace": SYSTEM_WORKSPACE_NAME,
            "examples_good": examples_good,
            "examples_bad": examples_bad,
            "confidence": 0.96,
        }

    def _rule_key(self, text: str) -> str:
        normalized = normalize_token(text)
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]
        words = normalized.split()[:5]
        slug = "_".join(words) if words else "training"
        return f"{slug}_{digest}"[:96]

    def _is_agent_training_intent(self, normalized: str) -> bool:
        if not normalized:
            return False
        markers = (
            "neo modo treino",
            "modo treino",
            "treine o neo",
            "treinar o neo",
            "neo deve",
            "quando acontecer",
            "quando isso acontecer",
            "quando ocorrer",
            "isso foi errado",
            "correto seria",
            "nao aja assim",
            "a partir de agora voce deve",
            "a partir de agora vc deve",
            "a partir de agora o neo deve",
        )
        return any(marker in normalized for marker in markers)

    def _looks_like_identity_training(self, normalized: str) -> bool:
        return any(token in normalized for token in ("personalidade", "tom", "estilo", "identidade", "persona"))

    def _looks_like_procedure_training(self, normalized: str) -> bool:
        return "quando" in normalized and any(token in normalized for token in ("faca", "faça", "deve", "responda", "use"))

    def _looks_like_correction_training(self, normalized: str) -> bool:
        return any(token in normalized for token in ("errado", "correto seria", "nao aja assim"))

    def _looks_like_behavior_policy_training(self, normalized: str) -> bool:
        return any(token in normalized for token in ("neo deve", "voce deve", "vc deve", "nao aja assim", "a partir de agora"))

    def _extract_assistant_communication_policy(self, text: str) -> dict[str, Any] | None:
        normalized = normalize_token(text)
        if not normalized:
            return None

        target_markers = (
            "voce",
            "vc",
            "neo",
            "assistente",
            "como voce responde",
            "estilo de resposta",
            "sua resposta",
            "suas respostas",
        )
        directive_markers = (
            "preferencia",
            "prefiro",
            "evite",
            "nao inclua",
            "nao incluir",
            "nao fale",
            "sem contexto",
            "responda",
            "responder",
            "ao menos que",
            "quando eu nao",
            "quando nao",
        )
        if not (
            any(marker in normalized for marker in target_markers)
            and any(marker in normalized for marker in directive_markers)
        ):
            return None

        examples: list[str] = []
        compact_text = " ".join((text or "").strip().split())
        if compact_text:
            examples.append(compact_text[:500])

        return {
            "type": "preference",
            "subtype": "assistant_communication_policy",
            "owner": "daniel",
            "owner_user_name": "Daniel Morais",
            "target_agent": SYSTEM_AGENT_NAME,
            "target_workspace": SYSTEM_WORKSPACE_NAME,
            "scope": "assistant_response_generation",
            "priority": "high",
            "rule_key": COMMUNICATION_POLICY_RULE_KEY,
            "rule_description": COMMUNICATION_POLICY_DESCRIPTION,
            "examples": examples or DEFAULT_COMMUNICATION_PREFS_PROPERTIES["examples"],
        }

    def _merge_structured_properties(
        self,
        current: dict[str, Any],
        incoming: dict[str, Any],
    ) -> dict[str, Any]:
        merged = {**current}
        for key, value in incoming.items():
            if key == "traits":
                existing = merged.get("traits")
                if not isinstance(existing, list):
                    existing = []
                incoming_traits = value if isinstance(value, list) else []
                merged["traits"] = sorted({str(item) for item in [*existing, *incoming_traits] if item})
            elif key in {"rules", "procedures", "examples", "lessons", "training_rules"}:
                merged[key] = self._merge_rule_items(merged.get(key), value)
            elif value is not None:
                merged[key] = value
        return merged

    def _merge_rule_items(self, existing: Any, incoming: Any) -> list[Any]:
        existing_items = existing if isinstance(existing, list) else []
        incoming_items = incoming if isinstance(incoming, list) else []
        by_key: dict[str, Any] = {}
        anonymous: list[Any] = []
        for item in [*existing_items, *incoming_items]:
            if isinstance(item, dict) and item.get("rule_key"):
                by_key[str(item["rule_key"])] = item
            elif item not in anonymous:
                anonymous.append(item)
        return [*anonymous, *by_key.values()]

    def _infer_personality_properties(self, text: str) -> dict[str, Any]:
        normalized = normalize_token(text)
        properties: dict[str, Any] = {
            "tone": "confident" if any(token in normalized for token in ("alpha", "confiante", "assertivo")) else None,
            "humor": "dry" if any(token in normalized for token in ("piadinha", "piadinhas", "ironia", "ironico")) else None,
            "communication_style": "direct" if any(token in normalized for token in ("alpha", "direto", "direta", "objetivo", "objetiva")) else None,
            "pressure_behavior": "calm" if any(token in normalized for token in ("calmo", "calma", "pressao", "caos", "leon")) else None,
            "sarcasm_level": 0.4 if any(token in normalized for token in ("piadinha", "piadinhas", "ironia", "sarcasmo", "leon")) else None,
            "inspiration": "Leon Kennedy energy" if "leon" in normalized and "kennedy" in normalized else None,
            "traits": [],
        }
        traits: list[str] = []
        if properties["tone"] == "confident":
            traits.append("confident")
        if properties["communication_style"] == "direct":
            traits.append("efficient")
        if properties["pressure_behavior"] == "calm":
            traits.append("calm_under_pressure")
        if properties["humor"]:
            traits.append("slightly_funny")
        properties["traits"] = traits
        return properties

    def _infer_goals_properties(self, text: str) -> dict[str, Any] | None:
        normalized = normalize_token(text)
        goal_tokens = ("meta", "metas", "objetivo", "objetivos", "goal", "prioridade", "foco")
        if not any(token in normalized for token in goal_tokens):
            return None
        entry = " ".join(text.strip().split())[:240]
        return {"active": [entry]}

    def _infer_profile_properties(self, text: str) -> dict[str, Any] | None:
        normalized = normalize_token(text)
        profile_tokens = ("sobre mim", "meu perfil", "lembra de mim", "me chama", "meu nome", "eu sou")
        if not any(token in normalized for token in profile_tokens):
            return None
        entry = " ".join(text.strip().split())[:240]
        return {"stable_facts": [entry]}

    def _infer_projects_properties(self, text: str) -> dict[str, Any] | None:
        normalized = normalize_token(text)
        project_tokens = ("projeto", "projetos", "feature", "roadmap", "sprint")
        if not any(token in normalized for token in project_tokens):
            return None
        entry = " ".join(text.strip().split())[:240]
        return {"items": [entry]}

    def _infer_world_state_properties(self, text: str) -> dict[str, Any] | None:
        normalized = normalize_token(text)
        state_tokens = ("estado atual", "contexto atual", "agora", "neste momento", "world state", "situação atual")
        if not any(token in normalized for token in state_tokens):
            return None
        entry = " ".join(text.strip().split())[:240]
        return {"recent_events": [entry]}

    async def _learn_from_confirmed_artifact(
        self,
        *,
        workspace_id: UUID,
        user_message: str,
        assistant_response: str,
        recent_messages: list[dict[str, str]] | None,
        source_message_id: str | None,
    ) -> list[dict[str, Any]]:
        if not self._is_memory_confirmation(user_message):
            return []
        logger.info("[PKS][plan_memory] confirmation_detected source_message_id=%s", source_message_id)
        artifact = self._find_confirmed_assistant_artifact(
            assistant_response=assistant_response,
            recent_messages=recent_messages,
        )
        if not artifact or not self._looks_like_workout_plan(artifact):
            logger.info("[PKS][plan_memory] confirmation_without_supported_artifact")
            return []

        async with self.db.get_session() as session:
            primary_entity = await self.repository.get_primary_user_entity(session, workspace_id)
            if primary_entity is None:
                logger.info("[PKS][plan_memory] confirmation_without_primary_entity workspace_id=%s", workspace_id)
                return []
            applied = await self._apply_confirmed_workout_plan(
                session,
                workspace_id=workspace_id,
                primary_entity=primary_entity,
                plan_text=artifact,
                source_message_id=source_message_id,
            )
            if applied:
                await session.commit()
            logger.info("[PKS][plan_memory] confirmation_applied operations=%d", len(applied))
            return applied

    def _find_confirmed_assistant_artifact(
        self,
        *,
        assistant_response: str,
        recent_messages: list[dict[str, str]] | None,
    ) -> str | None:
        candidates: list[str] = []
        if assistant_response:
            candidates.append(assistant_response)
        for message in reversed(recent_messages or []):
            if message.get("role") != "assistant":
                continue
            content = (message.get("content") or "").strip()
            if content:
                candidates.append(content)
        for candidate in candidates:
            if self._looks_like_workout_plan(candidate):
                return candidate
        return candidates[0] if candidates else None

    async def _plan_operations(
        self,
        *,
        user_message: str,
        assistant_response: str,
        pks_context: str,
        recent_history: str,
    ) -> list[PKSOperation]:
        return await self.memory_planner.plan(
            user_message=user_message,
            assistant_response=assistant_response,
            pks_context=pks_context,
            recent_history=recent_history,
            max_operations=MAX_PLANNER_OPERATIONS,
        )

    async def _apply_direct_fact_writes(
        self,
        session,
        *,
        workspace_id: UUID,
        user_message: str,
        source_message_id: str | None,
    ) -> dict[str, Any]:
        primary_entity = await self.repository.get_primary_user_entity(session, workspace_id)
        if primary_entity is None:
            return {"applied": False, "confirm_profile_save": False}

        normalized = " ".join((user_message or "").strip().split())
        summary = {
            "applied": False,
            "confirm_profile_save": False,
            "saved_name": None,
            "operations": [],
        }

        direct_name = self._extract_name_statement(normalized)
        if direct_name:
            first_time_name = primary_entity.name == "Usuário principal" or (
                await self.repository.get_field(session, primary_entity.id, "full_name")
            ) is None
            record, before = await self.repository.update_entity_identity(
                session,
                entity_id=primary_entity.id,
                name=direct_name,
                aliases=[direct_name.split()[0], "eu", "usuário principal"],
                metadata={"is_primary_user": True},
                source="conversation",
                source_message_id=source_message_id,
                confidence=0.99,
            )
            await self.audit.log(
                session,
                workspace_id=workspace_id,
                actor_type="agent",
                actor_id="pks_writer",
                action="upsert_entity_identity",
                target_type="entity",
                target_id=record.id,
                before=before,
                after=record.model_dump(mode="json"),
                reason="declaração direta de nome do usuário",
                confidence=0.99,
                source_message_id=source_message_id,
            )
            field_record, field_before = await self.repository.upsert_field(
                session,
                UpsertFieldInput(
                    entity_id=record.id,
                    key="full_name",
                    value_type="text",
                    value=direct_name,
                    confidence=0.99,
                    sensitivity="private",
                    source="conversation",
                    source_message_id=source_message_id,
                    actor="agent",
                ),
            )
            await self.audit.log(
                session,
                workspace_id=workspace_id,
                actor_type="agent",
                actor_id="pks_writer",
                action="upsert_field",
                target_type="field",
                target_id=field_record.id,
                before=field_before,
                after=field_record.model_dump(mode="json"),
                reason="salvar nome completo do usuário",
                confidence=0.99,
                source_message_id=source_message_id,
            )
            await self.repository.update_workspace_name(session, workspace_id, direct_name)
            summary["applied"] = True
            summary["confirm_profile_save"] = first_time_name
            summary["saved_name"] = direct_name
            summary["operations"].extend(["upsert_entity_identity", "upsert_field:full_name"])
            primary_entity = record

        device_fact = self._extract_device_statement(normalized)
        if device_fact:
            device_entity, _ = await self.repository.upsert_entity(
                session,
                UpsertEntityInput(
                    workspace_id=workspace_id,
                    name=device_fact["name"],
                    type="device",
                    aliases=device_fact["aliases"],
                    space_id=await self._space_id(session, workspace_id, "Dispositivos", "device"),
                    confidence=0.95,
                    source="conversation",
                    source_message_id=source_message_id,
                ),
            )
            field_record, field_before = await self.repository.upsert_field(
                session,
                UpsertFieldInput(
                    entity_id=device_entity.id,
                    key=device_fact["field_key"],
                    value_type="text",
                    value=device_fact["field_value"],
                    confidence=0.95,
                    sensitivity="private",
                    source="conversation",
                    source_message_id=source_message_id,
                    actor="agent",
                ),
            )
            await self.audit.log(
                session,
                workspace_id=workspace_id,
                actor_type="agent",
                actor_id="pks_writer",
                action="upsert_field",
                target_type="field",
                target_id=field_record.id,
                before=field_before,
                after=field_record.model_dump(mode="json"),
                reason="declaração direta sobre dispositivo do usuário",
                confidence=0.95,
                source_message_id=source_message_id,
            )
            relation_record, relation_before = await self.repository.upsert_relation(
                session,
                UpsertRelationInput(
                    workspace_id=workspace_id,
                    from_entity_id=primary_entity.id,
                    relation_type="owns",
                    to_entity_id=device_entity.id,
                    confidence=0.95,
                    sensitivity="private",
                    source="conversation",
                    source_message_id=source_message_id,
                ),
            )
            await self.audit.log(
                session,
                workspace_id=workspace_id,
                actor_type="agent",
                actor_id="pks_writer",
                action="upsert_relation",
                target_type="relation",
                target_id=relation_record.id,
                before=relation_before,
                after=relation_record.model_dump(mode="json"),
                reason="vincular dispositivo ao usuário",
                confidence=0.95,
                source_message_id=source_message_id,
            )
            summary["applied"] = True
            summary["operations"].append("upsert_device_fact")

        relative_fact = self._extract_relative_statement(normalized)
        if relative_fact:
            person_entity, _ = await self.repository.upsert_entity(
                session,
                UpsertEntityInput(
                    workspace_id=workspace_id,
                    name=relative_fact["name"],
                    type="person",
                    aliases=[relative_fact["name"].split()[0], relative_fact["role"]],
                    space_id=await self._space_id(session, workspace_id, "Pessoas", "person"),
                    confidence=0.95,
                    source="conversation",
                    source_message_id=source_message_id,
                    metadata={"relationship_role": relative_fact["role"]},
                ),
            )
            relation_type = relative_fact["relation_type"]
            relation_record, relation_before = await self.repository.upsert_relation(
                session,
                UpsertRelationInput(
                    workspace_id=workspace_id,
                    from_entity_id=person_entity.id if relation_type == "sibling_of" else primary_entity.id,
                    relation_type=relation_type,
                    to_entity_id=primary_entity.id if relation_type == "sibling_of" else person_entity.id,
                    confidence=0.95,
                    sensitivity="private",
                    source="conversation",
                    source_message_id=source_message_id,
                ),
            )
            await self.audit.log(
                session,
                workspace_id=workspace_id,
                actor_type="agent",
                actor_id="pks_writer",
                action="upsert_relation",
                target_type="relation",
                target_id=relation_record.id,
                before=relation_before,
                after=relation_record.model_dump(mode="json"),
                reason="declaração direta sobre parente",
                confidence=0.95,
                source_message_id=source_message_id,
            )
            summary["applied"] = True
            summary["operations"].append("upsert_relative_fact")

        entertainment_habit = self._extract_entertainment_habit(normalized)
        if entertainment_habit:
            saved_any = False
            for key, value in entertainment_habit["fields"].items():
                record = await self._upsert_field_logged(
                    session,
                    workspace_id=workspace_id,
                    entity_id=primary_entity.id,
                    key=key,
                    value_type="text",
                    value=value,
                    confidence=0.92,
                    source_message_id=source_message_id,
                    reason="usuário informou hábito estável de entretenimento",
                )
                if record is not None:
                    saved_any = True
                    summary["operations"].append(f"upsert_field:{key}")
            if saved_any:
                summary["applied"] = True

        workout_summary = await self._apply_workout_direct_writes(
            session,
            workspace_id=workspace_id,
            primary_entity=primary_entity,
            user_message=normalized,
            source_message_id=source_message_id,
        )
        if workout_summary["applied"]:
            summary["applied"] = True
            summary["operations"].extend(workout_summary["operations"])

        return summary

    async def _apply_workout_direct_writes(
        self,
        session,
        *,
        workspace_id: UUID,
        primary_entity,
        user_message: str,
        source_message_id: str | None,
    ) -> dict[str, Any]:
        normalized = normalize_token(user_message)
        if not self._looks_like_workout_fact(normalized):
            return {"applied": False, "operations": []}

        operations: list[str] = []
        academy_space_id = await self._space_id(session, workspace_id, "Academia", "workout")
        routine = await self._upsert_entity_logged(
            session,
            workspace_id=workspace_id,
            name="Plano de treino de academia",
            entity_type="routine",
            aliases=["plano de treino", "treino split"],
            space_id=academy_space_id,
            confidence=0.95,
            source_message_id=source_message_id,
            reason="usuário falou sobre plano de treino de academia",
        )
        operations.append("upsert_workout_routine")
        await self._upsert_relation_logged(
            session,
            workspace_id=workspace_id,
            from_entity_id=primary_entity.id,
            relation_type="works_on",
            to_entity_id=routine.id,
            confidence=0.95,
            source_message_id=source_message_id,
            reason="vincular usuário ao plano de treino",
        )
        operations.append("upsert_relation:works_on")

        split = self._extract_weekly_split(user_message)
        if split:
            await self._upsert_field_logged(
                session,
                workspace_id=workspace_id,
                entity_id=routine.id,
                key="routine_type",
                value_type="text",
                value="split",
                confidence=0.95,
                source_message_id=source_message_id,
                reason="usuário descreveu treino split semanal",
            )
            operations.append("upsert_field:routine_type")
            if len(split) >= 2:
                await self._upsert_field_logged(
                    session,
                    workspace_id=workspace_id,
                    entity_id=routine.id,
                    key="frequency_per_week",
                    value_type="number",
                    value=len(split),
                    confidence=0.95,
                    source_message_id=source_message_id,
                    reason="usuário descreveu quantidade de dias de treino",
                )
                operations.append("upsert_field:frequency_per_week")
            for day_key, groups in split.items():
                workout = await self._ensure_day_workout(
                    session,
                    workspace_id=workspace_id,
                    academy_space_id=academy_space_id,
                    routine_id=routine.id,
                    day_key=day_key,
                    source_message_id=source_message_id,
                )
                operations.append(f"upsert_workout:{day_key}")
                for group_name in groups:
                    group = await self._upsert_entity_logged(
                        session,
                        workspace_id=workspace_id,
                        name=group_name,
                        entity_type="muscle_group",
                        aliases=[],
                        space_id=academy_space_id,
                        confidence=0.95,
                        source_message_id=source_message_id,
                        reason="usuário descreveu grupo muscular no treino semanal",
                    )
                    await self._upsert_relation_logged(
                        session,
                        workspace_id=workspace_id,
                        from_entity_id=workout.id,
                        relation_type="targets",
                        to_entity_id=group.id,
                        confidence=0.95,
                        source_message_id=source_message_id,
                        reason="vincular treino do dia ao grupo muscular",
                    )
                    operations.append(f"target:{day_key}:{group_name}")

        exercise_specs = self._extract_workout_exercises(user_message)
        if exercise_specs:
            day_key = "monday" if "segunda" in normalized else "treino"
            workout = await self._ensure_day_workout(
                session,
                workspace_id=workspace_id,
                academy_space_id=academy_space_id,
                routine_id=routine.id,
                day_key=day_key,
                source_message_id=source_message_id,
            )
            for spec in exercise_specs:
                exercise = await self._upsert_entity_logged(
                    session,
                    workspace_id=workspace_id,
                    name=spec["name"],
                    entity_type="exercise",
                    aliases=spec.get("aliases", []),
                    space_id=academy_space_id,
                    confidence=spec.get("confidence", 0.92),
                    source_message_id=source_message_id,
                    reason="usuário descreveu exercício do treino",
                )
                await self._upsert_relation_logged(
                    session,
                    workspace_id=workspace_id,
                    from_entity_id=workout.id,
                    relation_type="has_exercise",
                    to_entity_id=exercise.id,
                    confidence=spec.get("confidence", 0.92),
                    source_message_id=source_message_id,
                    reason="vincular exercício ao treino do dia",
                )
                for key, value in spec.get("fields", {}).items():
                    unit = "kg" if key == "weight_kg" else None
                    value_type = "number" if isinstance(value, (int, float)) else "text"
                    await self._upsert_field_logged(
                        session,
                        workspace_id=workspace_id,
                        entity_id=exercise.id,
                        key=key,
                        value_type=value_type,
                        value=value,
                        unit=unit,
                        confidence=spec.get("confidence", 0.92),
                        source_message_id=source_message_id,
                        reason="salvar parâmetros do exercício",
                    )
                operations.append(f"upsert_exercise:{spec['name']}")

        return {"applied": bool(operations), "operations": operations}

    async def _apply_confirmed_workout_plan(
        self,
        session,
        *,
        workspace_id: UUID,
        primary_entity,
        plan_text: str,
        source_message_id: str | None,
    ) -> list[dict[str, Any]]:
        sections = self._extract_workout_plan_sections(plan_text)
        if not sections:
            return []

        academy_space_id = await self._space_id(session, workspace_id, "Academia", "workout")
        routine = await self._upsert_entity_logged(
            session,
            workspace_id=workspace_id,
            name="Plano de treino de academia",
            entity_type="routine",
            aliases=["plano de treino", "treino split", "treino abcde"],
            space_id=academy_space_id,
            confidence=0.99,
            source_message_id=source_message_id,
            reason="usuário confirmou salvar plano de treino na memória",
        )
        await self._upsert_relation_logged(
            session,
            workspace_id=workspace_id,
            from_entity_id=primary_entity.id,
            relation_type="works_on",
            to_entity_id=routine.id,
            confidence=0.99,
            source_message_id=source_message_id,
            reason="vincular usuário ao plano de treino confirmado",
        )
        await self._upsert_field_logged(
            session,
            workspace_id=workspace_id,
            entity_id=routine.id,
            key="routine_type",
            value_type="text",
            value="split",
            confidence=0.99,
            source_message_id=source_message_id,
            reason="plano confirmado de treino split",
        )
        await self._upsert_field_logged(
            session,
            workspace_id=workspace_id,
            entity_id=routine.id,
            key="frequency_per_week",
            value_type="number",
            value=len(sections),
            confidence=0.99,
            source_message_id=source_message_id,
            reason="plano confirmado com frequência semanal definida",
        )

        applied: list[dict[str, Any]] = [
            {"op": "upsert_entity", "entity_id": str(routine.id), "name": routine.name, "type": routine.type},
        ]
        for section in sections:
            workout = await self._ensure_day_workout(
                session,
                workspace_id=workspace_id,
                academy_space_id=academy_space_id,
                routine_id=routine.id,
                day_key=section["day_key"],
                source_message_id=source_message_id,
            )
            applied.append({"op": "upsert_entity", "entity_id": str(workout.id), "name": workout.name, "type": workout.type})
            for group_name in section["muscle_groups"]:
                group = await self._upsert_entity_logged(
                    session,
                    workspace_id=workspace_id,
                    name=group_name,
                    entity_type="muscle_group",
                    aliases=[],
                    space_id=academy_space_id,
                    confidence=0.99,
                    source_message_id=source_message_id,
                    reason="grupo muscular extraído de plano de treino confirmado",
                )
                await self._upsert_relation_logged(
                    session,
                    workspace_id=workspace_id,
                    from_entity_id=workout.id,
                    relation_type="targets",
                    to_entity_id=group.id,
                    confidence=0.99,
                    source_message_id=source_message_id,
                    reason="vincular treino confirmado ao grupo muscular",
                )
            for exercise_spec in section["exercises"]:
                exercise = await self._upsert_entity_logged(
                    session,
                    workspace_id=workspace_id,
                    name=exercise_spec["name"],
                    entity_type="exercise",
                    aliases=[],
                    space_id=academy_space_id,
                    confidence=0.99,
                    source_message_id=source_message_id,
                    reason="exercício extraído de plano de treino confirmado",
                )
                await self._upsert_relation_logged(
                    session,
                    workspace_id=workspace_id,
                    from_entity_id=workout.id,
                    relation_type="has_exercise",
                    to_entity_id=exercise.id,
                    confidence=0.99,
                    source_message_id=source_message_id,
                    reason="vincular exercício ao treino confirmado",
                )
                for field_key, payload in exercise_spec["fields"].items():
                    await self._upsert_field_logged(
                        session,
                        workspace_id=workspace_id,
                        entity_id=exercise.id,
                        key=field_key,
                        value_type=payload["value_type"],
                        value=payload["value"],
                        unit=payload.get("unit"),
                        confidence=0.99,
                        source_message_id=source_message_id,
                        reason="parâmetro extraído de plano de treino confirmado",
                    )
                applied.append({"op": "upsert_entity", "entity_id": str(exercise.id), "name": exercise.name, "type": exercise.type})
        return applied

    def _looks_like_workout_fact(self, normalized: str) -> bool:
        tokens = (
            "academia", "treino", "exercicio", "exercicios", "supino", "crucifixo",
            "crusifixo", "triceps", "biceps", "peito", "costa", "costas", "perna",
            "ombro", "quilos", "reps", "sessoes", "series",
        )
        weekdays = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")
        return any(t in normalized for t in tokens) or any(d in normalized for d in weekdays)

    def _looks_like_workout_plan(self, text: str) -> bool:
        normalized = normalize_token(text)
        return "treino abcde" in normalized or "segunda feira" in normalized and "sexta feira" in normalized

    def _is_memory_confirmation(self, text: str) -> bool:
        normalized = normalize_token(text)
        confirmations = (
            "salva so na sua memoria",
            "salva na sua memoria",
            "salva isso na memoria",
            "salva na memoria",
            "salva isso",
            "pode salvar",
            "pode salvar isso",
            "guarda na memoria",
            "guarda isso",
            "lembra disso",
            "memoriza isso",
            "vou fazer esse",
            "vou fazer esses",
            "esse sera meu treino",
            "esse vai ser meu treino",
        )
        return any(phrase in normalized for phrase in confirmations)

    def _is_system_memory_intent(self, text: str) -> bool:
        normalized = normalize_token(text)
        if not normalized:
            return False
        intents = (
            "salva",
            "guarda",
            "lembra",
            "memoriza",
            "na sua memoria",
            "no seu workspace",
            "quero que voce lembre",
            "quero que voce guarde",
            "anota isso",
        )
        return any(token in normalized for token in intents)

    def _extract_system_style_preference(self, text: str) -> str | None:
        normalized = normalize_token(text)
        if not normalized:
            return None
        if not any(
            marker in normalized
            for marker in (
                "personalidade",
                "estilo",
                "tom",
                "forma de falar",
                "responda",
                "fale",
                "seja",
            )
        ):
            return None
        if not any(
            cue in normalized
            for cue in (
                "quero que voce",
                "quero que vc",
                "a partir de agora",
                "daqui pra frente",
                "prefiro que voce",
                "seja mais",
                "fale mais",
                "responda mais",
            )
        ):
            return None
        return "Estilo do Neo: " + " ".join((text or "").strip().split())[:500]

    def _extract_weekly_split(self, text: str) -> dict[str, list[str]]:
        day_pattern = re.compile(r"\bna\s+(segunda|terça|terca|quarta|quinta|sexta|sábado|sabado|domingo)\b", flags=re.IGNORECASE)
        split: dict[str, list[str]] = {}
        matches = list(day_pattern.finditer(text))
        for index, match in enumerate(matches):
            day = self._canonical_weekday(match.group(1))
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            segment = text[match.end():end]
            segment = re.sub(r"^\s*(?:eu\s+)?(?:faço|faco)\s+", "", segment, flags=re.IGNORECASE)
            groups = self._extract_muscle_groups(segment)
            if day and groups:
                split[day] = groups
        return split

    def _extract_muscle_groups(self, text: str) -> list[str]:
        normalized = normalize_token(text).replace("mix entre", " ").replace(" e ", " ")
        mapping = {
            "peito": "Peito",
            "triceps": "Tríceps",
            "biceps": "Bíceps",
            "costa": "Costas",
            "costas": "Costas",
            "perna": "Perna",
            "pernas": "Perna",
            "ombro": "Ombro",
            "ombros": "Ombro",
        }
        found: list[str] = []
        for token, canonical in mapping.items():
            if re.search(rf"\b{re.escape(token)}\b", normalized) and canonical not in found:
                found.append(canonical)
        return found

    def _extract_workout_exercises(self, text: str) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        normalized = normalize_token(text)

        supino = re.search(
            r"(?:faço|faco)?\s*(?:(\d+)\s+)?supino\s+com\s+(\d+)\s+quilos?.*?(?:faço|faco)\s+(\d+)\s+s[eé]ss(?:o|õ)es?\s+de\s+(\d+)\s+reps?",
            text,
            flags=re.IGNORECASE,
        )
        if supino:
            fields: dict[str, Any] = {
                "weight_kg": int(supino.group(2)),
                "sets": int(supino.group(3)),
                "reps": int(supino.group(4)),
            }
            if supino.group(1):
                fields["variant_count"] = int(supino.group(1))
            specs.append({"name": "Supino", "aliases": [], "fields": fields, "confidence": 0.95})

        crucifixo = re.search(
            r"cru[cs]ifixo\s+com\s+(\d+)\s+quilos?.*?(\d+)\s+reps?\s+em\s+(\d+)\s+s(?:e|é)?ss(?:a|ã|o|õ)es?",
            text,
            flags=re.IGNORECASE,
        )
        if crucifixo:
            specs.append({
                "name": "Crucifixo",
                "aliases": ["crusifixo"],
                "fields": {
                    "weight_kg": int(crucifixo.group(1)),
                    "reps": int(crucifixo.group(2)),
                    "sets": int(crucifixo.group(3)),
                },
                "confidence": 0.94,
            })

        triceps_corda = re.search(
            r"tr[ií]ceps.*?(?:pull|puxada).*?corda.*?com\s+(\d+)\s+quilos?.*?(\d+)\s+rep(?:s)?.*?em\s+(\d+)\s+s(?:e|é)?ss(?:a|ã|o|õ)es?",
            text,
            flags=re.IGNORECASE,
        )
        if triceps_corda:
            specs.append({
                "name": "Tríceps corda",
                "aliases": ["pull com corda", "puxada com corda"],
                "fields": {
                    "weight_kg": int(triceps_corda.group(1)),
                    "reps": int(triceps_corda.group(2)),
                    "sets": int(triceps_corda.group(3)),
                },
                "confidence": 0.92,
            })

        if "trceps frances" in normalized or "triceps frances" in normalized:
            specs.append({
                "name": "Tríceps francês crossover",
                "aliases": ["tríceps francês", "trceps frances", "crossover"],
                "fields": {
                    "sets": 4,
                    "reps": 15,
                    "weight_note": "peso menor não especificado",
                },
                "confidence": 0.86,
            })

        return specs

    def _extract_workout_plan_sections(self, text: str) -> list[dict[str, Any]]:
        heading_pattern = re.compile(
            r"^##\s*(Segunda-feira|Terça-feira|Terca-feira|Quarta-feira|Quinta-feira|Sexta-feira|Sábado|Sabado|Domingo)\s*[—-]\s*(.+)$",
            flags=re.IGNORECASE | re.MULTILINE,
        )
        matches = list(heading_pattern.finditer(text))
        sections: list[dict[str, Any]] = []
        for index, match in enumerate(matches):
            day_label = match.group(1)
            title = match.group(2).strip()
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = text[start:end]
            exercises = self._extract_plan_table_exercises(body)
            if not exercises:
                continue
            day_key = self._canonical_weekday(day_label)
            muscle_groups = self._extract_muscle_groups(title)
            if not muscle_groups:
                muscle_groups = self._extract_muscle_groups(body)
            sections.append(
                {
                    "day_key": day_key,
                    "label": day_label,
                    "muscle_groups": muscle_groups,
                    "exercises": exercises,
                }
            )
        return sections

    def _extract_plan_table_exercises(self, section_text: str) -> list[dict[str, Any]]:
        exercises: list[dict[str, Any]] = []
        row_pattern = re.compile(r"^\|\s*([^|\n]+?)\s*\|\s*([^|\n]*)\s*\|\s*([^|\n]*)\s*\|\s*([^|\n]*)\s*\|?\s*$", re.MULTILINE)
        for match in row_pattern.finditer(section_text):
            raw_name = self._clean_table_cell(match.group(1))
            if not raw_name:
                continue
            lowered = normalize_token(raw_name)
            if lowered in {"exercicio", "series", "repeticoes", "descanso"}:
                continue
            if set(lowered) == {"-"}:
                continue
            if lowered in {"biceps", "triceps"}:
                continue

            fields: dict[str, dict[str, Any]] = {}
            sets_value = self._clean_table_cell(match.group(2))
            reps_value = self._clean_table_cell(match.group(3))
            rest_value = self._clean_table_cell(match.group(4))
            if sets_value.isdigit():
                fields["sets"] = {"value_type": "number", "value": int(sets_value)}
            elif sets_value:
                fields["sets"] = {"value_type": "text", "value": sets_value}
            if reps_value:
                fields["reps"] = {"value_type": "text", "value": reps_value}
            if rest_value:
                fields["rest_interval"] = {"value_type": "text", "value": rest_value}
            exercises.append({"name": raw_name, "fields": fields})
        return exercises

    def _clean_table_cell(self, value: str) -> str:
        cleaned = re.sub(r"\*\*", "", value or "")
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
        return cleaned

    async def _ensure_day_workout(
        self,
        session,
        *,
        workspace_id: UUID,
        academy_space_id: UUID,
        routine_id: UUID,
        day_key: str,
        source_message_id: str | None,
    ):
        label = self._weekday_label(day_key)
        workout = await self._upsert_entity_logged(
            session,
            workspace_id=workspace_id,
            name=f"Treino de {label.lower()}",
            entity_type="workout",
            aliases=[day_key, label],
            space_id=academy_space_id,
            confidence=0.95,
            source_message_id=source_message_id,
            reason="usuário descreveu treino semanal",
        )
        await self._upsert_field_logged(
            session,
            workspace_id=workspace_id,
            entity_id=workout.id,
            key="weekday",
            value_type="text",
            value=day_key,
            confidence=0.95,
            source_message_id=source_message_id,
            reason="salvar dia da semana do treino",
        )
        await self._upsert_field_logged(
            session,
            workspace_id=workspace_id,
            entity_id=workout.id,
            key="label",
            value_type="text",
            value=label,
            confidence=0.95,
            source_message_id=source_message_id,
            reason="salvar rótulo do treino",
        )
        await self._upsert_relation_logged(
            session,
            workspace_id=workspace_id,
            from_entity_id=routine_id,
            relation_type="has_member",
            to_entity_id=workout.id,
            confidence=0.95,
            source_message_id=source_message_id,
            reason="vincular treino do dia ao plano semanal",
        )
        return workout

    def _canonical_weekday(self, value: str) -> str:
        token = normalize_token(value)
        return {
            "segunda": "monday",
            "segunda feira": "monday",
            "monday": "monday",
            "terca": "tuesday",
            "terça": "tuesday",
            "terca feira": "tuesday",
            "terça feira": "tuesday",
            "tuesday": "tuesday",
            "quarta": "wednesday",
            "quarta feira": "wednesday",
            "wednesday": "wednesday",
            "quinta": "thursday",
            "quinta feira": "thursday",
            "thursday": "thursday",
            "sexta": "friday",
            "sexta feira": "friday",
            "friday": "friday",
            "sabado": "saturday",
            "sábado": "saturday",
            "sabado feira": "saturday",
            "sábado feira": "saturday",
            "saturday": "saturday",
            "domingo": "sunday",
            "sunday": "sunday",
        }.get(token, token)

    def _weekday_label(self, day_key: str) -> str:
        return {
            "monday": "Segunda-feira",
            "tuesday": "Terça-feira",
            "wednesday": "Quarta-feira",
            "thursday": "Quinta-feira",
            "friday": "Sexta-feira",
            "saturday": "Sábado",
            "sunday": "Domingo",
            "treino": "Treino",
        }.get(day_key, day_key.title())

    async def _upsert_entity_logged(
        self,
        session,
        *,
        workspace_id: UUID,
        name: str,
        entity_type: str,
        aliases: list[str],
        space_id: UUID,
        confidence: float,
        source_message_id: str | None,
        reason: str,
    ):
        record, before = await self.repository.upsert_entity(
            session,
            UpsertEntityInput(
                workspace_id=workspace_id,
                name=name,
                type=entity_type,
                aliases=aliases,
                space_id=space_id,
                confidence=confidence,
                sensitivity="private",
                source="conversation",
                source_message_id=source_message_id,
            ),
        )
        await self.audit.log(
            session,
            workspace_id=workspace_id,
            actor_type="agent",
            actor_id="pks_writer",
            action="upsert_entity",
            target_type="entity",
            target_id=record.id,
            before=before,
            after=record.model_dump(mode="json"),
            reason=reason,
            confidence=confidence,
            source_message_id=source_message_id,
        )
        return record

    async def _upsert_field_logged(
        self,
        session,
        *,
        workspace_id: UUID,
        entity_id: UUID,
        key: str,
        value_type: str,
        value: Any,
        confidence: float,
        source_message_id: str | None,
        reason: str,
        unit: str | None = None,
    ):
        value_type, value, unit = self._normalize_field_payload(
            key=key,
            value_type=value_type,
            value=value,
            unit=unit,
        )
        if await self._should_skip_field_update(session, entity_id, key, value):
            return None

        record, before = await self.repository.upsert_field(
            session,
            UpsertFieldInput(
                entity_id=entity_id,
                key=key,
                value_type=value_type,
                value=value,
                unit=unit,
                confidence=confidence,
                sensitivity="private",
                source="conversation",
                source_message_id=source_message_id,
                actor="agent",
            ),
        )
        await self.audit.log(
            session,
            workspace_id=workspace_id,
            actor_type="agent",
            actor_id="pks_writer",
            action="upsert_field",
            target_type="field",
            target_id=record.id,
            before=before,
            after=record.model_dump(mode="json"),
            reason=reason,
            confidence=confidence,
            source_message_id=source_message_id,
        )
        return record

    def _normalize_field_payload(
        self,
        *,
        key: str,
        value_type: str,
        value: Any,
        unit: str | None,
    ) -> tuple[str, Any, str | None]:
        if key == "weekday" and value is not None:
            return "text", self._canonical_weekday(str(value)), unit
        if key == "frequency_per_week" and value is not None:
            try:
                return "number", int(value), unit
            except (TypeError, ValueError):
                return value_type, value, unit
        if key == "birthday" and value is not None:
            normalized = self._normalize_birthday_value(value)
            if normalized is not None:
                return "date", normalized, unit
        if key == "weight_kg":
            return "number", value, unit or "kg"
        return value_type, value, unit

    def _normalize_birthday_value(self, value: Any) -> date | datetime | None:
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return value
        if value is None:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        lowered = normalize_token(raw)
        month_names = {
            "janeiro": 1,
            "fevereiro": 2,
            "marco": 3,
            "abril": 4,
            "maio": 5,
            "junho": 6,
            "julho": 7,
            "agosto": 8,
            "setembro": 9,
            "outubro": 10,
            "novembro": 11,
            "dezembro": 12,
        }
        match = re.search(r"\b(\d{1,2})\s+de\s+([a-z]+)\b", lowered)
        if match and match.group(2) in month_names:
            return date(1900, month_names[match.group(2)], int(match.group(1)))

        numeric = re.search(r"(\d{1,2})\D+(\d{1,2})(?:\D+(\d{4}))?$", raw)
        if not numeric:
            return None
        first = int(numeric.group(1))
        second = int(numeric.group(2))
        year = int(numeric.group(3)) if numeric.group(3) else 1900
        if first > 12:
            day, month = first, second
        elif second > 12:
            month, day = first, second
        else:
            if "/" in raw:
                day, month = first, second
            else:
                month, day = first, second
        try:
            return date(year, month, day)
        except ValueError:
            return None

    async def _should_skip_field_update(self, session, entity_id: UUID, key: str, new_value: Any) -> bool:
        if key != "frequency_per_week":
            return False
        existing = await self.repository.get_field(session, entity_id, key)
        if existing is None or existing.value is None:
            return False
        try:
            return int(existing.value) > int(new_value)
        except (TypeError, ValueError):
            return False

    async def _upsert_relation_logged(
        self,
        session,
        *,
        workspace_id: UUID,
        from_entity_id: UUID,
        relation_type: str,
        to_entity_id: UUID,
        confidence: float,
        source_message_id: str | None,
        reason: str,
    ):
        record, before = await self.repository.upsert_relation(
            session,
            UpsertRelationInput(
                workspace_id=workspace_id,
                from_entity_id=from_entity_id,
                relation_type=relation_type,
                to_entity_id=to_entity_id,
                confidence=confidence,
                sensitivity="private",
                source="conversation",
                source_message_id=source_message_id,
            ),
        )
        await self.audit.log(
            session,
            workspace_id=workspace_id,
            actor_type="agent",
            actor_id="pks_writer",
            action="upsert_relation",
            target_type="relation",
            target_id=record.id,
            before=before,
            after=record.model_dump(mode="json"),
            reason=reason,
            confidence=confidence,
            source_message_id=source_message_id,
        )
        return record

    async def _apply_operation(
        self,
        session,
        *,
        workspace_id: UUID,
        op: PKSOperation,
        source_message_id: str | None,
    ) -> dict[str, Any] | None:
        if op.op == "upsert_entity":
            return await self._apply_upsert_entity(session, workspace_id, op, source_message_id)
        if op.op == "upsert_field":
            return await self._apply_upsert_field(session, workspace_id, op, source_message_id)
        if op.op == "upsert_relation":
            return await self._apply_upsert_relation(session, workspace_id, op, source_message_id)
        if op.op == "soft_delete_entity":
            return await self._apply_soft_delete_entity(session, workspace_id, op, source_message_id)
        if op.op == "expire_relation":
            return None
        return None

    async def _apply_upsert_entity(self, session, workspace_id: UUID, op: PKSOperation, source_message_id: str | None):
        entity = op.entity or {}
        entity_type = self.validator.entity_type(entity.get("type"))
        name = str(entity.get("name") or "").strip()
        if not name or not entity_type:
            return None
        entity_type = self._canonical_entity_type(name, entity_type)

        space_id = await self._space_id(session, workspace_id, entity.get("space"), entity_type)
        collection_id = None
        if entity.get("collection"):
            collection_id = await self.repository.ensure_collection(
                session,
                workspace_id,
                space_id,
                str(entity["collection"]),
                entity_type,
            )
        record, before = await self.repository.upsert_entity(
            session,
            UpsertEntityInput(
                workspace_id=workspace_id,
                name=name,
                type=entity_type,
                aliases=[str(a) for a in entity.get("aliases") or []],
                space_id=space_id,
                collection_id=collection_id,
                metadata=entity.get("metadata") or {},
                external_refs=entity.get("external_refs") or {},
                confidence=op.confidence,
                sensitivity=self.validator.sensitivity(entity.get("sensitivity")),
                source="conversation",
                source_message_id=source_message_id,
            ),
        )
        await self.audit.log(
            session,
            workspace_id=workspace_id,
            actor_type="agent",
            actor_id="pks_writer",
            action="upsert_entity",
            target_type="entity",
            target_id=record.id,
            before=before,
            after=record.model_dump(mode="json"),
            reason=op.reason,
            confidence=op.confidence,
            source_message_id=source_message_id,
        )
        return {"op": op.op, "entity_id": str(record.id), "name": record.name, "type": record.type}

    async def _apply_upsert_field(self, session, workspace_id: UUID, op: PKSOperation, source_message_id: str | None):
        field = op.field or {}
        entity_ref = field.get("entity") or {}
        entity_type = self.validator.entity_type(entity_ref.get("type"))
        entity_name = str(entity_ref.get("name") or "").strip()
        key = self.validator.field_key(field.get("key"))
        if not entity_name or not entity_type or not key:
            return None
        entity_type = self._canonical_entity_type(entity_name, entity_type)
        value_type = self.validator.value_type(field.get("value_type"))
        value = field.get("value")
        unit = field.get("unit")
        value_type, value, unit = self._normalize_field_payload(
            key=key,
            value_type=value_type,
            value=value,
            unit=unit,
        )

        entity = await self.resolver.resolve_one(
            session,
            workspace_id=workspace_id,
            query=entity_name,
            entity_type=entity_type,
            min_score=0.80,
        )
        if entity is None:
            entity, _ = await self.repository.upsert_entity(
                session,
                UpsertEntityInput(
                    workspace_id=workspace_id,
                    name=entity_name,
                    type=entity_type,
                    space_id=await self._space_id(session, workspace_id, None, entity_type),
                    confidence=op.confidence,
                    sensitivity=self.validator.sensitivity(field.get("sensitivity")),
                    source="conversation",
                    source_message_id=source_message_id,
                ),
            )

        if await self._should_skip_field_update(session, entity.id, key, value):
            return None

        record, before = await self.repository.upsert_field(
            session,
            UpsertFieldInput(
                entity_id=entity.id,
                key=key,
                value_type=value_type,
                value=value,
                unit=unit,
                confidence=op.confidence,
                sensitivity=self.validator.sensitivity(field.get("sensitivity")),
                source="conversation",
                source_message_id=source_message_id,
                actor="agent",
                metadata=field.get("metadata") or {},
            ),
        )
        await self.audit.log(
            session,
            workspace_id=workspace_id,
            actor_type="agent",
            actor_id="pks_writer",
            action="upsert_field",
            target_type="field",
            target_id=record.id,
            before=before,
            after=record.model_dump(mode="json"),
            reason=op.reason,
            confidence=op.confidence,
            source_message_id=source_message_id,
        )
        return {"op": op.op, "entity_id": str(entity.id), "field_id": str(record.id), "key": record.key}

    async def _apply_upsert_relation(self, session, workspace_id: UUID, op: PKSOperation, source_message_id: str | None):
        relation = op.relation or {}
        relation_type = self.validator.relation_type(relation.get("relation_type"))
        from_ref = relation.get("from_entity") or {}
        to_ref = relation.get("to_entity") or {}
        from_type = self.validator.entity_type(from_ref.get("type"))
        to_type = self.validator.entity_type(to_ref.get("type"))
        from_name = str(from_ref.get("name") or "").strip()
        to_name = str(to_ref.get("name") or "").strip()
        if not relation_type or not from_type or not to_type or not from_name or not to_name:
            return None
        from_type = self._canonical_entity_type(from_name, from_type)
        to_type = self._canonical_entity_type(to_name, to_type)

        from_entity = await self._resolve_or_create_entity(session, workspace_id, from_name, from_type, op.confidence, source_message_id)
        to_entity = await self._resolve_or_create_entity(session, workspace_id, to_name, to_type, op.confidence, source_message_id)
        record, before = await self.repository.upsert_relation(
            session,
            UpsertRelationInput(
                workspace_id=workspace_id,
                from_entity_id=from_entity.id,
                relation_type=relation_type,
                to_entity_id=to_entity.id,
                metadata=relation.get("metadata") or {},
                confidence=op.confidence,
                sensitivity=self.validator.sensitivity(relation.get("sensitivity")),
                source="conversation",
                source_message_id=source_message_id,
            ),
        )
        await self.audit.log(
            session,
            workspace_id=workspace_id,
            actor_type="agent",
            actor_id="pks_writer",
            action="upsert_relation",
            target_type="relation",
            target_id=record.id,
            before=before,
            after=record.model_dump(mode="json"),
            reason=op.reason,
            confidence=op.confidence,
            source_message_id=source_message_id,
        )
        return {"op": op.op, "relation_id": str(record.id), "relation_type": record.relation_type}

    async def _apply_soft_delete_entity(self, session, workspace_id: UUID, op: PKSOperation, source_message_id: str | None):
        entity = op.entity or {}
        entity_type = self.validator.entity_type(entity.get("type"))
        name = str(entity.get("name") or "").strip()
        if not name:
            return None
        if entity_type:
            entity_type = self._canonical_entity_type(name, entity_type)
        record = await self.resolver.resolve_one(
            session,
            workspace_id=workspace_id,
            query=name,
            entity_type=entity_type,
            min_score=0.80,
        )
        if not record:
            return None
        before = await self.repository.soft_delete_entity(session, record.id)
        await self.audit.log(
            session,
            workspace_id=workspace_id,
            actor_type="agent",
            actor_id="pks_writer",
            action="soft_delete_entity",
            target_type="entity",
            target_id=record.id,
            before=before,
            after={"deleted": True},
            reason=op.reason,
            confidence=op.confidence,
            source_message_id=source_message_id,
        )
        return {"op": op.op, "entity_id": str(record.id), "name": record.name}

    async def _resolve_or_create_entity(
        self,
        session,
        workspace_id: UUID,
        name: str,
        entity_type: str,
        confidence: float,
        source_message_id: str | None,
    ):
        entity_type = self._canonical_entity_type(name, entity_type)
        entity = await self.resolver.resolve_one(
            session,
            workspace_id=workspace_id,
            query=name,
            entity_type=entity_type,
            min_score=0.80,
        )
        if entity:
            return entity
        entity, _ = await self.repository.upsert_entity(
            session,
            UpsertEntityInput(
                workspace_id=workspace_id,
                name=name,
                type=entity_type,
                space_id=await self._space_id(session, workspace_id, None, entity_type),
                confidence=confidence,
                source="conversation",
                source_message_id=source_message_id,
            ),
        )
        return entity

    def _canonical_entity_type(self, name: str, entity_type: str) -> str:
        token = normalize_token(name)
        if entity_type == "routine" and token.startswith("treino de "):
            return "workout"
        return entity_type

    async def _space_id(self, session, workspace_id: UUID, requested: Any, entity_type: str) -> UUID:
        requested_token = normalize_token(str(requested)) if requested else ""
        if entity_type in {"workout", "exercise", "muscle_group"}:
            return await self.repository.ensure_space(session, workspace_id, "Academia", "fitness")
        if entity_type == "routine" and requested_token in {"academia", "saude", "fitness"}:
            return await self.repository.ensure_space(session, workspace_id, "Academia", "fitness")
        if requested and requested_token in {
            "perfil", "pessoas", "projetos", "dispositivos", "academia", "documentos", "preferencias"
        }:
            base_types = {
                "perfil": "profile",
                "pessoas": "people",
                "projetos": "projects",
                "dispositivos": "devices",
                "academia": "fitness",
                "documentos": "documents",
                "preferencias": "preferences",
            }
            names = {
                "perfil": "Perfil",
                "pessoas": "Pessoas",
                "projetos": "Projetos",
                "dispositivos": "Dispositivos",
                "academia": "Academia",
                "documentos": "Documentos",
                "preferencias": "Preferências",
            }
            return await self.repository.ensure_space(session, workspace_id, names[requested_token], base_types[requested_token])
        defaults = {
            "person": ("Pessoas", "people"),
            "device": ("Dispositivos", "devices"),
            "software": ("Dispositivos", "devices"),
            "technology": ("Dispositivos", "devices"),
            "hardware_problem": ("Dispositivos", "devices"),
            "project": ("Projetos", "projects"),
            "company": ("Projetos", "projects"),
            "workout": ("Academia", "fitness"),
            "exercise": ("Academia", "fitness"),
            "food": ("Academia", "fitness"),
            "meal": ("Academia", "fitness"),
            "supplement": ("Academia", "fitness"),
            "health_state": ("Perfil", "profile"),
            "restriction": ("Perfil", "profile"),
            "routine": ("Preferências", "preferences"),
            "preference": ("Preferências", "preferences"),
            "goal": ("Perfil", "profile"),
        }
        name, type_ = defaults.get(entity_type, ("Documentos", "documents"))
        return await self.repository.ensure_space(session, workspace_id, name, type_)

    def _extract_name_statement(self, text: str) -> str | None:
        invalid_names = {
            "a", "as", "ao", "aos",
            "com",
            "da", "das", "de", "do", "dos",
            "e",
            "eu",
            "em",
            "me",
            "mim",
            "na", "nas", "no", "nos",
            "o", "os",
            "para",
            "pela", "pelas", "pelo", "pelos",
            "por",
            "sem",
            "voce", "você",
        }
        patterns = (
            r"\b(?:o\s+)?meu\s+nome(?:\s+completo)?\s+(?:e|é)\s+([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\wÁÀÂÃÉÊÍÓÔÕÚÇáàâãéêíóôõúç]+(?:\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\wÁÀÂÃÉÊÍÓÔÕÚÇáàâãéêíóôõúç]+){0,4})",
            r"\bm(?:e|eu|enu)\s+nome (?:e|é)\s+([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\wÁÀÂÃÉÊÍÓÔÕÚÇáàâãéêíóôõúç]+(?:\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\wÁÀÂÃÉÊÍÓÔÕÚÇáàâãéêíóôõúç]+){0,4})",
            r"\beu me chamo\s+([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\wÁÀÂÃÉÊÍÓÔÕÚÇáàâãéêíóôõúç]+(?:\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\wÁÀÂÃÉÊÍÓÔÕÚÇáàâãéêíóôõúç]+){0,4})",
            r"\beu sou\s+([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\wÁÀÂÃÉÊÍÓÔÕÚÇáàâãéêíóôõúç]+(?:\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\wÁÀÂÃÉÊÍÓÔÕÚÇáàâãéêíóôõúç]+){0,4})",
            r"\bsou\s+([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\wÁÀÂÃÉÊÍÓÔÕÚÇáàâãéêíóôõúç]+(?:\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\wÁÀÂÃÉÊÍÓÔÕÚÇáàâãéêíóôõúç]+){0,4})",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                cleaned = self._clean_person_name(match.group(1))
                if len(cleaned) < 2 or cleaned.lower() in invalid_names:
                    continue
                return cleaned
        return None

    def _extract_device_statement(self, text: str) -> dict[str, str | list[str]] | None:
        match = re.search(r"\bmeu\s+(pc|computador|notebook|desktop)\s+tem\s+(.+)$", text, flags=re.IGNORECASE)
        if not match:
            return None
        label = match.group(1).strip().lower()
        fact = match.group(2).strip(" .")
        field_key = "specification"
        lowered_fact = fact.lower()
        if "processador" in lowered_fact or "cpu" in lowered_fact:
            field_key = "cpu"
        elif "memoria" in lowered_fact or "ram" in lowered_fact:
            field_key = "memory"
        elif "gpu" in lowered_fact or "placa de video" in lowered_fact:
            field_key = "gpu"
        return {
            "name": "PC principal",
            "aliases": [label, "pc principal", "meu pc"],
            "field_key": field_key,
            "field_value": fact,
        }

    def _extract_entertainment_habit(self, text: str) -> dict[str, dict[str, str]] | None:
        token = normalize_token(text)
        frequency_markers = {
            "geralmente",
            "normalmente",
            "costumo",
            "sempre",
            "habitualmente",
            "quase sempre",
            "todo dia",
            "todos os dias",
        }
        if not any(marker in token for marker in frequency_markers):
            return None

        media_labels = (
            ("series", {"serie", "series"}),
            ("filmes", {"filme", "filmes"}),
            ("animes", {"anime", "animes"}),
        )
        media = next(
            (label for label, markers in media_labels if any(marker in token for marker in markers)),
            None,
        )
        if media is None and not any(marker in token for marker in {"assistir", "assisto", "vejo", "ver"}):
            return None
        if media is None:
            media = "conteudo audiovisual"

        time_label = None
        time_text = ""
        if any(marker in token for marker in {"a noite", "as noites", "de noite", "noite", "noites"}):
            time_label = "night"
            time_text = " à noite"
        elif any(marker in token for marker in {"de manha", "pela manha", "manha", "manhas"}):
            time_label = "morning"
            time_text = " pela manhã"
        elif any(marker in token for marker in {"de tarde", "a tarde", "tarde", "tardes"}):
            time_label = "afternoon"
            time_text = " à tarde"

        fields = {
            "entertainment_habit": f"assiste {media}{time_text}",
        }
        if time_label:
            fields["preferred_entertainment_time"] = time_label
        return {"fields": fields}

    def _extract_relative_statement(self, text: str) -> dict[str, str] | None:
        match = re.search(
            r"\bminha\s+(irma|irmã)\s+se\s+chama\s+([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\wÁÀÂÃÉÊÍÓÔÕÚÇáàâãéêíóôõúç]+(?:\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\wÁÀÂÃÉÊÍÓÔÕÚÇáàâãéêíóôõúç]+){0,4})",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return {"role": "irmã", "name": self._clean_person_name(match.group(2)), "relation_type": "sibling_of"}
        return None

    def _clean_person_name(self, value: str) -> str:
        cleaned = re.sub(r"\s+", " ", value.strip(" .,;:!?"))
        return " ".join(part[:1].upper() + part[1:] for part in cleaned.split() if part)
