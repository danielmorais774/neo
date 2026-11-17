"""Generic config tool definitions — server-side.

One set of tools edits ANY registered setting (see :mod:`modules.config.specs`),
so a new adjustable setting is just a new ``ConfigSpec`` — no new tool. ``config_set``
is the single write path ("usa a voz do X", "troca o engine pra qwen", …); the two
reads help discovery.
"""

from shared import ToolParameter, ToolSchema

CONFIG_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="config_set",
        description=(
            "Ajusta uma CONFIGURAÇÃO do Neo. Use quando o usuário pedir para "
            "trocar/mudar/definir um ajuste, ex: 'usa a voz do Jarvis' "
            "(setting='tts.voice', value='jarvis'), 'fala com a voz do Ryan' "
            "(setting='tts.voice', value='Ryan'), 'troca o engine pra qwen' ou 'usa "
            "o motor coqui' (setting='tts.engine', value='qwen'/'coqui'). O `value` "
            "pode ser aproximado — a tool resolve contra as opções válidas e, se não "
            "casar, devolve as opções disponíveis. NÃO invente settings: use os ids "
            "expostos por config_list (ex.: 'tts.voice', 'tts.engine')."
        ),
        # O prompt do classificador já traz o mapeamento "usa a voz do X" →
        # {setting: tts.voice, value: X}; repeti-lo aqui é pagar duas vezes.
        router_summary=(
            "Muda uma CONFIGURAÇÃO do Neo (voz/engine de TTS, etc.) pelo id do ajuste."
        ),
        module="config",
        execution="server-side",
        timeout=15,
        parameters={
            "setting": ToolParameter(
                type="string",
                description="Id do ajuste a mudar, ex.: 'tts.voice', 'tts.engine'. "
                "Aceita também o nome natural (ex.: 'voz', 'engine').",
                required=True,
            ),
            "value": ToolParameter(
                type="string",
                description="Novo valor desejado (ex.: 'jarvis', 'Ryan', 'qwen', "
                "'coqui'). Pode ser aproximado.",
                required=True,
            ),
        },
    ),
    ToolSchema(
        name="config_get",
        description=(
            "Retorna o valor atual de UMA configuração do Neo (e as opções válidas). "
            "Ex.: 'qual engine de voz você tá usando?' → setting='tts.engine'."
        ),
        module="config",
        execution="server-side",
        timeout=10,
        parameters={
            "setting": ToolParameter(
                type="string",
                description="Id do ajuste, ex.: 'tts.voice', 'tts.engine'. Aceita "
                "também o nome natural.",
                required=True,
            ),
        },
    ),
    ToolSchema(
        name="config_list",
        description=(
            "Lista TODAS as configurações que o Neo permite ajustar (id, descrição, "
            "valor atual e opções). Use para 'o que dá pra configurar?'."
        ),
        module="config",
        execution="server-side",
        timeout=15,
        parameters={},
    ),
]
