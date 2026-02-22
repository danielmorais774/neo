"""Agent executor - loop principal de execução do agente."""

import asyncio
from typing import Any
from uuid import UUID

from core.agent.context_manager import ContextManager
from core.agent.prompt_builder import PromptBuilder
from core.llm import LLMRouter
from core.memory import ConversationMemory
from core.tools import ToolExecutor, ToolRegistry
from shared import (
    AgentIterationLimitError,
    AgentStep,
    AgentTimeoutError,
    ExecutionStatus,
    ToolCall,
    get_settings,
)


class AgentExecutor:
    """
    Executor principal do agente Neo.

    Gerencia o loop de execução: LLM → Tools → LLM → ...
    """

    def __init__(
        self,
        llm_router: LLMRouter,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
    ) -> None:
        """
        Inicializa o executor.

        Args:
            llm_router: Router de LLM providers
            tool_registry: Registry de tools
            tool_executor: Executor de tools
        """
        self.llm_router = llm_router
        self.tool_registry = tool_registry
        self.tool_executor = tool_executor
        self.settings = get_settings()

        self.prompt_builder = PromptBuilder()
        self.context_manager = ContextManager()

    async def execute(
        self,
        task: str,
        session_id: UUID | None = None,
        memory: ConversationMemory | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """
        Executa uma task do usuário.

        Args:
            task: Descrição da task
            session_id: ID da sessão (para tracking)
            memory: Memória da conversação (opcional)
            provider: Provider LLM a usar
            model: Modelo específico

        Returns:
            Resultado da execução com status, steps, resposta final

        Raises:
            AgentIterationLimitError: Se atingir limite de iterações
            AgentTimeoutError: Se timeout global
        """
        # Setup
        steps: list[AgentStep] = []
        status = ExecutionStatus.RUNNING

        # Busca tools disponíveis
        available_tools = self.tool_registry.get_all()

        # Constrói mensagens iniciais
        initial_messages = self.prompt_builder.build_initial_messages(
            task, available_tools
        )
        self.context_manager.set_initial_messages(initial_messages)

        # Salva na memória se fornecida
        if memory:
            for msg in initial_messages:
                memory.add_message(msg)

        # Loop principal
        iteration = 0
        final_response: str | None = None

        try:
            while iteration < self.settings.max_iterations:
                iteration += 1

                # Chama LLM
                current_messages = self.context_manager.get_messages()
                llm_provider = self.llm_router.get_provider(provider)

                response = await llm_provider.complete(
                    messages=current_messages,
                    tools=available_tools,
                    model=model or self.llm_router.get_default_model(provider),
                    temperature=self.settings.default_temperature,
                    max_tokens=self.settings.default_max_tokens,
                )

                # Processa resposta
                content = response.get_content()
                tool_calls = response.get_tool_calls()

                # Se não há tool calls, terminamos
                if not tool_calls:
                    final_response = content or "Task completed."
                    status = ExecutionStatus.COMPLETED
                    break

                # Cria step
                step = AgentStep(
                    step_number=iteration,
                    thought=content,
                    tool_calls=tool_calls,
                )

                # Executa tools
                step.tool_results = await self._execute_tools(tool_calls)
                steps.append(step)

                # Adiciona ao contexto
                if content:
                    self.context_manager.add_assistant_message(content)
                self.context_manager.add_tool_results(step)

                # Salva na memória
                if memory:
                    memory.add_step(step)

                # Verifica se todas as tools falharam
                all_failed = all(r.error for r in step.tool_results)
                if all_failed:
                    final_response = (
                        "All tool executions failed. Unable to complete task."
                    )
                    status = ExecutionStatus.FAILED
                    break

            # Atingiu limite de iterações
            if iteration >= self.settings.max_iterations and status == ExecutionStatus.RUNNING:
                status = ExecutionStatus.TIMEOUT
                final_response = (
                    f"Reached maximum iterations ({self.settings.max_iterations}). "
                    "Task may be incomplete."
                )

        except Exception as e:
            status = ExecutionStatus.FAILED
            final_response = f"Execution error: {str(e)}"

        return {
            "status": status.value,
            "final_response": final_response,
            "steps": steps,
            "iterations": iteration,
            "session_id": str(session_id) if session_id else None,
        }

    async def _execute_tools(self, tool_calls: list[ToolCall]) -> list[Any]:
        """
        Executa múltiplas tool calls em paralelo.

        Args:
            tool_calls: Lista de tool calls

        Returns:
            Lista de resultados
        """
        tasks = [
            self.tool_executor.execute_tool_call(tool_call) for tool_call in tool_calls
        ]
        return await asyncio.gather(*tasks)
