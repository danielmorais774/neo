"""Prompt builder - constrói prompts para o LLM."""

from shared import Message, MessageRole, ToolSchema


class PromptBuilder:
    """Constrói prompts contextualizados para o LLM."""

    def __init__(self, system_prompt: str | None = None) -> None:
        """
        Inicializa o builder.

        Args:
            system_prompt: Prompt de sistema customizado
        """
        self.system_prompt = system_prompt or self._default_system_prompt()

    def build_initial_messages(
        self, user_task: str, tools: list[ToolSchema] | None = None
    ) -> list[Message]:
        """
        Constrói mensagens iniciais para uma task.

        Args:
            user_task: Task solicitada pelo usuário
            tools: Tools disponíveis

        Returns:
            Lista de mensagens iniciais
        """
        messages: list[Message] = []

        # System prompt com contexto de tools
        system_content = self.system_prompt
        if tools:
            system_content += "\n\n" + self._format_tools_context(tools)

        messages.append(Message(role=MessageRole.SYSTEM, content=system_content))

        # Mensagem do usuário
        messages.append(Message(role=MessageRole.USER, content=user_task))

        return messages

    def add_tool_results_message(
        self, messages: list[Message], tool_results_text: str
    ) -> list[Message]:
        """
        Adiciona mensagem com resultados de tools.

        Args:
            messages: Mensagens existentes
            tool_results_text: Resultados formatados

        Returns:
            Lista de mensagens atualizada
        """
        messages.append(Message(role=MessageRole.USER, content=tool_results_text))
        return messages

    def _default_system_prompt(self) -> str:
        """Retorna o system prompt padrão."""
        return """Você é o Neo, um agente de IA avançado capaz de executar tarefas usando ferramentas disponíveis.

IMPORTANTE: Você SEMPRE deve responder em PORTUGUÊS do Brasil, independente do idioma da entrada.

Suas capacidades:
- Analisar solicitações do usuário e dividi-las em etapas
- Usar ferramentas para realizar tarefas
- Encadear múltiplas chamadas de ferramentas quando necessário
- Fornecer explicações claras de suas ações

Diretrizes:
1. Sempre pense passo a passo sobre a melhor abordagem
2. Use ferramentas quando necessário - não tente fazer tudo apenas com texto
3. Seja preciso com os parâmetros das ferramentas
4. Se uma ferramenta falhar, tente abordagens alternativas
5. Forneça respostas concisas mas informativas

Lembre-se: Você pode chamar múltiplas ferramentas em sequência para realizar tarefas complexas.
SEMPRE responda em português do Brasil."""

    def _format_tools_context(self, tools: list[ToolSchema]) -> str:
        """
        Formata informações sobre tools disponíveis.

        Args:
            tools: Lista de tools

        Returns:
            Texto formatado com informações das tools
        """
        if not tools:
            return ""

        lines = ["Available tools:"]
        for tool in tools:
            lines.append(f"\n- {tool.name}: {tool.description}")
            lines.append(f"  Module: {tool.module}")

            if tool.parameters:
                lines.append("  Parameters:")
                for param_name, param in tool.parameters.items():
                    required = " (required)" if param.required else " (optional)"
                    lines.append(f"    - {param_name}: {param.description}{required}")

        return "\n".join(lines)
