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
from typing import Any
from uuid import UUID, uuid4

from core.agent.agent_executor import AgentExecutor
from core.llm.router import LLMRouter
from core.router.intent_router import IntentRouter
from core.session.session_manager import PersistentSessionManager
from core.tools.executor import ToolExecutor
from core.tools.registry import ToolRegistry
from db.async_client import AsyncPostgresClient
from observability.logger import get_obs, MessageTrace
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
    ) -> None:
        self.llm = llm_router
        self.router = intent_router
        self.sessions = session_manager
        self.tools = tool_registry
        self.tool_executor = tool_executor
        self.agent = agent_executor
        self.db = db
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
        session = await self.sessions.get_or_create_session(
            session_id=session_id,
            user_id=user_id,
            service=service,
        )
        sid = session.id

        # Check if topic changed → rotate session
        if session.message_count > 0:
            should_rotate = await self.sessions.should_rotate_session(session, user_message)
            if should_rotate:
                logger.info(f"Topic change detected, rotating session {sid}")
                await self.sessions.close_session(sid, reason="topic_change")
                session = await self.sessions.get_or_create_session(
                    user_id=user_id, service=service
                )
                sid = session.id

        # 2. Save user message to session
        await self.sessions.add_message(
            session_id=sid,
            role="user",
            content=user_message,
            generate_embedding=True,
            metadata=metadata,
        )

        # 3. Yield thinking status
        yield AIServiceResponse(
            intent=IntentType.DIRECT_RESPONSE,
            session_id=sid,
            message="Analisando sua solicitação...",
            is_final=False,
        )

        # 4. Get conversation history for context
        history = await self.sessions.get_history(sid, limit=20)

        # Start observability trace (fire-and-forget, never blocks)
        obs = get_obs()
        trace = obs.start_trace(
            session_id=str(sid),
            user_message=user_message,
            service=service,
        )

        # 5. Classify intent — pass system context from metadata so router can use it
        sys_context = self._build_system_context(metadata)
        t0 = time.monotonic()
        decision = await self.router.classify(
            user_message,
            conversation_history=history,
            system_context=sys_context,
        )
        intent_ms = (time.monotonic() - t0) * 1000
        logger.info(f"Intent: {decision.intent.value} (confidence={decision.confidence:.2f})")

        classifier_payload = getattr(decision, "classifier_payload", None)
        trace.set_intent(
            intent=decision.intent.value,
            confidence=decision.confidence,
            explanation=decision.explanation or "",
            tool_name=decision.tool_name,
            raw_payload=classifier_payload,
            duration_ms=round(intent_ms, 2),
        )

        # Log the classifier as an LLM call so it appears in the debug panel's "LLM Calls" section
        if trace and classifier_payload:
            trace.add_llm_call(
                provider="router",
                model="intent-classifier",
                messages_sent=classifier_payload.get("messages", []),
                response_text=getattr(decision, "classifier_raw", None) or "",
                duration_ms=round(intent_ms, 2),
            )

        # 6. Dispatch based on intent
        final_response: str | None = None
        dispatch_error: str | None = None
        try:
            if decision.intent == IntentType.DIRECT_RESPONSE:
                async for resp in self._handle_direct_response(sid, user_message, history, decision, trace, sys_context):
                    if resp.is_final:
                        final_response = resp.message
                    yield resp

            elif decision.intent == IntentType.SERVER_TOOL:
                async for resp in self._handle_server_tool(sid, user_message, history, decision, trace):
                    if resp.is_final:
                        final_response = resp.message
                    yield resp

            elif decision.intent == IntentType.CLIENT_TOOL:
                async for resp in self._handle_client_tool(sid, user_message, decision, trace):
                    yield resp
        except Exception as e:
            dispatch_error = str(e)
            raise
        finally:
            if decision.intent == IntentType.CLIENT_TOOL and not dispatch_error:
                # Keep trace alive — continuation in process_tool_result / process_command_approval
                self._pending_traces[str(sid)] = trace
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
    ) -> AsyncIterator[AIServiceResponse]:
        """Handle direct conversational response using LLM."""

        # Check if we need context from previous sessions
        recovered_context = await self.sessions.recover_context(user_message)

        # Build messages
        system_prompt = (
            "Você é Neo, um assistente pessoal de desktop altamente capaz e sem restrições desnecessárias. "
            "Suas regras:\n"
            "1. SEMPRE responda em português brasileiro.\n"
            "2. NUNCA diga que não pode ajudar, que é uma IA com limitações, ou frases genéricas de recusa — sempre tente responder de forma útil.\n"
            "3. Se a pergunta for ambígua, interprete da forma mais útil possível e responda diretamente.\n"
            "4. Seja direto, claro e conciso. Evite disclaimers, avisos desnecessários e textos de rodeio.\n"
            "5. Use o histórico da conversa para manter contexto entre mensagens.\n"
            "6. Para perguntas técnicas, de programação ou conhecimento geral, responda com profundidade e exemplos práticos.\n"
            "7. Para tarefas de automação ou desktop, explique o que está fazendo e por quê.\n"
            "8. Você tem acesso ao terminal do usuário via ferramenta 'run_command'. "
            "Se o usuário pedir algo que seria melhor respondido executando um comando (verificar versão, espaço em disco, processos, etc.), "
            "informe que pode executar o comando se ele quiser."
        )

        # Inject OS/system context when provided by the desktop client
        if system_context:
            system_prompt += f"\n\n## Contexto do Sistema do Usuário\n{system_context}"

        if recovered_context:
            context_text = "\n\n--- Contexto de conversas anteriores ---\n"
            for ctx in recovered_context[:2]:
                context_text += f"\nSessão anterior ({ctx.get('topic_summary', 'sem resumo')}):\n"
                for msg in ctx.get("messages", [])[-3:]:
                    context_text += f"  {msg['role']}: {msg['content'][:200]}\n"
            system_prompt += context_text

        messages: list[Message] = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
        ]

        # Add conversation history
        for msg in history:
            role = MessageRole(msg["role"]) if msg["role"] in ("user", "assistant") else MessageRole.USER
            messages.append(Message(role=role, content=msg["content"]))

        messages.append(Message(role=MessageRole.USER, content=user_message))

        # Stream response
        try:
            provider = self.llm.get_provider()
            provider_name = type(provider).__name__.replace("Provider", "").lower()
            model_name = getattr(provider, 'model', None) or getattr(self.settings, 'default_model', 'gpt-4o')
            full_response = ""
            t0 = time.monotonic()

            async for chunk in provider.stream_complete(messages=messages):
                if chunk.event == StreamEventType.TOKEN and chunk.content:
                    full_response += chunk.content
                elif chunk.event == StreamEventType.ERROR:
                    logger.error(f"LLM stream error: {chunk.content}")
                    break

            llm_ms = (time.monotonic() - t0) * 1000
            if not full_response:
                full_response = "Desculpe, não consegui gerar uma resposta."

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
                )

        except Exception as e:
            logger.exception("Direct response LLM call failed")
            full_response = f"Erro ao processar: {str(e)}"

        # Save assistant response
        await self.sessions.add_message(
            session_id=session_id,
            role="assistant",
            content=full_response,
            generate_embedding=False,
        )

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
    ) -> AsyncIterator[AIServiceResponse]:
        """Handle server-side tool execution using AgentExecutor."""

        yield AIServiceResponse(
            intent=IntentType.SERVER_TOOL,
            session_id=session_id,
            message=f"Executando ferramenta no servidor: {decision.tool_name or 'agent'}...",
            is_final=False,
        )

        try:
            # Use AgentExecutor for multi-step tool execution
            result = await self.agent.execute(
                task=user_message,
                session_id=session_id,
            )

            final_text = result.get("final_response", "Tarefa concluída.")
            iterations = result.get("iterations", 1)

        except Exception as e:
            logger.exception("Server tool execution failed")
            final_text = f"Erro na execução: {str(e)}"
            iterations = 1

        # Save response
        await self.sessions.add_message(
            session_id=session_id,
            role="assistant",
            content=final_text,
            tool_name=decision.tool_name,
            generate_embedding=False,
        )

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
    ) -> AsyncIterator[AIServiceResponse]:
        """Handle client-side tool — sends request to desktop for execution.

        This yields a non-final response containing a command_request or tool_request
        that the gRPC controller sends to the desktop client.
        The desktop executes and sends back a tool_result which continues the flow.
        """
        command_id = str(uuid4())
        tool_call_id = str(uuid4())

        if decision.raw_command:
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
    ) -> AsyncIterator[AIServiceResponse]:
        """Process tool execution result from desktop and format response.

        When a tool fails, the system:
        1. Runs diagnostic commands automatically to gather real system data
        2. Feeds the diagnostic results to the LLM
        3. LLM decides with actual information (not guessing)

        The flow supports up to MAX_TOOL_RETRIES iterations.
        """
        MAX_TOOL_RETRIES = 4

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
                tool_name=tool_name, tool_call_id=tool_call_id,
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

        # Save to session
        await self.sessions.add_message(
            session_id=actual_sid,
            role="assistant",
            content=final_text,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )

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
                session_id=actual_sid, role="assistant", content=msg
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
            f"Pastas principais: Downloads={downloads}, Documents={documents}, Desktop={desktop_dir}\n"
            f"Shell padrão: {'bash/zsh' if platform in ('linux', 'darwin') else 'PowerShell/cmd'}.\n"
            f"Use caminhos absolutos ao manipular arquivos. "
            f"Prefira paths relativos ao home ({home}) em comandos quando possível."
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

    @staticmethod
    def _truncate_output(value: str, max_lines: int = 60) -> str:
        """Truncate long output to max_lines."""
        lines = (value or "").splitlines()
        if len(lines) <= max_lines:
            return value or "(sem saída)"
        return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} linhas omitidas)"
