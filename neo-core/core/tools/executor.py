"""Executor de tools - coordena execução de tools."""

import asyncio
from typing import Any, Callable, Protocol

from core.authority import gate_tool_call, note_tool_result
from core.tools.registry import ToolRegistry
from core.tools.validator import ToolValidator
from shared import ToolCall, ToolExecutionError, ToolResult, get_settings


class ToolExecutorProtocol(Protocol):
    """Protocol para executores de tools específicos de módulos."""

    async def execute(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Executa uma tool e retorna o resultado."""
        ...


class ToolExecutor:
    """
    Coordena execução de tools.

    Para client-side tools, delega para o módulo apropriado.
    Para server-side tools, executa diretamente.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        """
        Inicializa o executor.

        Args:
            registry: Registry de tools
        """
        self.registry = registry
        self.validator = ToolValidator()
        self.settings = get_settings()

        # Executores específicos por módulo (injetados)
        self._module_executors: dict[str, ToolExecutorProtocol] = {}

    def register_module_executor(
        self, module: str, executor: ToolExecutorProtocol
    ) -> None:
        """
        Registra um executor para um módulo específico.

        Args:
            module: Nome do módulo (ex: "desktop")
            executor: Instância do executor
        """
        self._module_executors[module] = executor

    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        """
        Executa uma tool call.

        Args:
            tool_call: Tool call a executar

        Returns:
            Resultado da execução

        Raises:
            ToolExecutionError: Se execução falhar
        """
        start_time = asyncio.get_event_loop().time()

        try:
            # Busca tool no registry
            tool = self.registry.get(tool_call.name)

            # Valida argumentos
            validated_args = self.validator.validate_arguments(
                tool, tool_call.arguments
            )

            # Portão de autoridade (docs/release-request-plan.md §5.2). Este é um
            # dos DOIS trilhos que executam tool; o outro é o
            # `CognitiveReadExecutor`. A decisão mora numa função só — duas
            # divergiriam no primeiro nome novo, em silêncio (foi o que já
            # aconteceu com as job tools). Em `shadow` (padrão) nada é barrado:
            # só sai a linha `[authority][sombra]` que alimenta o relatório.
            decisao = await gate_tool_call(tool_call.name, validated_args, tool_call.id)
            if decisao.blocked:
                raise ToolExecutionError(
                    decisao.mensagem(),
                    details={"tool": tool_call.name, "efeito": decisao.efeito},
                )

            # Executa baseado no tipo
            if tool.execution == "server-side":
                result = await self._execute_server_side(
                    tool_call.name, validated_args, tool_call.id
                )
            else:
                # Client-side: delega para o módulo
                result = await self._execute_client_side(
                    tool.module, tool_call.name, validated_args, tool_call.id
                )

            # Procedência + contaminação: o que este resultado trouxe para o
            # turno (URLs vistas, texto de terceiro). Nunca levanta.
            note_tool_result(tool_call.name, result)

            execution_time = asyncio.get_event_loop().time() - start_time

            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                result=result,
                execution_time=execution_time,
            )

        except Exception as e:
            execution_time = asyncio.get_event_loop().time() - start_time
            error_msg = str(e)

            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                result=None,
                error=error_msg,
                execution_time=execution_time,
            )

    async def _dispatch(
        self,
        executor: Any,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float,
    ) -> Any:
        """Chama o executor do módulo aceitando os DOIS contratos que existem no core.

        Metade dos executores fala ``execute(nome, args) -> dict`` (weather,
        web_search, services, workbench); a outra metade fala
        ``execute_tool_call(ToolCall) -> ToolResult`` — o contrato do
        ``AgentExecutor`` (jobs, HA, whatsapp, devices, cofre, email). Este
        método só sabia o primeiro, então registrar um executor do segundo tipo
        aqui morria em ``AttributeError: object has no attribute 'execute'``, que
        o ``except Exception`` de ``execute_tool_call`` transformava na frase
        genérica "Todas as ferramentas falharam". Foi o que matou ``email_inbox``,
        ``email_message`` e ``vault_list`` em produção — nos DOIS caminhos, o do
        roteador e o inline do cérebro (que cai aqui pelo fallthrough do
        ``CognitiveReadExecutor``).

        ``execute`` vem primeiro de propósito: quem já tem os dois (devices)
        continua exatamente como estava.
        """
        if hasattr(executor, "execute"):
            return await asyncio.wait_for(
                executor.execute(tool_name, arguments), timeout=timeout
            )

        call = ToolCall(id=tool_call_id, name=tool_name, arguments=arguments)
        result = await asyncio.wait_for(
            executor.execute_tool_call(call), timeout=timeout
        )
        # O ToolResult já traz o erro pronto para humano ("login recusado …
        # senha de app"). Levantar preserva essa mensagem: o `except` de
        # execute_tool_call a copia para ToolResult.error, e o AgentExecutor
        # agora a mostra em vez da frase genérica.
        if getattr(result, "error", None):
            raise ToolExecutionError(
                str(result.error), details={"tool": tool_name}
            )
        return getattr(result, "result", result)

    async def _execute_server_side(
        self, tool_name: str, arguments: dict[str, Any], tool_call_id: str = ""
    ) -> Any:
        """
        Executa uma tool server-side via module executor registrado.

        Args:
            tool_name: Nome da tool
            arguments: Argumentos validados
            tool_call_id: Id da tool call (repassado ao executor por-ToolCall)

        Returns:
            Resultado da execução
        """
        tool = self.registry.get(tool_name)
        module = tool.module

        if module not in self._module_executors:
            raise ToolExecutionError(
                f"Executor para módulo server-side '{module}' não registrado",
                details={"module": module, "tool": tool_name},
            )

        executor = self._module_executors[module]

        try:
            return await self._dispatch(
                executor,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                timeout=tool.timeout or self.settings.tool_timeout,
            )
        except asyncio.TimeoutError:
            raise ToolExecutionError(
                f"Timeout executando tool server-side '{tool_name}'",
                details={"tool": tool_name, "timeout": tool.timeout},
            )

    async def _execute_client_side(
        self,
        module: str,
        tool_name: str,
        arguments: dict[str, Any],
        tool_call_id: str = "",
    ) -> Any:
        """
        Delega execução para o executor do módulo.

        Args:
            module: Nome do módulo
            tool_name: Nome da tool
            arguments: Argumentos validados
            tool_call_id: Id da tool call (repassado ao executor por-ToolCall)

        Returns:
            Resultado da execução
        """
        if module not in self._module_executors:
            raise ToolExecutionError(
                f"Executor para módulo '{module}' não registrado",
                details={"module": module, "tool": tool_name},
            )

        executor = self._module_executors[module]

        # Timeout protection
        try:
            return await self._dispatch(
                executor,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                timeout=self.settings.tool_timeout,
            )
        except asyncio.TimeoutError:
            raise ToolExecutionError(
                f"Timeout executando tool '{tool_name}'",
                details={"tool": tool_name, "timeout": self.settings.tool_timeout},
            )
