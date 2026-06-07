"""Facade for PKS read/write operations."""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from uuid import UUID

from core.llm.router import LLMRouter
from core.pks.audit import AuditLogger
from core.pks.bootstrap import PKSBootstrap
from core.pks.context_builder import ContextBuilder
from core.pks.entity_resolver import EntityResolver
from core.pks.ontology import OntologyResolver, OntologyValidator
from core.pks.repository import PKSRepository
from core.pks.retriever import PKSRetriever
from core.pks.system_bootstrap import (
    NeoSystemBootstrap,
    SYSTEM_COMMUNICATION_PREFS_NAME,
    SYSTEM_COMMUNICATION_PREFS_TYPE,
    SYSTEM_CORRECTION_EXAMPLE_TYPE,
    SYSTEM_GOALS_NAME,
    SYSTEM_GOALS_TYPE,
    SYSTEM_LEARNED_LESSON_TYPE,
    SYSTEM_OPERATING_PROCEDURES_NAME,
    SYSTEM_OPERATING_PROCEDURES_TYPE,
    SYSTEM_PERSONALITY_NAME,
    SYSTEM_PERSONALITY_TYPE,
    SYSTEM_PROFILE_NAME,
    SYSTEM_PROFILE_TYPE,
    SYSTEM_PROJECTS_NAME,
    SYSTEM_PROJECTS_TYPE,
    SYSTEM_RESPONSE_RULES_NAME,
    SYSTEM_RESPONSE_RULES_TYPE,
    SYSTEM_WORLD_STATE_NAME,
    SYSTEM_WORLD_STATE_TYPE,
)
from core.pks.writer import PKSWriter
from db.async_client import AsyncPostgresClient
from shared.config import get_settings


class PKSService:
    def __init__(self, db: AsyncPostgresClient, llm_router: LLMRouter) -> None:
        self.db = db
        self.llm = llm_router
        self.repository = PKSRepository(db.session_factory)
        self.ontology = OntologyResolver()
        self.validator = OntologyValidator(self.ontology)
        self.audit = AuditLogger()
        self.bootstrap = PKSBootstrap(db, self.repository)
        self.system_bootstrap = NeoSystemBootstrap(
            db,
            self.repository,
            state_file=get_settings().pks_system_state_file,
        )
        self.resolver = EntityResolver(self.repository, self.ontology)
        self.context_builder = ContextBuilder()
        self.retriever = PKSRetriever(
            db=db,
            repository=self.repository,
            bootstrap=self.bootstrap,
            resolver=self.resolver,
            context_builder=self.context_builder,
        )
        self.writer = PKSWriter(
            db=db,
            llm_router=llm_router,
            repository=self.repository,
            bootstrap=self.bootstrap,
            resolver=self.resolver,
            validator=self.validator,
            audit=self.audit,
        )

    async def retrieve_context(
        self,
        *,
        user_id: UUID,
        keywords: list[str] | None,
        user_message: str,
    ) -> tuple[dict[str, Any], str]:
        result, context_text = await self.retriever.retrieve(
            user_id=user_id,
            user_message=user_message,
            keywords=keywords,
        )
        return result.model_dump(mode="json"), context_text

    async def ensure_user_pks(self, *, user_id: UUID) -> UUID:
        return await self.bootstrap.ensure_workspace(user_id)

    async def ensure_system_pks(self) -> dict[str, Any]:
        return await self.system_bootstrap.ensure()

    async def learn_before_response(
        self,
        *,
        user_id: UUID,
        user_message: str,
        memory_target: str = "user",
        source_message_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.writer.learn_before_response(
            user_id=user_id,
            user_message=user_message,
            memory_target=memory_target,
            source_message_id=source_message_id,
        )

    async def learn(
        self,
        *,
        user_id: UUID,
        user_message: str,
        assistant_response: str,
        memory_target: str = "user",
        pks_context: str = "",
        keywords: list[str] | None = None,
        recent_history: str = "",
        recent_messages: list[dict[str, str]] | None = None,
        source_message_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.writer.learn(
            user_id=user_id,
            user_message=user_message,
            assistant_response=assistant_response,
            memory_target=memory_target,
            pks_context=pks_context,
            keywords=keywords,
            recent_history=recent_history,
            recent_messages=recent_messages,
            source_message_id=source_message_id,
        )

    async def register_device(
        self,
        *,
        user_id: UUID,
        device_id: str,
        hardware_id: str | None = None,
        os_platform: str | None = None,
        os_arch: str | None = None,
        device_type: str | None = None,
        label: str | None = None,
        aliases: list[str] | None = None,
        source_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Deterministically persist the current device's identity (no LLM)."""
        return await self.writer.write_device_identity(
            user_id=user_id,
            device_id=device_id,
            hardware_id=hardware_id,
            os_platform=os_platform,
            os_arch=os_arch,
            device_type=device_type,
            label=label,
            aliases=aliases,
            source_message_id=source_message_id,
        )

    def parse_training_envelope(self, user_message: str):
        """Parse a message for an explicit Neo training envelope (no DB writes)."""
        return self.writer.envelope_parser.parse(user_message)

    def is_training_message(self, user_message: str) -> bool:
        """True when the message is an explicit Neo training message.

        Used by the desktop pipeline to block parallel routes (user preference
        extraction, personal-memory writes) for Neo training turns.
        """
        return self.writer.envelope_parser.parse(user_message).is_training

    async def retrieve_system_context(self, *, user_message: str = "") -> str:
        async with self.db.get_session() as session:
            workspace = await self.repository.get_system_workspace(session)
            if not workspace:
                return ""
            lines: list[str] = []
            structured_entities = (
                (SYSTEM_PERSONALITY_NAME, SYSTEM_PERSONALITY_TYPE),
                (SYSTEM_RESPONSE_RULES_NAME, SYSTEM_RESPONSE_RULES_TYPE),
                (SYSTEM_OPERATING_PROCEDURES_NAME, SYSTEM_OPERATING_PROCEDURES_TYPE),
                (SYSTEM_COMMUNICATION_PREFS_NAME, SYSTEM_COMMUNICATION_PREFS_TYPE),
                (SYSTEM_GOALS_NAME, SYSTEM_GOALS_TYPE),
                (SYSTEM_PROFILE_NAME, SYSTEM_PROFILE_TYPE),
                (SYSTEM_PROJECTS_NAME, SYSTEM_PROJECTS_TYPE),
                (SYSTEM_WORLD_STATE_NAME, SYSTEM_WORLD_STATE_TYPE),
            )
            for entity_name, entity_type in structured_entities:
                entity = await self.repository.find_entity_by_name(
                    session,
                    workspace["id"],
                    entity_name,
                    entity_type,
                )
                if not entity:
                    continue
                properties_field = await self.repository.get_field(session, entity.id, "properties")
                properties = properties_field.value if properties_field else None
                if not isinstance(properties, dict):
                    continue
                if entity_name in {SYSTEM_COMMUNICATION_PREFS_NAME, SYSTEM_RESPONSE_RULES_NAME}:
                    lines.append(f"### {entity_name} [{entity_type}] (Alta Prioridade)")
                else:
                    lines.append(f"### {entity_name} [{entity_type}]")
                lines.extend(self._format_system_properties(properties, user_message=user_message))

            for extra_type in (SYSTEM_CORRECTION_EXAMPLE_TYPE, SYSTEM_LEARNED_LESSON_TYPE):
                entities = await self.repository.list_entities_by_type(
                    session,
                    workspace["id"],
                    extra_type,
                    limit=6,
                )
                for entity in entities:
                    properties_field = await self.repository.get_field(session, entity.id, "properties")
                    properties = properties_field.value if properties_field else None
                    if not isinstance(properties, dict):
                        continue
                    lines.append(f"### {entity.name} [{entity.type}]")
                    lines.extend(self._format_system_properties(properties, user_message=user_message))

            agent = await self.repository.get_system_agent_entity(session, workspace["id"])
            if agent:
                fields = await self.repository.get_entity_fields(session, agent.id)
                blocks = {
                    field.key: field.value
                    for field in fields
                    if field.key in {"identity", "procedures"}
                }
                identity = blocks.get("identity")
                if isinstance(identity, dict) and identity.get("persona"):
                    lines.append(f"- Base persona: {identity['persona']}")
                procedures = blocks.get("procedures")
                if isinstance(procedures, dict):
                    learned = procedures.get("learned")
                    if isinstance(learned, list):
                        for item in learned[-3:]:
                            lines.append(f"- Procedimento aprendido: {item}")
            return "\n".join(lines)

    def _format_system_properties(self, properties: dict[str, Any], *, user_message: str = "") -> list[str]:
        lines: list[str] = []
        for key, value in properties.items():
            if isinstance(value, list):
                for item in value[-8:]:
                    if isinstance(item, dict):
                        if not self._should_render_training_item(item, user_message=user_message):
                            continue
                        description = item.get("description") or item.get("rule_description")
                        rule_key = item.get("rule_key")
                        priority = item.get("priority")
                        if description:
                            prefix = f"- {key}"
                            if priority:
                                prefix += f" ({priority})"
                            if rule_key:
                                prefix += f" [{rule_key}]"
                            lines.append(f"{prefix}: {description}")
                            trigger = item.get("trigger")
                            if trigger:
                                lines.append(f"  - Gatilho: {trigger}")
                            action = item.get("action")
                            if action:
                                lines.append(f"  - Ação: {action}")
                            variation_policy = item.get("variation_policy")
                            if isinstance(variation_policy, dict) and variation_policy:
                                enabled = [
                                    name for name, enabled in variation_policy.items()
                                    if enabled is True
                                ]
                                if enabled:
                                    lines.append(
                                        "  - Política de variação: "
                                        + ", ".join(enabled)
                                    )
                            examples_good = item.get("examples_good")
                            if isinstance(examples_good, list) and examples_good:
                                # Verbatim examples are the main parroting hazard
                                # (session 6794b3ba: the model copied the
                                # "aspirador" example into an unrelated compliment
                                # turn). Surface them ONLY when this rule's trigger
                                # is contextually relevant, and label them as style
                                # references — never scripts to repeat literally.
                                if self._trigger_is_relevant(trigger, user_message):
                                    preview = "; ".join(str(example) for example in examples_good[:3])
                                    lines.append(
                                        "  - Exemplos de TOM (referência de estilo — NÃO copie "
                                        f"literalmente nem use fora do gatilho): {preview}"
                                    )
                        continue
                    lines.append(f"- {key}: {item}")
                continue
            if value is not None:
                lines.append(f"- {key}: {value}")
        return lines

    # Envelope fragments that must never be injected as an active rule, even if
    # they survive in legacy entities created before the Memory Pipeline v2.
    _ENVELOPE_FRAGMENTS = (
        "modo treino",
        "nao e uma preferencia pessoal",
        "não é uma preferencia pessoal",
        "não é uma preferência pessoal",
        "salve no workspace do neo",
        "salve este aprendizado",
    )

    def _should_render_training_item(self, item: dict[str, Any], *, user_message: str) -> bool:
        description = str(item.get("description") or item.get("rule_description") or "")
        normalized = description.lower()
        if any(fragment in normalized for fragment in self._ENVELOPE_FRAGMENTS):
            return False
        if item.get("priority") == "high":
            return True
        if not user_message:
            return True
        query_terms = {term for term in user_message.lower().split() if len(term) >= 4}
        return any(term in normalized for term in query_terms)

    # Filler words carry no topical signal — excluded so a trigger's meta-words
    # ("usuário", "frases", "quando"...) don't accidentally match any message.
    _TRIGGER_STOPWORDS = frozenset(
        {
            "usuario", "usuarios", "frase", "frases", "quando", "como", "para",
            "pelo", "pela", "uma", "uns", "umas", "com", "sem", "que", "isso",
            "esse", "essa", "este", "esta", "mais", "menos", "meio", "fizer",
            "fazer", "responder", "resposta", "respostas", "pedindo", "mandando",
            "deve", "devem", "tratar", "entrar", "abrir", "seguida", "exemplo",
            "exemplos", "etc", "ou", "e", "de", "do", "da", "no", "na", "em",
            "ao", "os", "as", "um", "se", "por", "uma", "tipo", "algo", "mensagem",
            "contexto", "contextual", "claramente", "especialmente", "geral",
        }
    )

    @classmethod
    def _trigger_is_relevant(cls, trigger: Any, user_message: str) -> bool:
        """True when a trigger-rule's situation plausibly matches the message.

        Trigger-specific verbatim examples must only surface when the current
        message actually matches the trigger; otherwise the weak chat model
        parrots them out of context. Errs toward withholding (precision): a
        false negative just drops a canned example, while a false positive is
        the exact bug we are fixing.
        """
        if not trigger:
            return True  # no trigger → generally-applicable rule
        if not user_message:
            return False  # cannot confirm relevance → withhold verbatim examples
        trigger_terms = cls._distinctive_terms(str(trigger))
        message_terms = cls._distinctive_terms(user_message)
        return bool(trigger_terms & message_terms)

    @classmethod
    def _distinctive_terms(cls, text: str) -> set[str]:
        """Accent-stripped content words (len>=4, minus filler) from ``text``."""
        normalized = unicodedata.normalize("NFD", text or "").lower()
        normalized = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
        terms = {t for t in re.findall(r"[a-z0-9]+", normalized) if len(t) >= 4}
        return terms - cls._TRIGGER_STOPWORDS

    async def search_entities(self, *, user_id: UUID, query: str) -> list[dict[str, Any]]:
        workspace_id = await self.bootstrap.ensure_workspace(user_id)
        async with self.db.get_session() as session:
            rows = await self.repository.search_entities(session, workspace_id, query)
            return [
                {
                    **entity.model_dump(mode="json"),
                    "score": score,
                    "matched_by": matched_by,
                }
                for entity, score, matched_by in rows
            ]
