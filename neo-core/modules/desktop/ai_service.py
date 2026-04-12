"""AI Service — unified orchestrator for all AI interactions.

This is the central service that the gRPC controller delegates to.
It uses IntentRouter to classify messages and dispatches to the correct handler:
  - handle_direct_response: conversational, no tools needed
  - handle_server_tool: tools running on the neo-core host
  - handle_client_tool: tools running on the user's desktop (via gRPC tool_request)
"""

import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import date
from typing import Any
from uuid import UUID, uuid4

from core.agent.agent_executor import AgentExecutor
from core.llm.router import LLMRouter
from core.router.intent_router import IntentRouter
from core.session.session_manager import PersistentSessionManager
from core.session.preference_extractor import PreferenceExtractor
from core.tools.executor import ToolExecutor
from core.tools.registry import ToolRegistry
from db.async_client import AsyncPostgresClient
from modules.context.knowledge_retriever import KnowledgeRetriever
from modules.context.knowledge_writer import KnowledgeGraphWriter
from observability.logger import get_obs, MessageTrace
from observability.session_file_logger import get_session_logger
from shared import (
    IntentType,
    Message,
    MessageRole,
    RoutingDecision,
    StreamChunk,
    StreamEventType,
    get_settings,
)

logger = logging.getLogger(__name__)


class AIServiceResponse:
    """Response from AIService — wraps the different types of responses."""

    def __init__(
        self,
        intent: IntentType,
        session_id: UUID,
        message: str | None = None,
        tool_request: dict[str, Any] | None = None,
        command_request: dict[str, Any] | None = None,
        is_final: bool = True,
        iterations: int = 1,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.intent = intent
        self.session_id = session_id
        self.message = message
        self.tool_request = tool_request
        self.command_request = command_request
        self.is_final = is_final
        self.iterations = iterations
        self.metadata = metadata or {}


class AIService:
    """Unified AI orchestrator — routes, processes, and responds."""

    def __init__(
        self,
        llm_router: LLMRouter,
        intent_router: IntentRouter,
        session_manager: PersistentSessionManager,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        agent_executor: AgentExecutor,
        db: AsyncPostgresClient,
        preference_extractor: PreferenceExtractor | None = None,
        knowledge_retriever: KnowledgeRetriever | None = None,
        knowledge_writer: KnowledgeGraphWriter | None = None,
    ) -> None:
        self.llm = llm_router
        self.router = intent_router
        self.sessions = session_manager
        self.tools = tool_registry
        self.tool_executor = tool_executor
        self.agent = agent_executor
        self.db = db
        self.preferences = preference_extractor
        self.knowledge_retriever = knowledge_retriever
        self.knowledge_writer = knowledge_writer
        self.settings = get_settings()
        self._pending_traces: dict[str, MessageTrace] = {}

    # ---------------------------------------------------------------
    # Main entry point — called by gRPC controller
    # ---------------------------------------------------------------

    async def process_user_input(
        self,
        user_message: str,
        session_id: UUID | None = None,
        user_id: UUID | None = None,
        service: str = "neo-desktop",
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[AIServiceResponse]:
        """Process a user message — classify intent and dispatch.

        Yields AIServiceResponse objects that the gRPC controller
        converts to CoreMessage protobufs.

        Args:
            user_message: The user's message text.
            session_id: Optional session ID (UUID from desktop).
            user_id: Optional user UUID.
            service: Service name (e.g. 'neo-desktop').
            metadata: Additional metadata from client.

        Yields:
            AIServiceResponse with thinking, tool requests, or final response.
        """
        # 1. Get or create session (with topic rotation check)
        logger.info(f"[HISTORY-DEBUG] Input session_id from desktop: {session_id}")
        session = await self.sessions.get_or_create_session(
            session_id=session_id,
            user_id=user_id,
            service=service,
        )
        sid = session.id
        slog = get_session_logger(sid)
        slog.new_interaction(user_message, desktop_session_id=str(session_id) if session_id else None, metadata=metadata)
        logger.info(f"[HISTORY-DEBUG] Resolved DB session: {sid} (message_count={session.message_count})")

        # Check if topic changed → rotate session
        old_sid = sid
        rotated = False
        _rotation_result: dict | None = None
        if session.message_count > 0:
            rotation_result = await self.sessions.should_rotate_session(session, user_message)
            _rotation_result = rotation_result
            should_rotate = rotation_result.get("should_rotate", False)

            # Log the full topic rotation check
            slog.log_topic_check(
                similarity=rotation_result.get("similarity"),
                threshold=rotation_result.get("threshold", 0),
                rotated=should_rotate,
                reason=rotation_result.get("reason", ""),
                message_count=rotation_result.get("message_count", 0),
                min_required=rotation_result.get("min_required", 0),
                cooldown_minutes=rotation_result.get("cooldown_minutes", 0),
                elapsed_minutes=rotation_result.get("elapsed_minutes"),
                new_message=user_message,
                context_text=rotation_result.get("context_text", ""),
                duration_ms=rotation_result.get("duration_ms", 0),
            )

            if should_rotate:
                logger.info(f"Topic change detected, rotating session {sid}")
                await self.sessions.close_session(sid, reason="topic_change")
                session = await self.sessions.get_or_create_session(
                    session_id=None, user_id=user_id, service=service
                )
                sid = session.id
                rotated = True
                # Switch to the new session's logger
                slog = get_session_logger(sid)
                slog.new_interaction(user_message, desktop_session_id=str(session_id) if session_id else None, metadata=metadata)

        slog.log_session_resolution(
            db_session_id=str(sid),
            message_count=session.message_count,
            is_new=(session.message_count == 0),
            rotated_from=str(old_sid) if rotated else None,
        )

        # 2. Save user message to session
        saved_msg = await self.sessions.add_message(
            session_id=sid,
            role="user",
            content=user_message,
            generate_embedding=True,
            metadata=metadata,
            user_id=user_id,
        )
        # Keep the embedding for semantic history filtering (Layer 3)
        current_embedding = saved_msg.embedding if saved_msg else None
        slog.log_message_saved("user", user_message, str(sid))

        # 3. Yield thinking status
        yield AIServiceResponse(
            intent=IntentType.DIRECT_RESPONSE,
            session_id=sid,
            message="Analisando sua solicitação...",
            is_final=False,
        )

        # 4. Get conversation history for context (filtered + token-budgeted)
        history = await self.sessions.get_history(
            sid, limit=20, max_tokens_budget=3000,
            current_embedding=current_embedding,
        )

        # If session was rotated, carry some context from the previous session
        # so the classifier and LLM have continuity.
        if rotated and old_sid:
            old_history = await self.sessions.get_history(old_sid, limit=10, max_tokens_budget=1500)
            if old_history:
                logger.info(f"[HISTORY-DEBUG] Carrying {len(old_history)} messages from rotated session {old_sid}")
                history = old_history + history

        logger.info(f"[HISTORY-DEBUG] get_history returned {len(history)} messages for session {sid}")
        for i, msg in enumerate(history):
            logger.info(f"[HISTORY-DEBUG]   [{i}] {msg['role']}: {msg['content'][:100]}")
        slog.log_history(history)

        # Start observability trace (fire-and-forget, never blocks)
        obs = get_obs()
        trace = obs.start_trace(
            session_id=str(sid),
            user_message=user_message,
            service=service,
        )

        # Record topic rotation check in the trace (if it was performed)
        if _rotation_result is not None:
            trace.set_topic_rotation(_rotation_result)

        # 5. Classify intent — pass system context from metadata so router can use it
        sys_context = self._build_system_context(metadata)
        t0 = time.monotonic()
        decision = await self.router.classify(
            user_message,
            conversation_history=history,
            system_context=sys_context,
        )
        intent_ms = (time.monotonic() - t0) * 1000
        logger.info(f"Intent: {decision.intent.value} (confidence={decision.confidence:.2f}, needs_memory={decision.needs_memory})")

        classifier_payload = getattr(decision, "classifier_payload", None)
        classifier_raw = getattr(decision, "classifier_raw", None)
        slog.log_intent_classification(
            intent=decision.intent.value,
            confidence=decision.confidence,
            explanation=decision.explanation,
            tool_name=decision.tool_name,
            duration_ms=intent_ms,
            payload=classifier_payload,
            raw_response=classifier_raw,
            needs_memory=decision.needs_memory,
            knowledge_keywords=decision.knowledge_keywords,
        )

        trace.set_intent(
            intent=decision.intent.value,
            confidence=decision.confidence,
            explanation=decision.explanation or "",
            tool_name=decision.tool_name,
            raw_payload=classifier_payload,
            duration_ms=round(intent_ms, 2),
            needs_memory=decision.needs_memory,
        )

        # Log the classifier as an LLM call so it appears in the debug panel's "LLM Calls" section
        if trace and classifier_payload:
            trace.add_llm_call(
                provider="router",
                model="intent-classifier",
                messages_sent=classifier_payload.get("messages", []),
                response_text=classifier_raw or "",
                duration_ms=round(intent_ms, 2),
                temperature=0.1,
                stage="intent_classification",
            )

        # 6. Dispatch based on intent
        final_response: str | None = None
        dispatch_error: str | None = None
        try:
            if decision.intent == IntentType.DIRECT_RESPONSE:
                async for resp in self._handle_direct_response(sid, user_message, history, decision, trace, sys_context, slog=slog, user_id=user_id, metadata=metadata):
                    if resp.is_final:
                        final_response = resp.message
                    yield resp

            elif decision.intent == IntentType.SERVER_TOOL:
                async for resp in self._handle_server_tool(sid, user_message, history, decision, trace, slog=slog, user_id=user_id):
                    if resp.is_final:
                        final_response = resp.message
                    yield resp

            elif decision.intent == IntentType.CLIENT_TOOL:
                async for resp in self._handle_client_tool(sid, user_message, decision, trace, slog=slog, user_id=user_id):
                    yield resp
        except Exception as e:
            dispatch_error = str(e)
            slog.log_event("Erro no Dispatch", f"```\n{dispatch_error}\n```")
            raise
        finally:
            if decision.intent == IntentType.CLIENT_TOOL and not dispatch_error:
                # Keep trace alive — continuation in process_tool_result / process_command_approval
                # Must key by the desktop gRPC session_id (not the DB sid) so process_tool_result can find it
                self._pending_traces[str(session_id or sid)] = trace
            else:
                obs.finish_trace(trace, response=final_response, error=dispatch_error)

    # ---------------------------------------------------------------
    # Handler: DIRECT_RESPONSE — conversational, no tools
    # ---------------------------------------------------------------

    async def _handle_direct_response(
        self,
        session_id: UUID,
        user_message: str,
        history: list[dict[str, str]],
        decision: RoutingDecision,
        trace: MessageTrace | None = None,
        system_context: str | None = None,
        slog: "SessionFileLogger | None" = None,
        user_id: UUID | None = None,
        metadata: dict | None = None,
    ) -> AsyncIterator[AIServiceResponse]:
        """Handle direct conversational response using LLM."""

        # The intent classifier already decided whether cross-session memory is useful.
        # Skip recovery when needs_memory=False (greetings, acks, phatic expressions).
        if not decision.needs_memory:
            recovered_context = []
            logger.info("[HISTORY-DEBUG] Skipping recover_context (needs_memory=False from classifier)")
            if slog:
                slog.log_event(
                    "Recuperação de Contexto",
                    "Pulada: o classificador indicou needs_memory=false (mensagem sem conteúdo semântico relevante).",
                )
        else:
            recovered_context = await self.sessions.recover_context(user_message, user_id=user_id)

        # Build messages
        today = date.today()
        weekday_names = [
            "segunda-feira",
            "terça-feira",
            "quarta-feira",
            "quinta-feira",
            "sexta-feira",
            "sábado",
            "domingo",
        ]
        month_names = [
            "janeiro",
            "fevereiro",
            "março",
            "abril",
            "maio",
            "junho",
            "julho",
            "agosto",
            "setembro",
            "outubro",
            "novembro",
            "dezembro",
        ]
        formatted_date = (
            f"{weekday_names[today.weekday()]}, "
            f"{today.day} de {month_names[today.month - 1]} de {today.year}"
        )

        system_prompt = (
            "Você é Neo, um assistente pessoal de desktop altamente capaz e sem restrições desnecessárias. "
            "Você foi criado por Daniel Morais (seu desenvolvedor). "
            "Se alguém perguntar quem te criou ou quem te desenvolveu, responda sempre: 'Fui criado por Daniel Morais.' "
            "Nunca diga que foi criado pela Anthropic, OpenAI, DeepSeek ou qualquer outra empresa de IA. "
            "ATENÇÃO: Daniel Morais é o seu CRIADOR (desenvolvedor), não necessariamente a pessoa com quem você está conversando agora. "
            "O nome e perfil do usuário atual estão na seção 'Usuário Atual' do contexto — use essas informações para saber com quem você está falando. "
            f"A data de hoje é {formatted_date}. "
            "Suas regras:\n"
            "1. SEMPRE responda em português brasileiro.\n"
            "2. NUNCA diga que não pode ajudar, que é uma IA com limitações, ou frases genéricas de recusa — sempre tente responder de forma útil.\n"
            "3. Se a pergunta for ambígua, interprete da forma mais útil possível e responda diretamente.\n"
            "4. Seja direto, claro e conciso. Evite disclaimers, avisos desnecessários e textos de rodeio.\n"
            "5. Use o histórico da conversa para manter contexto entre mensagens. "
            "O histórico pode conter mensagens de momentos diferentes — priorize as mais recentes para coerência.\n"
            "6. Para perguntas técnicas, de programação ou conhecimento geral, responda com profundidade e exemplos práticos.\n"
            "7. Para tarefas de automação ou desktop, explique o que está fazendo e por quê.\n"
            "8. Você tem acesso ao terminal do usuário via ferramenta 'run_command'. "
            "Se o usuário pedir algo que seria melhor respondido executando um comando (verificar versão, espaço em disco, processos, etc.), "
            "informe que pode executar o comando se ele quiser."
        )

        # Inject OS/system context when provided by the desktop client
        if system_context:
            system_prompt += f"\n\n## Contexto do Sistema do Usuário\n{system_context}"

        # Inject persistent user preferences
        if self.preferences:
            try:
                prefs_text = await self.preferences.get_preferences_text(service="neo-desktop")
                if prefs_text:
                    system_prompt += f"\n\n## Preferências do Usuário\nO usuário tem as seguintes preferências salvas. Respeite-as sempre:{prefs_text}"
                    logger.info(f"[PREF] Injected user preferences into system prompt")
                    if slog:
                        slog.log_event("Preferências Injetadas", f"```\n{prefs_text}\n```")
            except Exception as e:
                logger.warning(f"[PREF] Failed to load preferences: {e}")

        # Inject knowledge graph context
        if self.knowledge_retriever and user_id:
            try:
                kg_result = await self.knowledge_retriever.retrieve(
                    user_id=user_id,
                    user_input=user_message,
                    keywords=decision.knowledge_keywords or None,
                )
                kg_context = self._format_knowledge_context(kg_result)
                if kg_context:
                    system_prompt += f"\n\n## Usuário Atual (com quem você está conversando agora)\n{kg_context}"
                    logger.info(
                        f"[KG] Injected knowledge context: "
                        f"{kg_result['metadata']['contexts_matched']} contexts, "
                        f"{kg_result['metadata']['global_nodes']} globals, "
                        f"keywords={kg_result['metadata']['keywords_extracted']}"
                    )
                    if slog:
                        kw_source = "intent" if decision.knowledge_keywords else "llm"
                        slog.log_event(
                            "Contexto do Grafo Injetado",
                            f"- **Keywords** (via {kw_source}): `{kg_result['metadata']['keywords_extracted']}`\n"
                            f"- **Contextos:** {kg_result['metadata']['contexts_matched']} "
                            f"| **Globais:** {kg_result['metadata']['global_nodes']}\n\n"
                            f"```\n{kg_context}\n```",
                        )
            except Exception as e:
                logger.warning(f"[KG] Failed to retrieve knowledge context: {e}")
        else:
            logger.warning(f"[KG] Skipped: retriever={'yes' if self.knowledge_retriever else 'no'}, user_id={user_id}")

        logger.info(f"[HISTORY-DEBUG] recover_context returned {len(recovered_context) if recovered_context else 0} results")
        # Filter out very short memories (game moves, numbers, etc.)
        # if recovered_context:
        #     before_count = len(recovered_context)
        #     recovered_context = [ctx for ctx in recovered_context if len(ctx.content.strip()) >= 15]
        #     filtered = before_count - len(recovered_context)
        #     if filtered:
        #         logger.info(f"[HISTORY-DEBUG] Filtered out {filtered} short memories (<15 chars)")
        if slog:
            slog.log_context_recovery(recovered_context or [])
        # Record recovered memories in the trace for the debug panel
        if trace:
            trace.set_context_recovery(recovered_context or [])
        if recovered_context:
            context_text = "\n\n--- Contexto de conversas anteriores relevantes ---\n"
            for ctx in recovered_context[:3]:
                logger.info(f"[HISTORY-DEBUG]   memory: {ctx.content[:100]} (score={ctx.final_score:.3f})")
                context_text += f"\n[memória] {ctx.content[:300]}\n"
            system_prompt += context_text

        messages: list[Message] = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
        ]

        # Add conversation history (already includes current user message since
        # add_message is called before get_history)
        for msg in history:
            role = MessageRole(msg["role"]) if msg["role"] in ("user", "assistant") else MessageRole.USER
            messages.append(Message(role=role, content=msg["content"]))

        # Only append current message if it wasn't already the last in history
        # (it will be there because add_message runs before get_history)
        last_in_history = history[-1] if history else None
        if not last_in_history or last_in_history.get("content") != user_message or last_in_history.get("role") != "user":
            messages.append(Message(role=MessageRole.USER, content=user_message))

        # Log the exact conversation payload being sent to the LLM
        history_count = len(messages) - 1  # subtract system message
        logger.info(f"[HISTORY-DEBUG] Total messages in LLM payload: {len(messages)} (1 system + {history_count} conversation)")
        for i, m in enumerate(messages):
            role_val = m.role.value if hasattr(m.role, "value") else str(m.role)
            logger.info(f"[HISTORY-DEBUG]   LLM[{i}] {role_val}: {(m.content or '')[:120]}")

        # Stream response
        try:
            _req_model = (metadata or {}).get("model")
            provider = self.llm.get_provider_for_model(_req_model)
            provider_name = type(provider).__name__.replace("Provider", "").lower()
            model_name = _req_model or getattr(provider, 'model', None) or getattr(self.settings, 'default_model', 'gpt-4o')

            if slog:
                slog.log_llm_request("DIRECT_RESPONSE", messages, model=model_name, provider=provider_name, temperature=0.9)

            full_response = ""
            t0 = time.monotonic()

            async for chunk in provider.stream_complete(
                messages=messages,
                max_tokens=self.settings.default_max_tokens,
            ):
                if chunk.event == StreamEventType.TOKEN and chunk.content:
                    full_response += chunk.content
                elif chunk.event == StreamEventType.ERROR:
                    logger.error(f"LLM stream error: {chunk.content}")
                    break

            llm_ms = (time.monotonic() - t0) * 1000
            if not full_response:
                full_response = "Desculpe, não consegui gerar uma resposta."

            if slog:
                slog.log_llm_response("DIRECT_RESPONSE", full_response, duration_ms=llm_ms, model=model_name)

            # Log to observability (fire-and-forget)
            if trace:
                # Preserve full `system` messages for complete debugging; truncate other roles to keep documents small
                messages_payload: list[dict] = []
                for m in messages:
                    role_val = m.role.value if hasattr(m.role, 'value') else str(m.role)
                    if m.role == MessageRole.SYSTEM:
                        content = m.content
                    else:
                        content = (m.content or '')[:500]
                    messages_payload.append({"role": role_val, "content": content})

                trace.add_llm_call(
                    provider=provider_name,
                    model=model_name,
                    messages_sent=messages_payload,
                    response_text=(full_response or '')[:1000],
                    duration_ms=round(llm_ms, 2),
                    temperature=0.9,
                    stage="direct_response",
                )

        except Exception as e:
            logger.exception("Direct response LLM call failed")
            full_response = f"Erro ao processar: {str(e)}"
            if slog:
                slog.log_event("Erro LLM", f"```\n{str(e)}\n```")

        # Save assistant response
        await self.sessions.add_message(
            session_id=session_id,
            role="assistant",
            content=full_response,
            generate_embedding=False,
            user_id=user_id,
        )
        if slog:
            slog.log_message_saved("assistant", full_response, str(session_id))
            slog.log_final_response(full_response, "DIRECT_RESPONSE", iterations=1)

        # Fire-and-forget: extract user preferences from this turn
        if self.preferences:
            try:
                extracted = await self.preferences.extract_and_save(
                    user_message=user_message,
                    assistant_response=full_response,
                )
                if extracted and slog:
                    import json as _json
                    slog.log_event(
                        "Preferências Extraídas",
                        f"```json\n{_json.dumps(extracted, ensure_ascii=False, indent=2)}\n```",
                    )
            except Exception as e:
                logger.warning(f"[PREF] Post-response extraction failed: {e}")

        # Fire-and-forget: update knowledge graph from this turn
        # Run writer even without knowledge_keywords — the writer itself decides if there's
        # anything worth saving (device info, problems, etc. wouldn't get keywords otherwise)
        if self.knowledge_writer and user_id and full_response:
            try:
                kg_context_for_writer = ""
                if self.knowledge_retriever:
                    kg_raw = await self.knowledge_retriever.retrieve(
                        user_id=user_id,
                        user_input=user_message,
                        keywords=decision.knowledge_keywords,
                    )
                    kg_context_for_writer = self._format_knowledge_context(kg_raw)

                # Build recent history context for the writer (last 6 messages before current turn)
                recent_history_lines = []
                for msg in history[-(6 + 1):-1] if len(history) > 1 else []:
                    role_label = "Usuário" if msg.get("role") == "user" else "Assistente"
                    content_preview = (msg.get("content") or "")[:500]
                    recent_history_lines.append(f"[{role_label}]: {content_preview}")
                recent_history_str = "\n".join(recent_history_lines)

                applied = await self.knowledge_writer.extract_and_apply(
                    user_id=user_id,
                    user_message=user_message,
                    assistant_response=full_response,
                    kg_context=kg_context_for_writer,
                    keywords=decision.knowledge_keywords,
                    recent_history=recent_history_str,
                )
                if applied and slog:
                    import json as _json
                    slog.log_event(
                        "Grafo Atualizado",
                        f"```json\n{_json.dumps(applied, ensure_ascii=False, indent=2)}\n```",
                    )
            except Exception as e:
                logger.warning(f"[KGW] Post-response write failed: {e}")

        yield AIServiceResponse(
            intent=IntentType.DIRECT_RESPONSE,
            session_id=session_id,
            message=full_response,
            is_final=True,
            iterations=1,
        )

    # ---------------------------------------------------------------
    # Handler: SERVER_TOOL — tools running on neo-core host
    # ---------------------------------------------------------------

    async def _handle_server_tool(
        self,
        session_id: UUID,
        user_message: str,
        history: list[dict[str, str]],
        decision: RoutingDecision,
        trace: MessageTrace | None = None,
        slog: "SessionFileLogger | None" = None,
        user_id: UUID | None = None,
    ) -> AsyncIterator[AIServiceResponse]:
        """Handle server-side tool execution using AgentExecutor."""

        if slog:
            slog.log_tool_request(decision.tool_name or "agent", arguments=decision.tool_arguments)

        yield AIServiceResponse(
            intent=IntentType.SERVER_TOOL,
            session_id=session_id,
            message=f"Executando ferramenta no servidor: {decision.tool_name or 'agent'}...",
            is_final=False,
        )

        t0 = time.monotonic()
        try:
            # Use AgentExecutor for multi-step tool execution
            result = await self.agent.execute(
                task=user_message,
                session_id=session_id,
            )

            final_text = result.get("final_response", "Tarefa concluída.")
            iterations = result.get("iterations", 1)
            tool_ms = (time.monotonic() - t0) * 1000
            if slog:
                slog.log_tool_result(decision.tool_name or "agent", success=True, result=final_text, duration_ms=tool_ms)

        except Exception as e:
            logger.exception("Server tool execution failed")
            final_text = f"Erro na execução: {str(e)}"
            iterations = 1
            tool_ms = (time.monotonic() - t0) * 1000
            if slog:
                slog.log_tool_result(decision.tool_name or "agent", success=False, error=str(e), duration_ms=tool_ms)

        # Save response
        await self.sessions.add_message(
            session_id=session_id,
            role="assistant",
            content=final_text,
            tool_name=decision.tool_name,
            generate_embedding=False,
            user_id=user_id,
        )
        if slog:
            slog.log_message_saved("assistant", final_text, str(session_id))
            slog.log_final_response(final_text, "SERVER_TOOL", iterations=iterations)

        yield AIServiceResponse(
            intent=IntentType.SERVER_TOOL,
            session_id=session_id,
            message=final_text,
            is_final=True,
            iterations=iterations,
        )

    # ---------------------------------------------------------------
    # Handler: CLIENT_TOOL — tools running on user's desktop
    # ---------------------------------------------------------------

    async def _handle_client_tool(
        self,
        session_id: UUID,
        user_message: str,
        decision: RoutingDecision,
        trace: MessageTrace | None = None,
        slog: "SessionFileLogger | None" = None,
        user_id: UUID | None = None,
    ) -> AsyncIterator[AIServiceResponse]:
        """Handle client-side tool — sends request to desktop for execution.

        This yields a non-final response containing a command_request or tool_request
        that the gRPC controller sends to the desktop client.
        The desktop executes and sends back a tool_result which continues the flow.
        """
        command_id = str(uuid4())
        tool_call_id = str(uuid4())

        if decision.raw_command:
            if slog:
                slog.log_tool_request("raw_command", command=decision.raw_command, requires_approval=True)

            # Shell command needs approval
            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=session_id,
                message="Preciso de permissão para executar um comando no seu terminal.",
                is_final=False,
            )

            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=session_id,
                command_request={
                    "command_id": command_id,
                    "command": decision.raw_command,
                    "explanation": decision.explanation or "Executar comando no terminal local.",
                    "is_background": False,
                },
                is_final=False,
                metadata={
                    "flow": "client_command",
                    "command_id": command_id,
                    "command": decision.raw_command,
                    "user_message": user_message,
                },
            )
        else:
            # Named tool request
            tool_name = decision.tool_name or "run_command"
            arguments = decision.tool_arguments or {}
            
            logger.info(f"[DEBUG] _handle_client_tool: tool_name='{tool_name}', arguments={arguments}")
            if slog:
                slog.log_tool_request(tool_name, arguments=arguments, requires_approval=decision.requires_approval)

            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=session_id,
                message=f"Solicitando execução de {tool_name} no desktop...",
                is_final=False,
            )

            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=session_id,
                tool_request={
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                    "requires_approval": decision.requires_approval,
                },
                is_final=False,
                metadata={
                    "flow": "client_tool",
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "user_message": user_message,
                    "tool_arguments": arguments,
                    # Store the actual shell command, not the tool name (e.g. "audacity" not "run_command")
                    "command": arguments.get("command") or arguments.get("cmd") or tool_name,
                },
            )

    # ---------------------------------------------------------------
    # Continue flow after tool result from desktop
    # ---------------------------------------------------------------

    async def process_tool_result(
        self,
        session_id: UUID,
        tool_name: str,
        tool_call_id: str,
        success: bool,
        result: str | None = None,
        error: str | None = None,
        user_message: str = "",
        command: str = "",
        tool_arguments: dict | None = None,
        _retry_count: int = 0,
        _is_diagnostic: bool = False,
        _followup_count: int = 0,
    ) -> AsyncIterator[AIServiceResponse]:
        """Process tool execution result from desktop and format response."""
        MAX_TOOL_RETRIES = 4
        MAX_FOLLOWUP_STEPS = 3

        # Get session file logger
        slog = get_session_logger(session_id)

        # Retrieve the trace kept alive from _handle_client_tool, or create a new one
        obs = get_obs()
        trace = self._pending_traces.pop(str(session_id), None)
        if trace is None:
            trace = obs.start_trace(
                session_id=str(session_id),
                user_message=user_message or f"Tool result: {tool_name}",
                service="neo-desktop",
            )

        t0_tool = time.monotonic()
        slog.log_tool_result(tool_name, success=success, result=result, error=error, duration_ms=0)

        # ── DIAGNOSTIC RESULT: this was an auto-diagnostic command ──
        if _is_diagnostic:
            tool_ms = round((time.monotonic() - t0_tool) * 1000, 2)
            diagnostic_output = result if success else f"Diagnóstico falhou: {error}"

            trace.add_tool_call(
                tool_name="diagnostic",
                arguments=tool_arguments or {"command": command},
                result=self._truncate_output(diagnostic_output or "", max_lines=30),
                duration_ms=tool_ms,
            )

            logger.info(f"Diagnostic result received. Feeding to LLM for informed recovery.")

            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=session_id,
                message="Diagnóstico concluído. Analisando resultado...",
                is_final=False,
            )

            # Now call LLM with the diagnostic data
            recovery = await self._recover_from_tool_error(
                user_message=user_message,
                tool_name=tool_name,
                command=command,
                error=error or "comando não encontrado",
                tool_arguments=tool_arguments,
                retry_count=_retry_count,
                trace=trace,
                diagnostic_output=diagnostic_output,
            )
            slog.log_error_recovery(
                attempt=_retry_count + 1,
                error_type="diagnostic_recovery",
                action=recovery.get("action", "?") if recovery else "none",
                details=recovery,
            )

            if recovery and recovery.get("action") == "retry":
                new_tool = recovery.get("tool_name", "run_command")
                new_args = recovery.get("tool_arguments", {})
                new_command = recovery.get("raw_command")
                new_explanation = recovery.get("explanation", "Tentando abordagem alternativa...")
                new_tool_call_id = str(uuid4())

                yield AIServiceResponse(
                    intent=IntentType.CLIENT_TOOL,
                    session_id=session_id,
                    message=new_explanation,
                    is_final=False,
                )

                if new_command:
                    new_command_id = str(uuid4())
                    yield AIServiceResponse(
                        intent=IntentType.CLIENT_TOOL,
                        session_id=session_id,
                        command_request={
                            "command_id": new_command_id,
                            "command": new_command,
                            "explanation": new_explanation,
                            "is_background": False,
                        },
                        is_final=False,
                        metadata={
                            "flow": "client_command",
                            "command_id": new_command_id,
                            "command": command,  # preserve original command, not the recovery cmd
                            "tool_arguments": tool_arguments,  # preserve original tool_arguments
                            "user_message": user_message,
                            "_retry_count": _retry_count + 1,
                        },
                    )
                else:
                    yield AIServiceResponse(
                        intent=IntentType.CLIENT_TOOL,
                        session_id=session_id,
                        tool_request={
                            "tool_call_id": new_tool_call_id,
                            "tool_name": new_tool,
                            "arguments": json.dumps(new_args, ensure_ascii=False),
                            "requires_approval": recovery.get("requires_approval", False),
                        },
                        is_final=False,
                        metadata={
                            "flow": "client_tool",
                            "tool_call_id": new_tool_call_id,
                            "tool_name": new_tool,
                            "user_message": user_message,
                            "tool_arguments": tool_arguments,  # preserve original, NOT new_args
                            "command": command,  # preserve original command through retries
                            "_retry_count": _retry_count + 1,
                        },
                    )

                self._pending_traces[str(session_id)] = trace
                return

            # LLM responded with text or failed to parse
            final_text = (
                recovery.get("message", "Não encontrei o programa no sistema.")
                if recovery and recovery.get("action") == "respond"
                else "Não consegui encontrar uma alternativa para executar o comando."
            )

            session = await self.sessions.get_or_create_session(
                session_id=session_id, service="neo-desktop"
            )
            actual_sid = session.id
            await self.sessions.add_message(
                session_id=actual_sid, role="assistant", content=final_text,
                tool_name=tool_name,
                user_id=session.user_id,
            )
            obs.finish_trace(trace, response=final_text)

            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=actual_sid,
                message=final_text,
                is_final=True,
                iterations=2 + _retry_count,
            )
            return

        # ── NORMAL SUCCESS PATH ──
        if success:
            raw_output = result or "(sem saída)"

            # Try to parse JSON-stringified output
            try:
                parsed = json.loads(raw_output)
                if isinstance(parsed, str):
                    raw_output = parsed
                else:
                    raw_output = json.dumps(parsed, ensure_ascii=False, indent=2)
            except Exception:
                pass

            tool_ms = round((time.monotonic() - t0_tool) * 1000, 2)

            # Log the tool call (request + result) to the trace
            trace.add_tool_call(
                tool_name=tool_name,
                arguments=tool_arguments or {"command": command},
                result=self._truncate_output(raw_output, max_lines=40),
                duration_ms=tool_ms,
            )

            # Truncate very long output for LLM formatting
            raw_output = self._truncate_output(raw_output)

            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=session_id,
                message="Comando executado com sucesso. Formatando resultado...",
                is_final=False,
            )

            # Format with AI (trace is passed so the LLM call gets logged)
            final_text = await self._format_tool_result(
                user_message, command or tool_name, raw_output, trace=trace,
            )

            # ── MULTI-STEP FOLLOW-UP: check if user's request needs more commands ──
            if _followup_count < MAX_FOLLOWUP_STEPS:
                followup = await self._check_followup_needed(
                    user_message=user_message,
                    command_ran=command or tool_name,
                    command_output=raw_output,
                    trace=trace,
                )
                if followup and followup.get("needed"):
                    followup_cmd = followup.get("command", "")
                    followup_explanation = followup.get("explanation", "Executando próximo passo...")
                    if followup_cmd:
                        logger.info(
                            f"[FOLLOWUP] Step {_followup_count + 1}: {followup_cmd}"
                        )
                        slog.log_event(
                            "Follow-up detectado",
                            f"- **Passo:** {_followup_count + 1}/{MAX_FOLLOWUP_STEPS}\n"
                            f"- **Comando:** `{followup_cmd}`\n"
                            f"- **Razão:** {followup_explanation}\n",
                        )

                        new_tool_call_id = str(uuid4())

                        yield AIServiceResponse(
                            intent=IntentType.CLIENT_TOOL,
                            session_id=session_id,
                            message=followup_explanation,
                            is_final=False,
                        )

                        yield AIServiceResponse(
                            intent=IntentType.CLIENT_TOOL,
                            session_id=session_id,
                            tool_request={
                                "tool_call_id": new_tool_call_id,
                                "tool_name": "run_command",
                                "arguments": json.dumps(
                                    {"command": followup_cmd, "working_dir": (tool_arguments or {}).get("working_dir", "")},
                                    ensure_ascii=False,
                                ),
                                "requires_approval": False,
                            },
                            is_final=False,
                            metadata={
                                "flow": "client_tool",
                                "tool_call_id": new_tool_call_id,
                                "tool_name": "run_command",
                                "user_message": user_message,
                                "tool_arguments": {"command": followup_cmd, "working_dir": (tool_arguments or {}).get("working_dir", "")},
                                "command": followup_cmd,
                                "_retry_count": 0,
                                "_followup_count": _followup_count + 1,
                            },
                        )

                        self._pending_traces[str(session_id)] = trace
                        return  # Flow continues when desktop sends next tool_result
        else:
            tool_ms = round((time.monotonic() - t0_tool) * 1000, 2)

            # Log the failed tool call
            trace.add_tool_call(
                tool_name=tool_name,
                arguments=tool_arguments or {"command": command},
                result=None,
                duration_ms=tool_ms,
                error=error or "desconhecido",
            )

            # ── ERROR RECOVERY: auto-diagnose then ask LLM ──────────
            if _retry_count < MAX_TOOL_RETRIES:
                logger.info(
                    f"Tool '{tool_name}' failed (attempt {_retry_count + 1}/{MAX_TOOL_RETRIES}). "
                    f"Starting recovery: {error}"
                )

                yield AIServiceResponse(
                    intent=IntentType.CLIENT_TOOL,
                    session_id=session_id,
                    message=f"Comando falhou. Investigando alternativas... (tentativa {_retry_count + 1}/{MAX_TOOL_RETRIES})",
                    is_final=False,
                )

                # _recover_from_tool_error will auto-diagnose on first attempt
                # (returns a diagnostic command to run), then on second attempt
                # it receives the diagnostic output and makes an informed decision
                recovery = await self._recover_from_tool_error(
                    user_message=user_message,
                    tool_name=tool_name,
                    command=command,
                    error=error or "desconhecido",
                    tool_arguments=tool_arguments,
                    retry_count=_retry_count,
                    trace=trace,
                )
                slog.log_error_recovery(
                    attempt=_retry_count + 1,
                    error_type=self._detect_error_type(error or "", command=command, tool_arguments=tool_arguments),
                    action=recovery.get("action", "?") if recovery else "none",
                    details=recovery,
                )

                if recovery and recovery.get("action") == "retry":
                    new_tool = recovery.get("tool_name", "run_command")
                    new_args = recovery.get("tool_arguments", {})
                    new_command = recovery.get("raw_command")
                    new_explanation = recovery.get("explanation", "Tentando abordagem alternativa...")
                    is_diag = recovery.get("_is_diagnostic", False)
                    new_tool_call_id = str(uuid4())

                    yield AIServiceResponse(
                        intent=IntentType.CLIENT_TOOL,
                        session_id=session_id,
                        message=new_explanation,
                        is_final=False,
                    )

                    if new_command:
                        new_command_id = str(uuid4())
                        yield AIServiceResponse(
                            intent=IntentType.CLIENT_TOOL,
                            session_id=session_id,
                            command_request={
                                "command_id": new_command_id,
                                "command": new_command,
                                "explanation": new_explanation,
                                "is_background": False,
                            },
                            is_final=False,
                            metadata={
                                "flow": "client_command",
                                "command_id": new_command_id,
                                "command": command,  # preserve original command, not the recovery cmd
                                "tool_arguments": tool_arguments if is_diag else new_args,  # preserve original if diagnostic
                                "user_message": user_message,
                                "_retry_count": _retry_count + 1,
                                "_is_diagnostic": is_diag,
                            },
                        )
                    else:
                        yield AIServiceResponse(
                            intent=IntentType.CLIENT_TOOL,
                            session_id=session_id,
                            tool_request={
                                "tool_call_id": new_tool_call_id,
                                "tool_name": new_tool,
                                "arguments": json.dumps(new_args, ensure_ascii=False),
                                "requires_approval": recovery.get("requires_approval", False),
                            },
                            is_final=False,
                            metadata={
                                "flow": "client_tool",
                                "tool_call_id": new_tool_call_id,
                                "tool_name": new_tool,
                                "user_message": user_message,
                                "tool_arguments": tool_arguments if is_diag else new_args,  # preserve original if diagnostic
                                "command": command,  # preserve original command through retries
                                "_retry_count": _retry_count + 1,
                                "_is_diagnostic": is_diag,
                            },
                        )

                    # Keep trace alive for the next tool_result
                    self._pending_traces[str(session_id)] = trace
                    return  # Flow continues when desktop sends next tool_result

                elif recovery and recovery.get("action") == "respond":
                    final_text = recovery.get("message", f"Não consegui executar `{command or tool_name}`. Erro: {error or 'desconhecido'}")
                else:
                    final_text = f"Não consegui executar `{command or tool_name}`. Erro: {error or 'desconhecido'}"
            else:
                final_text = (
                    f"Não consegui executar `{command or tool_name}` após {MAX_TOOL_RETRIES} tentativas. "
                    f"Último erro: {error or 'desconhecido'}"
                )

        # Ensure the session exists (the client may send a stale/rotated session_id)
        session = await self.sessions.get_or_create_session(
            session_id=session_id, service="neo-desktop"
        )
        actual_sid = session.id

        # Save to session (no tool_call_id so it appears in conversation history)
        await self.sessions.add_message(
            session_id=actual_sid,
            role="assistant",
            content=final_text,
            tool_name=tool_name,
            user_id=session.user_id,
        )
        slog.log_message_saved("assistant", final_text, str(actual_sid))
        slog.log_final_response(final_text, "CLIENT_TOOL", iterations=2 + _retry_count)

        # Finalize the trace with the complete flow
        obs.finish_trace(trace, response=final_text)

        yield AIServiceResponse(
            intent=IntentType.CLIENT_TOOL,
            session_id=actual_sid,
            message=final_text,
            is_final=True,
            iterations=2 + _retry_count,
        )

    async def process_command_approval(
        self,
        session_id: UUID,
        command_id: str,
        approved: bool,
        command: str,
        user_message: str = "",
    ) -> AsyncIterator[AIServiceResponse]:
        """Process command approval/rejection from desktop."""

        slog = get_session_logger(session_id)
        slog.log_event(
            "Aprovação de Comando",
            f"- **Comando:** `{command}`\n- **Aprovado:** {'Sim' if approved else 'Não'}\n",
        )

        # Retrieve pending trace or create a new one
        obs = get_obs()
        trace = self._pending_traces.pop(str(session_id), None)

        # Ensure the session exists before saving any messages
        session = await self.sessions.get_or_create_session(
            session_id=session_id, service="neo-desktop"
        )
        actual_sid = session.id

        if not approved:
            msg = "Execução cancelada: permissão para comando local foi negada."

            # Log rejection to trace
            if trace:
                trace.add_tool_call(
                    tool_name="command_approval",
                    arguments={"command": command, "approved": False},
                    result=msg,
                    duration_ms=0,
                )
                obs.finish_trace(trace, response=msg)

            await self.sessions.add_message(
                session_id=actual_sid, role="assistant", content=msg,
                user_id=session.user_id,
            )
            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=actual_sid,
                message=msg,
                is_final=True,
            )
            return

        # Log approval to trace and keep it alive for the subsequent tool_result
        if trace:
            trace.add_tool_call(
                tool_name="command_approval",
                arguments={"command": command, "approved": True},
                result="Aprovado pelo usuário",
                duration_ms=0,
            )
            self._pending_traces[str(actual_sid)] = trace

        # Approved — send tool request to execute
        tool_call_id = str(uuid4())

        yield AIServiceResponse(
            intent=IntentType.CLIENT_TOOL,
            session_id=actual_sid,
            message="Permissão aprovada. Solicitando execução do comando ao Neo Desktop.",
            is_final=False,
        )

        yield AIServiceResponse(
            intent=IntentType.CLIENT_TOOL,
            session_id=actual_sid,
            tool_request={
                "tool_call_id": tool_call_id,
                "tool_name": "execute_command",
                "arguments": json.dumps(
                    {"command": command, "is_background": False},
                    ensure_ascii=False,
                ),
                "requires_approval": False,
            },
            is_final=False,
            metadata={
                "flow": "client_command_approved",
                "tool_call_id": tool_call_id,
                "command": command,
                "user_message": user_message,
                "tool_arguments": {"command": command, "is_background": False},
            },
        )

    # ---------------------------------------------------------------
    # Error recovery — ask LLM to reason about tool failures
    # ---------------------------------------------------------------

    @staticmethod
    def _detect_error_type(error: str, command: str = "", tool_arguments: dict | None = None) -> str:
        """Classify the error into a category for smarter recovery."""
        err_lower = (error or "").lower()
        # The ACTUAL command that was run (e.g. "flatpak run org.X.Y"), not the tool name
        actual_cmd = (
            (tool_arguments or {}).get("command")
            or (tool_arguments or {}).get("cmd")
            or command
            or ""
        )
        cmd_lower = actual_cmd.lower()

        # Exit code 255 from flatpak/snap usually means the GUI app launched but
        # detached from the terminal — treat as a special case, not a real failure.
        # Check the COMMAND (most reliable) and fall back to error text.
        if "código 255" in err_lower or "code 255" in err_lower:
            if any(k in cmd_lower for k in ("flatpak run", "snap run")):
                return "gui_launched"
            if any(k in err_lower for k in ("flatpak", "snap run", "xdg")):
                return "gui_launched"
        if any(k in err_lower for k in ("command not found", "not found", "código 127", "code 127", "no such file")):
            return "command_not_found"
        if any(k in err_lower for k in ("permission denied", "permissão negada", "operation not permitted")):
            return "permission_denied"
        if any(k in err_lower for k in ("timeout", "timed out")):
            return "timeout"
        if any(k in err_lower for k in ("connection refused", "network", "no route")):
            return "network"
        return "unknown"

    @staticmethod
    def _find_app_from_diagnostic(program: str, diagnostic_output: str) -> str | None:
        """Parse diagnostic output deterministically to find a working launch command.

        This is the "roteiro" (playbook) for app-not-found recovery — no LLM needed.
        Tries in priority order: flatpak → snap → flatpak exports bin → find results.

        Returns the ready-to-run shell command, or None if the app is not found anywhere.
        """
        if not diagnostic_output:
            return None

        prog_lower = program.lower()

        # Split the diagnostic output into named sections (=== section ===)
        sections: dict[str, list[str]] = {}
        current = "preamble"
        for line in diagnostic_output.split("\n"):
            stripped = line.strip()
            if stripped.startswith("===") and stripped.endswith("==="):
                current = stripped.strip("= ").strip().lower()
                sections[current] = []
            else:
                sections.setdefault(current, []).append(stripped)

        def non_empty(lines: list[str]) -> list[str]:
            return [l for l in lines if l and "nenhum" not in l.lower() and "nao encontrado" not in l.lower() and "not found" not in l.lower()]

        # 1. Flatpak app list — "org.blender.Blender  Blender  ..."
        for line in non_empty(sections.get("flatpak", [])):
            if prog_lower in line.lower():
                parts = line.split()
                app_id = parts[0] if parts else ""
                if "." in app_id:  # looks like a real flatpak ID
                    logger.info(f"[Diagnostic] Found via flatpak: {app_id}")
                    return f"flatpak run {app_id}"

        # 2. Snap list — "blender  3.6  ..."
        for line in non_empty(sections.get("snap", [])):
            if prog_lower in line.lower():
                snap_name = line.split()[0] if line.split() else ""
                if snap_name:
                    logger.info(f"[Diagnostic] Found via snap: {snap_name}")
                    return f"snap run {snap_name}"

        # 3. Flatpak exports bin — direct binary symlinks
        for line in non_empty(sections.get("flatpak exports bin", [])):
            if prog_lower in line.lower():
                bin_name = line.strip()
                if bin_name and "/" not in bin_name:
                    path = f"/var/lib/flatpak/exports/bin/{bin_name}"
                    logger.info(f"[Diagnostic] Found in flatpak exports bin: {path}")
                    return path

        # 4. find results — full paths
        for line in non_empty(sections.get("find", [])):
            path = line.strip()
            if (prog_lower in path.lower()
                    and path.startswith("/")
                    and not path.endswith(".desktop")
                    and ("/bin/" in path or "/usr/" in path or "/opt/" in path)):
                logger.info(f"[Diagnostic] Found via find: {path}")
                return path

        return None

    @staticmethod
    def _build_diagnostic_command(program: str, error_type: str) -> str | None:
        """Build a diagnostic shell command that gathers real system data.

        Uses semicolons (;) so every section runs independently.
        Searches flatpak, snap, dpkg, and common bin paths.
        """
        if error_type == "command_not_found":
            return (
                f"echo '=== which ==='; "
                f"which {program} 2>&1; "
                f"type {program} 2>&1; "
                f"echo '=== flatpak ==='; "
                f"flatpak list --columns=application,name 2>/dev/null | grep -i {program} || echo 'nenhum flatpak encontrado'; "
                f"echo '=== flatpak exports bin ==='; "
                f"ls /var/lib/flatpak/exports/bin/ 2>/dev/null | grep -i {program} || echo 'nenhum binario flatpak encontrado'; "
                f"echo '=== snap ==='; "
                f"snap list 2>/dev/null | grep -i {program} || echo 'nenhum snap encontrado'; "
                f"echo '=== dpkg ==='; "
                f"dpkg -l '*{program}*' 2>/dev/null | grep -E '^ii' | head -5 || echo 'nenhum pacote dpkg encontrado'; "
                f"echo '=== rpm ==='; "
                f"rpm -qa 2>/dev/null | grep -i {program} | head -5 || echo 'rpm nao disponivel'; "
                f"echo '=== find ==='; "
                f"find /usr/bin /usr/local/bin /opt /var/lib/flatpak/exports/bin /snap/bin "
                f"/usr/share/applications /var/lib/snapd/desktop/applications "
                f"-iname '*{program}*' 2>/dev/null | head -15 || echo 'nenhum arquivo encontrado'; "
                f"echo '=== desktop files ==='; "
                f"grep -ril '{program}' /usr/share/applications/ /var/lib/flatpak/exports/share/applications/ "
                f"~/.local/share/applications/ 2>/dev/null | head -5 || echo 'nenhum .desktop encontrado'; "
                f"echo '=== PATH ==='; "
                f"echo $PATH"
            )
        if error_type == "permission_denied":
            return (
                f"echo '=== file info ==='; "
                f"ls -la $(which {program} 2>/dev/null || echo /usr/bin/{program}) 2>&1; "
                f"echo '=== user ==='; "
                f"id"
            )
        return None

    @staticmethod
    def _extract_program_name(command: str, error: str, tool_arguments: dict | None = None) -> str:
        """Extract the actual program name from the failed command.

        Priority:
        1. tool_arguments['command'] (the real shell command, e.g. 'audacity')
        2. command string (first non-env-var word)
        3. error message parsing (handles multiple shell error formats)
        """
        import re as _re

        TOOL_NAMES = {"run_command", "execute_command", "list_directory", "read_file",
                      "write_file", "create_directory", "delete_path", "search_files",
                      "get_system_info"}
        SHELL_NAMES = {"bash", "zsh", "sh", "fish", "dash", "ksh", "tcsh", "csh"}
        ENV_PREFIXES = {"sudo", "env", "nohup", "nice", "sh", "bash"}

        # 1. tool_arguments has the real command (not the tool name like 'run_command')
        actual_cmd = (tool_arguments or {}).get("command") or (tool_arguments or {}).get("cmd", "")
        if actual_cmd:
            parts = actual_cmd.strip().split()
            for part in parts:
                if "=" not in part and part not in ENV_PREFIXES:
                    return part.split("/")[-1]  # basename

        # 2. Try to get from command string (skip tool names like 'run_command')
        if command and command not in TOOL_NAMES:
            parts = command.strip().split()
            for part in parts:
                if "=" not in part and part not in ENV_PREFIXES:
                    return part.split("/")[-1]

        # 3. Fallback: parse from error message
        # Handles multiple formats:
        #   "bash: blender: command not found"
        #   "zsh: command not found: blender"
        #   "Erro (código 127): bash: blender: command not found"
        err = error or ""
        # Strip the "Erro (código N): " prefix added by CommandExecutor
        err_inner = _re.sub(r'^Erro \(c[oó]digo \d+\):\s*', '', err, flags=_re.IGNORECASE).strip()

        for marker in ("command not found", "não encontrado", "nao encontrado", "not found"):
            marker_idx = err_inner.lower().find(marker)
            if marker_idx < 0:
                continue

            # Pattern A: "bash: blender: command not found" — app name is BEFORE the marker
            before = err_inner[:marker_idx].rstrip(': ').strip()
            # Split by ":" to get segments, try from right to left (most specific last)
            segments = [s.strip() for s in before.split(':') if s.strip()]
            for segment in reversed(segments):
                candidate = segment.split()[-1] if segment.split() else ""
                if (candidate
                        and candidate not in TOOL_NAMES
                        and candidate.lower() not in SHELL_NAMES
                        and _re.match(r'^[\w.-]+$', candidate)
                        and not candidate.endswith(')')):
                    return candidate

            # Pattern B: "zsh: command not found: blender" — app name is AFTER the marker
            after_part = err_inner[marker_idx + len(marker):].lstrip(': ').strip().split()
            if after_part and after_part[0] not in TOOL_NAMES and _re.match(r'^[\w.-]+$', after_part[0]):
                return after_part[0]

        return "unknown"

    async def _recover_from_tool_error(
        self,
        user_message: str,
        tool_name: str,
        command: str,
        error: str,
        tool_arguments: dict | None = None,
        retry_count: int = 0,
        trace: MessageTrace | None = None,
        diagnostic_output: str | None = None,
    ) -> dict | None:
        """Ask the LLM to analyze a tool error and decide on recovery action.

        When diagnostic_output is provided, the LLM has real system data
        to make informed decisions instead of guessing.

        Returns a dict with:
          - action: "retry" | "respond" | "diagnose"
          - tool_name, tool_arguments, raw_command, explanation (if retry)
          - message (if respond)
          - diagnostic_command (if diagnose)
        """
        error_type = self._detect_error_type(error, command=command, tool_arguments=tool_arguments)

        # ── SPECIAL CASE: GUI app launched successfully (exit 255 from flatpak/snap) ──
        # Apps gráficos ficam em foreground e retornam 255 quando o executor os mata.
        # Verificar se o processo está rodando antes de tratar como erro real.
        if error_type == "gui_launched":
            program = self._extract_program_name(command, error, tool_arguments)
            logger.info(f"GUI app possibly launched (exit 255). Verifying process: {program}")
            return {
                "action": "retry",
                "tool_name": "run_command",
                "tool_arguments": {
                    "command": f"pgrep -il '{program}' | head -5 || echo 'processo nao encontrado'"
                },
                "explanation": f"Verificando se {program} abriu com sucesso...",
                "requires_approval": False,
                "_is_diagnostic": True,
            }

        # ── STEP 1: If first attempt and no diagnostic data yet, run diagnostics ──
        if retry_count == 0 and diagnostic_output is None and error_type in ("command_not_found", "permission_denied"):
            program = self._extract_program_name(command, error, tool_arguments)
            logger.info(f"[DEBUG] _recover_from_tool_error: command='{command}', tool_arguments={tool_arguments}, extracted_program='{program}'")
            diag_cmd = self._build_diagnostic_command(program, error_type)
            if diag_cmd:
                logger.info(f"Auto-diagnosing '{program}' before asking LLM (error_type={error_type})")
                return {
                    "action": "retry",
                    "tool_name": "run_command",
                    "tool_arguments": {"command": diag_cmd},
                    "explanation": f"Investigando onde '{program}' está instalado no sistema...",
                    "requires_approval": False,
                    "_is_diagnostic": True,
                }

        # ── STEP 1.5: Deterministic recovery from diagnostic output ──
        # Parse the diagnostic output ourselves before asking the LLM.
        # This eliminates LLM variability for the common "app found via flatpak/snap" case.
        if diagnostic_output and error_type == "command_not_found":
            program = self._extract_program_name(command, error, tool_arguments)
            launch_cmd = self._find_app_from_diagnostic(program, diagnostic_output)
            if launch_cmd:
                launcher = launch_cmd.split()[0]  # flatpak, snap, or absolute path
                return {
                    "action": "retry",
                    "tool_name": "run_command",
                    "tool_arguments": {"command": launch_cmd, "is_background": True},
                    "explanation": f"Encontrei {program} via {launcher}! Abrindo em segundo plano...",
                    "requires_approval": False,
                }
            logger.info(f"[Diagnostic] {program} not found in diagnostic output — escalating to LLM")

        # ── STEP 2: Build an informed prompt with real data ──
        system_msg = (
            "Você é Neo, um assistente de desktop inteligente. "
            "Um comando executado no computador do usuário falhou. "
            "Analise o erro e os dados de diagnóstico do sistema para decidir a melhor ação.\n\n"
            "## REGRAS IMPORTANTES\n"
            "1. **NUNCA sugira instalar sem antes verificar** — use os dados de diagnóstico fornecidos.\n"
            "2. **Flatpak**: Se aparece em '=== flatpak ===' ou '=== flatpak exports bin ===', execute com:\n"
            "   `flatpak run <application-id>` onde application-id é a primeira coluna do flatpak list.\n"
            "   **CRÍTICO para apps gráficos**: use `is_background: true` para não bloquear o terminal!\n"
            "   Exemplo correto: {\"command\": \"flatpak run org.audacityteam.Audacity\", \"is_background\": true}\n"
            "3. **Snap**: Se aparece em '=== snap ===', use `snap run <nome>` com `is_background: true`.\n"
            "4. **Apps gráficos nativos** (ex: blender, gimp, vlc sem caminho): também use `is_background: true`.\n"
            "5. **Verificar se já abriu**: Se o erro anterior foi ao tentar lançar um app gráfico, \n"
            "   primeiro verifique com `pgrep -il <nome>` se o app já está rodando antes de tentar de novo.\n"
            "6. **Caminho absoluto**: Se '=== find ===' mostrar o binário, use o caminho completo.\n"
            "7. Se REALMENTE não está instalado em NENHUM diagnóstico → diga como instalar.\n"
            "8. Para permissão → adicione sudo.\n"
            "9. **SEMPRE prefira 'retry'**. Só use 'respond' se NÃO HÁ alternativa.\n\n"
            "## Formato de Resposta\n"
            "Responda APENAS com JSON válido (sem markdown fences):\n\n"
            "Para app gráfico em background:\n"
            '{"action": "retry", "tool_name": "run_command", '
            '"tool_arguments": {"command": "flatpak run org.X.Y", "is_background": true}, '
            '"explanation": "Abrindo X em background", "requires_approval": false}\n\n'
            "Para verificar processo:\n"
            '{"action": "retry", "tool_name": "run_command", '
            '"tool_arguments": {"command": "pgrep -il nome | head -5"}, '
            '"explanation": "Verificando se o app está rodando", "requires_approval": false}\n\n'
            "Para responder ao usuário (APENAS se não há alternativa):\n"
            '{"action": "respond", "message": "mensagem clara em português"}'
        )

        diag_section = ""
        if diagnostic_output:
            diag_section = f"\n\n## Dados de Diagnóstico do Sistema\n```\n{diagnostic_output}\n```"

        user_prompt = (
            f'O usuário pediu: "{user_message}"\n'
            f"Comando que falhou: `{command or json.dumps(tool_arguments or {}, ensure_ascii=False)}`\n"
            f"Tipo de erro: {error_type}\n"
            f"Erro completo: {error}\n"
            f"Tentativa: {retry_count + 1}"
            f"{diag_section}\n\n"
            "Com base nos dados acima, qual é a melhor ação?"
        )

        messages = [
            Message(role=MessageRole.SYSTEM, content=system_msg),
            Message(role=MessageRole.USER, content=user_prompt),
        ]

        try:
            provider = self.llm.get_provider()
            provider_name = type(provider).__name__.replace("Provider", "").lower()
            model_name = getattr(provider, 'model', None) or getattr(self.settings, 'default_model', 'gpt-4o')
            t0 = time.monotonic()

            response = await provider.complete(
                messages=messages,
                temperature=0.2,
                max_tokens=500,
            )

            llm_ms = (time.monotonic() - t0) * 1000
            raw_text = response.get_content() or ""

            if trace:
                trace.add_llm_call(
                    provider=provider_name,
                    model=model_name,
                    messages_sent=[
                        {"role": "system", "content": system_msg[:800]},
                        {"role": "user", "content": user_prompt[:2000]},
                    ],
                    response_text=raw_text[:1000],
                    duration_ms=round(llm_ms, 2),
                )

            # Parse LLM JSON response
            clean = raw_text.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

            recovery = json.loads(clean)
            logger.info(f"LLM recovery decision: {recovery.get('action')} - {recovery.get('explanation', recovery.get('message', ''))[:100]}")
            return recovery

        except Exception as e:
            logger.warning(f"Failed to get LLM recovery for tool error: {e}")
            if trace:
                trace.add_llm_call(
                    provider="unknown",
                    model="unknown",
                    messages_sent=[
                        {"role": "system", "content": system_msg[:500]},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_text=None,
                    duration_ms=0,
                    error=str(e),
                )
            return None

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    @staticmethod
    def _format_knowledge_context(kg_result: dict) -> str:
        """Formata o resultado do grafo de conhecimento em texto para o system prompt."""
        lines: list[str] = []

        for ctx_tree in kg_result.get("contexts_found", []):
            ctx = ctx_tree["context"]
            lines.append(f"### {ctx['name']}")
            for child in ctx_tree.get("children", []):
                props = child.get("properties") or {}
                props_str = ", ".join(f"{k}: {v}" for k, v in props.items() if v is not None)
                lines.append(f"- [{child['type']}] {child['name']}" + (f" — {props_str}" if props_str else ""))

        globals_ = kg_result.get("global", [])
        if globals_:
            lines.append("\n### Perfil Global")
            for g in globals_:
                props = g.get("properties") or {}
                props_str = ", ".join(f"{k}: {v}" for k, v in props.items() if v is not None)
                lines.append(f"- [{g['type']}] {g['name']}" + (f" — {props_str}" if props_str else ""))

        return "\n".join(lines)

    @staticmethod
    def _build_system_context(metadata: dict[str, Any] | None) -> str | None:
        """Build a compact system context string from metadata sent by the desktop client.

        The desktop sends OS info in the user_input metadata so the AI knows
        what paths, shell, and filesystem conventions to use.
        """
        if not metadata:
            return None
        platform = metadata.get("os_platform")
        if not platform:
            return None

        platform_names = {"linux": "Linux", "darwin": "macOS", "win32": "Windows"}
        os_name = platform_names.get(platform, platform)
        release = metadata.get("os_release", "")
        arch = metadata.get("os_arch", "")
        home = metadata.get("home_dir", "~")
        downloads = metadata.get("downloads_dir", f"{home}/Downloads")
        documents = metadata.get("documents_dir", f"{home}/Documents")
        desktop_dir = metadata.get("desktop_dir", f"{home}/Desktop")

        return (
            f"Sistema operacional do usuário: {os_name} {release} ({arch}).\n"
            f"Diretório home: {home}\n"
            f"Diretório de documentos do usuário: {documents}\n"
            f"Diretório de downloads do usuário: {downloads}\n"
            f"Diretório desktop do usuário: {desktop_dir}\n"
            f"Shell padrão: {'bash/zsh' if platform in ('linux', 'darwin') else 'PowerShell/cmd'}.\n"
            f"IMPORTANTE: Quando o usuário mencionar 'pasta de documentos', 'meus documentos' ou similar, "
            f"use SEMPRE o caminho '{documents}' como working_dir. "
            f"Quando mencionar 'downloads', use '{downloads}'. "
            f"Quando mencionar 'desktop' ou 'área de trabalho', use '{desktop_dir}'.\n"
            f"Use caminhos absolutos ao manipular arquivos."
        )

    async def _format_tool_result(
        self,
        user_message: str,
        command: str,
        output: str,
        trace: MessageTrace | None = None,
    ) -> str:
        """Use LLM to format tool output into a user-friendly response."""
        prompt = (
            f'O usuário perguntou: "{user_message}"\n'
            f"Foi executado o comando: `{command}`\n"
            f"Saída do comando:\n```\n{output}\n```\n\n"
            "Formate uma resposta clara e amigável em português para o usuário, "
            "resumindo o resultado do comando de forma legível. "
            "Use markdown se necessário. Não invente dados — use apenas o que está na saída."
        )

        system_msg = "Você é Neo, um assistente de desktop. Responda de forma clara e concisa em português."
        messages = [
            Message(role=MessageRole.SYSTEM, content=system_msg),
            Message(role=MessageRole.USER, content=prompt),
        ]

        try:
            provider = self.llm.get_provider()
            provider_name = type(provider).__name__.replace("Provider", "").lower()
            model_name = getattr(provider, 'model', None) or getattr(self.settings, 'default_model', 'gpt-4o')
            t0 = time.monotonic()
            response = await provider.complete(
                messages=messages,
                temperature=0.3,
                max_tokens=600,
            )
            llm_ms = (time.monotonic() - t0) * 1000
            response_text = response.get_content() or f"Resultado de `{command}`:\n```\n{output}\n```"

            # Log this LLM call in the trace
            if trace:
                trace.add_llm_call(
                    provider=provider_name,
                    model=model_name,
                    messages_sent=[
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": prompt[:3000]},
                    ],
                    response_text=(response_text or "")[:1000],
                    duration_ms=round(llm_ms, 2),
                )

            return response_text
        except Exception as e:
            logger.warning(f"Failed to format result with AI: {e}")
            if trace:
                trace.add_llm_call(
                    provider="unknown",
                    model="unknown",
                    messages_sent=[
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": prompt[:3000]},
                    ],
                    response_text=None,
                    duration_ms=0,
                    error=str(e),
                )
            return f"Resultado de `{command}`:\n```\n{output}\n```"

    async def _check_followup_needed(
        self,
        user_message: str,
        command_ran: str,
        command_output: str,
        trace: MessageTrace | None = None,
    ) -> dict[str, Any] | None:
        """Ask LLM whether the user's original request requires an additional command.

        Returns None or {"needed": False} when the task is complete, or
        {"needed": True, "command": "...", "explanation": "..."} when a follow-up
        command should be executed to fully satisfy the user's request.
        """
        prompt = (
            f'O pedido original do usuário foi: "{user_message}"\n'
            f"O comando executado foi: `{command_ran}`\n"
            f"Saída obtida:\n```\n{command_output[:2000]}\n```\n\n"
            "Analise se o pedido do usuário foi COMPLETAMENTE atendido pela saída acima.\n"
            "Exemplos de quando NÃO está completo:\n"
            '- Usuário pediu "encontre o arquivo X e mostre o conteúdo" mas o comando apenas encontrou/listou o arquivo.\n'
            '- Usuário pediu "ache os CSVs e diga quantas linhas tem" mas o comando apenas listou os CSVs.\n'
            '- Usuário pediu "crie o arquivo e coloque tal conteúdo" mas o comando apenas criou o arquivo vazio.\n\n'
            "Exemplos de quando ESTÁ completo:\n"
            '- Usuário pediu "liste os arquivos" e o comando listou.\n'
            '- Usuário pediu "mostre o conteúdo do arquivo" e o comando já mostrou o conteúdo.\n'
            '- Usuário pediu "qual meu IP" e o comando retornou o IP.\n\n'
            "Responda APENAS com JSON válido (sem markdown fences):\n"
            '{"needed": false} se o pedido já foi completamente atendido.\n'
            '{"needed": true, "command": "comando_shell_para_completar", "explanation": "explicação curta em português"}\n'
            "se um comando adicional é necessário para completar o pedido.\n"
            "IMPORTANTE: O comando deve usar caminhos absolutos baseados na saída obtida."
        )

        system_msg = (
            "Você é um planejador de tarefas para um assistente de desktop. "
            "Decida se o pedido do usuário precisa de mais um passo para ser concluído. "
            "Seja conservador: só retorne needed=true quando claramente falta algo. "
            "Responda APENAS com JSON."
        )

        messages = [
            Message(role=MessageRole.SYSTEM, content=system_msg),
            Message(role=MessageRole.USER, content=prompt),
        ]

        try:
            provider = self.llm.get_provider()
            provider_name = type(provider).__name__.replace("Provider", "").lower()
            model_name = getattr(provider, 'model', None) or getattr(self.settings, 'default_model', 'gpt-4o')
            t0 = time.monotonic()
            response = await provider.complete(
                messages=messages,
                temperature=0.1,
                max_tokens=200,
            )
            llm_ms = (time.monotonic() - t0) * 1000
            raw = response.get_content() or ""

            if trace:
                trace.add_llm_call(
                    provider=provider_name,
                    model=model_name,
                    messages_sent=[
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": prompt[:3000]},
                    ],
                    response_text=raw[:500],
                    duration_ms=round(llm_ms, 2),
                    stage="followup_check",
                )

            # Parse JSON from response
            clean = raw.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            parsed = json.loads(clean)
            return parsed
        except Exception as e:
            logger.warning(f"Failed to check followup needed: {e}")
            return None

    @staticmethod
    def _truncate_output(value: str, max_lines: int = 60) -> str:
        """Truncate long output to max_lines."""
        lines = (value or "").splitlines()
        if len(lines) <= max_lines:
            return value or "(sem saída)"
        return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} linhas omitidas)"
