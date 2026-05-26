"""Bootstrap for Neo's own PKS workspace and primary agent."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from core.pks.repository import PKSRepository
from core.pks.schemas import UpsertEntityInput, UpsertFieldInput
from db.async_client import AsyncPostgresClient

logger = logging.getLogger(__name__)

SYSTEM_WORKSPACE_NAME = "neo"
SYSTEM_WORKSPACE_TYPE = "system"
SYSTEM_SPACE_NAME = "Sistema"
SYSTEM_COLLECTION_NAME = "Agentes"
SYSTEM_AGENT_NAME = "neo-core"
SYSTEM_AGENT_TYPE = "software"
SYSTEM_IDENTITY_COLLECTION_NAME = "identity"
SYSTEM_PERSONALITY_NAME = "neo-personality"
SYSTEM_PERSONALITY_TYPE = "identity"
SYSTEM_BEHAVIOR_POLICIES_COLLECTION_NAME = "behavior_policies"
SYSTEM_RESPONSE_RULES_NAME = "neo-response-rules"
SYSTEM_RESPONSE_RULES_TYPE = "preference"
SYSTEM_PROCEDURES_COLLECTION_NAME = "procedures"
SYSTEM_OPERATING_PROCEDURES_NAME = "neo-operating-procedures"
SYSTEM_OPERATING_PROCEDURES_TYPE = "document"
SYSTEM_TRAINING_EXAMPLES_COLLECTION_NAME = "training_examples"
SYSTEM_CORRECTION_EXAMPLE_TYPE = "correction_example"
SYSTEM_EVALUATIONS_COLLECTION_NAME = "evaluations"
SYSTEM_REFLECTION_COLLECTION_NAME = "reflection"
SYSTEM_LEARNED_LESSON_TYPE = "learned_lesson"
SYSTEM_GOALS_NAME = "neo-goals"
SYSTEM_GOALS_TYPE = "goal"
SYSTEM_PROFILE_NAME = "daniel-profile"
SYSTEM_PROFILE_TYPE = "person"
SYSTEM_PROJECTS_NAME = "active-projects"
SYSTEM_PROJECTS_TYPE = "project"
SYSTEM_WORLD_STATE_NAME = "neo-world-state"
SYSTEM_WORLD_STATE_TYPE = "document"
SYSTEM_COMMUNICATION_PREFS_COLLECTION_NAME = "preferences"
SYSTEM_COMMUNICATION_PREFS_NAME = "neo-communication-preferences"
SYSTEM_COMMUNICATION_PREFS_TYPE = "preference"
COMMUNICATION_POLICY_RULE_KEY = "avoid_unrelated_historical_context"
COMMUNICATION_POLICY_DESCRIPTION = (
    "Neo deve evitar incluir fatos, objetivos, rotinas, treinos, projetos ou qualquer contexto "
    "histórico não relacionado quando o usuário não mencionar esse assunto ou quando isso não "
    "ajudar diretamente na resposta atual."
)

DEFAULT_PERSONALITY_PROPERTIES: dict[str, Any] = {
    "tone": "pragmatic",
    "humor": "subtle",
    "communication_style": "direct",
    "pressure_behavior": "calm",
    "sarcasm_level": 0.2,
    "inspiration": None,
    "traits": [
        "pragmatic",
        "persistent",
        "factual",
        "context_oriented",
    ],
}

DEFAULT_GOALS_PROPERTIES: dict[str, Any] = {
    "active": [],
    "long_term": [],
    "constraints": [],
}

DEFAULT_PROFILE_PROPERTIES: dict[str, Any] = {
    "name": "Daniel",
    "preferences": [],
    "stable_facts": [],
}

DEFAULT_PROJECTS_PROPERTIES: dict[str, Any] = {
    "items": [],
    "focus": None,
    "status": "active",
}

DEFAULT_WORLD_STATE_PROPERTIES: dict[str, Any] = {
    "session_mode": "normal",
    "current_focus": None,
    "recent_events": [],
}

DEFAULT_COMMUNICATION_PREFS_PROPERTIES: dict[str, Any] = {
    "type": "preference",
    "subtype": "assistant_communication_policy",
    "owner": "daniel",
    "owner_user_id": None,
    "owner_user_name": "Daniel Morais",
    "target_agent": SYSTEM_AGENT_NAME,
    "target_workspace": SYSTEM_WORKSPACE_NAME,
    "scope": "assistant_response_generation",
    "priority": "high",
    "rule_key": COMMUNICATION_POLICY_RULE_KEY,
    "rule_description": COMMUNICATION_POLICY_DESCRIPTION,
    "examples": [
        "Não incluir treinos, objetivos, projetos ou contexto histórico quando o usuário não mencionou esse assunto.",
        "Responder apenas ao assunto atual quando memória recuperada não ajudar diretamente.",
    ],
}

DEFAULT_RESPONSE_RULES_PROPERTIES: dict[str, Any] = {
    "rules": [],
}

DEFAULT_OPERATING_PROCEDURES_PROPERTIES: dict[str, Any] = {
    "procedures": [],
}

SYSTEM_MEMORY_BLOCKS: dict[str, dict[str, Any]] = {
    "identity": {
        "name": "Neo",
        "agent": SYSTEM_AGENT_NAME,
        "workspace": SYSTEM_WORKSPACE_NAME,
        "role": "assistente pessoal de desktop",
        "persona": "pragmático, persistente, factual e orientado a contexto",
    },
    "user_profile": {
        "subject": "Daniel",
        "summary": "",
        "stable_facts": [],
    },
    "goals": {
        "active": [],
        "long_term": [],
    },
    "projects": {
        "known": [],
    },
    "preferences": {
        "known": [],
    },
    "procedures": {
        "learned": [],
    },
}


class NeoSystemBootstrap:
    """Ensures the Neo PKS system workspace and primary agent exist."""

    def __init__(
        self,
        db: AsyncPostgresClient,
        repository: PKSRepository,
        *,
        state_file: str,
    ) -> None:
        self.db = db
        self.repository = repository
        self.state_file = Path(state_file)

    async def ensure(self) -> dict[str, Any]:
        logger.info("[PKS][system_bootstrap] start state_file=%s", self.state_file)
        local_state = self._load_local_state()

        async with self.db.get_session() as session:
            workspace = await self._ensure_workspace(session, local_state)
            logger.info("[PKS][system_bootstrap] workspace_ready workspace_id=%s", workspace["id"])

            space_id = await self.repository.ensure_space(
                session,
                workspace["id"],
                SYSTEM_SPACE_NAME,
                "system",
            )
            logger.info("[PKS][system_bootstrap] space_ready space_id=%s", space_id)

            collection_id = await self.repository.ensure_collection(
                session,
                workspace["id"],
                space_id,
                SYSTEM_COLLECTION_NAME,
                "software",
            )
            logger.info("[PKS][system_bootstrap] collection_ready collection_id=%s", collection_id)
            await self._ensure_training_collections(session, workspace["id"], space_id)

            agent = await self._ensure_agent(
                session,
                workspace_id=workspace["id"],
                space_id=space_id,
                collection_id=collection_id,
            )
            logger.info("[PKS][system_bootstrap] agent_ready agent_id=%s", agent.id)

            identity_collection_id = await self.repository.ensure_collection(
                session,
                workspace["id"],
                space_id,
                SYSTEM_IDENTITY_COLLECTION_NAME,
                SYSTEM_PERSONALITY_TYPE,
            )
            personality = await self._ensure_personality(
                session,
                workspace_id=workspace["id"],
                space_id=space_id,
                collection_id=identity_collection_id,
            )
            logger.info("[PKS][system_bootstrap] personality_ready entity_id=%s", personality.id)

            goals = await self._ensure_structured_entity(
                session,
                workspace_id=workspace["id"],
                space_id=space_id,
                collection_name="Objetivos",
                name=SYSTEM_GOALS_NAME,
                type_=SYSTEM_GOALS_TYPE,
                aliases=["goals", "neo goals", "objective tracker"],
                metadata={"workspace_name": SYSTEM_WORKSPACE_NAME, "is_core_goals": True},
                properties=DEFAULT_GOALS_PROPERTIES,
            )
            profile = await self._ensure_structured_entity(
                session,
                workspace_id=workspace["id"],
                space_id=space_id,
                collection_name="Perfis",
                name=SYSTEM_PROFILE_NAME,
                type_=SYSTEM_PROFILE_TYPE,
                aliases=["profile", "daniel profile", "user profile"],
                metadata={"workspace_name": SYSTEM_WORKSPACE_NAME, "is_core_profile": True},
                properties=DEFAULT_PROFILE_PROPERTIES,
            )
            projects = await self._ensure_structured_entity(
                session,
                workspace_id=workspace["id"],
                space_id=space_id,
                collection_name="Projetos",
                name=SYSTEM_PROJECTS_NAME,
                type_=SYSTEM_PROJECTS_TYPE,
                aliases=["projects", "active projects", "project tracker"],
                metadata={"workspace_name": SYSTEM_WORKSPACE_NAME, "is_core_projects": True},
                properties=DEFAULT_PROJECTS_PROPERTIES,
            )
            world_state = await self._ensure_structured_entity(
                session,
                workspace_id=workspace["id"],
                space_id=space_id,
                collection_name="Estado",
                name=SYSTEM_WORLD_STATE_NAME,
                type_=SYSTEM_WORLD_STATE_TYPE,
                aliases=["world state", "neo world state", "session state"],
                metadata={"workspace_name": SYSTEM_WORKSPACE_NAME, "is_core_world_state": True},
                properties=DEFAULT_WORLD_STATE_PROPERTIES,
            )
            communication_preferences = await self._ensure_structured_entity(
                session,
                workspace_id=workspace["id"],
                space_id=space_id,
                collection_name=SYSTEM_COMMUNICATION_PREFS_COLLECTION_NAME,
                name=SYSTEM_COMMUNICATION_PREFS_NAME,
                type_=SYSTEM_COMMUNICATION_PREFS_TYPE,
                aliases=[
                    "assistant communication policy",
                    "estilo de resposta",
                    "preferência de comunicação",
                    "como o Neo responde",
                ],
                metadata={
                    "workspace_name": SYSTEM_WORKSPACE_NAME,
                    "is_assistant_communication_policy": True,
                    "target_agent": SYSTEM_AGENT_NAME,
                    "target_workspace": SYSTEM_WORKSPACE_NAME,
                    "priority": "high",
                    "rule_key": COMMUNICATION_POLICY_RULE_KEY,
                },
                properties=DEFAULT_COMMUNICATION_PREFS_PROPERTIES,
            )
            response_rules = await self._ensure_structured_entity(
                session,
                workspace_id=workspace["id"],
                space_id=space_id,
                collection_name=SYSTEM_BEHAVIOR_POLICIES_COLLECTION_NAME,
                name=SYSTEM_RESPONSE_RULES_NAME,
                type_=SYSTEM_RESPONSE_RULES_TYPE,
                aliases=["behavior policies", "response rules", "neo behavior rules"],
                metadata={
                    "workspace_name": SYSTEM_WORKSPACE_NAME,
                    "is_behavior_policy_store": True,
                    "target_agent": SYSTEM_AGENT_NAME,
                    "target_workspace": SYSTEM_WORKSPACE_NAME,
                },
                properties=DEFAULT_RESPONSE_RULES_PROPERTIES,
            )
            operating_procedures = await self._ensure_structured_entity(
                session,
                workspace_id=workspace["id"],
                space_id=space_id,
                collection_name=SYSTEM_PROCEDURES_COLLECTION_NAME,
                name=SYSTEM_OPERATING_PROCEDURES_NAME,
                type_=SYSTEM_OPERATING_PROCEDURES_TYPE,
                aliases=["operating procedures", "when then procedures", "neo procedures"],
                metadata={
                    "workspace_name": SYSTEM_WORKSPACE_NAME,
                    "is_operating_procedure_store": True,
                    "target_agent": SYSTEM_AGENT_NAME,
                    "target_workspace": SYSTEM_WORKSPACE_NAME,
                },
                properties=DEFAULT_OPERATING_PROCEDURES_PROPERTIES,
            )
            logger.info(
                "[PKS][system_bootstrap] structured_entities_ready goals=%s profile=%s projects=%s world_state=%s communication_prefs=%s response_rules=%s procedures=%s",
                goals.id,
                profile.id,
                projects.id,
                world_state.id,
                communication_preferences.id,
                response_rules.id,
                operating_procedures.id,
            )

            memory_block_field_ids = await self._ensure_memory_blocks(session, agent.id)
            await session.commit()

        state = {
            "workspace_id": str(workspace["id"]),
            "space_id": str(space_id),
            "collection_id": str(collection_id),
            "agent_id": str(agent.id),
            "personality_entity_id": str(personality.id),
            "goals_entity_id": str(goals.id),
            "profile_entity_id": str(profile.id),
            "projects_entity_id": str(projects.id),
            "world_state_entity_id": str(world_state.id),
            "communication_preferences_entity_id": str(communication_preferences.id),
            "response_rules_entity_id": str(response_rules.id),
            "operating_procedures_entity_id": str(operating_procedures.id),
            "memory_block_field_ids": {key: str(value) for key, value in memory_block_field_ids.items()},
        }
        self._persist_local_state(state)
        logger.info(
            "[PKS][system_bootstrap] complete workspace_id=%s agent_id=%s blocks=%d",
            state["workspace_id"],
            state["agent_id"],
            len(memory_block_field_ids),
        )
        return state

    async def _ensure_workspace(self, session, local_state: dict[str, Any]) -> dict[str, Any]:
        workspace_id = local_state.get("workspace_id")
        workspace = None
        if workspace_id:
            try:
                workspace = await self.repository.get_workspace_by_id(session, UUID(workspace_id))
            except (TypeError, ValueError):
                logger.warning("[PKS][system_bootstrap] invalid_workspace_id_in_state value=%s", workspace_id)
                workspace = None
            if workspace and workspace.get("type") == SYSTEM_WORKSPACE_TYPE:
                logger.info("[PKS][system_bootstrap] reusing workspace from state workspace_id=%s", workspace_id)
            else:
                workspace = None
        if workspace is None:
            workspace = await self.repository.find_workspace_by_name(
                session,
                name=SYSTEM_WORKSPACE_NAME,
                type_=SYSTEM_WORKSPACE_TYPE,
            )
            if workspace:
                logger.info("[PKS][system_bootstrap] reusing workspace by name workspace_id=%s", workspace["id"])
        if workspace is None:
            workspace = await self.repository.create_workspace(
                session,
                name=SYSTEM_WORKSPACE_NAME,
                type_=SYSTEM_WORKSPACE_TYPE,
                metadata={"is_system_workspace": True, "system_name": SYSTEM_WORKSPACE_NAME},
                external_refs={"bootstrap_key": SYSTEM_WORKSPACE_NAME},
            )
            logger.info("[PKS][system_bootstrap] created workspace workspace_id=%s", workspace["id"])
        workspace = await self.repository.merge_workspace_metadata(
            session,
            workspace_id=workspace["id"],
            name=SYSTEM_WORKSPACE_NAME,
            metadata={"is_system_workspace": True, "system_name": SYSTEM_WORKSPACE_NAME},
            external_refs={"bootstrap_key": SYSTEM_WORKSPACE_NAME},
        )
        if workspace is None:
            raise RuntimeError("Failed to ensure Neo system workspace")
        return workspace

    async def _ensure_training_collections(self, session, workspace_id: UUID, space_id: UUID) -> dict[str, UUID]:
        specs = (
            (SYSTEM_IDENTITY_COLLECTION_NAME, SYSTEM_PERSONALITY_TYPE),
            (SYSTEM_BEHAVIOR_POLICIES_COLLECTION_NAME, SYSTEM_RESPONSE_RULES_TYPE),
            (SYSTEM_PROCEDURES_COLLECTION_NAME, SYSTEM_OPERATING_PROCEDURES_TYPE),
            (SYSTEM_TRAINING_EXAMPLES_COLLECTION_NAME, SYSTEM_CORRECTION_EXAMPLE_TYPE),
            (SYSTEM_EVALUATIONS_COLLECTION_NAME, "evaluation"),
            (SYSTEM_REFLECTION_COLLECTION_NAME, SYSTEM_LEARNED_LESSON_TYPE),
        )
        collection_ids: dict[str, UUID] = {}
        for name, type_ in specs:
            collection_ids[name] = await self.repository.ensure_collection(
                session,
                workspace_id,
                space_id,
                name,
                type_,
            )
        return collection_ids

    async def _ensure_agent(
        self,
        session,
        *,
        workspace_id: UUID,
        space_id: UUID,
        collection_id: UUID,
    ):
        return (
            await self.repository.upsert_entity(
                session,
                UpsertEntityInput(
                    workspace_id=workspace_id,
                    space_id=space_id,
                    collection_id=collection_id,
                    name=SYSTEM_AGENT_NAME,
                    type=SYSTEM_AGENT_TYPE,
                    aliases=["neo", "assistant", "neo core"],
                    metadata={
                        "is_system_agent": True,
                        "workspace_name": SYSTEM_WORKSPACE_NAME,
                        "memory_blocks": sorted(SYSTEM_MEMORY_BLOCKS.keys()),
                    },
                    external_refs={"bootstrap_key": SYSTEM_AGENT_NAME},
                    confidence=1.0,
                    sensitivity="private",
                    source="bootstrap",
                ),
            )
        )[0]

    async def _ensure_personality(
        self,
        session,
        *,
        workspace_id: UUID,
        space_id: UUID,
        collection_id: UUID,
    ):
        personality = (
            await self.repository.upsert_entity(
                session,
                UpsertEntityInput(
                    workspace_id=workspace_id,
                    space_id=space_id,
                    collection_id=collection_id,
                    name=SYSTEM_PERSONALITY_NAME,
                    type=SYSTEM_PERSONALITY_TYPE,
                    aliases=["personality", "neo identity", "neo persona"],
                    metadata={
                        "workspace_name": SYSTEM_WORKSPACE_NAME,
                        "is_core_identity": True,
                    },
                    external_refs={"bootstrap_key": SYSTEM_PERSONALITY_NAME},
                    confidence=1.0,
                    sensitivity="private",
                    source="bootstrap",
                ),
            )
        )[0]
        if await self.repository.get_field(session, personality.id, "properties") is None:
            await self.repository.upsert_field(
                session,
                UpsertFieldInput(
                    entity_id=personality.id,
                    key="properties",
                    value_type="json",
                    value=DEFAULT_PERSONALITY_PROPERTIES,
                    confidence=1.0,
                    sensitivity="private",
                    source="bootstrap",
                    actor="system",
                    metadata={"identity_properties": True},
                ),
            )
        return personality

    async def _ensure_structured_entity(
        self,
        session,
        *,
        workspace_id: UUID,
        space_id: UUID,
        collection_name: str,
        name: str,
        type_: str,
        aliases: list[str],
        metadata: dict[str, Any],
        properties: dict[str, Any],
    ):
        collection_id = await self.repository.ensure_collection(
            session,
            workspace_id,
            space_id,
            collection_name,
            type_,
        )
        entity = (
            await self.repository.upsert_entity(
                session,
                UpsertEntityInput(
                    workspace_id=workspace_id,
                    space_id=space_id,
                    collection_id=collection_id,
                    name=name,
                    type=type_,
                    aliases=aliases,
                    metadata=metadata,
                    confidence=1.0,
                    sensitivity="private",
                    source="bootstrap",
                ),
            )
        )[0]
        if await self.repository.get_field(session, entity.id, "properties") is None:
            await self.repository.upsert_field(
                session,
                UpsertFieldInput(
                    entity_id=entity.id,
                    key="properties",
                    value_type="json",
                    value=properties,
                    confidence=1.0,
                    sensitivity="private",
                    source="bootstrap",
                    actor="system",
                    metadata={"structured_properties": True},
                ),
            )
        return entity

    async def _ensure_memory_blocks(self, session, agent_id: UUID) -> dict[str, UUID]:
        field_ids: dict[str, UUID] = {}
        for block_key, payload in SYSTEM_MEMORY_BLOCKS.items():
            record, _ = await self.repository.upsert_field(
                session,
                UpsertFieldInput(
                    entity_id=agent_id,
                    key=block_key,
                    value_type="json",
                    value=payload,
                    confidence=1.0,
                    sensitivity="private",
                    source="bootstrap",
                    actor="system",
                    metadata={"memory_block": True, "block_key": block_key},
                ),
            )
            field_ids[block_key] = record.id
            logger.info(
                "[PKS][system_bootstrap] memory_block_ready block=%s field_id=%s",
                block_key,
                record.id,
            )
        return field_ids

    def _load_local_state(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[PKS][system_bootstrap] failed_to_read_state_file path=%s error=%s", self.state_file, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _persist_local_state(self, state: dict[str, Any]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        logger.info("[PKS][system_bootstrap] state_file_updated path=%s", self.state_file)
