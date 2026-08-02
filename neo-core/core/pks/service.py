"""Facade for PKS read/write operations."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
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
from core.pks.schemas import UpsertEntityInput, UpsertFieldInput, UpsertRelationInput
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
        confirmed_save: bool = False,
        forget: bool = False,
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
            confirmed_save=confirmed_save,
            forget=forget,
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

    async def retrieve_system_context(
        self, *, user_message: str = "", register: str | None = None
    ) -> str:
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
                lines.extend(self._format_system_properties(
                    properties, user_message=user_message, register=register,
                ))

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
                    lines.extend(self._format_system_properties(
                        properties, user_message=user_message, register=register,
                    ))

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

    def _format_system_properties(
        self,
        properties: dict[str, Any],
        *,
        user_message: str = "",
        register: str | None = None,
    ) -> list[str]:
        lines: list[str] = []
        for key, value in properties.items():
            if isinstance(value, list):
                for item in value[-8:]:
                    if isinstance(item, dict):
                        if not self._should_render_training_item(
                            item, user_message=user_message, register=register,
                        ):
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

    def _should_render_training_item(
        self,
        item: dict[str, Any],
        *,
        user_message: str,
        register: str | None = None,
    ) -> bool:
        description = str(item.get("description") or item.get("rule_description") or "")
        normalized = description.lower()
        if any(fragment in normalized for fragment in self._ENVELOPE_FRAGMENTS):
            return False
        # Predicado de registro (docs/context-register-plan.md): uma regra
        # escopada por `registers:` só renderiza dentro dos registros listados.
        # Sem registro ativo (feature off / caminho sem registro) → renderiza
        # tudo (fail-open, retrocompatível com o jsonb existente).
        allowed = item.get("registers")
        if register and isinstance(allowed, list) and allowed:
            if register not in {str(r).strip().lower() for r in allowed}:
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

    async def get_user_location(self, *, user_id: UUID) -> dict[str, Any] | None:
        """Resolve the user's home city from the PKS graph.

        Follows the primary user's ``located_in`` relation to a ``place`` entity
        and returns it as a geocodable location, or ``None`` when unknown. This is
        the authoritative source for "where is the user" — no guessing, no defaults.
        """
        workspace_id = await self.bootstrap.ensure_workspace(user_id)
        async with self.db.get_session() as session:
            primary = await self.repository.get_primary_user_entity(session, workspace_id)
            if not primary:
                return None
            relations = await self.repository.get_relations(session, primary.id)
            for rel in relations:
                if (
                    rel.relation_type == "located_in"
                    and rel.from_entity_id == primary.id
                    and rel.to_type == "place"
                ):
                    place = await self.repository.get_entity(session, rel.to_entity_id)
                    if place is None:
                        continue
                    return self._format_place(place)
        return None

    # ── Neo affect (per-user emotional vector) ────────────────────────────
    async def get_user_affect(self, *, user_id: UUID):
        """Read Neo's stored emotional state toward this user (no decay applied).

        Returns a fresh baseline :class:`AffectState` when nothing is stored yet
        or there is no primary user entity. Decay is the caller's job at read
        time, since it depends on the configured half-lives.
        """
        from core.agent.affect import AFFECT_FIELD_KEY, AffectState, AffectTuning

        tuning = AffectTuning.from_settings(get_settings())
        workspace_id = await self.bootstrap.ensure_workspace(user_id)
        async with self.db.get_session() as session:
            primary = await self.repository.get_primary_user_entity(session, workspace_id)
            if not primary:
                return AffectState.fresh(tuning)
            field = await self.repository.get_field(session, primary.id, AFFECT_FIELD_KEY)
            if not field or field.value is None:
                return AffectState.fresh(tuning)
            return AffectState.from_field(field.value)

    async def update_user_affect(
        self,
        *,
        user_id: UUID,
        signal,
        wrong_name: bool = False,
        threat: bool = False,
        source_message_id: str | None = None,
    ):
        """Merge one turn's appraised affect into the stored state and persist it.

        Read-modify-write in a single session (atomic across concurrent turns):
        the stored state is first decayed to *now*, then the bounded ``signal``
        is applied. ``wrong_name`` and ``threat`` are the deterministic detectors'
        verdicts — they enforce floors so the reaction fires even if the appraiser
        under-rated it. Apologies go through the forgiveness curve instead of raw
        deltas. Returns the new :class:`AffectState`, or ``None`` if no user.
        """
        from core.agent.affect import (
            AFFECT_FIELD_KEY,
            OFFENSIVE_TRIGGERS,
            AffectSignal,
            AffectState,
            AffectTuning,
            apply_apology,
            apply_decay,
            bump_grudge,
            merge_signal,
        )

        tuning = AffectTuning.from_settings(get_settings())
        if not isinstance(signal, AffectSignal):
            signal = AffectSignal.model_validate(signal)

        if wrong_name:
            deltas = dict(signal.deltas)
            deltas["ira"] = max(deltas.get("ira", 0.0), tuning.wrong_name_ira_floor)
            escalations = {"insult", "teasing", "rude", "threat_coercion"}
            signal = signal.model_copy(update={
                "deltas": deltas,
                "trigger": signal.trigger if signal.trigger in escalations else "wrong_name",
                "why": signal.why or "chamou o Neo por nome de outra IA",
            })
        if threat:
            deltas = dict(signal.deltas)
            deltas["ira"] = max(deltas.get("ira", 0.0), tuning.threat_ira_bump)
            deltas["confianca"] = min(deltas.get("confianca", 0.0), -tuning.threat_conf_drop)
            signal = signal.model_copy(update={
                "deltas": deltas,
                "trigger": "threat_coercion",
                "why": signal.why or "ameaçou a integridade do Neo",
            })

        workspace_id = await self.bootstrap.ensure_workspace(user_id)
        async with self.db.get_session() as session:
            primary = await self.repository.get_primary_user_entity(session, workspace_id)
            if not primary:
                return None
            field = await self.repository.get_field(session, primary.id, AFFECT_FIELD_KEY)
            current: AffectState = AffectState.from_field(field.value if field else None)
            state = apply_decay(current, tuning)

            if signal.trigger == "apology":
                # Forgiveness curve owns ira/tristeza; drop those raw deltas so the
                # appraiser can't double-dip on top of the cut.
                state, _relief = apply_apology(state, tuning)
                signal = signal.model_copy(update={
                    "deltas": {k: v for k, v in signal.deltas.items() if k not in ("ira", "tristeza")},
                })

            merged = merge_signal(state, signal, tuning)
            if threat and merged.stats.medo < tuning.threat_medo_floor:
                merged.stats.medo = tuning.threat_medo_floor
            if signal.trigger in OFFENSIVE_TRIGGERS:
                merged = bump_grudge(merged, tuning)

            await self.repository.upsert_field(
                session,
                UpsertFieldInput(
                    entity_id=primary.id,
                    key=AFFECT_FIELD_KEY,
                    value_type="json",
                    value=merged.to_field(),
                    source="affect",
                    source_message_id=source_message_id,
                    actor="system",
                ),
            )
            await session.commit()
            return merged

    async def reset_user_affect(self, *, user_id: UUID):
        """Reset Neo's emotional state toward this user to the personality baselines.

        Admin escape hatch (panel button / API). Clears the recent ring buffer and
        the grudge counter. Returns the fresh state, or ``None`` if no user.
        """
        from core.agent.affect import AFFECT_FIELD_KEY, AffectState, AffectTuning

        tuning = AffectTuning.from_settings(get_settings())
        fresh = AffectState.fresh(tuning)
        fresh.updated_at = datetime.now(timezone.utc).isoformat()
        fresh.last_trigger = "reset"
        workspace_id = await self.bootstrap.ensure_workspace(user_id)
        async with self.db.get_session() as session:
            primary = await self.repository.get_primary_user_entity(session, workspace_id)
            if not primary:
                return None
            await self.repository.upsert_field(
                session,
                UpsertFieldInput(
                    entity_id=primary.id,
                    key=AFFECT_FIELD_KEY,
                    value_type="json",
                    value=fresh.to_field(),
                    source="affect",
                    source_message_id=None,
                    actor="system",
                ),
            )
            await session.commit()
            return fresh

    # Relation types that describe a person's relationship to the user, mapped to a
    # display label. Coarse (gender-neutral); a free-text ``relationship`` field on
    # the entity, when present, always wins over this.
    _RELATION_LABELS: dict[str, str] = {
        "sibling_of": "irmão/irmã",
        "child_of": "filho/filha",
        "parent_of": "pai/mãe",
        "related_to": "parente",
    }

    async def resolve_person_contact(
        self, *, user_id: UUID, query: str, min_score: float = 0.3,
    ) -> dict[str, Any] | None:
        """Resolve a person the user knows into a WhatsApp-sendable contact (PKS-first).

        Looks up a ``person`` entity in the user's PKS by name/alias/relationship and
        reads its contact fields (``whatsapp_jid``, ``phone_e164``, ``linked_user_id``)
        plus the relationship. Returns ``None`` when nothing matches above
        ``min_score`` — the caller then falls back to the saved contact table / phone-
        book. A match with no number is still returned (jid/phone ``None``) so the
        caller can decide to ask for it and persist it back onto this entity.
        """
        q = (query or "").strip()
        if not q:
            return None
        workspace_id = await self.bootstrap.ensure_workspace(user_id)
        async with self.db.get_session() as session:
            candidates = await self.resolver.resolve(
                session,
                workspace_id=workspace_id,
                query=q,
                entity_type="person",
                limit=3,
            )
            if not candidates or candidates[0].score < min_score:
                return None
            top = candidates[0]
            entity = top.entity
            fields = await self.repository.get_entity_fields(session, entity.id)
            fmap = {f.key: (str(f.value).strip() if f.value is not None else None) for f in fields}

            def _pick(*keys: str) -> str | None:
                for k in keys:
                    v = fmap.get(k)
                    if v:
                        return v
                return None

            relationship = _pick("relationship", "relacao", "parentesco")
            if not relationship:
                relations = await self.repository.get_relations(session, entity.id)
                for rel in relations:
                    label = self._RELATION_LABELS.get(rel.relation_type)
                    if label:
                        relationship = label
                        break

            return {
                "entity_id": str(entity.id),
                "name": entity.name,
                "jid": _pick("whatsapp_jid", "jid"),
                "phone_e164": _pick("phone_e164", "phone", "telefone"),
                "linked_user_id": _pick("linked_user_id"),
                "relationship": relationship,
                "score": top.score,
                "candidates": [
                    {"entity_id": str(c.entity.id), "name": c.entity.name, "score": c.score}
                    for c in candidates
                ],
            }

    async def remember_user_city(
        self,
        *,
        user_id: UUID,
        city: str,
        state: str | None = None,
        country: str | None = None,
        source_message_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Persist the user's city: upsert a ``place`` entity + ``located_in`` relation.

        Used when the user is asked for their city and answers — so Neo learns it
        once and never asks again. Returns the stored location, or ``None`` if there
        is no primary user to link.
        """
        name = (city or "").strip()
        if not name:
            return None
        meta: dict[str, Any] = {}
        if state:
            meta["state"] = state.strip()
        if country:
            meta["country"] = country.strip()

        workspace_id = await self.bootstrap.ensure_workspace(user_id)
        async with self.db.get_session() as session:
            primary = await self.repository.get_primary_user_entity(session, workspace_id)
            if not primary:
                return None
            place, _ = await self.repository.upsert_entity(
                session,
                UpsertEntityInput(
                    workspace_id=workspace_id,
                    name=name,
                    type="place",
                    metadata=meta,
                    source="conversation",
                    source_message_id=source_message_id,
                ),
            )
            await self.repository.upsert_relation(
                session,
                UpsertRelationInput(
                    workspace_id=workspace_id,
                    from_entity_id=primary.id,
                    relation_type="located_in",
                    to_entity_id=place.id,
                    source="conversation",
                    source_message_id=source_message_id,
                ),
            )
            await session.commit()
            return self._format_place(place)

    @staticmethod
    def _format_place(place: Any) -> dict[str, Any]:
        meta = place.metadata or {}
        parts = [place.name]
        if meta.get("state"):
            parts.append(str(meta["state"]))
        if meta.get("country"):
            parts.append(str(meta["country"]))
        return {
            "city": place.name,
            "full": ", ".join(parts),
            "state": meta.get("state"),
            "country": meta.get("country"),
            "source": "pks",
        }
