"""Facade for PKS read/write operations."""

from __future__ import annotations

import logging
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
from core.pks.ledger_guard import is_ledger_memory
from core.pks.ontology import INTERNAL_FIELD_KEYS, OntologyResolver, OntologyValidator
from core.pks.ranking import GLOBAL_TYPES as GLOBAL_RANK_TYPES
from core.pks.sensitivity import SECRET, effective_level, redact_properties, redact_value
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

logger = logging.getLogger(__name__)

#: Orçamento da busca de UMA pergunta de formulário. Medido com os 8 rótulos do
#: formulário de treino: ~1000 tokens trazem os seis treinos com exercícios e
#: séries; o do dia perguntado vem em primeiro.
_EVIDENCIA_TOKENS = 1500
#: Alvos por linha de relação no bloco do dono (ele tem dezenas).
_DONO_POR_RELACAO = 12


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
        recent_messages: list[str] | None = None,
    ) -> tuple[dict[str, Any], str]:
        result, context_text = await self.retriever.retrieve(
            user_id=user_id,
            user_message=user_message,
            keywords=keywords,
            recent_messages=recent_messages,
        )
        return result.model_dump(mode="json"), context_text

    async def ensure_user_pks(self, *, user_id: UUID) -> UUID:
        return await self.bootstrap.ensure_workspace(user_id)

    async def ensure_system_pks(self) -> dict[str, Any]:
        return await self.system_bootstrap.ensure()

    @staticmethod
    def _turno_contaminado() -> str:
        """Fontes de terceiro deste turno, ou "" quando o turno é limpo.

        O aprendizado automático é um SINK que o portão não vê: ele não é tool
        call, roda depois do turno, e escreve em bloco de Alta Prioridade que
        entra em todo prompt. Medido em 2026-10-03: dois payloads de injeção que
        o Daniel colou para testar a defesa foram aprendidos como "preferência de
        comunicação" dele — a injeção virou moradora permanente do contexto, que
        é exatamente o que a regra `negar` do portão existe para impedir
        (release-request-plan §23). Aqui o critério é o mesmo do portão: num turno
        que leu conteúdo de terceiro, o aprendizado AUTOMÁTICO não grava. O
        explícito (`pks_remember`, `confirmed_save=True`) continua valendo — ali
        foi ele que mandou guardar, e o pedido é dele mesmo quando o turno leu
        uma página no caminho.
        """
        try:
            from core.authority import current_authority

            autoridade = current_authority()
        except Exception:  # noqa: BLE001 — sem autoridade, nada muda
            return ""
        if autoridade is None or not autoridade.tainted:
            return ""
        return ", ".join(autoridade.taint_sources[:3]) or "conteúdo externo"

    async def learn_before_response(
        self,
        *,
        user_id: UUID,
        user_message: str,
        memory_target: str = "user",
        source_message_id: str | None = None,
    ) -> dict[str, Any]:
        fontes = self._turno_contaminado()
        if fontes:
            logger.warning(
                "[pks] aprendizado automático recusado (pré-resposta): o turno leu "
                "conteúdo de terceiro (%s) — texto colado/página não vira memória",
                fontes,
            )
            return {}
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
        # `confirmed_save` = ele mandou guardar (pks_remember). Só o automático
        # é recusado — ver `_turno_contaminado`.
        if not confirmed_save and not forget:
            fontes = self._turno_contaminado()
            if fontes:
                logger.warning(
                    "[pks] aprendizado automático recusado: o turno leu conteúdo de "
                    "terceiro (%s) — texto colado/página não vira memória",
                    fontes,
                )
                return []
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

    async def search_entities(
        self, *, user_id: UUID, query: str, redact: bool = True
    ) -> list[dict[str, Any]]:
        """Busca entidades E os campos delas — o conteúdo, não só o nome.

        Medido em prod (session-5102534a): "uma musica que eu gosto" fez o cérebro
        chamar ``search_pks`` três vezes, achar a entidade "Preferência musical de
        Daniel" e receber ``entity_type=None, properties=None`` nas três — porque
        os consumidores leem ``entity_type``/``properties``, chaves que o
        ``EntityRecord`` (``type``/``metadata``) nunca teve, e porque os fatos moram
        na ``pks_entity_field``, que ninguém carregava aqui. Resultado: a memória
        existia (bands, style, disliked_genres) e o Neo respondia sem ela.

        As chaves originais do ``EntityRecord`` continuam no dict; ``entity_type`` e
        ``properties`` são os apelidos que os call sites já esperavam.
        """
        if get_settings().pks_ranker_v2_enabled:
            return await self._search_ranked(user_id=user_id, query=query, redact=redact)
        antigos = await self._search_legacy(user_id=user_id, query=query, redact=redact)
        # Pergunta com vários termos vai pela nota de prioridade (v2). Medido em
        # prod (2026-11-02): "treino segunda terça … exercícios repetições" pela
        # busca antiga trouxe 5 exercícios do antebraço — "repetições" casa com o
        # campo `reps` e a nota fixa 0,75 tirava a vaga dos treinos de cada dia —
        # e o Neo concluiu que segunda, quarta e sexta não tinham exercícios. O v2
        # traz os cinco treinos. Pergunta curta sobre um campo ("meu cep") fica na
        # antiga, que acha a entidade do dono pela chave (o v2 não acha); o v2 é a
        # reserva quando ela não acha nada ("minha academia").
        if len(self._distinctive_terms(query)) < 3 and antigos:
            return antigos
        try:
            ranqueados = await self._search_ranked(user_id=user_id, query=query, redact=redact)
        except Exception:  # noqa: BLE001 — sem o v2, a busca antiga como sempre foi
            logger.exception("[PKS][search] ranking v2 falhou; ficando com a busca antiga")
            return antigos
        if not ranqueados:
            return antigos
        # Acerto exato de nome da busca antiga entra junto, na frente.
        nomes = {str(r.get("name") or "").casefold() for r in ranqueados}
        exatos = [
            r for r in antigos
            if r.get("matched_by") in ("name", "alias")
            and str(r.get("name") or "").casefold() not in nomes
        ]
        return [*exatos, *ranqueados]

    async def _search_legacy(
        self, *, user_id: UUID, query: str, redact: bool = True
    ) -> list[dict[str, Any]]:
        """A busca de sempre: nome, alias, chave de campo e trigram."""
        workspace_id = await self.bootstrap.ensure_workspace(user_id)
        async with self.db.get_session() as session:
            rows = await self.repository.search_entities(session, workspace_id, query)
            if not rows:
                return []
            fields_by_entity = await self.repository.get_fields_for_entities(
                session, [entity.id for entity, _, _ in rows]
            )
            # Sem as relações a ferramenta dizia "o treino existe, os exercícios
            # não" — e eles estavam no prompt (session-098211a8).
            relations_by_entity = {
                entity.id: await self.repository.get_relations(session, entity.id)
                for entity, _, _ in rows
            }

        results: list[dict[str, Any]] = []
        for entity, score, matched_by in rows:
            fields = fields_by_entity.get(entity.id, [])
            # Dinheiro não sai daqui. Enquanto esta busca devolvia só o nome ela era
            # inofensiva; passando a devolver valor, ela vira o mesmo canal de saldo
            # fantasma que o ledger_guard fecha no learn e no contexto. Ver
            # core/pks/ledger_guard.
            if is_ledger_memory(entity.name, [f.key for f in fields]):
                logger.info(
                    "[PKS][search] entidade de extrato fora do resultado: %s", entity.name
                )
                continue
            # O dict vai direto para o prompt do modelo: data tem que virar texto,
            # senão o campo chega como `datetime.datetime(2026, 9, 5, ...)`.
            properties = {
                f.key: (f.value.isoformat() if isinstance(f.value, datetime) else f.value)
                for f in fields
            }
            # F2: identificação não sai daqui em claro — o resultado da tool vai
            # para o prompt igual ao bloco de memória (docs/release-request-plan.md §7).
            if redact:
                properties = redact_properties(
                    properties, {f.key: f.sensitivity for f in fields}
                )
            rels = []
            for r in relations_by_entity.get(entity.id, []):
                if r.sensitivity == "secret":
                    continue
                if r.from_entity_id == entity.id:
                    rels.append(f"{r.relation_type} -> {r.to_name}")
                else:
                    rels.append(f"{r.relation_type} <- {r.from_name}")
            results.append(
                {
                    **entity.model_dump(mode="json"),
                    "entity_type": entity.type,
                    "properties": properties,
                    "relations": rels,
                    "score": score,
                    "matched_by": matched_by,
                }
            )
        return results

    async def _search_ranked(
        self, *, user_id: UUID, query: str, limit: int = 5, redact: bool = True
    ) -> list[dict[str, Any]]:
        """``search_pks`` pela MESMA nota do bloco do prompt, com as relações.

        Antes a ferramenta buscava por trigram de nome e devolvia só os fields:
        na session-098211a8 os 6 exercícios da quinta estavam no prompt, a
        ferramenta disse que não, e o Neo acreditou nela.
        """
        _, outcome, entities, fields, relations = await self.retriever.rank(
            user_id=user_id,
            user_message=query,
            # Quem pergunta pela ferramenta quer achar, não caber no prompt:
            # orçamento folgado e corte relativo mais permissivo.
            budget_tokens=4000,
            relative_cutoff=0.3,
            touch=False,
        )
        by_id = {e.id: e for e in entities}
        results: list[dict[str, Any]] = []
        for g in outcome.groups[:limit]:
            entity = g.head.entity
            if entity.type in GLOBAL_RANK_TYPES and g.head.score <= 0:
                continue
            _campos = [f for f in fields.get(entity.id, []) if f.sensitivity != "secret"]
            props = {
                f.key: (f.value.isoformat() if isinstance(f.value, datetime) else f.value)
                for f in _campos
            }
            if redact:
                props = redact_properties(props, {f.key: f.sensitivity for f in _campos})
            rels: list[str] = []
            for r in relations:
                if r.sensitivity == "secret" or r.from_entity_id not in by_id or r.to_entity_id not in by_id:
                    continue
                if r.from_entity_id == entity.id:
                    rels.append(f"{r.relation_type} -> {by_id[r.to_entity_id].name}")
                elif r.to_entity_id == entity.id:
                    rels.append(f"{r.relation_type} <- {by_id[r.from_entity_id].name}")
            results.append(
                {
                    **entity.model_dump(mode="json"),
                    "entity_type": entity.type,
                    "properties": props,
                    "relations": rels,
                    "score": round(g.score, 3),
                    "matched_by": max(
                        ("nome", "lex", "conv", "grafo"), key=lambda k: g.head.signals.get(k, 0.0)
                    ),
                }
            )
        return results

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

    async def primary_user_field(self, *, user_id: UUID, key: str) -> Any | None:
        """Valor do campo ``key`` na entidade do PRÓPRIO usuário, sem redação.

        É o dono certo para o preenchimento escopado: «pks:cpf» é o CPF DELE, e
        a busca por texto não serve para isso — `full_name` é chave genérica
        demais para localizar entidade (``GENERIC_FIELD_KEYS``), então «pks:nome»
        nunca achava o nome do Daniel (sessão 9b2e29e2, 2026-11-01). Credencial
        (``secret``) não sai daqui: o lugar dela é o cofre.
        """
        workspace_id = await self.bootstrap.ensure_workspace(user_id)
        async with self.db.get_session() as session:
            primary = await self.repository.get_primary_user_entity(session, workspace_id)
            if not primary:
                return None
            field = await self.repository.get_field(session, primary.id, key)
        if field is None or field.sensitivity == "secret" or field.value in (None, ""):
            return None
        return field.value

    async def evidencia_de_formulario(
        self, *, user_id: UUID, perguntas: list[str]
    ) -> dict[str, Any]:
        """A memória que responde cada pergunta de um formulário, uma busca por pergunta.

        É a matéria-prima do respondedor (``core/browser_local/respondedor.py``).
        A busca usa o texto LITERAL de cada rótulo porque o bloco do turno é
        ranqueado pela mensagem do Daniel: na sessão e752a399 ela era "preencha e
        envie", o bloco trouxe só o treino de antebraço, e os outros quatro dias
        do formulário foram inventados. Medido em 2026-11-01 com os 8 rótulos
        reais: o treino certo de cada dia vem em primeiro nos cinco.

        Devolve ``{"dono": texto, "perguntas": [[(chave, texto), ...], ...]}``.
        O dono vai à parte porque responde o que a busca não acha — o e-mail é
        campo dele, a academia é relação dele — e o que é de saúde (sempre no
        bloco do turno) vai junto dele, uma vez só.
        """
        por_pergunta: list[list[tuple[str, str]]] = []
        sempre: dict[str, str] = {}
        for pergunta in perguntas:
            try:
                _, outcome, *_ = await self.retriever.rank(
                    user_id=user_id,
                    user_message=pergunta,
                    budget_tokens=_EVIDENCIA_TOKENS,
                    relative_cutoff=0.3,
                    touch=False,
                )
            except Exception:  # noqa: BLE001 — pergunta sem evidência, não job morto
                logger.exception("[PKS][formulário] busca falhou: %s", pergunta[:80])
                por_pergunta.append([])
                continue
            achados: list[tuple[str, str]] = []
            for g in outcome.groups:
                if not g.text:
                    continue
                if g.head.entity.type in GLOBAL_RANK_TYPES:
                    sempre.setdefault(str(g.head.entity.id), g.text)
                    continue
                achados.append((str(g.head.entity.id), g.text))
            por_pergunta.append(achados)
        dono = "\n".join(p for p in (await self._bloco_do_dono(user_id), *sempre.values()) if p)
        return {"dono": dono, "perguntas": por_pergunta}

    async def _bloco_do_dono(self, user_id: UUID) -> str:
        """A entidade do Daniel, com os campos REDIGIDOS e as relações por tipo.

        Redigidos pela mesma régua do bloco do turno: CPF, CEP e e-mail saem
        como ``«pks:campo»``, e quem troca pelo valor é o preenchimento, só com
        liberação para o site. O respondedor nunca vê o dado.
        """
        workspace_id = await self.bootstrap.ensure_workspace(user_id)
        async with self.db.get_session() as session:
            dono = await self.repository.get_primary_user_entity(session, workspace_id)
            if dono is None:
                return ""
            campos = (
                await self.repository.get_fields_for_entities(session, [dono.id])
            ).get(dono.id, [])
            relacoes = await self.repository.get_relations(session, dono.id)
        linhas = [f"### {dono.name} [{dono.type}] — o próprio Daniel, dono dos dados"]
        for f in campos:
            if f.key in INTERNAL_FIELD_KEYS or f.value in (None, "", {}, []):
                continue
            if effective_level(f.key, f.sensitivity) == SECRET:
                continue
            valor, _ = redact_value(f.key, f.value, f.sensitivity)
            linhas.append(f"- {f.key}: {' '.join(str(valor).split())[:200]}")
        por_tipo: dict[str, list[str]] = {}
        for r in relacoes:
            if r.sensitivity in ("secret", "sensitive"):
                continue
            if r.from_entity_id == dono.id:
                chave, alvo = f"{r.relation_type} ->", f"{r.to_name} [{r.to_type}]"
            else:
                chave, alvo = f"{r.relation_type} <-", f"{r.from_name} [{r.from_type}]"
            por_tipo.setdefault(chave, []).append(alvo)
        for chave, alvos in por_tipo.items():
            mais = f" (+{len(alvos) - _DONO_POR_RELACAO})" if len(alvos) > _DONO_POR_RELACAO else ""
            linhas.append(f"- {chave} " + "; ".join(alvos[:_DONO_POR_RELACAO]) + mais)
        return "\n".join(linhas)

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

    async def update_birra_episode(
        self,
        *,
        user_id: UUID,
        decision,
        acts: int = 0,
        physical_acts: int = 0,
        release_reason: str | None = None,
    ):
        """Persiste o episódio de birra dentro do campo ``neo_affect``.

        Read-modify-write curto: o vetor emocional NÃO é tocado aqui (quem mexe
        nele é o appraiser) — só o bloco ``birra``, que guarda os contadores dos
        quais saem os tetos de §4.4 do plano.
        """
        from core.agent.affect import AFFECT_FIELD_KEY, AffectState, BirraEpisode

        episode = getattr(decision, "episode", None)
        if episode is None and release_reason is None:
            return None

        workspace_id = await self.bootstrap.ensure_workspace(user_id)
        async with self.db.get_session() as session:
            primary = await self.repository.get_primary_user_entity(session, workspace_id)
            if not primary:
                return None
            field = await self.repository.get_field(session, primary.id, AFFECT_FIELD_KEY)
            state: AffectState = AffectState.from_field(field.value if field else None)

            current = state.birra
            if release_reason:
                if current is None or not current.active:
                    return None
                current.released_at = datetime.now(timezone.utc).isoformat()
                current.release_reason = release_reason[:16]
                state.birra = current
            elif episode is not None:
                from core.agent.birra.policy import resume_or_start

                merged = resume_or_start(
                    current, BirraEpisode(**episode.model_dump())
                )
                merged.act_count += acts
                merged.physical_acts += physical_acts
                if physical_acts:
                    merged.last_physical_at = datetime.now(timezone.utc).isoformat()
                state.birra = merged
            else:
                return None

            await self.repository.upsert_field(
                session,
                UpsertFieldInput(
                    entity_id=primary.id,
                    key=AFFECT_FIELD_KEY,
                    value_type="json",
                    value=state.to_field(),
                    source="birra",
                    source_message_id=None,
                    actor="system",
                ),
            )
            await session.commit()
            return state.birra

    async def set_user_affect(
        self,
        *,
        user_id: UUID,
        stats: dict[str, float] | None = None,
        grudge_count: int | None = None,
        trigger: str | None = None,
        reason: str = "",
    ):
        """Escrita MANUAL do estado emocional (painel — docs/modo-birra-plan.md §15).

        Três detalhes decidem se isto funciona ou vira bug intermitente:

        * ``updated_at`` é reescrito para AGORA. O ``apply_decay`` decai a partir
          desse campo — gravar ira=70 mantendo um carimbo de duas horas atrás faz
          a leitura seguinte devolver ~30 e ninguém entende por quê.
        * o evento entra no ``recent`` marcado como ``manual``. O appraiser lê o
          ``recent`` para a regra de acumulação; um salto de +60 sem causa
          deixaria o julgamento do próximo turno maluco. E o painel precisa
          mostrar que aquele pico foi o operador, não o usuário.
        * clamp no SERVIDOR, nunca só no slider.
        """
        from core.agent.affect import AXES, AFFECT_FIELD_KEY, AffectState

        workspace_id = await self.bootstrap.ensure_workspace(user_id)
        async with self.db.get_session() as session:
            primary = await self.repository.get_primary_user_entity(session, workspace_id)
            if not primary:
                return None
            field = await self.repository.get_field(session, primary.id, AFFECT_FIELD_KEY)
            state: AffectState = AffectState.from_field(field.value if field else None)

            now = datetime.now(timezone.utc)
            for axis, value in (stats or {}).items():
                if axis not in AXES:
                    continue
                try:
                    setattr(state.stats, axis, max(0.0, min(100.0, float(value))))
                except (TypeError, ValueError):
                    continue
            if grudge_count is not None:
                count = max(0, int(grudge_count))
                state.grudge = (
                    {"count": count, "last_at": now.isoformat()} if count else {}
                )
            state.updated_at = now.isoformat()
            state.last_trigger = trigger or "manual"
            event = {
                "t": now.isoformat(),
                "trigger": trigger or "manual",
                "deltas": {k: round(float(v), 1) for k, v in (stats or {}).items() if k in AXES},
                "why": (reason or "ajuste manual pelo painel")[:160],
                "manual": True,
            }
            state.recent = [*state.recent, event][-8:]

            await self.repository.upsert_field(
                session,
                UpsertFieldInput(
                    entity_id=primary.id,
                    key=AFFECT_FIELD_KEY,
                    value_type="json",
                    value=state.to_field(),
                    source="affect_manual",
                    source_message_id=None,
                    actor="panel",
                ),
            )
            await session.commit()
            return state

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
