"""Tool registry - gerencia tools disponíveis."""

from typing import Any

from shared import ToolNotFoundError, ToolSchema


class ToolRegistry:
    """Registry central de todas as tools disponíveis."""

    def __init__(self) -> None:
        """Inicializa o registry vazio."""
        self._tools: dict[str, ToolSchema] = {}

    def register(self, tool: ToolSchema) -> None:
        """
        Registra uma nova tool.

        Args:
            tool: Schema da tool a registrar

        Raises:
            ValueError: Se tool já existe
        """
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' já registrada")

        self._tools[tool.name] = tool

    def register_many(self, tools: list[ToolSchema]) -> None:
        """
        Registra múltiplas tools de uma vez.

        Args:
            tools: Lista de schemas de tools
        """
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> ToolSchema:
        """
        Retorna uma tool pelo nome.

        Args:
            name: Nome da tool

        Returns:
            Schema da tool

        Raises:
            ToolNotFoundError: Se tool não existe
        """
        if name not in self._tools:
            raise ToolNotFoundError(
                f"Tool '{name}' não encontrada",
                details={"tool_name": name, "available": list(self._tools.keys())},
            )

        return self._tools[name]

    def get_all(self) -> list[ToolSchema]:
        """
        Retorna todas as tools registradas.

        Returns:
            Lista de todas as tools
        """
        return list(self._tools.values())

    def get_by_module(self, module: str) -> list[ToolSchema]:
        """
        Retorna todas as tools de um módulo específico.

        Args:
            module: Nome do módulo (ex: "desktop", "browser")

        Returns:
            Lista de tools do módulo
        """
        return [tool for tool in self._tools.values() if tool.module == module]

    def exists(self, name: str) -> bool:
        """
        Verifica se uma tool existe.

        Args:
            name: Nome da tool

        Returns:
            True se existe, False caso contrário
        """
        return name in self._tools

    def unregister(self, name: str) -> None:
        """
        Remove uma tool do registry.

        Args:
            name: Nome da tool

        Raises:
            ToolNotFoundError: Se tool não existe
        """
        if name not in self._tools:
            raise ToolNotFoundError(f"Tool '{name}' não encontrada")

        del self._tools[name]

    def clear(self) -> None:
        """Remove todas as tools do registry."""
        self._tools.clear()

    def count(self) -> int:
        """Retorna o número de tools registradas."""
        return len(self._tools)

    def list_names(self) -> list[str]:
        """Retorna lista de nomes de todas as tools."""
        return list(self._tools.keys())
