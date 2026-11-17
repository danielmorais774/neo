"""Validador de tools e parâmetros."""

from typing import Any

from pydantic import ValidationError

from shared import ToolSchema, ToolValidationError


class ToolValidator:
    """Valida parâmetros de tools."""

    @staticmethod
    def validate_arguments(
        tool: ToolSchema, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Valida argumentos de uma tool call.

        Args:
            tool: Schema da tool
            arguments: Argumentos fornecidos

        Returns:
            Argumentos validados e limpos

        Raises:
            ToolValidationError: Se validação falhar
        """
        validated: dict[str, Any] = {}
        errors: list[str] = []

        # Verifica parâmetros required
        for param_name, param in tool.parameters.items():
            if param.required and param_name not in arguments:
                errors.append(f"Parâmetro obrigatório '{param_name}' ausente")
                continue

            if param_name not in arguments:
                continue

            value = arguments[param_name]

            # Valida tipo básico
            try:
                validated_value = ToolValidator._validate_type(
                    param_name, value, param.type, param.enum
                )
                validated[param_name] = validated_value
            except ToolValidationError as e:
                errors.append(str(e))

        # Verifica parâmetros extras
        extra_params = set(arguments.keys()) - set(tool.parameters.keys())
        if extra_params:
            errors.append(f"Parâmetros desconhecidos: {', '.join(extra_params)}")

        if errors:
            raise ToolValidationError(
                f"Erros de validação na tool '{tool.name}'",
                details={"tool": tool.name, "errors": errors},
            )

        return validated

    @staticmethod
    def _validate_type(
        param_name: str,
        value: Any,
        expected_type: str,
        enum_values: list[str] | None = None,
    ) -> Any:
        """
        Valida tipo de um parâmetro.

        Args:
            param_name: Nome do parâmetro
            value: Valor fornecido
            expected_type: Tipo esperado (string, number, boolean, object, array)
            enum_values: Valores válidos se enum

        Returns:
            Valor validado

        Raises:
            ToolValidationError: Se tipo inválido
        """
        # Validação de enum
        if enum_values and value not in enum_values:
            raise ToolValidationError(
                f"Parâmetro '{param_name}' deve ser um de: {', '.join(enum_values)}"
            )

        # Validação de tipo
        type_validators = {
            "string": lambda v: isinstance(v, str),
            "number": lambda v: isinstance(v, (int, float)),
            "integer": lambda v: isinstance(v, int),
            "boolean": lambda v: isinstance(v, bool),
            "object": lambda v: isinstance(v, dict),
            "array": lambda v: isinstance(v, list),
        }

        validator = type_validators.get(expected_type)
        if not validator:
            # Tipo desconhecido, aceita qualquer coisa
            return value

        if not validator(value):
            raise ToolValidationError(
                f"Parâmetro '{param_name}' deve ser do tipo {expected_type}, "
                f"recebido {type(value).__name__}"
            )

        return value
