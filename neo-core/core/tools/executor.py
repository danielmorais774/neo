"""Executor de tools - coordena execução de tools."""

import asyncio
from typing import Any, Callable, Protocol

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

            # Executa baseado no tipo
            if tool.execution == "server-side":
                result = await self._execute_server_side(tool_call.name, validated_args)
            else:
                # Client-side: delega para o módulo
                result = await self._execute_client_side(
                    tool.module, tool_call.name, validated_args
                )

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

    async def _execute_server_side(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> Any:
        """
        Executa uma tool server-side via module executor registrado.

        Args:
            tool_name: Nome da tool
            arguments: Argumentos validados

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
            result = await asyncio.wait_for(
                executor.execute(tool_name, arguments),
                timeout=tool.timeout or self.settings.tool_timeout,
            )
            return result
        except asyncio.TimeoutError:
            raise ToolExecutionError(
                f"Timeout executando tool server-side '{tool_name}'",
                details={"tool": tool_name, "timeout": tool.timeout},
            )

    async def _execute_client_side(
        self, module: str, tool_name: str, arguments: dict[str, Any]
    ) -> Any:
        """
        Delega execução para o executor do módulo.

        Args:
            module: Nome do módulo
            tool_name: Nome da tool
            arguments: Argumentos validados

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
            result = await asyncio.wait_for(
                executor.execute(tool_name, arguments),
                timeout=self.settings.tool_timeout,
            )
            return result
        except asyncio.TimeoutError:
            raise ToolExecutionError(
                f"Timeout executando tool '{tool_name}'",
                details={"tool": tool_name, "timeout": self.settings.tool_timeout},
            )
