"""Agent executor — double-loop com loop detection e compactação.

Outer loop: retry/recovery (context overflow, loops, provider errors)
Inner loop: LLM → Tools → LLM (execução normal)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable
from uuid import UUID

from core.agent.context_compactor import ContextCompactor
from core.agent.context_manager import ContextManager
from core.agent.loop_detector import LoopDetector
from core.agent.prompt_builder import PromptBuilder
from core.agent.token_counter import TokenCounter
from core.authority import com_portao
from core.liveness import TurnLiveness, current_liveness, touch_progress
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

#: Chamadas de tool com JSON quebrado que o loop ainda pede de novo antes de
#: desistir. Sessão b230a40d (2026-11-05): o nemotron-3.5-lightning devolveu os
#: argumentos do `browser_task` com uma string aberta, o `json.loads` levantou e
#: o `except` genérico entregou "Erro na execução: Unterminated string…" como a
#: resposta do Neo. Mesmo teto do loop cognitivo (`_MAX_TRUNCATED_TOOL_ROUNDS`
#: em modules/desktop/ai_service.py), que já tratava isso.
_MAX_TOOL_CALL_QUEBRADA = 2


class _AgentStalled(Exception):
    """O agente ficou ``idle`` segundos sem avançar."""

    def __init__(self, idle: float, fase: str) -> None:
        super().__init__(f"sem avanço há {idle:.0f}s ({fase})")
        self.idle = idle
        self.fase = fase


class _StallWatch:
    """Há quanto tempo o agente está SEM avançar — não há quanto tempo começou.

    Avanço é o que o próprio loop registra (resposta do LLM, resultado de tool) e,
    dentro de um turno do desktop, também o que o resto do turno registra no
    ``TurnLiveness`` (o probe do sandbox toca nele enquanto um ``neo_bash`` longo
    trabalha). Tocar aqui propaga para o turno: é isso que mantém o keepalive do
    gRPC saindo enquanto o agente progride.
    """

    def __init__(self) -> None:
        self._proprio = TurnLiveness()
        self.fase = "começando"

    def touch(self, fase: str) -> None:
        self.fase = fase
        self._proprio.touch()
        touch_progress()

    def idle_seconds(self) -> float:
        parado = self._proprio.idle_seconds()
        turno = current_liveness()
        if turno is not None:
            parado = min(parado, turno.idle_seconds())
        return parado


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
        force_tool_first: bool = False,
        on_tool_calls: Callable[[list[ToolCall]], Awaitable[None]] | None = None,
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
            force_tool_first: impõe ``tool_choice="required"`` no PRIMEIRO round.

                Para quando a decisão de agir já foi tomada antes deste loop e
                "responder em texto" não é um desfecho válido. Sem isto, o
                modelo pode devolver uma promessa ("deixa eu ver o estado dos
                jobs...") e encerrar o turno sem ter feito nada — foi o que
                aconteceu na sessão 8c449cb8 (2026-09-21): o classificador
                escolheu `browser_local_task`, montou os argumentos completos, e
                o agente respondeu prosa em 1 iteração. Nenhum job foi criado, e
                o usuário só descobriu 9 minutos depois, perguntando.
            on_tool_calls: chamado com as tools de cada rodada ANTES de rodarem.
                É por onde quem chama mostra o progresso (o desktop: "olhando a
                caixa de email" e quem está trabalhando). Falha aqui é só log —
                progresso nunca derruba o agente.
        Returns:
            Dict com status, final_response, steps, iterations, metadata.
        """
        max_outer_retries = 3
        loop_hints_injected: set[LoopType] = set()

        # Escolha do painel (role "cognitive") — resolvida UMA vez por turno, e
        # não a cada iteração: dentro do inner loop uma troca no meio do caminho
        # significaria metade do turno num modelo e metade em outro.
        provider, model = await self._resolve_brain(provider, model)
        brain = {"provider": provider, "model": model}

        watch = _StallWatch()
        for outer_attempt in range(max_outer_retries):
            # Os passos vivem FORA do inner loop para sobreviverem ao corte por
            # inatividade: é o que já foi apurado, e jogar fora é obrigar o
            # usuário a pagar tudo de novo no turno seguinte.
            passos: list[AgentStep] = []
            try:
                result = await self._run_watched(
                    self._inner_loop(
                        task, session_id, memory, provider, model, loop_hints_injected,
                        system_prompt_override=system_prompt_override,
                        tool_subset=tool_subset,
                        tool_executor=tool_executor,
                        available_tools=available_tools,
                        force_tool_first=force_tool_first,
                        steps_sink=passos,
                        watch=watch,
                        on_tool_calls=on_tool_calls,
                    ),
                    watch,
                )
                result["metadata"] = {**brain, **(result.get("metadata") or {})}
                return result

            except _AgentStalled as e:
                logger.error(
                    "Agent parado há %.0fs (%s) — %d passo(s) feitos",
                    e.idle, e.fase, len(passos),
                )
                return self._build_result(
                    status=ExecutionStatus.TIMEOUT,
                    response=self._stalled_text(e, passos),
                    steps=passos,
                    iterations=len(passos),
                    session_id=session_id,
                    metadata={**brain, "stalled_after_s": round(e.idle), "stalled_in": e.fase},
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

    async def _run_watched(self, coro: Any, watch: _StallWatch) -> dict[str, Any]:
        """Roda o inner loop e o corta só quando ele PARA de avançar.

        Substitui um ``wait_for(300)`` contado do início: um agente lento mas
        produtivo (5 consultas SQL a 30-60s por ida ao modelo) morria com o
        trabalho quase pronto. Exceções do loop (loop detectado, overflow)
        atravessam intactas para o outer loop tratar.
        """
        limite = float(self.settings.agent_stall_timeout)
        vigia = max(0.01, min(5.0, limite / 4))
        tarefa = asyncio.ensure_future(coro)
        try:
            while True:
                feitas, _ = await asyncio.wait({tarefa}, timeout=vigia)
                if feitas:
                    return tarefa.result()
                parado = watch.idle_seconds()
                if parado >= limite:
                    tarefa.cancel()
                    # O cancelamento chega às tools (o sandbox mata a árvore do
                    # processo no CancelledError); esperar garante isso antes de
                    # o turno seguir.
                    await asyncio.gather(tarefa, return_exceptions=True)
                    raise _AgentStalled(parado, watch.fase)
        finally:
            if not tarefa.done():
                tarefa.cancel()

    @staticmethod
    def _stalled_text(stall: _AgentStalled, passos: list[AgentStep]) -> str:
        """O que o usuário lê quando o agente parou: onde travou e o que já tinha.

        O resultado das últimas tools entra no texto porque ele vira a resposta
        salva no histórico — o turno seguinte ("continua") parte dali em vez de
        refazer as consultas.
        """
        linhas = [f"Parei: fiquei {stall.idle:.0f}s sem avançar ({stall.fase})."]
        resultados = [r for p in passos for r in p.tool_results]
        if not resultados:
            linhas.append("Nenhuma ferramenta chegou a rodar antes disso.")
            return "\n".join(linhas)

        contagem: dict[str, int] = {}
        for r in resultados:
            contagem[r.name] = contagem.get(r.name, 0) + 1
        feitas = ", ".join(f"{n} ×{c}" if c > 1 else n for n, c in contagem.items())
        linhas.append(f"Antes disso rodei {len(resultados)} ferramenta(s): {feitas}.")

        for r in passos[-1].tool_results[-2:]:
            corpo = r.error or str(r.result or "")
            if len(corpo) > 1500:
                corpo = corpo[:1500] + "\n[…cortado]"
            rotulo = "erro" if r.error else "resultado"
            linhas.append(f"\nÚltimo {rotulo} de `{r.name}`:\n```\n{corpo}\n```")
        return "\n".join(linhas)

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
        force_tool_first: bool = False,
        steps_sink: list[AgentStep] | None = None,
        watch: _StallWatch | None = None,
        on_tool_calls: Callable[[list[ToolCall]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Loop interno: LLM → Tools → LLM com detecção de loops.

        ``steps_sink``: lista onde os passos são acumulados, para quem chama
        continuar com eles se o loop for cancelado no meio. ``watch``: onde cada
        avanço é registrado (o corte por inatividade lê daí).
        """
        steps: list[AgentStep] = steps_sink if steps_sink is not None else []
        watch = watch or _StallWatch()
        status = ExecutionStatus.RUNNING
        iteration = 0
        quebradas = 0
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
                # `iteration` já foi incrementado no topo do laço: o primeiro
                # round é o 1. Com `== 0` (como estava) o forçamento nunca
                # disparava — toda chamada da session-d35696d4 saiu com
                # tool_choice "auto".
                #
                # "Até a primeira tool rodar", e não "só no round 1": quando a
                # chamada forçada volta com JSON quebrado, o round seguinte ainda
                # é o primeiro a agir, e soltá-lo daria ao modelo a saída de
                # responder em prosa. Sem chamada quebrada, os dois são o mesmo
                # round (`passos` nasce vazio a cada tentativa do outer loop).
                forcando = bool(force_tool_first and not steps and available_tools)
                # O raciocínio do OpenRouter vem ligado sozinho, é gerado antes do
                # primeiro token e ignora o max_tokens: na session-d35696d4 cada ida
                # ao modelo levou 31-61s. Mesma flag do loop cognitivo — é o mesmo
                # cérebro, não pode pensar de um jeito num trilho e de outro no outro.
                extra: dict[str, Any] = {
                    "disable_thinking": not self.settings.cognitive_reasoning_enabled,
                }
                if forcando:
                    extra["tool_choice"] = "required"
                    # E aqui é obrigatório: o deepseek-v4 RECUSA tool_choice
                    # forçado com 400 em thinking mode.
                    extra["disable_thinking"] = True
                watch.touch("esperando a resposta do modelo")
                response = await llm_provider.complete(
                    messages=current_messages,
                    tools=available_tools,
                    model=model or self.llm_router.get_default_model(provider),
                    temperature=self.settings.default_temperature,
                    max_tokens=self.settings.default_max_tokens,
                    **extra,
                )

                content = response.get_content()
                try:
                    tool_calls = response.get_tool_calls()
                except (json.JSONDecodeError, ValueError, TypeError) as e:
                    # Argumentos que não são JSON (cortados, ou com aspas
                    # soltas). NADA roda: consertar o JSON executaria uma tarefa
                    # pela metade, ou um e-mail com o corpo cortado.
                    quebradas += 1
                    logger.warning(
                        "[agent] tool call com JSON quebrado (%d/%d): %s",
                        quebradas, _MAX_TOOL_CALL_QUEBRADA, e,
                    )
                    if quebradas > _MAX_TOOL_CALL_QUEBRADA:
                        final_response = (
                            f"O modelo devolveu a chamada da ferramenta quebrada "
                            f"{quebradas} vezes seguidas (JSON inválido), então nada "
                            "foi executado. Me peça de novo; se repetir, troque o "
                            "modelo no seletor."
                        )
                        status = ExecutionStatus.FAILED
                        break
                    context_mgr.add_user_message(
                        "Sua última chamada de ferramenta chegou com os argumentos "
                        f"QUEBRADOS (JSON inválido ou cortado: {e}), então ela NÃO "
                        "rodou. Refaça a chamada com JSON válido e argumentos curtos, "
                        "sem repetir texto longo."
                    )
                    continue

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
                watch.touch("rodando " + ", ".join(tc.name for tc in tool_calls))
                if on_tool_calls is not None:
                    try:
                        await on_tool_calls(tool_calls)
                    except Exception as exc:  # noqa: BLE001 — progresso nunca derruba o agente
                        logger.warning("Aviso de progresso do agente falhou: %s", exc)
                step.tool_results = await self._execute_tools(tool_calls, executor=tool_executor)
                steps.append(step)
                watch.touch("tools concluídas")

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
        """Executa tool calls em paralelo, cada uma pelo portão de autoridade.

        O portão vive DENTRO de dois executores, e este laço recebe o executor
        por parâmetro: o pipeline do WhatsApp e o do Home Assistant injetam o
        deles, que não chama o portão nenhuma vez. Envolver aqui cobre qualquer
        executor injetado, inclusive o que ainda não existe — e envolver um que
        já gateia por dentro é inofensivo (o portão deduplica por
        ``tool_call.id``). Ver core/authority/wrap.py.
        """
        ex = com_portao(executor or self.tool_executor, rotulo="agente")
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
