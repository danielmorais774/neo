"""Agent executor — double-loop com loop detection e compactação.

Outer loop: retry/recovery (context overflow, loops, provider errors)
Inner loop: LLM → Tools → LLM (execução normal)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from core.agent.context_compactor import ContextCompactor
from core.agent.context_manager import ContextManager
from core.agent.loop_detector import LoopDetector
from core.agent.prompt_builder import PromptBuilder
from core.agent.token_counter import TokenCounter
from core.llm import LLMRouter
from core.memory import ConversationMemory
from core.tools import ToolExecutor, ToolRegistry
from shared import (
    AgentIterationLimitError,
    AgentLoopDetectedError,
    AgentStep,
    AgentTimeoutError,
    ContextOverflowError,
    ExecutionStatus,
    LoopType,
    Message,
    MessageRole,
    ToolCall,
    get_settings,
)

logger = logging.getLogger(__name__)


class AgentExecutor:
    """Executor do agente Neo com double-loop resiliente.

    Outer loop — trata erros recuperáveis:
      - ContextOverflowError → compacta e re-tenta
      - AgentLoopDetectedError → injeta hint e re-tenta 1x
      - LLMProviderError → backoff e re-tenta

    Inner loop — execução normal:
      - LLM → tool calls → resultados → LLM → ...
      - Loop detector analisa cada step
      - Context manager compacta se necessário
    """

    def __init__(
        self,
        llm_router: LLMRouter,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        token_counter: TokenCounter | None = None,
        compactor: ContextCompactor | None = None,
    ) -> None:
        self.llm_router = llm_router
        self.tool_registry = tool_registry
        self.tool_executor = tool_executor
        self.settings = get_settings()

        self._token_counter = token_counter or TokenCounter(model=self.settings.default_model)
        self._compactor = compactor

    # ------------------------------------------------------------------
    # Execução principal (outer loop)
    # ------------------------------------------------------------------

    async def execute(
        self,
        task: str,
        session_id: UUID | None = None,
        memory: ConversationMemory | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Executa uma task com resiliência (double-loop).

        Returns:
            Dict com status, final_response, steps, iterations, metadata.
        """
        max_outer_retries = 3
        loop_hints_injected: set[LoopType] = set()

        for outer_attempt in range(max_outer_retries):
            try:
                result = await asyncio.wait_for(
                    self._inner_loop(
                        task, session_id, memory, provider, model, loop_hints_injected,
                    ),
                    timeout=self.settings.agent_global_timeout,
                )
                return result

            except asyncio.TimeoutError:
                logger.error(
                    f"Agent timeout após {self.settings.agent_global_timeout}s"
                )
                return self._build_result(
                    status=ExecutionStatus.TIMEOUT,
                    response=(
                        f"Timeout: execução excedeu "
                        f"{self.settings.agent_global_timeout}s."
                    ),
                    steps=[],
                    iterations=0,
                    session_id=session_id,
                )

            except AgentLoopDetectedError as e:
                if e.loop_type in loop_hints_injected:
                    # Já tentou recovery para este tipo — desiste
                    logger.warning(f"Loop {e.loop_type.value} persistiu após recovery")
                    return self._build_result(
                        status=ExecutionStatus.FAILED,
                        response=(
                            f"Execução interrompida: loop detectado "
                            f"({e.loop_type.value})."
                        ),
                        steps=[],
                        iterations=0,
                        session_id=session_id,
                        metadata={"loop_type": e.loop_type.value},
                    )
                # Primeira vez: marcar e re-tentar com hint
                loop_hints_injected.add(e.loop_type)
                logger.info(
                    f"Loop {e.loop_type.value} detectado, re-tentando com hint"
                )
                continue

            except ContextOverflowError:
                logger.info("Context overflow no outer loop, re-tentando")
                continue

            except Exception as e:
                logger.error(
                    f"Erro não recuperável no outer loop: {e}", exc_info=True
                )
                return self._build_result(
                    status=ExecutionStatus.FAILED,
                    response=f"Erro na execução: {e}",
                    steps=[],
                    iterations=0,
                    session_id=session_id,
                )

        return self._build_result(
            status=ExecutionStatus.FAILED,
            response="Execução falhou após múltiplas tentativas de recovery.",
            steps=[],
            iterations=0,
            session_id=session_id,
        )

    # ------------------------------------------------------------------
    # Inner loop
    # ------------------------------------------------------------------

    async def _inner_loop(
        self,
        task: str,
        session_id: UUID | None,
        memory: ConversationMemory | None,
        provider: str | None,
        model: str | None,
        loop_hints: set[LoopType],
    ) -> dict[str, Any]:
        """Loop interno: LLM → Tools → LLM com detecção de loops."""
        steps: list[AgentStep] = []
        status = ExecutionStatus.RUNNING
        iteration = 0
        final_response: str | None = None

        # Componentes por execução (estado isolado)
        context_mgr = ContextManager(
            token_counter=self._token_counter,
            compactor=self._compactor,
            max_context_tokens=self.settings.max_context_tokens,
            compaction_threshold=self.settings.compaction_threshold_ratio,
        )
        loop_detector = LoopDetector(self.settings)
        loop_detector.reset()

        prompt_builder = PromptBuilder()
        available_tools = self.tool_registry.get_all()

        # Construir mensagens iniciais
        initial_messages = prompt_builder.build_initial_messages(task, available_tools)

        # Injetar hints de loop do outer loop
        for loop_type in loop_hints:
            hint = LoopDetector.get_recovery_hint(loop_type)
            initial_messages.append(Message(role=MessageRole.SYSTEM, content=hint))

        # Nota sobre compactação no system prompt
        if loop_hints or self._compactor is not None:
            initial_messages[0] = Message(
                role=MessageRole.SYSTEM,
                content=(
                    initial_messages[0].content
                    + "\n\nNOTA: Se você receber um '[Resumo do contexto anterior]', "
                    "confie nele — ele preserva as informações essenciais da conversa."
                ),
            )

        context_mgr.set_initial_messages(initial_messages)

        if memory:
            for msg in initial_messages:
                memory.add_message(msg)

        try:
            while iteration < self.settings.max_iterations:
                iteration += 1

                # 1. Verificar loops antes de cada iteração
                detected = loop_detector.check()
                if detected is not None:
                    raise AgentLoopDetectedError(
                        message=f"Loop detectado: {detected.value}",
                        loop_type=detected,
                    )

                # 2. Obter contexto (pode compactar automaticamente)
                current_messages = await context_mgr.get_messages()

                # 3. Chamar LLM
                llm_provider = self.llm_router.get_provider(provider)
                response = await llm_provider.complete(
                    messages=current_messages,
                    tools=available_tools,
                    model=model or self.llm_router.get_default_model(provider),
                    temperature=self.settings.default_temperature,
                    max_tokens=self.settings.default_max_tokens,
                )

                content = response.get_content()
                tool_calls = response.get_tool_calls()

                # 4. Sem tool calls → resposta final
                if not tool_calls:
                    final_response = content or "Tarefa concluída."
                    status = ExecutionStatus.COMPLETED
                    break

                # 5. Criar step e executar tools
                step = AgentStep(
                    step_number=iteration,
                    thought=content,
                    tool_calls=tool_calls,
                )
                step.tool_results = await self._execute_tools(tool_calls)
                steps.append(step)

                # 6. Registrar no loop detector
                loop_detector.record_step(step)

                # 7. Adicionar ao contexto
                if content:
                    context_mgr.add_assistant_message(content)
                context_mgr.add_tool_results(step)

                if memory:
                    memory.add_step(step)

                # 8. Todas as tools falharam?
                all_failed = all(r.error for r in step.tool_results)
                if all_failed:
                    final_response = (
                        "Todas as ferramentas falharam. "
                        "Não foi possível completar a tarefa."
                    )
                    status = ExecutionStatus.FAILED
                    break

            # Atingiu limite de iterações
            if (
                iteration >= self.settings.max_iterations
                and status == ExecutionStatus.RUNNING
            ):
                raise AgentIterationLimitError(
                    f"Atingiu limite de {self.settings.max_iterations} iterações"
                )

        except AgentIterationLimitError:
            status = ExecutionStatus.TIMEOUT
            final_response = (
                f"Atingiu o limite de {self.settings.max_iterations} iterações. "
                "A tarefa pode estar incompleta."
            )

        except AgentLoopDetectedError:
            raise  # Propagar para o outer loop tratar

        except Exception as e:
            status = ExecutionStatus.FAILED
            final_response = f"Erro na execução: {e}"
            logger.error(f"Erro no inner loop: {e}", exc_info=True)

        return self._build_result(
            status=status,
            response=final_response,
            steps=steps,
            iterations=iteration,
            session_id=session_id,
            metadata={"compaction_applied": context_mgr.compaction_applied},
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _execute_tools(self, tool_calls: list[ToolCall]) -> list[Any]:
        """Executa tool calls em paralelo."""
        tasks = [self.tool_executor.execute_tool_call(tc) for tc in tool_calls]
        return await asyncio.gather(*tasks)

    @staticmethod
    def _build_result(
        status: ExecutionStatus,
        response: str | None,
        steps: list[AgentStep],
        iterations: int,
        session_id: UUID | None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Constrói resultado padronizado da execução."""
        return {
            "status": status.value,
            "final_response": response,
            "steps": steps,
            "iterations": iterations,
            "session_id": str(session_id) if session_id else None,
            "metadata": metadata or {},
        }
