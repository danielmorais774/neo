"""Voice tool definitions — server-side (READ-ONLY introspection).

Tools to INSPECT the TTS voices of Neo: the catalog (``voice_list``) and the
active selection (``voice_get``). *Changing* the active voice/engine is done
through the generic config layer (``modules/config``, setting ``tts.voice`` /
``tts.engine``), which persists into the same ``user_preference`` row these tools
read. See ``modules/voice/executor.py`` and ``modules/config``.
"""

from shared import ToolParameter, ToolSchema

VOICE_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="voice_list",
        description=(
            "Lista as vozes de TTS disponíveis no neo-voice (vozes embutidas e clones), "
            "com engine e descrição. Use para responder 'quais vozes você tem?', para "
            "conferir os nomes antes de trocar, ou para saber quais vozes/personagens "
            "dá pra IMITAR (clones do coqui — passe engine=\"coqui\")."
        ),
        router_summary="Lista as vozes de TTS disponíveis.",
        module="voice",
        execution="server-side",
        timeout=15,
        parameters={
            "engine": ToolParameter(
                type="string",
                description=(
                    "Filtra por engine. Opcional (sem filtro lista todas). Para "
                    "imitação de vozes use \"coqui\"."
                ),
                enum=["coqui", "qwen", "dia", "cosyvoice", "f5", "chatterbox"],
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="voice_get",
        description=(
            "Retorna a voz/engine de TTS ativa no momento (a que o Neo está usando "
            "para falar)."
        ),
        module="voice",
        execution="server-side",
        timeout=10,
        parameters={},
    ),
]
