"""Device tool definitions — server-side.

``device_control`` e ``device_power`` são separadas de propósito
(docs/device-control-plan.md §4): uma tool por ação encheria o catálogo e o
classificador diluiria a regra específica na genérica; tudo numa só colocaria
"desligar o PC" no mesmo nível de risco que "abaixar o volume". A separação
espelha o que o repo já faz entre FILE_ACTUATION_TOOL_NAMES e
SHELL_ACTUATION_TOOL_NAMES.

Nenhuma das duas aceita ``device_id`` — só NOME. Ver modules/devices/control.py
para o porquê (resumo: o modelo inventa ids).
"""

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


DEVICE_CONTROL_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="device_control",
        description=(
            "Controla o áudio e a mídia de um dispositivo do usuário: volume, mudo, "
            "play/pause, faixa seguinte/anterior e bloquear a tela. Funciona TAMBÉM "
            "em outro dispositivo que não o desta conversa — é assim que 'abaixa o "
            "volume do PC da sala' funciona a partir do celular. "
            "Identifique o aparelho pelo NOME que o usuário usa ('PC da sala', "
            "'notebook'); se ele não citar nenhum, omita o campo e o dispositivo "
            "desta conversa é o alvo. NUNCA invente um identificador. "
            "Use list_connected_devices antes se não souber quais existem. "
            "O resultado traz o estado REAL medido no aparelho (ex: o volume que "
            "ficou) — reporte esse valor, não o que você pediu."
        ),
        router_summary=(
            "Ajusta volume/mudo/play/pause/faixa/bloqueio de um dispositivo do "
            "usuário, inclusive um dispositivo remoto citado pelo nome."
        ),
        module="devices",
        execution="server-side",
        timeout=20,
        parameters={
            "action": ToolParameter(
                type="string",
                description=(
                    "volume_set (exige value 0-100), volume_up, volume_down, mute, "
                    "unmute, play, pause, play_pause, next, previous, lock, get_status "
                    "(lê volume e estado sem mudar nada)."
                ),
                required=True,
                enum=[
                    "volume_set",
                    "volume_up",
                    "volume_down",
                    "mute",
                    "unmute",
                    "play",
                    "pause",
                    "play_pause",
                    "next",
                    "previous",
                    "lock",
                    "get_status",
                ],
            ),
            "device": ToolParameter(
                type="string",
                description=(
                    "NOME do dispositivo como o usuário o chama ('PC da sala'). "
                    "Omita quando ele não citar nenhum — aí vale o desta conversa."
                ),
                required=False,
            ),
            "value": ToolParameter(
                type="integer",
                description=(
                    "Nível 0-100 para volume_set. Para volume_up/volume_down é o "
                    "tamanho do passo (padrão 10). Ignorado nas demais ações."
                ),
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="device_power",
        description=(
            "Desliga, reinicia ou suspende um dispositivo do usuário. "
            "O aparelho SEMPRE pede confirmação na própria tela antes de executar — "
            "quem está na frente da máquina é quem decide — então você não precisa "
            "pedir confirmação no chat, mas também não prometa que já desligou: "
            "reporte o que a ferramenta devolver. "
            "O dispositivo é OBRIGATÓRIO e deve ser o nome que o usuário citou; "
            "nunca escolha um por conta própria e nunca invente identificador. "
            "Sucesso aqui significa que a máquina caiu de verdade (o aparelho "
            "desapareceu da rede); se o usuário recusar o diálogo, você recebe erro."
        ),
        router_summary=(
            "Desliga, reinicia ou suspende um dispositivo do usuário citado pelo nome."
        ),
        module="devices",
        execution="server-side",
        timeout=40,
        parameters={
            "action": ToolParameter(
                type="string",
                description="suspend (dormir), shutdown (desligar) ou restart (reiniciar).",
                required=True,
                enum=["suspend", "shutdown", "restart"],
            ),
            "device": ToolParameter(
                type="string",
                description=(
                    "NOME do dispositivo, obrigatório e explicitamente citado pelo "
                    "usuário ('PC da sala')."
                ),
                required=True,
            ),
        },
    ),
]

DEVICE_CONTROL_TOOL_NAMES: frozenset[str] = frozenset(
    t.name for t in DEVICE_CONTROL_TOOLS
)
