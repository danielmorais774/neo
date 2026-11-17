"""Plan Manager — multi-step task planning and execution for complex user requests.

Architecture:
  - PlanManager is stateless: plans are stored in-memory keyed by session_id
  - Each plan has ordered steps with status tracking
  - Steps can spawn sub-plans for unexpected complexity
  - After each tool_result, the plan advances or adapts
  - Falls back to existing 1-step behavior when no plan is needed
"""

import json
import logging
import time
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from core.llm.router import LLMRouter
from shared import Message, MessageRole, get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


def _pedido_literal(plan: "ExecutionPlan") -> str:
    """O bloco com o pedido LITERAL do usuário, para quem refina passos (vazio sem ele)."""
    pedido = (getattr(plan, "pedido", "") or "").strip()
    if not pedido:
        return ""
    return (
        f'Pedido do usuário (literal): "{pedido[:1500]}"\n'
        "Caminhos, nomes e destinos citados nele valem exatamente como escritos.\n\n"
    )


class PlanStep(BaseModel):
    """A single step in an execution plan."""

    id: str = Field(default_factory=lambda: str(uuid4())[:8])
    index: int
    description: str  # Human-readable: "Listar arquivos CSV na pasta de documentos"
    tool_name: str = "run_command"  # única tool client-side real hoje
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    raw_command: str | None = None  # Shell command if tool_name == run_command
    requires_approval: bool = False
    status: Literal["pending", "running", "completed", "failed", "skipped"] = "pending"
    result: str | None = None  # Actual output when completed
    error: str | None = None  # Error text if failed

    # Sub-plan support: a step can spawn a nested plan
    subplan_id: str | None = None  # ID of nested plan if this step was too complex

    # Dynamic: the command may be refined based on previous step outputs
    depends_on_previous: bool = False  # If True, command will be regenerated with context


class ExecutionPlan(BaseModel):
    """A multi-step execution plan for a complex user request."""

    id: str = Field(default_factory=lambda: str(uuid4())[:12])
    goal: str  # Resumo do objetivo escrito pelo PLANEJADOR (paráfrase, não o pedido)
    #: O pedido do usuário, LITERAL. O `goal` é a paráfrase do planejador e perde o
    #: que importa: na session-7b61bc5e o pedido dizia "compacta a pasta
    #: projeto-loja em ~/Documents/work/projeto-loja.zip", o goal virou "compactar
    #: diretório de trabalho", e o passo refinado gravou o zip na home.
    pedido: str = ""
    steps: list[PlanStep] = Field(default_factory=list)
    current_step_index: int = 0
    status: Literal["active", "completed", "failed", "cancelled"] = "active"
    parent_plan_id: str | None = None  # If this is a sub-plan
    parent_step_id: str | None = None  # Which step of the parent plan spawned this
    created_at: float = Field(default_factory=time.time)

    # Accumulated context from completed steps (step_id → output summary)
    step_results: dict[str, str] = Field(default_factory=dict)

    def get_current_step(self) -> PlanStep | None:
        """Get the next pending step."""
        for step in self.steps:
            if step.status == "pending":
                return step
        return None

    def get_completed_context(self) -> str:
        """Build a text summary of all completed steps for LLM context."""
        lines = []
        for step in self.steps:
            if step.status == "completed" and step.result:
                lines.append(f"Passo {step.index + 1} ({step.description}): {step.result[:500]}")
            elif step.status == "failed" and step.error:
                lines.append(f"Passo {step.index + 1} ({step.description}): FALHOU - {step.error[:200]}")
        return "\n".join(lines)

    def advance(self, step_result: str | None = None, step_error: str | None = None) -> PlanStep | None:
        """Mark current running step as completed/failed and return the next pending step."""
        # Find the "running" step first (it was set to running when dispatched).
        # Fallback to first pending only if nothing is running (edge case).
        current = None
        for step in self.steps:
            if step.status == "running":
                current = step
                break
        if current is None:
            current = self.get_current_step()
        if not current:
            self.status = "completed"
            return None

        if step_error:
            current.status = "failed"
            current.error = step_error
            self.step_results[current.id] = f"ERRO: {step_error[:300]}"
        else:
            current.status = "completed"
            current.result = step_result
            self.step_results[current.id] = (step_result or "")[:500]

        # O cursor anda AQUI — era o único lugar que podia mexer nele e não
        # mexia, então `current_step_index` ficava 0 pela vida inteira do plano.
        # Quem lê isso não é cosmético: `process_tool_result` deriva
        # `completed_step_idx` daqui e passa pro `advance_and_refine`, que monta
        # "## 2. Último Resultado / Step N: <descrição>" — com o cursor travado,
        # TODO passo depois do primeiro chegava no validador com o output de um
        # passo e a descrição de outro. O log da sessão também escrevia
        # "Passo 1/N" em todas as conclusões (sessão d9f9417b: "Passo 1/5" duas
        # vezes seguidas, "Passo 1/4" duas vezes).
        self.current_step_index = current.index + 1

        # Find next pending step
        next_step = self.get_current_step()
        if next_step is None:
            # Check if any step failed
            if any(s.status == "failed" for s in self.steps):
                self.status = "failed"
            else:
                self.status = "completed"
        return next_step

    @property
    def last_advanced_index(self) -> int:
        """Índice do passo que o último ``advance()`` fechou (0 antes do primeiro).

        Existe para os chamadores pararem de recalcular `current_step_index - 1`
        cada um do seu jeito — e para o caso do sub-plano, em que o chamador
        troca o plano ativo depois de já ter calculado o índice do plano antigo.
        """
        return max(self.current_step_index - 1, 0)

    @property
    def progress_text(self) -> str:
        """Human-readable progress: 'Passo 2/5'."""
        completed = sum(1 for s in self.steps if s.status in ("completed", "skipped"))
        return f"Passo {completed + 1}/{len(self.steps)}"

    @property
    def is_complete(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")

    def to_summary(self) -> str:
        """Resumo do plano em markdown — só para log/depuração.

        NÃO mandar isto pro cliente: o desktop desenha a timeline a partir do
        `plan_update` (``build_plan_update``), e este texto chegava lá como uma
        bolha de "Pensando" com ⬜/✅ crus, que é o que o usuário via em vez do
        cartão.
        """
        lines = [f"**Plano:** {self.goal}\n"]
        for step in self.steps:
            icon = {"pending": "⬜", "running": "🔄", "completed": "✅", "failed": "❌", "skipped": "⏭️"}[step.status]
            lines.append(f"{icon} **{step.index + 1}.** {step.description}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Plan Manager
# ---------------------------------------------------------------------------

MAX_PLAN_STEPS = 10
MAX_SUBPLAN_DEPTH = 2  # Maximum nesting level for sub-plans
MAX_TOTAL_STEPS = 15  # Maximum total steps across plan + all sub-plans


class PlanManager:
    """Manages creation, execution, and lifecycle of execution plans.

    Plans are stored in-memory keyed by session_id.
    The gRPC server passes plan state through pending metadata between cycles.
    """

    def __init__(self, llm_router: LLMRouter) -> None:
        self.llm = llm_router
        self.settings = get_settings()
        # Active plans: session_id → plan (or plan stack for sub-plans)
        self._plans: dict[str, list[ExecutionPlan]] = {}
        # Sub-plan history: session_id → list of hashes to detect loops
        self._subplan_history: dict[str, list[str]] = {}

    # ---------------------------------------------------------------
    # Plan Detection
    # ---------------------------------------------------------------

    async def should_create_plan(
        self,
        user_message: str,
        tool_name: str | None = None,
        tool_arguments: dict | None = None,
        system_context: str | None = None,
    ) -> bool:
        """Quick LLM check: does this request need multi-step planning?

        Returns True if the task requires 2+ steps that build on each other.
        Simple single-command tasks return False.
        """
        prompt = (
            f'Mensagem do usuário: "{user_message}"\n'
            f"Ferramenta sugerida: {tool_name or 'nenhuma'}\n"
            f"Argumentos: {json.dumps(tool_arguments or {}, ensure_ascii=False)}\n\n"
            "Esta tarefa precisa de MÚLTIPLOS passos sequenciais onde um passo depende do resultado do anterior?\n\n"
            "Exemplos de tarefa SIMPLES (NÃO precisa de plano):\n"
            '- "Liste os arquivos da pasta X" → 1 comando\n'
            '- "Qual meu IP?" → 1 comando\n'
            '- "Instale o pacote X" → 1 comando\n'
            '- "Mostre o conteúdo do arquivo X" → 1 comando\n'
            '- "Crie um arquivo com tal conteúdo" → 1 comando\n\n'
            "Exemplos de tarefa COMPLEXA (PRECISA de plano):\n"
            '- "Encontre os CSVs e mostre o conteúdo do maior" → find + identify + cat\n'
            '- "Faça backup da pasta X, compacte e mova para Y" → tar + mv\n'
            '- "Analise os logs de erro e crie um relatório" → grep + wc + format\n'
            '- "Instale o Docker, configure e suba um container" → install + config + run\n'
            '- "Renomeie todos os .txt para .md na pasta X" → find + rename\n'
            '- "Verifique quais portas estão abertas e mate os processos na 8080" → ss + kill\n\n'
            "IMPORTANTE: Se a tarefa pode ser feita com um ÚNICO comando shell combinado "
            "(usando pipes, &&, etc.), NÃO precisa de plano.\n"
            "Só precisa de plano quando um passo depende do RESULTADO do anterior para decidir o que fazer.\n\n"
            'Responda APENAS: {"needs_plan": true} ou {"needs_plan": false}'
        )

        messages = [
            Message(role=MessageRole.SYSTEM, content="Você é um classificador de complexidade de tarefas. Responda apenas com JSON."),
            Message(role=MessageRole.USER, content=prompt),
        ]

        try:
            provider = self.llm.get_provider()
            response = await provider.complete(
                messages=messages,
                temperature=0.1,
                max_tokens=50,
            )
            raw = (response.get_content() or "").strip()
            clean = raw
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            parsed = json.loads(clean)
            needs_plan = parsed.get("needs_plan", False)
            logger.info(f"[PLAN] should_create_plan: {needs_plan} for: {user_message[:80]}")
            return needs_plan
        except Exception as e:
            logger.warning(f"[PLAN] Failed to check plan need: {e}")
            return False

    # ---------------------------------------------------------------
    # Plan Generation
    # ---------------------------------------------------------------

    async def generate_plan(
        self,
        user_message: str,
        system_context: str | None = None,
        conversation_history: list[dict[str, str]] | None = None,
        memory_context: str | None = None,
    ) -> ExecutionPlan | None:
        """Ask LLM to create a step-by-step execution plan.

        Returns an ExecutionPlan with ordered steps, or None on failure.
        The first step is returned fully refined (complete tool_arguments).
        """
        context_section = ""
        if system_context:
            context_section = f"\n## Contexto do Sistema\n{system_context}\n"

        history_section = ""
        if conversation_history:
            recent = conversation_history[-6:]
            history_lines = []
            for msg in recent:
                role = "Usuário" if msg.get("role") == "user" else "Assistente"
                history_lines.append(f"[{role}]: {msg.get('content', '')[:200]}")
            history_section = f"\n## Histórico Recente\n" + "\n".join(history_lines) + "\n"

        memory_section = ""
        if memory_context:
            memory_section = f"\n## Memória Persistente\n{memory_context}\n"

        system_msg = (
            "Você é um planejador de tarefas para um assistente de desktop Linux.\n"
            "Crie um plano passo a passo para executar a tarefa do usuário.\n\n"
            "## Regras\n"
            "1. Cada passo deve ser UMA ação executável (um comando shell ou operação de arquivo)\n"
            "2. Use caminhos absolutos sempre que possível\n"
            "3. Máximo de 10 passos\n"
            "4. Passos que dependem do resultado do anterior devem ter depends_on_previous=true\n"
            "   Nesses casos, o campo 'raw_command' pode conter um TEMPLATE com {previous_output}\n"
            "   que será preenchido com o resultado real do passo anterior\n"
            "5. Marque requires_approval=true para operações destrutivas (delete, kill, format)\n"
            "6. A ÚNICA ferramenta client-side disponível é `run_command` — não existe\n"
            "   `read_file`/`write_file`/`list_directory`/`search_files`/`get_system_info`\n"
            "   (nunca tiveram execução real e travam o turno se você inventar). TUDO usa\n"
            "   `run_command` com um comando shell: listar (`ls`/`find`), ler (`cat`),\n"
            "   escrever (`printf '%s' \"$conteudo\" > caminho` ou heredoc), espaço/sistema\n"
            "   (`df -h`, `uname -a`).\n"
            "7. Para o primeiro passo e passos independentes, escreva o comando COMPLETO\n"
            "8. Para passos dependentes, descreva o que precisa ser feito — o sistema\n"
            "   vai gerar o comando exato quando o passo anterior completar\n"
            "9. CRÍTICO para tarefas de leitura de múltiplos arquivos + escrita de resumo:\n"
            "   - Crie um step `run_command` (`cat <path>`) SEPARADO para CADA arquivo, com\n"
            "     o path absoluto correto\n"
            "   - Cada step de leitura tem depends_on_previous=false (são independentes entre si)\n"
            "   - O step de escrita final DEVE ter depends_on_previous=true\n"
            "   - Nesse step, use command='[SERÁ GERADO COM BASE NOS ARQUIVOS LIDOS]' como\n"
            "     placeholder — o comando real (heredoc com o conteúdo) será construído após\n"
            "     todos os reads serem completados\n"
            "10. CRÍTICO — NUNCA assuma o nome/caminho exato de um arquivo que o usuário só\n"
            "    DESCREVEU sem citar o nome literal (ex.: \"meu relatório de finanças\", \"aquele\n"
            "    pdf da nota fiscal\"). Nesse caso o PRIMEIRO passo tem que ser uma sondagem\n"
            "    read-only na pasta mais provável (ver Contexto do Sistema: Desktop/Downloads/\n"
            "    Documents) — `ls -la <pasta>` ou `find <pasta> -iname '*palavra-chave*'` —\n"
            "    NUNCA um comando que já cita um nome de arquivo inventado. O passo seguinte\n"
            "    (depends_on_previous=true) só decide o arquivo real depois de ver esse\n"
            "    resultado. Se a sondagem não achar nada parecido, o plano deve terminar\n"
            "    relatando isso ao usuário — nunca inventar um nome pra seguir em frente.\n"
            "    Exemplo: usuário pede \"me manda meu relatório de finanças que tá no\n"
            "    desktop\" sem dizer o nome do arquivo →\n"
            "    passo 1: run_command `ls -la ~/Desktop` (depends_on_previous=false)\n"
            "    passo 2: descrição \"identificar e mostrar o arquivo de finanças encontrado\"\n"
            "    (depends_on_previous=true, comando definido depois de ver o passo 1)\n\n"
            "## Formato de Resposta\n"
            "Responda APENAS com JSON válido (sem markdown fences):\n"
            "{\n"
            '  "goal": "descrição curta do objetivo",\n'
            '  "steps": [\n'
            "    {\n"
            '      "description": "Descrição do passo em português",\n'
            '      "tool_name": "run_command",\n'
            '      "tool_arguments": {"command": "comando shell", "working_dir": "/caminho"},\n'
            '      "requires_approval": false,\n'
            '      "depends_on_previous": false\n'
            "    },\n"
            "    {\n"
            '      "description": "Passo que depende do anterior",\n'
            '      "tool_name": "run_command",\n'
            '      "tool_arguments": {"command": "será definido com base no resultado anterior"},\n'
            '      "requires_approval": false,\n'
            '      "depends_on_previous": true\n'
            "    }\n"
            "  ]\n"
            "}"
        )

        user_prompt = (
            f'Tarefa do usuário: "{user_message}"\n'
            f"{context_section}"
            f"{history_section}"
            f"{memory_section}"
            "\nCrie o plano de execução:"
        )

        messages = [
            Message(role=MessageRole.SYSTEM, content=system_msg),
            Message(role=MessageRole.USER, content=user_prompt),
        ]

        try:
            provider = self.llm.get_provider()
            t0 = time.monotonic()
            response = await provider.complete(
                messages=messages,
                temperature=0.2,
                max_tokens=1500,
            )
            plan_ms = (time.monotonic() - t0) * 1000

            raw = (response.get_content() or "").strip()
            # Vazio = o raciocínio consumiu o max_tokens (finish_reason=length), não
            # "sem plano" — ver deepseek-empty-response-reasoning. Sem retry, o
            # json.loads estoura "Expecting value: line 1 column 1", o plano volta
            # None, e quem chamou fica sem passo nenhum (sessão 9f7abeec).
            if not raw:
                logger.warning("[PLAN] resposta vazia (teto consumido pelo raciocínio) — retentando")
                response = await provider.complete(
                    messages=messages, temperature=0.2, max_tokens=3000,
                )
                raw = (response.get_content() or "").strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            if not raw:
                logger.warning("[PLAN] resposta vazia mesmo após retry — sem plano")
                return None

            parsed = json.loads(raw)
            logger.info(f"[PLAN] Generated plan in {plan_ms:.0f}ms: {len(parsed.get('steps', []))} steps")

            steps = []
            for i, step_data in enumerate(parsed.get("steps", [])):
                if i >= MAX_PLAN_STEPS:
                    logger.warning(f"[PLAN] Truncating plan at {MAX_PLAN_STEPS} steps")
                    break
                steps.append(PlanStep(
                    index=i,
                    description=step_data.get("description", f"Passo {i + 1}"),
                    tool_name=step_data.get("tool_name", "run_command"),
                    tool_arguments=step_data.get("tool_arguments", {}),
                    raw_command=step_data.get("tool_arguments", {}).get("command"),
                    requires_approval=step_data.get("requires_approval", False),
                    depends_on_previous=step_data.get("depends_on_previous", False),
                ))

            if not steps:
                logger.warning("[PLAN] LLM returned empty plan")
                return None

            plan = ExecutionPlan(
                goal=parsed.get("goal", user_message),
                pedido=user_message,
                steps=steps,
            )

            # Programmatic validation (no LLM call)
            if not self._validate_plan(plan):
                logger.warning("[PLAN] Plan failed validation, returning None")
                return None

            logger.info(f"[PLAN] Plan created: id={plan.id}, steps={len(steps)}")
            for s in steps:
                logger.info(f"[PLAN]   Step {s.index}: {s.description} (depends={s.depends_on_previous})")

            return plan

        except Exception as e:
            logger.warning(f"[PLAN] Failed to generate plan: {e}")
            return None

    # ---------------------------------------------------------------
    # Step Refinement (for dependent steps)
    # ---------------------------------------------------------------

    async def refine_step(
        self,
        plan: ExecutionPlan,
        step: PlanStep,
        system_context: str | None = None,
    ) -> PlanStep:
        """Refine a step that depends on previous results.

        Uses the accumulated context from completed steps to generate
        the exact command for this step.
        """
        if not step.depends_on_previous:
            return step  # No refinement needed

        completed_context = plan.get_completed_context()
        if not completed_context:
            return step  # No previous context available

        prompt = (
            f'Objetivo geral: "{plan.goal}"\n\n'
            + _pedido_literal(plan)
            + f"## Resultados dos passos anteriores\n{completed_context}\n\n"
            f"## Passo atual (#{step.index + 1})\n"
            f"Descrição: {step.description}\n"
            f"Ferramenta: {step.tool_name}\n\n"
            "Com base nos resultados anteriores, gere o comando EXATO para este passo.\n"
            "Use informações concretas dos resultados (caminhos de arquivo, nomes, valores, etc.).\n\n"
            "Responda APENAS com JSON:\n"
            "{\n"
            '  "tool_name": "run_command",\n'
            '  "tool_arguments": {"command": "comando exato", "working_dir": "/caminho"},\n'
            '  "requires_approval": false\n'
            "}"
        )

        if system_context:
            prompt += f"\n\nContexto do sistema:\n{system_context}"

        messages = [
            Message(
                role=MessageRole.SYSTEM,
                content="Você é um assistente que gera comandos shell precisos baseados em contexto. Responda apenas com JSON.",
            ),
            Message(role=MessageRole.USER, content=prompt),
        ]

        try:
            provider = self.llm.get_provider()
            response = await provider.complete(
                messages=messages,
                temperature=0.1,
                max_tokens=300,
            )
            raw = (response.get_content() or "").strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

            parsed = self._normalizar_refined_step(json.loads(raw))
            if parsed is None:
                logger.warning(f"[PLAN] refine do passo {step.index} veio sem objeto utilizável — mantendo o passo")
                return step
            step.tool_name = parsed.get("tool_name", step.tool_name)
            step.tool_arguments = parsed.get("tool_arguments", step.tool_arguments)
            step.raw_command = (step.tool_arguments or {}).get("command", step.raw_command)
            step.requires_approval = parsed.get("requires_approval", step.requires_approval)

            logger.info(f"[PLAN] Refined step {step.index}: {step.tool_name} → {step.raw_command or step.tool_arguments}")
            return step

        except Exception as e:
            logger.warning(f"[PLAN] Failed to refine step {step.index}: {e}")
            return step

    # ---------------------------------------------------------------
    # Sub-plan Creation
    # ---------------------------------------------------------------

    async def check_subplan_needed(
        self,
        plan: ExecutionPlan,
        step: PlanStep,
        step_result: str,
        system_context: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        """Check if a completed step's result reveals unexpected complexity requiring a sub-plan.

        Example: Step was "find CSV files" and found 20 files, now each needs processing.
        """
        # Don't create sub-plans too deep
        depth = self._get_plan_depth(plan)
        if depth >= MAX_SUBPLAN_DEPTH:
            logger.info(f"[PLAN] Max subplan depth ({MAX_SUBPLAN_DEPTH}) reached, skipping check")
            return False

        # Check total step count across all plans in session
        if session_id:
            total = self._count_total_steps(session_id)
            if total >= MAX_TOTAL_STEPS:
                logger.info(f"[PLAN] Max total steps ({MAX_TOTAL_STEPS}) reached ({total} steps), skipping subplan")
                return False

        # Loop detection: hash the step result pattern to detect repeated sub-plan attempts
        if session_id:
            result_hash = f"{step.tool_name}:{step.description[:50]}:{step_result[:100]}"
            history = self._subplan_history.get(session_id, [])
            if result_hash in history:
                logger.info(f"[PLAN] Duplicate subplan detected for step '{step.description[:50]}', skipping")
                return False

        prompt = (
            f'Objetivo geral: "{plan.goal}"\n\n'
            f"## Passo concluído (#{step.index + 1})\n"
            f"Descrição: {step.description}\n"
            f"Resultado:\n```\n{step_result[:2000]}\n```\n\n"
            f"## Próximos passos planejados\n"
        )
        remaining = [s for s in plan.steps if s.status == "pending"]
        if remaining:
            for s in remaining[:3]:
                prompt += f"- {s.description}\n"
        else:
            prompt += "(nenhum passo restante)\n"

        prompt += (
            "\nO resultado deste passo revela complexidade inesperada que exige um SUB-PLANO?\n"
            "Exemplos de quando SIM:\n"
            "- O resultado revelou que precisamos processar vários itens individualmente\n"
            "- O resultado mostrou que um passo planejado precisa ser dividido em sub-passos\n"
            "- O resultado revelou dependências ou pré-requisitos não previstos\n\n"
            "Exemplos de quando NÃO:\n"
            "- O resultado é simples e o próximo passo pode seguir normalmente\n"
            "- O resultado é exatamente o esperado\n"
            "- A complexidade pode ser resolvida com o próximo passo já planejado\n\n"
            'Responda APENAS: {"needs_subplan": true, "reason": "motivo"} ou {"needs_subplan": false}'
        )

        messages = [
            Message(role=MessageRole.SYSTEM, content="Você é um analisador de planos. Responda apenas com JSON."),
            Message(role=MessageRole.USER, content=prompt),
        ]

        try:
            provider = self.llm.get_provider()
            response = await provider.complete(
                messages=messages,
                temperature=0.1,
                max_tokens=100,
            )
            raw = (response.get_content() or "").strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            parsed = json.loads(raw)
            needs = parsed.get("needs_subplan", False)
            if needs:
                logger.info(f"[PLAN] Subplan needed after step {step.index}: {parsed.get('reason', '?')}")
            return needs
        except Exception as e:
            logger.warning(f"[PLAN] Failed to check subplan need: {e}")
            return False

    async def create_subplan(
        self,
        parent_plan: ExecutionPlan,
        parent_step: PlanStep,
        step_result: str,
        system_context: str | None = None,
        session_id: str | None = None,
    ) -> ExecutionPlan | None:
        """Create a sub-plan for a step that requires additional breakdown.

        The sub-plan will be executed before continuing with the parent plan's next step.
        """
        # Enforce total step limit
        if session_id:
            total = self._count_total_steps(session_id)
            remaining = MAX_TOTAL_STEPS - total
            if remaining <= 0:
                logger.info(f"[PLAN] Cannot create subplan: total steps ({total}) >= MAX_TOTAL_STEPS ({MAX_TOTAL_STEPS})")
                return None

        completed_context = parent_plan.get_completed_context()

        prompt = (
            f'Objetivo geral do plano principal: "{parent_plan.goal}"\n\n'
            f"## Contexto acumulado\n{completed_context}\n\n"
            f"## Passo que precisa de sub-plano (#{parent_step.index + 1})\n"
            f"Descrição: {parent_step.description}\n"
            f"Resultado:\n```\n{step_result[:2000]}\n```\n\n"
            "Crie um sub-plano para processar adequadamente o resultado deste passo.\n"
            "O sub-plano deve ter passos CONCRETOS e executáveis.\n"
            "Máximo 5 passos no sub-plano."
        )

        # Reuse generate_plan's system prompt style
        subplan = await self.generate_plan(
            user_message=prompt,
            system_context=system_context,
        )

        if subplan:
            # O `user_message` daqui é o prompt do sub-plano; o pedido é o do pai.
            subplan.pedido = parent_plan.pedido or subplan.pedido
            subplan.parent_plan_id = parent_plan.id
            subplan.parent_step_id = parent_step.id
            parent_step.subplan_id = subplan.id
            # Limit sub-plan steps
            max_subplan_steps = 5
            if session_id:
                remaining = MAX_TOTAL_STEPS - self._count_total_steps(session_id)
                max_subplan_steps = min(5, remaining)
            if len(subplan.steps) > max_subplan_steps:
                subplan.steps = subplan.steps[:max_subplan_steps]
                for i, s in enumerate(subplan.steps):
                    s.index = i
            logger.info(f"[PLAN] Created subplan {subplan.id} with {len(subplan.steps)} steps for parent step {parent_step.index}")

            # Track in history for loop detection
            if session_id:
                result_hash = f"{parent_step.tool_name}:{parent_step.description[:50]}:{step_result[:100]}"
                if session_id not in self._subplan_history:
                    self._subplan_history[session_id] = []
                self._subplan_history[session_id].append(result_hash)

        return subplan

    # ---------------------------------------------------------------
    # Plan Storage (in-memory per session)
    # ---------------------------------------------------------------

    def store_plan(self, session_id: str, plan: ExecutionPlan) -> None:
        """Store a plan for a session. Supports plan stacking for sub-plans."""
        if session_id not in self._plans:
            self._plans[session_id] = []
        self._plans[session_id].append(plan)
        logger.info(f"[PLAN] Stored plan {plan.id} for session {session_id} (stack depth={len(self._plans[session_id])})")

    def get_active_plan(self, session_id: str) -> ExecutionPlan | None:
        """Get the current active plan (top of stack — could be a sub-plan)."""
        stack = self._plans.get(session_id, [])
        if not stack:
            return None
        # Return the top-most active plan
        for plan in reversed(stack):
            if plan.status == "active":
                return plan
        return None

    def pop_completed_subplan(self, session_id: str) -> ExecutionPlan | None:
        """If the top plan is a completed sub-plan, pop it and return the parent plan."""
        stack = self._plans.get(session_id, [])
        if len(stack) < 2:
            return None

        top = stack[-1]
        if top.is_complete and top.parent_plan_id:
            stack.pop()
            parent = stack[-1] if stack else None
            logger.info(f"[PLAN] Popped completed subplan {top.id}, parent={parent.id if parent else 'none'}")
            return parent
        return None

    def clear_plans(self, session_id: str) -> None:
        """Remove all plans for a session."""
        self._plans.pop(session_id, None)
        self._subplan_history.pop(session_id, None)

    def get_plan_state_for_metadata(self, session_id: str) -> dict | None:
        """Serialize active plan state for gRPC metadata passing."""
        plan = self.get_active_plan(session_id)
        if not plan:
            return None
        return {
            "plan_id": plan.id,
            "plan_goal": plan.goal,
            "plan_step_index": plan.current_step_index,
            "plan_total_steps": len(plan.steps),
            "plan_progress": plan.progress_text,
        }

    # ---------------------------------------------------------------
    # Unified advance_and_refine (Fase 3)
    # ---------------------------------------------------------------

    async def advance_and_refine(
        self,
        plan: ExecutionPlan,
        step_index: int,
        raw_output: str,
        system_context: str | None = None,
        memory_discoveries: str | None = None,
    ) -> dict:
        """Unified LLM call that validates the last result AND decides the next action.

        Replaces separate refine_step() + check_subplan_needed() calls.

        Returns dict with:
          validation: {success, matches_expectation, discovery}
          decision: "continue" | "retry" | "subplan" | "finish"
          reason: str
          refined_step: {tool_name, tool_arguments, requires_approval} | None
          subplan_steps: list | None
          retry_classification: "transient" | "recoverable" | "fatal" | None
        """
        # Build state view for prompt
        steps_view = []
        for s in plan.steps:
            icon = {"pending": " ", "running": "→", "completed": "✓", "failed": "✗", "skipped": "⏭"}[s.status]
            result_snip = ""
            if s.result:
                result_snip = f" → {s.result[:1500]}"
            elif s.error:
                result_snip = f" → ERRO: {s.error[:200]}"
            steps_view.append(f"  [{icon}] {s.index + 1}. {s.description}{result_snip}")

        next_step = None
        for s in plan.steps:
            if s.status == "pending":
                next_step = s
                break

        current_step = plan.steps[step_index] if step_index < len(plan.steps) else None

        prompt = (
            f'Objetivo geral: "{plan.goal}"\n\n'
            + _pedido_literal(plan)
            + f"## 1. Estado Atual do Plano\n"
            + "\n".join(steps_view) + "\n\n"
            f"## 2. Último Resultado\n"
            f"Step {step_index + 1}: {current_step.description if current_step else '?'}\n"
            f"Tool: {current_step.tool_name if current_step else '?'}\n"
            f"Output: {raw_output[:2000]}\n\n"
        )

        if next_step:
            prompt += (
                f"## 3. Próximo Step Planejado\n"
                f"Step {next_step.index + 1}: {next_step.description}\n"
                f"Depende do anterior: {next_step.depends_on_previous}\n\n"
            )

        if memory_discoveries:
            prompt += f"## 4. Descobertas da Sessão\n{memory_discoveries}\n\n"

        # O sistema do usuário e as regras de comando ("Como escrever o comando"):
        # chegavam por parâmetro e eram descartados, então o passo refinado era
        # escrito sem saber o SO, a pasta padrão nem as regras de shell (o `zip -r`
        # sem `-q`, com caminho absoluto, da session-7b61bc5e). `refine_step` já usa.
        if system_context:
            prompt += f"## 5. Contexto do Sistema\n{system_context}\n\n"

        prompt += (
            "---\n\n"
            "## Instruções (responda em JSON com 2 seções)\n\n"
            "### validation\n"
            "- success: true/false/partial\n"
            "- matches_expectation: o output resolve o que o step queria?\n"
            "- discovery: string | null — fato novo que vale memorizar\n\n"
            "### decision\n"
            "- decision: \"continue\" | \"retry\" | \"subplan\" | \"finish\"\n"
            "- reason: motivo da decisão (1 frase)\n"
            "- retry_classification: \"transient\" | \"recoverable\" | \"fatal\" | null\n"
            "- refined_step: {tool_name, tool_arguments, requires_approval} | null\n"
            "  (obrigatório se decision=continue e próximo step depende do anterior)\n"
            "- subplan_steps: [{description, tool_name, tool_arguments, requires_approval}] | null\n\n"
            "## Regras CRÍTICAS\n"
            "- Caminho, nome de arquivo e DESTINO que o pedido do usuário cita valem exatamente\n"
            "  como ele escreveu: o refined_step usa esses caminhos, absolutos. Nunca troque um\n"
            "  destino citado por um nome relativo — sem `working_dir` o comando roda na home.\n"
            "- NUNCA pule steps do plano. Execute SEMPRE o próximo step pendente na ordem exata.\n"
            "- decision=continue significa executar o próximo step pendente — não altere a ordem sem razão técnica.\n"
            "- decision=finish SÓ se TODOS os steps estão com status ✓ e o goal foi atingido. NUNCA finalize com steps pendentes.\n"
            "- decision=subplan SÓ se o resultado revela N items que os steps restantes não cobrem\n"
            "- decision=retry SÓ se retry_classification ≠ \"fatal\"\n"
            "- subplan_steps tem MAX 5 steps\n"
            "- CRÍTICO para um step de escrita (run_command com heredoc/printf >) com\n"
            "  depends_on_previous=true: você DEVE retornar refined_step com o comando shell\n"
            "  REAL e COMPLETO (conteúdo já embutido) construído a partir dos resultados dos\n"
            "  steps de leitura anteriores (visíveis no estado do plano como '→ conteúdo').\n"
            "  NUNCA use placeholder como '[conteúdo baseado no arquivo]' ou '[SERÁ GERADO]'.\n"
            "  Se algum arquivo ainda não foi lido (step pendente), retorne decision=continue para ler esse arquivo primeiro.\n"
            "- CRÍTICO para um step de SONDAGEM (`ls`/`find` num arquivo que o usuário só\n"
            "  descreveu, sem nome exato): o refined_step do PRÓXIMO passo só pode citar um\n"
            "  nome de arquivo que REALMENTE apareceu no resultado da sondagem acima. Se a\n"
            "  sondagem não achou nada parecido, decision=finish relatando isso ao usuário —\n"
            "  NUNCA invente um nome pra seguir em frente.\n\n"
            "Responda APENAS com JSON válido."
        )

        messages = [
            Message(
                role=MessageRole.SYSTEM,
                content="Você é um validador de planos. Analise o resultado e decida o próximo passo. Responda apenas com JSON.",
            ),
            Message(role=MessageRole.USER, content=prompt),
        ]

        try:
            provider = self.llm.get_provider()
            response = await provider.complete(
                messages=messages,
                temperature=0.1,
                max_tokens=2000,
            )
            raw = (response.get_content() or "").strip()
            # Mesmo vazio do generate_plan (deepseek-empty-response-reasoning): o
            # raciocínio come o teto e volta content="". Sem o retry, o json.loads
            # estoura, o fallback devolve "continue" com refined_step=None — e um
            # passo dependente é despachado com o comando PLACEHOLDER que o prompt
            # do planner mandou pôr, que vai literalmente pro shell do usuário.
            if not raw:
                logger.warning("[PLAN] advance_and_refine vazio (teto consumido pelo raciocínio) — retentando")
                response = await provider.complete(
                    messages=messages, temperature=0.1, max_tokens=4000,
                )
                raw = (response.get_content() or "").strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

            parsed = json.loads(raw)
            parsed = self._flatten_decision(parsed, next_step)
            logger.info(f"[PLAN] advance_and_refine: decision={parsed.get('decision', '?')}, reason={str(parsed.get('reason', '?'))[:80]}")

            validation = parsed.get("validation", {})
            return {
                "validation": {
                    "success": validation.get("success", True),
                    "matches_expectation": validation.get("matches_expectation", True),
                    "discovery": validation.get("discovery"),
                },
                "decision": parsed.get("decision", "continue"),
                "reason": parsed.get("reason", ""),
                "refined_step": self._normalizar_refined_step(parsed.get("refined_step")),
                "subplan_steps": parsed.get("subplan_steps"),
                "retry_classification": parsed.get("retry_classification"),
            }

        except Exception as e:
            logger.warning(f"[PLAN] advance_and_refine failed: {e}")
            # Safe fallback: continue with the next step as-is
            return {
                "validation": {"success": True, "matches_expectation": True, "discovery": None},
                "decision": "continue" if next_step else "finish",
                "reason": f"fallback: {e}",
                "refined_step": None,
                "subplan_steps": None,
                "retry_classification": None,
            }

    @staticmethod
    def _normalizar_refined_step(valor: Any) -> dict | None:
        """O ``refined_step`` como objeto, com ``tool_arguments`` sempre dict (ou ausente).

        Os quatro lugares que aplicam o refinamento fazem
        ``tool_arguments.get("command")``. Na sessão e9947724 (16/11/2026) o modelo
        devolveu ``tool_arguments`` como TEXTO no retry de um passo que falhou, e
        o ``.get`` derrubou o turno inteiro com ``AttributeError`` no gRPC.

        Texto que é JSON vira objeto. Texto solto não vira comando: não dá para
        saber se é o comando ou uma frase, e adivinhar mandaria uma frase para o
        shell do usuário. Aí a chave sai e o passo segue com os argumentos que já
        tinha (e com as guardas de passo indespachável). ``None`` quando o próprio
        ``refined_step`` não é um objeto.
        """
        if isinstance(valor, str):
            try:
                valor = json.loads(valor)
            except (json.JSONDecodeError, ValueError):
                logger.warning("[PLAN] refined_step veio como texto solto — descartado")
                return None
        if not isinstance(valor, dict):
            return None
        if "tool_arguments" not in valor:
            return valor
        args = valor.get("tool_arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, ValueError):
                args = None
        if isinstance(args, dict):
            return {**valor, "tool_arguments": args}
        logger.warning(
            "[PLAN] refined_step.tool_arguments não é objeto (%s) — mantendo os argumentos do passo",
            type(valor.get("tool_arguments")).__name__,
        )
        return {k: v for k, v in valor.items() if k != "tool_arguments"}

    @staticmethod
    def _flatten_decision(parsed: dict, next_step: "PlanStep | None") -> dict:
        """Aceita o JSON do validador aninhado — e nunca devolve um `decision` inválido.

        O prompt pede os campos no TOPO (`decision`, `reason`, `refined_step`…),
        mas o modelo às vezes embrulha tudo num objeto `decision`:

            {"validation": {...},
             "decision": {"decision": "continue", "refined_step": {...}, ...}}

        Medido na sessão 3733f3ae (2026-09-12): `parsed["decision"]` virou um
        **dict**, nenhum `elif` do despachante casou (`== "finish"`,
        `in ("continue", …)`), e o turno caiu no fallthrough "plano completo"
        com o passo 2 de 2 **nunca executado** — e o `refined_step` correto, que
        o modelo tinha produzido, foi jogado fora. O usuário recebeu um `ls`
        truncado no meio da linha como resposta a "ler o corpo do email".

        Achatar é mais barato e mais confiável que brigar com o prompt. E um
        `decision` que ainda assim não seja um dos quatro verbos vira
        ``continue`` enquanto houver passo pendente: a falha segura de um
        validador é seguir o plano, nunca declarar vitória.
        """
        bloco = parsed.get("decision")
        if isinstance(bloco, dict):
            logger.info("[PLAN] decision veio aninhado — achatando")
            parsed = {**parsed, **bloco}

        decision = parsed.get("decision")
        if decision not in ("continue", "retry", "subplan", "finish"):
            seguro = "continue" if next_step else "finish"
            logger.warning(
                "[PLAN] decision inválido (%r) — assumindo %r para não abandonar passo pendente",
                decision, seguro,
            )
            parsed = {**parsed, "decision": seguro}
        return parsed

    @staticmethod
    def should_skip_llm(
        plan: ExecutionPlan,
        step_index: int,
        raw_output: str,
        next_step: PlanStep | None,
    ) -> dict | None:
        """Heuristics to skip the LLM call for advance_and_refine.

        Returns a dict with {decision, refined_step} if we can skip, or None if LLM is needed.
        """
        from core.state.execution_state import READ_ONLY_TOOLS

        if not next_step:
            # No more steps → finish
            return {"decision": "finish", "refined_step": None}

        # Skip 1: output empty/trivial AND next step doesn't depend → continue
        if not raw_output.strip() and not next_step.depends_on_previous:
            return {"decision": "continue", "refined_step": None}

        # Skip 2: read-only tool with small output AND next step independent → continue
        if (
            next_step.tool_name in READ_ONLY_TOOLS
            and len(raw_output) < 200
            and not next_step.depends_on_previous
        ):
            return {"decision": "continue", "refined_step": None}

        # Skip 3: next step doesn't depend on previous → send as-is
        if not next_step.depends_on_previous:
            return {"decision": "continue", "refined_step": None}

        # Skip 4: last step and it's a simple type → finish directly
        if step_index == len(plan.steps) - 1:
            return {"decision": "finish", "refined_step": None}

        # Need LLM
        return None

    @staticmethod
    def subplan_is_valid(subplan_steps: list[dict], remaining_parent_steps: list) -> bool:
        """Guard: reject sub-plan if >60% of its steps duplicate the parent plan."""
        if not subplan_steps:
            return False
        if len(subplan_steps) > 5:
            return False

        duplicate_count = 0
        for sub in subplan_steps:
            sub_tool = sub.get("tool_name", "")
            sub_desc = sub.get("description", "").lower()
            sub_tokens = set(sub_desc.split())
            for parent in remaining_parent_steps:
                if sub_tool == parent.tool_name:
                    parent_tokens = set(parent.description.lower().split())
                    if sub_tokens and parent_tokens:
                        overlap = len(sub_tokens & parent_tokens) / max(len(sub_tokens), 1)
                        if overlap > 0.7:
                            duplicate_count += 1
                            break

        return duplicate_count / max(len(subplan_steps), 1) <= 0.6

    # ---------------------------------------------------------------
    # Final Response (Fase 4 — template, LLM only for technical)
    # ---------------------------------------------------------------

    async def build_final_response(
        self,
        plan: ExecutionPlan,
        user_message: str,
    ) -> str:
        """Build a final response using a template. LLM only if output is complex/technical."""
        # Passo que nunca rodou não pode virar ✅. Na sessão 3733f3ae o plano
        # "listar + ler" parou no passo 1 (decision veio aninhado, ver
        # `_flatten_decision`) e a resposta abriu com um check verde por cima de
        # um `ls` truncado — o usuário tinha pedido pra LER o e-mail.
        pendentes = [s for s in plan.steps if s.status in ("pending", "running")]
        falhos = [s for s in plan.steps if s.status == "failed"]
        icone = "✅" if not pendentes and not falhos else "⚠️"
        lines = [f"{icone} **{plan.goal}**\n"]

        for step in plan.steps:
            if step.status == "completed":
                summary = (step.result or "")[:300]
                lines.append(f"- {step.description}: {summary}")
            elif step.status == "failed":
                lines.append(f"- ⚠️ {step.description}: falhou ({step.error or 'erro desconhecido'})")
            else:
                lines.append(f"- ⏳ {step.description}: NÃO executado")
        if pendentes:
            logger.warning(
                "[PLAN] resposta final com %d passo(s) pendente(s) — plano encerrado cedo demais",
                len(pendentes),
            )

        combined_output = "\n".join(s.result or "" for s in plan.steps if s.status == "completed")

        # Only call LLM for complex/technical output
        if len(combined_output) > 500 and self._has_technical_content(combined_output):
            try:
                return await self.format_plan_result(plan, user_message)
            except Exception:
                pass  # Fall through to template

        return "\n".join(lines)

    @staticmethod
    def _has_technical_content(text: str) -> bool:
        """Heuristic: detect if text has stack traces, JSON, logs, or code."""
        import re
        indicators = [
            r"Traceback \(most recent",       # Python stack trace
            r"at \w+\.\w+\(",                  # Java/JS stack trace
            r"\{[\s\S]*\"[^\"]+\":",           # JSON-like
            r"^\s*\|.*\|.*\|",                 # Table formatting
            r"^\d{4}-\d{2}-\d{2}.*\b(ERROR|WARN|INFO)\b",  # Log lines
            r"def |class |function |import |from ",  # Code
        ]
        for pattern in indicators:
            if re.search(pattern, text, re.MULTILINE):
                return True
        return False

    # ---------------------------------------------------------------
    # Validation (Fase 2.4)
    # ---------------------------------------------------------------

    def _validate_plan(self, plan: ExecutionPlan) -> bool:
        """Programmatic validation of generated plan (no LLM call)."""
        if len(plan.steps) > MAX_TOTAL_STEPS:
            logger.warning(f"[PLAN] Validation: too many steps ({len(plan.steps)} > {MAX_TOTAL_STEPS})")
            return False

        # First step must not depend on previous
        if plan.steps and plan.steps[0].depends_on_previous:
            logger.warning("[PLAN] Validation: first step depends_on_previous=True")
            plan.steps[0].depends_on_previous = False  # auto-fix

        # All steps must have non-empty description
        for s in plan.steps:
            if not s.description or not s.description.strip():
                logger.warning(f"[PLAN] Validation: step {s.index} has empty description")
                return False

        # Um passo `run_command` SEM comando é indespachável: o desktop recebe o
        # tool_request, não tem o que executar e nunca devolve resultado — o turno
        # trava em "Executando" até a conexão cair (sessão e8e5664b). A validação
        # olhava só descrição/contagem, então o plano furado passava e só quebrava
        # no cliente. Passos DEPENDENTES são exceção: o comando deles é montado
        # depois, pelo refine_step, com a saída do passo anterior em mãos.
        for s in plan.steps:
            if s.depends_on_previous:
                continue
            if s.tool_name == "run_command" and not str(
                (s.tool_arguments or {}).get("command") or s.raw_command or ""
            ).strip():
                logger.warning(
                    "[PLAN] Validation: step %s é run_command sem comando (indespachável)",
                    s.index,
                )
                return False

        return True

    # ---------------------------------------------------------------
    # Plan Formatting (legacy — used as fallback by build_final_response)
    # ---------------------------------------------------------------

    async def format_plan_result(
        self,
        plan: ExecutionPlan,
        user_message: str,
    ) -> str:
        """Use LLM to create a unified response from all plan step results."""
        completed_context = plan.get_completed_context()
        if not completed_context:
            return "Plano concluído mas sem resultados disponíveis."

        prompt = (
            f'O usuário pediu: "{user_message}"\n\n'
            f"Foi executado um plano com {len(plan.steps)} passos:\n\n"
            f"{completed_context}\n\n"
            "Crie uma resposta UNIFICADA e clara em português que resuma todos os resultados.\n"
            "Não liste os passos individualmente — sintetize a informação de forma útil.\n"
            "Use markdown se apropriado."
        )

        messages = [
            Message(
                role=MessageRole.SYSTEM,
                content="Você é Neo, um assistente de desktop. Responda de forma clara e concisa em português.",
            ),
            Message(role=MessageRole.USER, content=prompt),
        ]

        try:
            provider = self.llm.get_provider()
            response = await provider.complete(
                messages=messages,
                temperature=0.3,
                max_tokens=800,
            )
            return response.get_content() or completed_context
        except Exception as e:
            logger.warning(f"[PLAN] Failed to format plan result: {e}")
            return f"Plano concluído. Resultados:\n\n{completed_context}"

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    def _get_plan_depth(self, plan: ExecutionPlan) -> int:
        """Get the nesting depth of a plan (0 for root, 1 for sub-plan, etc.)."""
        depth = 0
        current = plan
        # Walk up the parent chain by checking stored plans
        for _session_plans in self._plans.values():
            for p in _session_plans:
                if p.id == current.parent_plan_id:
                    depth += 1
                    current = p
                    break
        return depth

    def _count_total_steps(self, session_id: str) -> int:
        """Count total steps across all plans (including sub-plans) in a session."""
        stack = self._plans.get(session_id, [])
        return sum(len(p.steps) for p in stack)

    def build_plan_update(self, plan: ExecutionPlan) -> dict:
        """Build a plan_update dict for sending to the desktop client."""
        return {
            "plan_id": plan.id,
            "goal": plan.goal,
            "status": plan.status,
            "current_step": plan.current_step_index,
            "parent_plan_id": plan.parent_plan_id or "",
            "steps": [
                {
                    "id": s.id,
                    "index": s.index,
                    "description": s.description,
                    "tool_name": s.tool_name,
                    "status": s.status,
                    "result_summary": (s.result or "")[:100],
                    "error": (s.error or "")[:100],
                }
                for s in plan.steps
            ],
        }
