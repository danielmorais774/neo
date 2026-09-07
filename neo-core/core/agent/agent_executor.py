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
        db: Any | None = None,
    ) -> None:
        self.llm_router = llm_router
        self.tool_registry = tool_registry
        self.tool_executor = tool_executor
        self.settings = get_settings()
        #: Só para ler o role "cognitive" do painel. Opcional de propósito: sem
        #: db o executor cai no env, que é exatamente o comportamento antigo.
        self.db = db

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
        system_prompt_override: str | None = None,
        tool_subset: list["ToolSchema"] | None = None,
        tool_executor: Any = None,
        available_tools: list["ToolSchema"] | None = None,
    ) -> dict[str, Any]:
        """Executa uma task com resiliência (double-loop).

        Args:
            task: o prompt / task description
            session_id: session ID opcional
            memory: ConversationMemory opcional
            provider: override de provider LLM
            model: override de model
            system_prompt_override: usa esse system prompt em vez do padrão
            tool_subset: lista de ToolSchema — substitui tools padrão
            tool_executor: executor custom para as tools da skill
            available_tools: tools visíveis ao LLM (mutually exclusive with tool_subset)
        Returns:
            Dict com status, final_response, steps, iterations, metadata.
        """
        max_outer_retries = 3
        loop_hints_injected: set[LoopType] = set()

        # Escolha do painel (role "cognitive") — resolvida UMA vez por turno, e
        # não a cada iteração: dentro do inner loop uma troca no meio do caminho
        # significaria metade do turno num modelo e metade em outro.
        provider, model = await self._resolve_brain(provider, model)

        for outer_attempt in range(max_outer_retries):
            try:
                result = await asyncio.wait_for(
                    self._inner_loop(
                        task, session_id, memory, provider, model, loop_hints_injected,
                        system_prompt_override=system_prompt_override,
                        tool_subset=tool_subset,
                        tool_executor=tool_executor,
                        available_tools=available_tools,
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

    async def _resolve_brain(
        self, provider: str | None, model: str | None
    ) -> tuple[str, str]:
        """Provider/modelo do cérebro: override do chamador → painel → env.

        O override explícito ganha sempre (é como o `service.py` deixa alguém
        pedir um modelo pontual). Sem ele, vale o que o operador escolheu na aba
        Modelos; e sem escolha, o env — o comportamento que existia antes deste
        select. Nunca levanta: um erro de leitura aqui derrubaria o turno inteiro
        por causa de uma preferência, então a falha degrada para o env.
        """
        env_provider = provider or self.settings.default_provider
        env_model = model or self.llm_router.get_default_model(env_provider)
        if provider and model:
            return provider, model
        try:
            from modules.model_config import get_model_config

            config = await get_model_config(
                self.db,
                "cognitive",
                fallback_provider=env_provider,
                fallback_model=env_model,
            )
            chosen = config.get("provider") or env_provider
            # Provider gravado mas sem chave configurada (removeram a key depois
            # da escolha): `get_provider` levantaria e TODO turno morreria por
            # causa de uma preferência velha. Volta para o env e avisa.
            if chosen not in self.llm_router.list_available_providers():
                logger.warning(
                    "Role 'cognitive' aponta para '%s', que não está configurado — "
                    "usando %s/%s",
                    chosen, env_provider, env_model,
                )
                return env_provider, env_model
            return (
                provider or chosen,
                model or config.get("model") or env_model,
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                "resolve cognitive model falhou, usando %s/%s: %s",
                env_provider, env_model, e,
            )
            return env_provider, env_model

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
        system_prompt_override: str | None = None,
        tool_subset: list["ToolSchema"] | None = None,
        tool_executor: Any = None,
        available_tools: list["ToolSchema"] | None = None,
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

        prompt_builder = PromptBuilder(system_prompt=system_prompt_override)
        available_tools = tool_subset or available_tools or self.tool_registry.get_all()

        # Construir mensagens iniciais
        initial_messages = prompt_builder.build_initial_messages(task, available_tools)
        if memory and memory.get_messages():
            task_message = initial_messages[-1]
            memory_messages = [
                msg for msg in memory.get_messages()
                if msg.role != MessageRole.SYSTEM
            ]
            initial_messages = [initial_messages[0], *memory_messages]
            if not memory_messages or not self._same_message(memory_messages[-1], task_message):
                initial_messages.append(task_message)

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
                if not any(self._same_message(existing, msg) for existing in memory.messages):
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
                step.tool_results = await self._execute_tools(tool_calls, executor=tool_executor)
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
                    final_response = self._failure_text(step.tool_results)
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

    async def _execute_tools(
        self,
        tool_calls: list[ToolCall],
        executor: Any = None,
    ) -> list[Any]:
        """Executa tool calls em paralelo."""
        ex = executor or self.tool_executor
        tasks = [ex.execute_tool_call(tc) for tc in tool_calls]
        return await asyncio.gather(*tasks)

    @staticmethod
    def _failure_text(tool_results: list[Any]) -> str:
        """Texto de "todas falharam" com o MOTIVO, não só o fato.

        A frase genérica escondia erros que já vinham escritos para humano
        ("login recusado … precisa ser uma SENHA DE APP") e deixava o turno sem
        rastro: o erro da tool não ia para o chat, nem para o session log, nem
        para o app.log. Custou duas sessões inteiras de 'Todas as ferramentas
        falharam' com o motivo real a uma linha de distância.
        """
        motivos: list[str] = []
        for r in tool_results:
            erro = str(getattr(r, "error", "") or "").strip()
            if not erro:
                continue
            nome = getattr(r, "name", None) or "tool"
            linha = f"{nome}: {erro[:400]}"
            if linha not in motivos:
                motivos.append(linha)
        if not motivos:
            return "Todas as ferramentas falharam. Não foi possível completar a tarefa."
        if len(motivos) == 1:
            return f"A ferramenta falhou — {motivos[0]}"
        return "Todas as ferramentas falharam:\n" + "\n".join(f"- {m}" for m in motivos)

    @staticmethod
    def _same_message(left: Message, right: Message) -> bool:
        return left.role == right.role and left.content == right.content

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
