"""Device tool definitions — server-side."""

from shared import ToolParameter, ToolSchema

DEVICES_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="list_connected_devices",
        description=(
            "Lista os dispositivos atualmente conectados ao neo-core (desktop/mobile) "
            "do usuário, com status de conexão em tempo real (online/offline), modelo, "
            "sistema operacional e versão do cliente."
        ),
        module="devices",
        execution="server-side",
        timeout=15,
        parameters={
            "only_online": ToolParameter(
                type="boolean",
                description="Se verdadeiro, retorna apenas os dispositivos online. Padrão: falso (lista todos).",
                required=False,
            ),
        },
    ),
]
